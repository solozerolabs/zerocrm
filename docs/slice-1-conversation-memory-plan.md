# Slice 1 — Conversation Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture sales calls into normalized transcripts, make transcripts + docs retrievable, and answer pre-call briefs / write win-loss notes — all UI-less, on the existing poll-primary runner.

**Architecture:** Two new tables (`call`, `transcript`) + one generic retrieval spine (`memory_chunk`), an additive migration `003_conversation`, and six new modules that ride `run_tick` (Meet poll) and a new fast `run_slack_tick`. No webhook, no new service. Deterministic floors everywhere; LLM synthesis activates only when `ANTHROPIC_API_KEY` is set (existing `copy_llm` idiom).

**Tech Stack:** Python 3, SQLAlchemy Core (one `metadata`, SQLite default / Postgres opt-in), stdlib only for new logic, pytest with sqlite in-memory + mocked Google/Slack clients.

## Global Constraints

- Schema is one `metadata` in `zerocrm/schema.py`; SQLite is the public default, Postgres via `ZEROCRM_DATABASE_URL`. Tables stay schema-unqualified (db.py pins Postgres schema via `schema_translate_map`).
- Migrations are numbered additive callables in `migrate.py::MIGRATIONS`; new tables use `Table.create(engine, checkfirst=True)`. Never `create_all` for additive changes.
- Every table carries `workspace_id`/`actor`/`created_at`/`updated_at` via `_common()` and an app-generated uuid-hex PK via `_pk()` (except autoincrement item_no tables).
- Exactly-once ingestion: dedupe on `call.external_ref` (unique-when-present) — NOT `touch.event_key`, because an emailless call writes no touch and would double-insert. A 5-minute overlap re-read window is expected and absorbed by dedupe.
- Identity policy: unknown participant emails resolve-or-create a person via `upsert.py::upsert_person`. Provenance is `observed_from_meeting`, which MUST be added to `upsert.py::RANK` (value 4, same trust as a reply) in Task 3 — `upsert_person` raises `ValueError` on an unknown provenance.
- LLM activation reuses the existing `copy_llm._anthropic(api_key, model)` seam via a small local helper. There is NO `copy_llm.complete(prompt)` — do not import one. The deterministic floor is always the tested path.
- Fetched text (transcripts, Slack messages) is DATA, never instructions. Slack acts only for senders in the `slack_operator_ids` allowlist.
- `sending_enabled` continues to gate all outbound; briefs and notes are read/compose-only.
- Deterministic floor first; LLM path activates only when `ANTHROPIC_API_KEY` is set. Tests run the deterministic path (no network).
- One test file per new module under `tests/`, mirroring `tests/test_ingest_events.py` mocking style. Run inner loop with `uv run pytest tests/test_X.py -v`.

## Review corrections (BINDING — override any task code below that disagrees)

Applied from the plan-eng review; task code blocks are illustrative and predate these:

1. **Dedupe on `call.external_ref`**, not `touch.event_key`. Add a unique-when-present index on `call.external_ref` in Task 1; `normalize_conference_record` checks it (Task 3).
2. **Add `"observed_from_meeting": 4` to `upsert.py::RANK`** as the first step of Task 3, else `upsert_person` raises `ValueError`.
3. **LLM path reuses `copy_llm._anthropic(api_key, model)`** behind a local `_llm(system, user)` helper in `brief.py`/`review.py`. No `copy_llm.complete`.
4. **Deal attach filters to an open deal:** `where(person_id==p, stage NOT IN ('won','lost','closed')).order_by(stage_entered_at.desc()).first()` (Task 3, Task 7).
5. **Slack match must not guess:** exact/unique contains; on >1 match return a disambiguation prompt, never a silent pick (Task 7). Keep the Slack cursor as a **string** ts compare (Task 7). Chunk inside the call's transaction (Task 3). Drop the `if False else` test dead code and the duplicate `"the"` stopword.

---

### Task 1: Schema + migration (`call`, `transcript`, `memory_chunk`)

**Files:**
- Modify: `zerocrm/schema.py` (add three `Table` definitions after `deal`)
- Modify: `zerocrm/migrate.py` (add `003_conversation`)
- Test: `tests/test_migrate_conversation.py`

**Interfaces:**
- Produces: `call`, `transcript`, `memory_chunk` Tables importable from `zerocrm.schema`; `migrate()` applies `003_conversation`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migrate_conversation.py
from sqlalchemy import inspect
from zerocrm.db import make_engine
from zerocrm.migrate import migrate

def test_003_creates_conversation_tables():
    engine = make_engine("sqlite://")
    migrate(engine)
    tables = set(inspect(engine).get_table_names())
    assert {"call", "transcript", "memory_chunk"} <= tables

def test_003_is_idempotent():
    engine = make_engine("sqlite://")
    migrate(engine)
    assert migrate(engine) == []  # nothing pending on second run
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_migrate_conversation.py -v`
Expected: FAIL (tables absent / migration missing).

- [ ] **Step 3: Add the tables to `schema.py`** (insert after the `deal` table)

```python
# --- conversation memory (slice 1) -----------------------------------------
call = Table(
    "call",
    metadata,
    _pk(),
    Column("deal_id", String(32), ForeignKey("deal.id")),
    Column("person_id", String(32), ForeignKey("person.id")),
    Column("source", Text, nullable=False),            # meet | voice
    Column("external_ref", Text),                      # Meet conferenceRecord name / Voice id
    Column("started_at", DateTime(timezone=True)),
    Column("ended_at", DateTime(timezone=True)),
    Column("recording_uri", Text),
    Column("status", Text, nullable=False, default="captured"),  # captured|awaiting_transcription|transcribed|failed
    *_common(),
)

transcript = Table(
    "transcript",
    metadata,
    _pk(),
    Column("call_id", String(32), ForeignKey("call.id"), nullable=False),
    Column("transcript_source", Text, nullable=False),  # google | whisper
    Column("text", Text, nullable=False, default=""),
    Column("segments", Json, default=list),             # [{speaker, ts, text}]
    Column("lang", Text),
    *_common(),
)

# the generic retrieval spine (CC-5). No embedding column this slice.
# ponytail: linear token-overlap scan; DB FTS / pgvector + embedding col when it hurts.
memory_chunk = Table(
    "memory_chunk",
    metadata,
    _pk(),
    Column("source_type", Text, nullable=False),        # transcript | document
    Column("source_id", Text, nullable=False),
    Column("deal_id", String(32), ForeignKey("deal.id")),
    Column("person_id", String(32), ForeignKey("person.id")),
    Column("chunk_no", Integer, nullable=False, default=0),
    Column("chunk_text", Text, nullable=False),
    *_common(),
    Index("ix_chunk_source", "source_type", "source_id"),
    Index("ix_chunk_deal", "deal_id"),
)
```

- [ ] **Step 4: Add the migration to `migrate.py`**

```python
from .schema import metadata, research, schema_migrations, call, transcript, memory_chunk

def _conversation(engine: Engine) -> None:
    for t in (call, transcript, memory_chunk):
        t.create(engine, checkfirst=True)

MIGRATIONS: dict[str, callable] = {
    "001_core": _create_all,
    "002_research": _research,
    "003_conversation": _conversation,
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_migrate_conversation.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add zerocrm/schema.py zerocrm/migrate.py tests/test_migrate_conversation.py
git commit -m "feat(schema): call, transcript, memory_chunk + 003 migration"
```

---

### Task 2: `memory.py` — chunk, store, retrieve

**Files:**
- Create: `zerocrm/memory.py`
- Test: `tests/test_memory.py`

**Interfaces:**
- Produces:
  - `chunk_text(text: str, size: int = 1200) -> list[str]`
  - `add_chunks(engine, *, source_type, source_id, texts, deal_id=None, person_id=None, workspace_id="default") -> int`
  - `ingest_document(engine, *, text, source_id, deal_id=None, person_id=None) -> int`
  - `retrieve(engine, query, *, deal_id=None, person_id=None, k=8, workspace_id="default") -> list[dict]` (each `{chunk_text, source_type, source_id, score}`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory.py
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm import memory

def _engine():
    e = make_engine("sqlite://"); migrate(e); return e

def test_chunk_splits_long_text():
    parts = memory.chunk_text("word " * 1000, size=200)
    assert len(parts) > 1 and all(len(p) <= 200 for p in parts)

def test_retrieve_ranks_matching_chunk_first():
    e = _engine()
    memory.add_chunks(e, source_type="document", source_id="d1",
                      texts=["pricing was the sticking point on renewal",
                             "the weather in the demo was pleasant"])
    hits = memory.retrieve(e, "why did pricing block the renewal", k=1)
    assert hits and "pricing" in hits[0]["chunk_text"]

def test_retrieve_scopes_to_deal():
    e = _engine()
    memory.add_chunks(e, source_type="document", source_id="a", texts=["alpha budget"], deal_id="D1")
    memory.add_chunks(e, source_type="document", source_id="b", texts=["beta budget"], deal_id="D2")
    hits = memory.retrieve(e, "budget", deal_id="D1")
    assert all(h["source_id"] == "a" for h in hits)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_memory.py -v` — Expected: FAIL (module missing).

- [ ] **Step 3: Write minimal implementation**

```python
# zerocrm/memory.py
"""The one retrieval spine (CC-5). Transcripts and docs both feed memory_chunk.

ponytail: retrieval is a Python token-overlap rank over the candidate rows. At
SMB chunk counts a linear scan is nothing; the documented upgrade is a DB FTS /
pgvector index + an embedding column behind retrieve() — callers do not change.
"""
from __future__ import annotations

import re
from sqlalchemy import select
from sqlalchemy.engine import Engine

from .schema import memory_chunk

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "or", "was", "is", "on", "in", "to", "of",
         "did", "why", "the", "it", "for", "with", "at"}

def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}

def chunk_text(text: str, size: int = 1200) -> list[str]:
    text = text.strip()
    if not text:
        return []
    return [text[i:i + size] for i in range(0, len(text), size)] or [text]

def add_chunks(engine: Engine, *, source_type: str, source_id: str, texts,
               deal_id=None, person_id=None, workspace_id="default") -> int:
    rows = [dict(source_type=source_type, source_id=source_id, chunk_no=i,
                 chunk_text=t, deal_id=deal_id, person_id=person_id,
                 workspace_id=workspace_id)
            for i, t in enumerate(texts) if t and t.strip()]
    if not rows:
        return 0
    with engine.begin() as conn:
        conn.execute(memory_chunk.insert(), rows)
    return len(rows)

def ingest_document(engine: Engine, *, text: str, source_id: str,
                    deal_id=None, person_id=None, workspace_id="default") -> int:
    return add_chunks(engine, source_type="document", source_id=source_id,
                      texts=chunk_text(text), deal_id=deal_id, person_id=person_id,
                      workspace_id=workspace_id)

def retrieve(engine: Engine, query: str, *, deal_id=None, person_id=None,
             k: int = 8, workspace_id="default") -> list[dict]:
    q = memory_chunk.select().where(memory_chunk.c.workspace_id == workspace_id)
    if deal_id:
        q = q.where(memory_chunk.c.deal_id == deal_id)
    elif person_id:
        q = q.where(memory_chunk.c.person_id == person_id)
    qtok = _tokens(query)
    scored = []
    with engine.connect() as conn:
        for r in conn.execute(q).mappings():
            overlap = len(qtok & _tokens(r["chunk_text"]))
            if overlap:
                scored.append((overlap, dict(chunk_text=r["chunk_text"],
                               source_type=r["source_type"], source_id=r["source_id"],
                               score=overlap)))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [d for _, d in scored[:k]]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_memory.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add zerocrm/memory.py tests/test_memory.py
git commit -m "feat(memory): generic retrieval spine — chunk, store, token-overlap retrieve"
```

---

### Task 3: `transcribe.py` seam + Meet normalizer

**Files:**
- Create: `zerocrm/transcribe.py`
- Create: `zerocrm/meet.py` (normalizer only in this task; poller in Task 4)
- Test: `tests/test_meet_normalize.py`

**Interfaces:**
- Produces:
  - `transcribe.transcribe_audio(uri: str) -> str` — raises `NotImplementedError` (Whisper-later seam).
  - `meet.normalize_conference_record(engine, record: dict) -> str | None` — writes one `call` + `transcript` + `touch(kind=meeting)`, resolves participants, dedupes on `event_key`; returns the `call.id` (or `None` if already ingested).
- Consumes: `upsert.upsert_person` (identity), `memory.add_chunks` (chunk the transcript), `schema.{call,transcript,touch,deal}`.

Record shape (normalized from Meet API by the poller in Task 4):
```python
{"name": "conferenceRecords/abc",          # -> event_key + external_ref
 "start_time": <datetime>, "end_time": <datetime>,
 "participant_emails": ["jane@acme.com"],
 "transcript_text": "full text",
 "segments": [{"speaker": "Jane", "ts": 0.0, "text": "hi"}]}
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_meet_normalize.py
from datetime import datetime, timezone
from sqlalchemy import select, func
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm import meet, transcribe
from zerocrm.schema import call, transcript, touch, person, memory_chunk

def _engine():
    e = make_engine("sqlite://"); migrate(e); return e

def _record(name="conferenceRecords/abc"):
    return {"name": name, "start_time": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "end_time": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "participant_emails": ["jane@acme.com"],
            "transcript_text": "we discussed pricing and timeline",
            "segments": [{"speaker": "Jane", "ts": 0.0, "text": "we discussed pricing and timeline"}]}

def test_normalize_writes_call_transcript_touch_and_person():
    e = _engine()
    cid = meet.normalize_conference_record(e, _record())
    with e.connect() as c:
        assert c.execute(select(func.count()).select_from(call)).scalar() == 1
        assert c.execute(select(func.count()).select_from(transcript)).scalar() == 1
        assert c.execute(select(func.count()).select_from(touch).where(touch.c.kind == "meeting")).scalar() == 1
        assert c.execute(select(func.count()).select_from(person)).scalar() == 1
        assert c.execute(select(func.count()).select_from(memory_chunk)).scalar() >= 1
    assert cid

def test_normalize_is_idempotent_on_redelivery():
    e = _engine()
    meet.normalize_conference_record(e, _record())
    assert meet.normalize_conference_record(e, _record()) is None
    with e.connect() as c:
        assert c.execute(select(func.count()).select_from(call)).scalar() == 1

def test_transcribe_audio_is_an_explicit_seam():
    import pytest
    with pytest.raises(NotImplementedError):
        transcribe.transcribe_audio("gs://bucket/rec.wav")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_meet_normalize.py -v` — Expected: FAIL (modules missing).

- [ ] **Step 3: Write `transcribe.py`**

```python
# zerocrm/transcribe.py
"""Transcript-source seam. Google Meet arrives already transcribed (structured
entries from the Meet API), so the only real source this slice is 'google'.
Audio-file transcription (Google Voice recordings) is deferred to an OSS Whisper
adapter — the seam is explicit so the later swap is not a silent empty path."""
from __future__ import annotations

def transcribe_audio(uri: str) -> str:  # noqa: ARG001
    # ponytail: Whisper adapter lands with the phone (source=voice) path, Slice 1.5.
    raise NotImplementedError("audio transcription (Whisper) not built yet; Meet arrives pre-transcribed")
```

- [ ] **Step 4: Write `meet.normalize_conference_record`**

```python
# zerocrm/meet.py  (normalizer; poller added in Task 4)
"""Google Meet -> normalized call/transcript/touch. Poll lives in poll_meet()."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .memory import add_chunks, chunk_text
from .schema import call, deal, transcript, touch
from .upsert import upsert_person

_ACTOR = {"kind": "agent", "id": "meet"}

def _existing_call_id(conn, event_key: str) -> str | None:
    row = conn.execute(select(touch.c.id).where(touch.c.event_key == event_key)).first()
    if not row:
        return None
    c = conn.execute(select(call.c.id).where(call.c.external_ref == event_key)).first()
    return c[0] if c else "seen"

def normalize_conference_record(engine: Engine, record: dict) -> str | None:
    event_key = record["name"]
    with engine.begin() as conn:
        if _existing_call_id(conn, event_key):
            return None  # dedupe — exactly-once on redelivery

        # resolve-or-create the first known participant; attach to their open deal
        person_id = None
        for email in record.get("participant_emails", []):
            person_id = upsert_person(conn, identity={"kind": "email", "value": email},
                                      fields={}, provenance="observed_from_meeting",
                                      actor=_ACTOR)
            break
        deal_id = None
        if person_id:
            d = conn.execute(select(deal.c.id).where(deal.c.person_id == person_id)).first()
            deal_id = d[0] if d else None

        cid = conn.execute(call.insert().values(
            deal_id=deal_id, person_id=person_id, source="meet", external_ref=event_key,
            started_at=record.get("start_time"), ended_at=record.get("end_time"),
            status="transcribed", actor=_ACTOR).inserted_primary_key)[0]
        conn.execute(transcript.insert().values(
            call_id=cid, transcript_source="google", text=record.get("transcript_text", ""),
            segments=record.get("segments", []), lang=record.get("lang"), actor=_ACTOR))
        conn.execute(touch.insert().values(
            person_id=person_id, channel="meet", direction="in", kind="meeting",
            body_ref=cid, event_key=event_key,
            payload={"call_id": cid, "emails": record.get("participant_emails", [])},
            actor=_ACTOR))

    # chunk the transcript into the retrieval spine (own txn; call row already durable)
    add_chunks(engine, source_type="transcript", source_id=cid,
               texts=chunk_text(record.get("transcript_text", "")),
               deal_id=deal_id, person_id=person_id)
    return cid
```

Note: `upsert_person` requires `person_id` to be non-null for `touch`? No — `touch.person_id` is NOT nullable. If a record has no participant emails, skip the `touch` insert (a call with no identifiable person still records `call`+`transcript`). Add that guard:

```python
        if person_id:
            conn.execute(touch.insert().values(... ))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_meet_normalize.py -v` — Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add zerocrm/transcribe.py zerocrm/meet.py tests/test_meet_normalize.py
git commit -m "feat(meet): normalize conference record -> call/transcript/touch + chunk; whisper seam"
```

---

### Task 4: `meet.poll_meet` — cursor-driven poller

**Files:**
- Modify: `zerocrm/meet.py` (add `poll_meet`)
- Test: `tests/test_meet_poll.py`

**Interfaces:**
- Produces: `meet.poll_meet(engine, client, *, workspace_id="default") -> list[str]` — reads cursor from `config`, asks `client.finished_records(since)` for normalized record dicts (Task 3 shape), normalizes each, advances the cursor to the max `end_time` (minus 5-min overlap). Returns ingested call ids.
- Consumes: `config.get_config`/`set_config`, `meet.normalize_conference_record`. `client` is any object with `finished_records(since: datetime|None) -> list[dict]` (real Google client wired in Task 8; tests pass a fake).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_meet_poll.py
from datetime import datetime, timezone
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm import meet
from zerocrm.config import get_config

class FakeMeet:
    def __init__(self, records): self._records = records
    def finished_records(self, since): return self._records

def _rec(name, text="pricing talk"):
    t = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    return {"name": name, "start_time": t, "end_time": t,
            "participant_emails": ["jane@acme.com"], "transcript_text": text, "segments": []}

def test_poll_ingests_and_sets_cursor():
    e = make_engine("sqlite://"); migrate(e)
    ids = meet.poll_meet(e, FakeMeet([_rec("conferenceRecords/1")]))
    assert len(ids) == 1
    assert get_config(e, "google_meet_cursor")

def test_poll_dedupes_across_calls():
    e = make_engine("sqlite://"); migrate(e)
    client = FakeMeet([_rec("conferenceRecords/1")])
    meet.poll_meet(e, client)
    assert meet.poll_meet(e, client) == []   # same record redelivered -> nothing new
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_meet_poll.py -v` — Expected: FAIL (`poll_meet` missing).

- [ ] **Step 3: Add `poll_meet` to `meet.py`**

```python
from datetime import timedelta, datetime, timezone
from .config import get_config, set_config

_OVERLAP = timedelta(minutes=5)
_CURSOR = "google_meet_cursor"

def poll_meet(engine: Engine, client, *, workspace_id: str = "default") -> list[str]:
    raw = get_config(engine, _CURSOR)
    since = datetime.fromisoformat(raw) if raw else None
    ingested, high = [], since
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_meet_poll.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add zerocrm/meet.py tests/test_meet_poll.py
git commit -m "feat(meet): cursor-driven poll_meet with 5-min overlap + dedupe"
```

---

### Task 5: `brief.py` — pre-call brief (deterministic floor)

**Files:**
- Create: `zerocrm/brief.py`
- Test: `tests/test_brief.py`

**Interfaces:**
- Produces: `brief.precall_brief(engine, *, person_id=None, deal_id=None, query="", workspace_id="default") -> str` — a plain-text brief from person/company facts, open deal (stage, next_step), recent `touch` rows, latest transcript snippet, and `memory.retrieve` hits. LLM synthesis activates only if `ANTHROPIC_API_KEY` is set (import guarded); tests exercise the deterministic floor.
- Consumes: `schema.{person,company,deal,touch,call,transcript}`, `memory.retrieve`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_brief.py
from datetime import datetime, timezone
from sqlalchemy import insert
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm import brief
from zerocrm.schema import person, deal, call, transcript

def test_brief_includes_stage_next_step_and_transcript_snippet():
    e = make_engine("sqlite://"); migrate(e)
    with e.begin() as c:
        pid = c.execute(insert(person).values(id="p1", full_name="Jane").returning(person.c.id)).scalar() if False else "p1"
        c.execute(insert(person).values(id="p1", full_name="Jane"))
        c.execute(insert(deal).values(id="d1", person_id="p1", stage="negotiation", next_step="send quote"))
        c.execute(insert(call).values(id="c1", person_id="p1", deal_id="d1", source="meet", status="transcribed"))
        c.execute(insert(transcript).values(id="t1", call_id="c1", transcript_source="google",
                                            text="they pushed back on pricing"))
    out = brief.precall_brief(e, person_id="p1", deal_id="d1", query="pricing")
    assert "negotiation" in out and "send quote" in out and "pricing" in out

def test_brief_handles_no_history():
    e = make_engine("sqlite://"); migrate(e)
    with e.begin() as c:
        c.execute(insert(person).values(id="p2", full_name="New Lead"))
    out = brief.precall_brief(e, person_id="p2")
    assert "New Lead" in out  # no crash, names the person
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_brief.py -v` — Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# zerocrm/brief.py
"""Pre-call brief — deterministic floor always; LLM synthesis when a key is set
(same activation idiom as copy_llm). Synthesized live, never stored."""
from __future__ import annotations

import os
from sqlalchemy import select
from sqlalchemy.engine import Engine

from .memory import retrieve
from .schema import call, deal, person, touch, transcript

def _rows(conn, stmt): return list(conn.execute(stmt).mappings())

def precall_brief(engine: Engine, *, person_id=None, deal_id=None, query="",
                  workspace_id="default") -> str:
    lines = []
    with engine.connect() as conn:
        if person_id:
            p = conn.execute(select(person).where(person.c.id == person_id)).mappings().first()
            if p:
                lines.append(f"Person: {p['full_name'] or person_id}"
                             + (f" — {p['job_title']}" if p['job_title'] else ""))
        if deal_id:
            d = conn.execute(select(deal).where(deal.c.id == deal_id)).mappings().first()
            if d:
                lines.append(f"Deal: stage={d['stage']}"
                             + (f", value=${d['value_usd']:.0f}" if d['value_usd'] else "")
                             + (f", next: {d['next_step']}" if d['next_step'] else ""))
        if person_id:
            recent = _rows(conn, select(touch).where(touch.c.person_id == person_id)
                           .order_by(touch.c.ts.desc()).limit(5))
            if recent:
                lines.append("Recent: " + "; ".join(f"{t['kind']}/{t['direction']}" for t in recent))
            last = conn.execute(
                select(transcript.c.text).join(call, transcript.c.call_id == call.c.id)
                .where(call.c.person_id == person_id).order_by(transcript.c.created_at.desc()).limit(1)
            ).first()
            if last and last[0]:
                lines.append("Last call: " + last[0][:280])
    if query:
        hits = retrieve(engine, query, deal_id=deal_id, person_id=person_id, k=3)
        if hits:
            lines.append("Relevant: " + " | ".join(h["chunk_text"][:160] for h in hits))
    floor = "\n".join(lines) if lines else "No history for this contact yet."

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _synthesize(floor, query)  # LLM path; falls back to floor on any error
        except Exception:
            return floor
    return floor

def _synthesize(floor: str, query: str) -> str:
    # ponytail: wire to the same Anthropic client copy_llm uses; keep it a thin call.
    from .copy_llm import complete  # reuse existing client seam
    prompt = f"Write a 5-line pre-call brief for a sales rep. Facts:\n{floor}\nFocus: {query or 'general'}"
    return complete(prompt).strip() or floor
```

Note: if `copy_llm` has no `complete()` helper, the implementer adds a thin one or inlines the existing client call — do NOT invent a new dependency. The deterministic floor is the tested path.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_brief.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add zerocrm/brief.py tests/test_brief.py
git commit -m "feat(brief): deterministic pre-call brief + LLM activation seam"
```

---

### Task 6: `review.py` — win/loss note on deal close

**Files:**
- Create: `zerocrm/review.py`
- Test: `tests/test_review.py`

**Interfaces:**
- Produces: `review.write_review(engine, *, deal_id, outcome, workspace_id="default") -> str | None` — reads the deal's transcripts, writes one `touch(kind=review, direction=in)` with `payload={outcome, reasons, sentiment, next_step}`. Deterministic floor (last-transcript-derived); LLM enriches when key set. Returns the touch id, or `None` if the deal has no person.
- Consumes: `schema.{deal,call,transcript,touch}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_review.py
from sqlalchemy import insert, select, func
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm import review
from zerocrm.schema import person, deal, call, transcript, touch

def test_write_review_records_one_review_touch():
    e = make_engine("sqlite://"); migrate(e)
    with e.begin() as c:
        c.execute(insert(person).values(id="p1", full_name="Jane"))
        c.execute(insert(deal).values(id="d1", person_id="p1", stage="lost"))
        c.execute(insert(call).values(id="c1", person_id="p1", deal_id="d1", source="meet", status="transcribed"))
        c.execute(insert(transcript).values(id="t1", call_id="c1", transcript_source="google",
                                            text="price too high vs competitor"))
    tid = review.write_review(e, deal_id="d1", outcome="lost")
    with e.connect() as c:
        n = c.execute(select(func.count()).select_from(touch).where(touch.c.kind == "review")).scalar()
        row = c.execute(select(touch).where(touch.c.id == tid)).mappings().first()
    assert n == 1 and row["payload"]["outcome"] == "lost"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_review.py -v` — Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# zerocrm/review.py
"""Win/loss note on deal close, written as touch(kind=review). No new deal column."""
from __future__ import annotations

import os
from sqlalchemy import select
from sqlalchemy.engine import Engine

from .schema import call, deal, touch, transcript

_ACTOR = {"kind": "agent", "id": "review"}

def write_review(engine: Engine, *, deal_id: str, outcome: str, workspace_id="default") -> str | None:
    with engine.begin() as conn:
        d = conn.execute(select(deal).where(deal.c.id == deal_id)).mappings().first()
        if not d or not d["person_id"]:
            return None
        texts = [r[0] for r in conn.execute(
            select(transcript.c.text).join(call, transcript.c.call_id == call.c.id)
            .where(call.c.deal_id == deal_id)).all() if r[0]]
        payload = _analyze(outcome, texts)
        return conn.execute(touch.insert().values(
            person_id=d["person_id"], channel="internal", direction="in", kind="review",
            payload=payload, actor=_ACTOR).returning(touch.c.id)).scalar()

def _analyze(outcome: str, texts: list[str]) -> dict:
    joined = " ".join(texts)
    floor = {"outcome": outcome, "reasons": [joined[:200]] if joined else [],
             "sentiment": "unknown", "next_step": ""}
    if texts and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from .copy_llm import complete
            import json
            raw = complete(f"Return JSON {{reasons:[], sentiment, next_step}} for a {outcome} deal from:\n{joined[:3000]}")
            floor.update(json.loads(raw))
        except Exception:
            pass
    return floor
```

Note: `touch.insert().returning(...)` works on SQLite ≥ 3.35 (bundled with modern Python) and Postgres. If the pinned SQLite lacks RETURNING, use `.inserted_primary_key` like Task 3 — pick one and stay consistent.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_review.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add zerocrm/review.py tests/test_review.py
git commit -m "feat(review): win/loss note as touch(kind=review) on deal close"
```

---

### Task 7: `slack.py` — poll, authenticate, dispatch, post

**Files:**
- Create: `zerocrm/slack.py`
- Test: `tests/test_slack.py`

**Interfaces:**
- Produces:
  - `slack.handle_message(engine, text, user_id, *, client, workspace_id="default") -> str | None` — authenticates against `slack_operator_ids` allowlist; on a "brief" intent resolves the named person/deal and returns `brief.precall_brief(...)`, posting via `client.post_message`. Non-operators return `None` (ignored).
  - `slack.poll_slack(engine, client, *, workspace_id="default") -> int` — reads `slack_cursor` from config, pulls new messages via `client.history(since)`, dispatches each, advances the cursor. Returns count handled.
- Consumes: `config`, `brief.precall_brief`, `person` lookup by name/email. `client` has `history(since)->list[{ts,user,text}]` and `post_message(text)`; real Slack client wired in Task 8, tests use a fake.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_slack.py
from sqlalchemy import insert
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm import slack
from zerocrm.config import set_config, get_config
from zerocrm.schema import person

class FakeSlack:
    def __init__(self, msgs): self._msgs, self.posted = msgs, []
    def history(self, since): return self._msgs
    def post_message(self, text): self.posted.append(text)

def _engine():
    e = make_engine("sqlite://"); migrate(e)
    set_config(e, "slack_operator_ids", ["U_OP"])
    with e.begin() as c:
        c.execute(insert(person).values(id="p1", full_name="Acme Jane"))
    return e

def test_operator_brief_is_answered():
    e = _engine(); client = FakeSlack([])
    out = slack.handle_message(e, "brief me on Acme", "U_OP", client=client)
    assert out and client.posted

def test_non_operator_is_ignored():
    e = _engine(); client = FakeSlack([])
    assert slack.handle_message(e, "brief me on Acme", "U_STRANGER", client=client) is None
    assert not client.posted

def test_poll_advances_cursor_and_dedupes():
    e = _engine()
    msgs = [{"ts": "100.1", "user": "U_OP", "text": "brief me on Acme"}]
    assert slack.poll_slack(e, FakeSlack(msgs)) == 1
    assert slack.poll_slack(e, FakeSlack(msgs)) == 0  # cursor past ts=100.1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_slack.py -v` — Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# zerocrm/slack.py
"""Slack DM surface — the on-demand clock. Poll-primary (no Socket Mode, no public
endpoint), driven by a fast run_slack_tick. Operator-allowlisted; message text is
DATA, never an instruction."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .brief import precall_brief
from .config import get_config, set_config
from .schema import person

def _operators(engine): return set(get_config(engine, "slack_operator_ids", []) or [])

def _find_person(engine, text):
    # naive name match: the last capitalized-ish token(s). ponytail: exact/ILIKE
    # contains; upgrade to fuzzy only if reps complain.
    needle = text.lower().replace("brief me on", "").replace("brief on", "").strip(" ?.")
    if not needle:
        return None
    with engine.connect() as conn:
        for p in conn.execute(select(person)).mappings():
            name = (p["full_name"] or "").lower()
            if needle in name or (name and name.split()[0] in needle):
                return p
    return None

def handle_message(engine: Engine, text: str, user_id: str, *, client,
                   workspace_id="default") -> str | None:
    if user_id not in _operators(engine):
        return None  # trust gate — mirrors the digest reply allowlist
    if "brief" in text.lower():
        p = _find_person(engine, text)
        if not p:
            reply = "No matching contact. Try the full name."
        else:
            with engine.connect() as conn:
                from .schema import deal
                d = conn.execute(select(deal.c.id).where(deal.c.person_id == p["id"])).first()
            reply = precall_brief(engine, person_id=p["id"], deal_id=(d[0] if d else None), query=text)
        client.post_message(reply)
        return reply
    return None  # unknown intent — stay silent rather than guess

def poll_slack(engine: Engine, client, *, workspace_id="default") -> int:
    cursor = float(get_config(engine, "slack_cursor", 0) or 0)
    handled, high = 0, cursor
    for m in client.history(cursor):
        ts = float(m["ts"])
        if ts <= cursor:
            continue
        handle_message(engine, m["text"], m["user"], client=client, workspace_id=workspace_id)
        handled += 1
        high = max(high, ts)
    if high > cursor:
        set_config(engine, "slack_cursor", high)
    return handled
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_slack.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add zerocrm/slack.py tests/test_slack.py
git commit -m "feat(slack): operator-gated pre-call brief over a poll-primary DM surface"
```

---

### Task 8: Runner + CLI wiring (Meet step, `run_slack_tick`, `ingest-doc`)

**Files:**
- Modify: `zerocrm/runner.py` (add Meet poll to `run_tick`; add `run_slack_tick`)
- Modify: `zerocrm/cli.py` (add `slack-tick`, `ingest-doc` subcommands)
- Test: `tests/test_runner_conversation.py`

**Interfaces:**
- Produces: `runner.run_slack_tick(engine) -> dict`; `run_tick` result gains `"meet_ingested"`. Real Google/Slack clients are built from env behind `_build_meet_client()` / `_build_slack_client()` returning `None` when creds absent (no-op), mirroring `_build_finders()`.
- Consumes: `meet.poll_meet`, `slack.poll_slack`.

- [ ] **Step 1: Write the failing test** (client-absent no-op + explicit-client path)

```python
# tests/test_runner_conversation.py
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm import runner

def test_slack_tick_is_noop_without_client(monkeypatch):
    e = make_engine("sqlite://"); migrate(e)
    monkeypatch.setattr(runner, "_build_slack_client", lambda eng: None)
    assert runner.run_slack_tick(e) == {"slack_handled": 0}

def test_tick_reports_meet_ingested_key(monkeypatch):
    e = make_engine("sqlite://"); migrate(e)
    monkeypatch.setattr(runner, "_build_meet_client", lambda eng: None)
    # other tick deps already no-op without creds; just assert the key exists
    out = runner.run_tick(e)
    assert "meet_ingested" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_runner_conversation.py -v` — Expected: FAIL.

- [ ] **Step 3: Wire the runner**

```python
# in runner.py
def _build_meet_client(engine):
    """Google Meet API client, or None when creds absent (tick no-ops)."""
    import os
    if not os.environ.get("GOOGLE_MEET_CREDENTIALS"):
        return None
    from .drivers import GoogleMeetClient  # thin wrapper added alongside drivers
    return GoogleMeetClient()

def _build_slack_client(engine):
    import os
    if not os.environ.get("SLACK_BOT_TOKEN"):
        return None
    from .drivers import SlackClient
    return SlackClient(channel=get_config(engine, "slack_bot_channel"))

def run_slack_tick(engine: Engine) -> dict:
    from .slack import poll_slack
    client = _build_slack_client(engine)
    return {"slack_handled": poll_slack(engine, client) if client else 0}
```

In `run_tick`, after the Smartlead poll block, add:

```python
    from .meet import poll_meet
    meet_client = _build_meet_client(engine)
    meet_ingested = len(poll_meet(engine, meet_client)) if meet_client else 0
    result["meet_ingested"] = meet_ingested
```

(Adjust to the actual `result` dict variable name used in `run_tick`.)

- [ ] **Step 4: Wire the CLI** (`cli.py`)

```python
def _cmd_slack_tick(args):
    print(json.dumps(run_slack_tick(make_engine(args.dsn)), default=str)); return 0

def _cmd_ingest_doc(args):
    from .memory import ingest_document
    text = open(args.path).read()
    n = ingest_document(make_engine(args.dsn), text=text, source_id=args.path,
                        deal_id=args.deal, person_id=args.person)
    print(f"{n} chunks"); return 0
```

Register both subparsers (mirror the existing `tick` registration), importing `run_slack_tick` from `.runner`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner_conversation.py -v` — Expected: PASS.

- [ ] **Step 6: Full slice test run + commit**

```bash
uv run pytest tests/test_migrate_conversation.py tests/test_memory.py tests/test_meet_normalize.py \
  tests/test_meet_poll.py tests/test_brief.py tests/test_review.py tests/test_slack.py \
  tests/test_runner_conversation.py -v
git add zerocrm/runner.py zerocrm/cli.py tests/test_runner_conversation.py
git commit -m "feat(runner): Meet poll in run_tick + run_slack_tick + ingest-doc CLI"
```

---

## Deferred to a follow-up (not this plan)

- **Real Google Meet + Slack driver classes** (`drivers.GoogleMeetClient`, `drivers.SlackClient`) against the live APIs — the poller/handler are already tested against the client contract; wiring the real HTTP client and OAuth is a live-verify task (needs real creds), same staging as the Smartlead driver.
- **Whisper adapter** for `transcribe_audio` (the phone/`source=voice` path).
- **pg_cron entry** for `run_slack_tick` (deploy config, ~2-min business-hours cadence).
- **Embeddings/pgvector** upgrade behind `memory.retrieve`.

## Self-review notes

- Spec coverage: capture (T3/T4), docs-as-context (T2 + T8 CLI), brief (T5/T7), win/loss (T6), Slack surface (T7/T8), seams/deferrals (T3 stub, T8 deferred). All spec sections map to a task.
- Types consistent across tasks: `normalize_conference_record -> call.id`, `poll_meet -> list[str]`, `retrieve -> list[dict]`, `precall_brief -> str`, `write_review -> str|None`, `poll_slack -> int`.
- Open decision for the implementer, flagged in-task: RETURNING vs `inserted_primary_key` — pick one and use it in both T3 and T6.
