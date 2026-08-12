from datetime import datetime, timezone

import pytest
from sqlalchemy import func, insert, select

from zerocrm import meet, transcribe
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import call, deal, memory_chunk, person, touch, transcript


def _engine():
    e = make_engine("sqlite://")
    migrate(e)
    return e


def _record(name="conferenceRecords/abc", emails=("jane@acme.com")):
    t = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return {"name": name, "start_time": t, "end_time": t,
            "participant_emails": list(emails) if not isinstance(emails, str) else [emails],
            "transcript_text": "we discussed pricing and timeline",
            "segments": [{"speaker": "Jane", "ts": 0.0, "text": "we discussed pricing and timeline"}]}


def _count(conn, table, *where):
    return conn.execute(select(func.count()).select_from(table).where(*where)).scalar()


def test_normalize_writes_call_transcript_touch_person_and_chunk():
    e = _engine()
    cid = meet.normalize_conference_record(e, _record())
    with e.connect() as c:
        assert _count(c, call) == 1
        assert _count(c, transcript) == 1
        assert _count(c, touch, touch.c.kind == "meeting") == 1
        assert _count(c, person) == 1
        assert _count(c, memory_chunk) >= 1
    assert cid


def test_normalize_is_idempotent_on_redelivery():
    e = _engine()
    meet.normalize_conference_record(e, _record())
    assert meet.normalize_conference_record(e, _record()) is None
    with e.connect() as c:
        assert _count(c, call) == 1


def test_emailless_call_still_records_and_dedupes():
    e = _engine()
    rec = _record(name="conferenceRecords/noemail", emails=[])
    cid = meet.normalize_conference_record(e, rec)
    assert cid
    # redelivery must NOT double-insert even though no touch exists to key on
    assert meet.normalize_conference_record(e, rec) is None
    with e.connect() as c:
        assert _count(c, call) == 1
        assert _count(c, touch) == 0


def test_normalize_attaches_open_deal_not_closed():
    e = _engine()
    with e.begin() as c:
        from zerocrm.upsert import upsert_person
        pid = upsert_person(c, identity={"kind": "email", "value": "jane@acme.com"},
                            fields={}, provenance="human", actor={"kind": "human", "id": "t"})
        c.execute(insert(deal).values(id="dlost", person_id=pid, stage="lost"))
        c.execute(insert(deal).values(id="dopen", person_id=pid, stage="negotiation"))
    cid = meet.normalize_conference_record(e, _record())
    with e.connect() as c:
        row = c.execute(select(call.c.deal_id).where(call.c.id == cid)).first()
    assert row[0] == "dopen"


def test_transcribe_audio_is_an_explicit_seam():
    with pytest.raises(NotImplementedError):
        transcribe.transcribe_audio("gs://bucket/rec.wav")
