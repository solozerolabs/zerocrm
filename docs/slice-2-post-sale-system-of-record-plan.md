# Slice 2 — Post-sale system-of-record Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give zerocrm a post-sale record: locations, living service contracts with a full change audit, docs-on-record, document generation, and location-tied inspections — UI-less, CRUD + audit, no new runtime.

**Architecture:** 8 new tables (migration `004_postsale`) and 3 modules. Every contract mutation writes one `contract_event` in the same transaction (append-only audit — no precedence gate, contracts have no competing writers). Documents optionally chunk into the slice-1 `memory_chunk` spine for retrieval.

**Tech Stack:** Python 3, SQLAlchemy Core, stdlib only (incl. `string.Template` for generation), pytest sqlite in-memory.

## Review corrections (BINDING — override any task code below that disagrees)

Applied from the plan-eng review; task code blocks predate these:

1. **Add a `body` Text column to `document`** (Task 1). `generate_document` MUST persist the rendered text there (a generated doc you can't read back is a phantom). Remove the `if False else` dead ternary and the non-existent `meta=None` from that insert — one clean statement.
2. **`attach_document` chunks atomically:** do the doc insert, the `add_chunks(conn=...)` call, and the `chunked=True` set inside ONE `engine.begin()`. Drop the `_ = chunked` dead code.
3. **`remove_line` captures what it removed:** read the line's `kind`/`description`/`unit_cost_usd` first, put them in the `line_removed` event payload, then delete.

## Global Constraints

- Schema is one `metadata` in `zerocrm/schema.py`; additive tables use `Table.create(engine, checkfirst=True)` in a numbered `migrate.py::MIGRATIONS` callable (`004_postsale`). Never `create_all` for additive changes.
- Every table uses `_pk()` (uuid-hex) + `_common()` (`workspace_id`/`actor`/`created_at`/`updated_at`). `Json` column for jsonb-on-PG / JSON-on-SQLite.
- **Audit is append-only `contract_event`, NOT the `upsert.py` precedence gate.** Every contract mutation writes exactly one event in the SAME `engine.begin()` block as the state change — atomic, so an event can never diverge from the state it describes.
- FK integrity is ON (SQLite pragma set in `db.py`). Tests MUST create real parent rows (company/location/staff) before inserting children — a fake FK id raises `IntegrityError` (learned in slice 1).
- Use `.inserted_primary_key[0]` for generated ids (cross-version; slice-1 convention).
- `staff` is a separate table from `person` (employees ≠ prospects). No `person.segment` overload.
- Document generation is text/HTML via `string.Template` — no PDF dependency this slice. Blob upload (file → `uri`) is caller-supplied; the storage driver is deferred.
- No outbound/sending in this slice. System-of-record only.
- One test file per new module under `tests/`; run `uv run pytest tests/test_X.py -v`.

---

### Task 1: Schema + migration (8 post-sale tables)

**Files:**
- Modify: `zerocrm/schema.py` (add 8 tables after the conversation-memory block)
- Modify: `zerocrm/migrate.py` (`004_postsale`)
- Test: `tests/test_migrate_postsale.py`

**Interfaces:**
- Produces: `location, staff, contract, contract_staff, contract_line, document, inspection, contract_event` importable from `zerocrm.schema`; `migrate()` applies `004_postsale`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migrate_postsale.py
from sqlalchemy import inspect
from zerocrm.db import make_engine
from zerocrm.migrate import migrate

_TABLES = {"location", "staff", "contract", "contract_staff",
           "contract_line", "document", "inspection", "contract_event"}

def test_004_creates_postsale_tables():
    e = make_engine("sqlite://"); migrate(e)
    assert _TABLES <= set(inspect(e).get_table_names())

def test_004_is_idempotent():
    e = make_engine("sqlite://"); migrate(e)
    assert migrate(e) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_migrate_postsale.py -v` — Expected: FAIL.

- [ ] **Step 3: Add the tables to `schema.py`** (after the `memory_chunk` block)

```python
# --- post-sale system of record (slice 2) ----------------------------------
location = Table(
    "location", metadata, _pk(),
    Column("company_id", String(32), ForeignKey("company.id")),
    Column("name", Text, nullable=False),
    Column("address", Json, default=dict),
    Column("meta", Json, default=dict),
    *_common(),
)

# the customer's OWN employees — a SEPARATE population from person (prospects).
staff = Table(
    "staff", metadata, _pk(),
    Column("full_name", Text, nullable=False),
    Column("email", Text),
    Column("role", Text),
    Column("active", Boolean, nullable=False, default=True),
    *_common(),
)

contract = Table(
    "contract", metadata, _pk(),
    Column("company_id", String(32), ForeignKey("company.id")),
    Column("location_id", String(32), ForeignKey("location.id")),
    Column("contract_type", Text),
    Column("scope", Text),
    Column("status", Text, nullable=False, default="active"),  # active|paused|ended
    Column("value_usd", Float),
    Column("starts_on", Text),   # ISO date; text keeps SQLite/PG identical
    Column("ends_on", Text),
    *_common(),
)

contract_staff = Table(
    "contract_staff", metadata, _pk(),
    Column("contract_id", String(32), ForeignKey("contract.id"), nullable=False),
    Column("staff_id", String(32), ForeignKey("staff.id"), nullable=False),
    Column("role", Text),
    Column("assigned_at", DateTime(timezone=True), default=_now),
    Column("removed_at", DateTime(timezone=True)),  # a swap sets this, never deletes
    *_common(),
)

contract_line = Table(
    "contract_line", metadata, _pk(),
    Column("contract_id", String(32), ForeignKey("contract.id"), nullable=False),
    Column("kind", Text, nullable=False),          # one_off_visit|material|supply
    Column("description", Text),
    Column("qty", Float, default=1),
    Column("unit_cost_usd", Float),                # nullable; Slice 3 links to a supply
    Column("occurred_on", Text),
    *_common(),
)

document = Table(
    "document", metadata, _pk(),
    Column("contract_id", String(32), ForeignKey("contract.id")),
    Column("location_id", String(32), ForeignKey("location.id")),
    Column("filename", Text, nullable=False),
    Column("uri", Text),
    Column("doc_type", Text),
    Column("generated", Boolean, nullable=False, default=False),
    Column("chunked", Boolean, nullable=False, default=False),
    *_common(),
)

inspection = Table(
    "inspection", metadata, _pk(),
    Column("location_id", String(32), ForeignKey("location.id"), nullable=False),
    Column("contract_id", String(32), ForeignKey("contract.id")),
    Column("scheduled_for", Text),
    Column("performed_at", DateTime(timezone=True)),
    Column("outcome", Text),                       # pass|fail|partial
    Column("score", Integer),
    Column("notes", Text),
    *_common(),
)

# append-only audit — one query renders a contract's whole living-document history
contract_event = Table(
    "contract_event", metadata, _pk(),
    Column("contract_id", String(32), ForeignKey("contract.id"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("payload", Json, default=dict),         # {before, after} or {field, value}
    Column("ts", DateTime(timezone=True), default=_now, nullable=False),
    *_common(),
    Index("ix_contract_event", "contract_id", "ts"),
)
```

- [ ] **Step 4: Add the migration** (`migrate.py`)

```python
from .schema import (metadata, research, schema_migrations, call, transcript,
                     memory_chunk, location, staff, contract, contract_staff,
                     contract_line, document, inspection, contract_event)

def _postsale(engine: Engine) -> None:
    for t in (location, staff, contract, contract_staff, contract_line,
              document, inspection, contract_event):
        t.create(engine, checkfirst=True)

MIGRATIONS: dict[str, callable] = {
    "001_core": _create_all,
    "002_research": _research,
    "003_conversation": _conversation,
    "004_postsale": _postsale,
}
```

- [ ] **Step 5: Run tests** — `uv run pytest tests/test_migrate_postsale.py -v` — Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add zerocrm/schema.py zerocrm/migrate.py tests/test_migrate_postsale.py
git commit -m "feat(schema): post-sale tables + 004 migration"
```

---

### Task 2: `contracts.py` — create, field edits, history (the audit core)

**Files:**
- Create: `zerocrm/contracts.py`
- Test: `tests/test_contracts.py`

**Interfaces:**
- Produces:
  - `create_contract(engine, *, company_id=None, location_id=None, contract_type=None, scope=None, value_usd=None, actor, workspace_id="default") -> str` — inserts the contract + a `created` event, returns id.
  - `set_field(engine, contract_id, field, value, *, actor) -> None` — updates one of {`scope`,`contract_type`,`status`,`value_usd`,`starts_on`,`ends_on`} and writes a `<field>_changed` event with `{before, after}`.
  - `contract_history(engine, contract_id) -> list[dict]` — events, ts order.
  - `_event(conn, contract_id, kind, payload, actor)` — internal, insert one event (reused by Tasks 3–5).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contracts.py
from sqlalchemy import insert
from zerocrm import contracts
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company, location

A = {"kind": "human", "id": "sid"}

def _engine():
    e = make_engine("sqlite://"); migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="co1", name="Acme"))
        c.execute(insert(location).values(id="loc1", company_id="co1", name="HQ"))
    return e

def test_create_writes_created_event():
    e = _engine()
    cid = contracts.create_contract(e, company_id="co1", location_id="loc1",
                                    contract_type="janitorial", scope="nightly", actor=A)
    hist = contracts.contract_history(e, cid)
    assert len(hist) == 1 and hist[0]["kind"] == "created"

def test_set_field_records_before_and_after():
    e = _engine()
    cid = contracts.create_contract(e, company_id="co1", location_id="loc1", scope="nightly", actor=A)
    contracts.set_field(e, cid, "scope", "nightly + weekend", actor=A)
    hist = contracts.contract_history(e, cid)
    changed = [h for h in hist if h["kind"] == "scope_changed"]
    assert changed and changed[0]["payload"]["before"] == "nightly"
    assert changed[0]["payload"]["after"] == "nightly + weekend"

def test_set_field_rejects_unknown_field():
    e = _engine()
    cid = contracts.create_contract(e, company_id="co1", location_id="loc1", actor=A)
    import pytest
    with pytest.raises(ValueError):
        contracts.set_field(e, cid, "id", "hacked", actor=A)
```

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/test_contracts.py -v`.

- [ ] **Step 3: Write minimal implementation**

```python
# zerocrm/contracts.py
"""Living service contracts + their append-only audit.

Every mutation writes exactly one contract_event in the SAME transaction as the
state change, so the audit can never diverge from the state. This is NOT the
upsert precedence gate — a contract has no competing writers."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, Engine

from .schema import contract, contract_event

_EDITABLE = {"scope", "contract_type", "status", "value_usd", "starts_on", "ends_on"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(conn: Connection, contract_id: str, kind: str, payload: dict, actor: dict,
           workspace_id: str = "default") -> None:
    conn.execute(insert(contract_event).values(
        contract_id=contract_id, kind=kind, payload=payload, actor=actor,
        workspace_id=workspace_id))


def create_contract(engine: Engine, *, company_id=None, location_id=None,
                    contract_type=None, scope=None, value_usd=None, actor,
                    workspace_id: str = "default") -> str:
    with engine.begin() as conn:
        cid = conn.execute(insert(contract).values(
            company_id=company_id, location_id=location_id, contract_type=contract_type,
            scope=scope, value_usd=value_usd, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        _event(conn, cid, "created",
               {"contract_type": contract_type, "scope": scope}, actor, workspace_id)
    return cid


def set_field(engine: Engine, contract_id: str, field: str, value, *, actor,
              workspace_id: str = "default") -> None:
    if field not in _EDITABLE:
        raise ValueError(f"field {field!r} is not editable")
    with engine.begin() as conn:
        before = conn.execute(
            select(contract.c[field]).where(contract.c.id == contract_id)).scalar()
        conn.execute(update(contract).where(contract.c.id == contract_id)
                     .values({field: value, "updated_at": _now()}))
        _event(conn, contract_id, f"{field}_changed",
               {"before": before, "after": value}, actor, workspace_id)


def contract_history(engine: Engine, contract_id: str) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(contract_event).where(contract_event.c.contract_id == contract_id)
            .order_by(contract_event.c.ts, contract_event.c.created_at)).mappings()]
```

- [ ] **Step 4: Run tests** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add zerocrm/contracts.py tests/test_contracts.py
git commit -m "feat(contracts): create + audited field edits + history"
```

---

### Task 3: Contract staff — assign, swap, remove (history preserved)

**Files:**
- Modify: `zerocrm/contracts.py`
- Test: `tests/test_contract_staff.py`

**Interfaces:**
- Produces (all write a `contract_event`):
  - `assign_staff(engine, contract_id, staff_id, *, role=None, actor) -> str`
  - `remove_staff(engine, contract_id, staff_id, *, actor) -> None` — sets `removed_at`, never deletes.
  - `swap_staff(engine, contract_id, out_staff_id, in_staff_id, *, role=None, actor) -> str` — remove old + assign new + ONE `staff_swapped` event.
  - `active_staff(engine, contract_id) -> list[dict]` — rows with `removed_at IS NULL`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contract_staff.py
from sqlalchemy import insert
from zerocrm import contracts
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company, location, staff

A = {"kind": "human", "id": "sid"}

def _engine():
    e = make_engine("sqlite://"); migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="co1", name="Acme"))
        c.execute(insert(location).values(id="loc1", company_id="co1", name="HQ"))
        c.execute(insert(staff).values(id="s_old", full_name="Pat"))
        c.execute(insert(staff).values(id="s_new", full_name="Robin"))
    return e

def test_swap_preserves_history_and_writes_one_event():
    e = _engine()
    cid = contracts.create_contract(e, company_id="co1", location_id="loc1", actor=A)
    contracts.assign_staff(e, cid, "s_old", actor=A)
    contracts.swap_staff(e, cid, "s_old", "s_new", actor=A)
    active = contracts.active_staff(e, cid)
    assert [a["staff_id"] for a in active] == ["s_new"]
    events = [h["kind"] for h in contracts.contract_history(e, cid)]
    assert events.count("staff_swapped") == 1
    # the removed assignment row still exists (history preserved, not deleted)
    from sqlalchemy import select, func
    from zerocrm.schema import contract_staff
    with e.connect() as c:
        total = c.execute(select(func.count()).select_from(contract_staff)
                          .where(contract_staff.c.contract_id == cid)).scalar()
    assert total == 2
```

- [ ] **Step 2: Run test to verify it fails.**

- [ ] **Step 3: Add to `contracts.py`**

```python
from .schema import contract_staff

def assign_staff(engine, contract_id, staff_id, *, role=None, actor, workspace_id="default"):
    with engine.begin() as conn:
        rid = conn.execute(insert(contract_staff).values(
            contract_id=contract_id, staff_id=staff_id, role=role, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        _event(conn, contract_id, "staff_added", {"staff_id": staff_id, "role": role},
               actor, workspace_id)
    return rid

def remove_staff(engine, contract_id, staff_id, *, actor, workspace_id="default"):
    with engine.begin() as conn:
        conn.execute(update(contract_staff)
                     .where(contract_staff.c.contract_id == contract_id,
                            contract_staff.c.staff_id == staff_id,
                            contract_staff.c.removed_at.is_(None))
                     .values(removed_at=_now()))
        _event(conn, contract_id, "staff_removed", {"staff_id": staff_id}, actor, workspace_id)

def swap_staff(engine, contract_id, out_staff_id, in_staff_id, *, role=None, actor,
               workspace_id="default"):
    with engine.begin() as conn:
        conn.execute(update(contract_staff)
                     .where(contract_staff.c.contract_id == contract_id,
                            contract_staff.c.staff_id == out_staff_id,
                            contract_staff.c.removed_at.is_(None))
                     .values(removed_at=_now()))
        rid = conn.execute(insert(contract_staff).values(
            contract_id=contract_id, staff_id=in_staff_id, role=role, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        _event(conn, contract_id, "staff_swapped",
               {"out": out_staff_id, "in": in_staff_id}, actor, workspace_id)
    return rid

def active_staff(engine, contract_id):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(contract_staff).where(contract_staff.c.contract_id == contract_id,
                                         contract_staff.c.removed_at.is_(None))).mappings()]
```

- [ ] **Step 4: Run tests** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add zerocrm/contracts.py tests/test_contract_staff.py
git commit -m "feat(contracts): audited staff assign/swap/remove, history preserved"
```

---

### Task 4: Contract lines — one-off visits + materials

**Files:**
- Modify: `zerocrm/contracts.py`
- Test: `tests/test_contract_lines.py`

**Interfaces:**
- Produces:
  - `add_line(engine, contract_id, *, kind, description=None, qty=1, unit_cost_usd=None, occurred_on=None, actor) -> str` — writes a `line_added` event.
  - `remove_line(engine, line_id, *, actor) -> None` — deletes the line, writes a `line_removed` event (looks up contract_id first).
  - `lines(engine, contract_id) -> list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contract_lines.py
from sqlalchemy import insert
from zerocrm import contracts
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company, location

A = {"kind": "human", "id": "sid"}

def _engine_cid():
    e = make_engine("sqlite://"); migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="co1", name="Acme"))
        c.execute(insert(location).values(id="loc1", company_id="co1", name="HQ"))
    cid = contracts.create_contract(e, company_id="co1", location_id="loc1", actor=A)
    return e, cid

def test_add_one_off_visit_writes_event_and_line():
    e, cid = _engine_cid()
    contracts.add_line(e, cid, kind="one_off_visit", description="emergency clean",
                       unit_cost_usd=120, actor=A)
    assert len(contracts.lines(e, cid)) == 1
    assert any(h["kind"] == "line_added" for h in contracts.contract_history(e, cid))
```

- [ ] **Step 2: Run test to verify it fails.**

- [ ] **Step 3: Add to `contracts.py`**

```python
from .schema import contract_line

def add_line(engine, contract_id, *, kind, description=None, qty=1,
             unit_cost_usd=None, occurred_on=None, actor, workspace_id="default"):
    with engine.begin() as conn:
        lid = conn.execute(insert(contract_line).values(
            contract_id=contract_id, kind=kind, description=description, qty=qty,
            unit_cost_usd=unit_cost_usd, occurred_on=occurred_on, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        _event(conn, contract_id, "line_added",
               {"kind": kind, "description": description, "unit_cost_usd": unit_cost_usd},
               actor, workspace_id)
    return lid

def remove_line(engine, line_id, *, actor, workspace_id="default"):
    with engine.begin() as conn:
        cid = conn.execute(select(contract_line.c.contract_id)
                           .where(contract_line.c.id == line_id)).scalar()
        if cid is None:
            return
        conn.execute(contract_line.delete().where(contract_line.c.id == line_id))
        _event(conn, cid, "line_removed", {"line_id": line_id}, actor, workspace_id)

def lines(engine, contract_id):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(contract_line).where(contract_line.c.contract_id == contract_id)).mappings()]
```

- [ ] **Step 4: Run tests** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add zerocrm/contracts.py tests/test_contract_lines.py
git commit -m "feat(contracts): audited contract lines (one-off visits, materials)"
```

---

### Task 5: `documents.py` — attach (+ optional chunk) and generate

**Files:**
- Create: `zerocrm/documents.py`
- Test: `tests/test_documents.py`

**Interfaces:**
- Produces:
  - `attach_document(engine, *, contract_id=None, location_id=None, filename, uri=None, doc_type=None, text=None, actor, workspace_id="default") -> str` — inserts a `document`; if `text` given, calls `memory.ingest_document(source_id=<doc id>, ...)` and sets `chunked=true`. Requires at least one of contract_id/location_id (ValueError otherwise). If contract_id, writes a `doc_attached` event.
  - `generate_document(engine, *, contract_id, template, filename="contract.md", actor, workspace_id="default") -> str` — renders `string.Template(template)` against the contract row, stores a `generated=true` document, writes a `doc_generated` event, returns the doc id.
- Consumes: `contracts._event`, `memory.ingest_document`, `schema.{document, contract}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_documents.py
import pytest
from sqlalchemy import func, insert, select
from zerocrm import contracts, documents
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company, document, location, memory_chunk

A = {"kind": "human", "id": "sid"}

def _engine_cid():
    e = make_engine("sqlite://"); migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="co1", name="Acme"))
        c.execute(insert(location).values(id="loc1", company_id="co1", name="HQ"))
    cid = contracts.create_contract(e, company_id="co1", location_id="loc1",
                                    scope="nightly janitorial", actor=A)
    return e, cid

def test_attach_with_text_chunks_into_memory():
    e, cid = _engine_cid()
    documents.attach_document(e, contract_id=cid, filename="confirm.txt",
                              text="signed confirmation of nightly service", actor=A)
    with e.connect() as c:
        assert c.execute(select(func.count()).select_from(memory_chunk)
                         .where(memory_chunk.c.source_type == "document")).scalar() >= 1
        assert c.execute(select(document.c.chunked)).scalar() == 1

def test_attach_without_text_does_not_chunk():
    e, cid = _engine_cid()
    documents.attach_document(e, contract_id=cid, filename="scan.pdf",
                              uri="s3://bucket/scan.pdf", actor=A)
    with e.connect() as c:
        assert c.execute(select(func.count()).select_from(memory_chunk)).scalar() == 0

def test_attach_requires_a_parent():
    e, _ = _engine_cid()
    with pytest.raises(ValueError):
        documents.attach_document(e, filename="orphan.txt", text="x", actor=A)

def test_generate_stores_rendered_doc_and_event():
    e, cid = _engine_cid()
    did = documents.generate_document(e, contract_id=cid,
                                      template="Scope: $scope / Type: $contract_type", actor=A)
    with e.connect() as c:
        row = c.execute(select(document).where(document.c.id == did)).mappings().first()
    assert row["generated"] == 1
    assert any(h["kind"] == "doc_generated" for h in contracts.contract_history(e, cid))
```

- [ ] **Step 2: Run test to verify it fails.**

- [ ] **Step 3: Write minimal implementation**

```python
# zerocrm/documents.py
"""Docs-on-record: the record of truth for uploaded/generated files. A text doc
MAY also chunk into the slice-1 memory_chunk spine for retrieval; the record here
stays authoritative. Blob upload (file -> uri) is caller-supplied; the storage
driver is deferred. Generation is string.Template text/HTML — PDF is a later seam."""
from __future__ import annotations

from string import Template

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from .contracts import _event
from .memory import ingest_document
from .schema import contract, document


def attach_document(engine: Engine, *, contract_id=None, location_id=None, filename,
                    uri=None, doc_type=None, text=None, actor, workspace_id="default") -> str:
    if not (contract_id or location_id):
        raise ValueError("a document must attach to a contract or a location")
    chunked = False
    with engine.begin() as conn:
        did = conn.execute(insert(document).values(
            contract_id=contract_id, location_id=location_id, filename=filename,
            uri=uri, doc_type=doc_type, generated=False, chunked=False, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        if contract_id:
            _event(conn, contract_id, "doc_attached", {"filename": filename}, actor, workspace_id)
    if text and text.strip():
        ingest_document(engine, text=text, source_id=did, workspace_id=workspace_id)
        with engine.begin() as conn:
            conn.execute(document.update().where(document.c.id == did).values(chunked=True))
        chunked = True
    _ = chunked
    return did


def generate_document(engine: Engine, *, contract_id, template, filename="contract.md",
                      actor, workspace_id="default") -> str:
    with engine.begin() as conn:
        row = conn.execute(select(contract).where(contract.c.id == contract_id)).mappings().first()
        rendered = Template(template).safe_substitute({k: ("" if v is None else v)
                                                       for k, v in dict(row or {}).items()})
        did = conn.execute(insert(document).values(
            contract_id=contract_id, filename=filename, doc_type="generated",
            uri=None, generated=True, chunked=False, actor=actor,
            workspace_id=workspace_id, meta=None) if False else insert(document).values(
            contract_id=contract_id, filename=filename, doc_type="generated",
            generated=True, chunked=False, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        _event(conn, contract_id, "doc_generated",
               {"filename": filename, "length": len(rendered)}, actor, workspace_id)
    return did
```

Note: the rendered text is captured in the event `length` and returned via the
record; storing the rendered bytes themselves is a blob-store concern (deferred) —
for now `generate_document` returns the id and the caller can re-render. If the
implementer prefers to persist the rendered text, add a `body` Text column to
`document` in Task 1 rather than smuggling it into Json. Pick one; don't leave the
rendered output unpersisted AND unreturned.

- [ ] **Step 4: Run tests** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add zerocrm/documents.py tests/test_documents.py
git commit -m "feat(documents): attach (+optional chunk) and template generation"
```

---

### Task 6: `inspections.py` — location-tied quality checks

**Files:**
- Create: `zerocrm/inspections.py`
- Test: `tests/test_inspections.py`

**Interfaces:**
- Produces:
  - `record_inspection(engine, *, location_id, contract_id=None, outcome, score=None, performed_at=None, notes="", workspace_id="default") -> str`
  - `location_inspections(engine, location_id) -> list[dict]` — for "track inspections back to the location".

- [ ] **Step 1: Write the failing test**

```python
# tests/test_inspections.py
from sqlalchemy import insert
from zerocrm import inspections
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company, location

def _engine():
    e = make_engine("sqlite://"); migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="co1", name="Acme"))
        c.execute(insert(location).values(id="loc1", company_id="co1", name="HQ"))
    return e

def test_record_and_list_by_location():
    e = _engine()
    inspections.record_inspection(e, location_id="loc1", outcome="pass", score=9)
    got = inspections.location_inspections(e, "loc1")
    assert len(got) == 1 and got[0]["outcome"] == "pass"
```

- [ ] **Step 2: Run test to verify it fails.**

- [ ] **Step 3: Write minimal implementation**

```python
# zerocrm/inspections.py
"""Periodic quality inspections, tied to a location. Recurrence reuses the digest
clock (a config-driven notice), NOT a new scheduler — this module stores + reads."""
from __future__ import annotations

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from .schema import inspection


def record_inspection(engine: Engine, *, location_id, contract_id=None, outcome,
                      score=None, performed_at=None, notes="", workspace_id="default") -> str:
    with engine.begin() as conn:
        return conn.execute(insert(inspection).values(
            location_id=location_id, contract_id=contract_id, outcome=outcome,
            score=score, performed_at=performed_at, notes=notes,
            workspace_id=workspace_id)).inserted_primary_key[0]


def location_inspections(engine: Engine, location_id: str) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(inspection).where(inspection.c.location_id == location_id)
            .order_by(inspection.c.created_at.desc())).mappings()]
```

- [ ] **Step 4: Run tests** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add zerocrm/inspections.py tests/test_inspections.py
git commit -m "feat(inspections): record + list quality inspections by location"
```

---

### Task 7: CLI verbs + full-slice run

**Files:**
- Modify: `zerocrm/cli.py`
- Test: covered by the module tests; this task wires the operator surface.

**Interfaces:**
- Produces subcommands: `add-location`, `add-staff`, `new-contract`, `contract-history`, `attach-doc`, `record-inspection`. Each parses args and calls the module function, printing the new id / JSON.

- [ ] **Step 1: Add commands** (mirror the existing `ingest-doc` subparser registration; `--dsn` is a top-level arg). Example for the two highest-value verbs:

```python
def _cmd_new_contract(args):
    from .contracts import create_contract
    cid = create_contract(make_engine(args.dsn), company_id=args.company,
                          location_id=args.location, contract_type=args.type,
                          scope=args.scope, actor={"kind": "human", "id": "cli"})
    print(cid); return 0

def _cmd_contract_history(args):
    from .contracts import contract_history
    print(json.dumps(contract_history(make_engine(args.dsn), args.contract), default=str))
    return 0
```

Register (in `main`):

```python
    nc = sub.add_parser("new-contract", help="create a service contract")
    nc.add_argument("--company"); nc.add_argument("--location")
    nc.add_argument("--type"); nc.add_argument("--scope")
    nc.set_defaults(func=_cmd_new_contract)

    ch = sub.add_parser("contract-history", help="print a contract's audit trail")
    ch.add_argument("contract")
    ch.set_defaults(func=_cmd_contract_history)
```

Add `add-location`, `add-staff`, `attach-doc`, `record-inspection` the same way.

- [ ] **Step 2: Smoke-test end to end**

```bash
uv run python -c "
from zerocrm.cli import main
import tempfile, os
db=tempfile.mktemp(suffix='.db'); dsn=f'sqlite:///{db}'
main(['--dsn', dsn, 'migrate'])
main(['--dsn', dsn, 'add-location', '--name', 'HQ'])
os.remove(db); print('CLI OK')"
```

- [ ] **Step 3: Full-slice test run + commit**

```bash
uv run pytest tests/test_migrate_postsale.py tests/test_contracts.py \
  tests/test_contract_staff.py tests/test_contract_lines.py \
  tests/test_documents.py tests/test_inspections.py -v
git add zerocrm/cli.py
git commit -m "feat(cli): post-sale verbs (location, staff, contract, history, doc, inspection)"
```

---

## Deferred to a follow-up (not this plan)

- **Blob storage driver** — actually uploading a file to `document.uri` (Supabase Storage / Fly volume). The record, audit, and retrieval hookup are built; the upload path is a live-verify task like slice 1's Google/Slack clients.
- **PDF generation** — `generate_document` renders text/HTML; a PDF renderer plugs in behind it.
- **Slack reporting intents** — "contracts by location", "who's on the Acme contract" as SQL-over-Slack (CC-3). The relational model enables it; the intent routing is a thin add on slice 1's `slack.py`.
- **Inspection recurrence notices** — a config-driven digest reminder (reuses the existing clock).
- **Slice 3 wiring** — `contract_line.unit_cost_usd` links to a vendor supply + `price_event`.

## Self-review notes

- Spec coverage: location (T1), living contract + audit (T2), staff swaps preserving history (T3), one-off visits/materials (T4), docs-on-record + optional retrieval + generation (T5), inspections-by-location (T6), operator surface (T7). All spec sections map to a task.
- Types consistent: every mutation returns an id (`str`) or `None`; `contract_history`/`lines`/`active_staff`/`location_inspections` return `list[dict]`; `_event` is the single event writer reused by T2–T5.
- Open decision flagged in-task (T5): persist rendered generation output as a `body` column vs return-and-re-render — pick one at implementation.
- Audit atomicity is asserted by construction (event in the same `engine.begin()` as the mutation); add the explicit rollback test from the spec's testing section during T2.
