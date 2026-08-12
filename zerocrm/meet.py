"""Google Meet -> normalized call/transcript/touch, plus the cursor-driven poll.

Ingestion is exactly-once on `call.external_ref` (always written), NOT the touch
event_key: an emailless call writes no touch, so a touch-keyed dedup would
double-insert on the 5-minute overlap re-read. See docs/slice-1-*-plan.md.

Record shape (the poller normalizes the Meet API into this):
    {"name": "conferenceRecords/abc",       # -> external_ref + touch event_key
     "start_time": <datetime>, "end_time": <datetime>,
     "participant_emails": ["jane@acme.com"],
     "transcript_text": "full text", "lang": "en-US",
     "segments": [{"speaker": "Jane", "ts": 0.0, "text": "hi"}]}
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from .config import get_config, set_config
from .memory import add_chunks, chunk_text
from .schema import call, deal, touch, transcript
from .upsert import upsert_person

_ACTOR = {"kind": "agent", "id": "meet"}
_OVERLAP = timedelta(minutes=5)
_CURSOR = "google_meet_cursor"
_OPEN_DEAL_EXCLUDES = ("won", "lost", "closed")


def _open_deal_id(conn: Connection, person_id: str) -> str | None:
    row = conn.execute(
        select(deal.c.id)
        .where(deal.c.person_id == person_id,
               deal.c.stage.notin_(_OPEN_DEAL_EXCLUDES))
        .order_by(deal.c.stage_entered_at.desc())
    ).first()
    return row[0] if row else None


def normalize_conference_record(engine: Engine, record: dict) -> str | None:
    """Write one call + transcript (+ touch, when a person is known) and chunk the
    transcript. Returns the call id, or None if this record was already ingested."""
    external_ref = record["name"]
    text = record.get("transcript_text", "")
    with engine.begin() as conn:
        seen = conn.execute(
            select(call.c.id).where(call.c.external_ref == external_ref)
        ).first()
        if seen:
            return None  # exactly-once on redelivery

        person_id = None
        for email in record.get("participant_emails", []):
            person_id = upsert_person(conn, identity={"kind": "email", "value": email},
                                      fields={}, provenance="observed_from_meeting",
                                      actor=_ACTOR)
            break  # first known participant owns the call; others resolve on their own touches
        deal_id = _open_deal_id(conn, person_id) if person_id else None

        cid = conn.execute(call.insert().values(
            deal_id=deal_id, person_id=person_id, source="meet", external_ref=external_ref,
            started_at=record.get("start_time"), ended_at=record.get("end_time"),
            status="transcribed", actor=_ACTOR)).inserted_primary_key[0]
        conn.execute(transcript.insert().values(
            call_id=cid, transcript_source="google", text=text,
            segments=record.get("segments", []), lang=record.get("lang"), actor=_ACTOR))
        if person_id:  # touch.person_id is NOT NULL — an emailless call still records call+transcript
            conn.execute(touch.insert().values(
                person_id=person_id, channel="meet", direction="in", kind="meeting",
                body_ref=cid, event_key=external_ref,
                payload={"call_id": cid, "emails": record.get("participant_emails", [])},
                actor=_ACTOR))

        # chunk inside the same txn so a call and its chunks commit atomically
        add_chunks(engine, source_type="transcript", source_id=cid,
                   texts=chunk_text(text), deal_id=deal_id, person_id=person_id, conn=conn)
    return cid


def poll_meet(engine: Engine, client, *, workspace_id: str = "default") -> list[str]:
    """Ingest finished Meet conference records since the stored cursor. `client`
    is any object with `finished_records(since: datetime|None) -> list[dict]`."""
    raw = get_config(engine, _CURSOR)
    since = datetime.fromisoformat(raw) if raw else None
    ingested: list[str] = []
    high = since
    for record in client.finished_records(since):
        cid = normalize_conference_record(engine, record)
        if cid:
            ingested.append(cid)
        end = record.get("end_time")
        if end and (high is None or end > high):
            high = end
    if high:
        set_config(engine, _CURSOR, (high - _OVERLAP).isoformat())
    return ingested
