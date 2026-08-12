# Slice 3 — CPQ / price book Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A master supply list with vendor price-change history, price-impact visibility on active contracts (surfaced in the digest), and basic CPQ (quote from supplies + margin) — UI-less, no invoice entity.

**Architecture:** 5 new tables + one guarded additive column (`contract_line.supply_id`, migration `005_cpq`) and 2 modules. `record_price_change` writes an append-only `price_event` and updates the supply cost in one transaction; `price_impacts` joins the supply to active contracts; `quotes` snapshots cost and applies a header margin.

**Tech Stack:** Python 3, SQLAlchemy Core, stdlib only, pytest sqlite in-memory.

## Global Constraints

- Additive tables via `Table.create(engine, checkfirst=True)` in `migrate.py::MIGRATIONS` (`005_cpq`). The additive COLUMN on `contract_line` needs a guarded raw `ALTER TABLE ... ADD COLUMN` (create_all/`Table.create` never adds a column to an existing table).
- `contract_line.supply_id` is a SOFT reference (plain `String(32)`, NO `ForeignKey`) — SQLite cannot `ADD COLUMN` with a FK, so a soft ref keeps the ALTER cross-backend. App-enforced.
- Every table uses `_pk()` + `_common()`. Money columns are `Float` (matches `deal.value_usd`; the repo has no Numeric convention).
- `record_price_change` writes the `price_event` and updates `supply.current_cost_usd` in the SAME `engine.begin()` — the event can never disagree with the supply's cost.
- No invoice entity. Price impacts are surfaced via `warmup.add_notice(engine, message)` (signature confirmed) — a digest notice, not an outbound action.
- FK integrity is ON; tests create real parent rows (vendor/supply/company/contract) before children.
- Use `.inserted_primary_key[0]` for ids. One test file per module.

---

### Task 1: Schema + migration (5 tables + `contract_line.supply_id`)

**Files:**
- Modify: `zerocrm/schema.py` (5 tables after the post-sale block; add `supply_id` to `contract_line`)
- Modify: `zerocrm/migrate.py` (`005_cpq` with a guarded ALTER)
- Test: `tests/test_migrate_cpq.py`

**Interfaces:**
- Produces: `vendor, supply, price_event, quote, quote_line` importable from `zerocrm.schema`; `contract_line` has a `supply_id` column; `migrate()` applies `005_cpq`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migrate_cpq.py
from sqlalchemy import inspect
from zerocrm.db import make_engine
from zerocrm.migrate import migrate

_TABLES = {"vendor", "supply", "price_event", "quote", "quote_line"}

def test_005_creates_cpq_tables_and_supply_id_column():
    e = make_engine("sqlite://"); migrate(e)
    insp = inspect(e)
    assert _TABLES <= set(insp.get_table_names())
    cols = {c["name"] for c in insp.get_columns("contract_line")}
    assert "supply_id" in cols

def test_005_is_idempotent():
    e = make_engine("sqlite://"); migrate(e)
    assert migrate(e) == []
```

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/test_migrate_cpq.py -v`.

- [ ] **Step 3: Add tables + the column to `schema.py`**

Add `supply_id` to the existing `contract_line` table (plain, no FK):

```python
    Column("supply_id", String(32)),  # soft ref -> supply (slice 3); no DB FK (ALTER-safe)
```

Add after the post-sale block:

```python
# --- CPQ / price book (slice 3) --------------------------------------------
vendor = Table(
    "vendor", metadata, _pk(),
    Column("name", Text, nullable=False),
    Column("contact", Json, default=dict),
    Column("meta", Json, default=dict),
    *_common(),
)

supply = Table(
    "supply", metadata, _pk(),
    Column("vendor_id", String(32), ForeignKey("vendor.id")),
    Column("sku", Text),
    Column("name", Text, nullable=False),
    Column("unit", Text),
    Column("current_cost_usd", Float),
    *_common(),
)

# append-only vendor price-change history (mirrors contract_event)
price_event = Table(
    "price_event", metadata, _pk(),
    Column("supply_id", String(32), ForeignKey("supply.id"), nullable=False),
    Column("old_cost_usd", Float),
    Column("new_cost_usd", Float),
    Column("effective_on", Text),
    Column("source", Text),
    Column("ts", DateTime(timezone=True), default=_now, nullable=False),
    *_common(),
    Index("ix_price_event_supply", "supply_id", "ts"),
)

quote = Table(
    "quote", metadata, _pk(),
    Column("company_id", String(32), ForeignKey("company.id")),
    Column("contract_id", String(32), ForeignKey("contract.id")),
    Column("deal_id", String(32), ForeignKey("deal.id")),
    Column("status", Text, nullable=False, default="draft"),  # draft|sent|accepted|rejected
    Column("margin_pct", Float, nullable=False, default=0.0),
    Column("total_usd", Float, default=0.0),
    *_common(),
)

quote_line = Table(
    "quote_line", metadata, _pk(),
    Column("quote_id", String(32), ForeignKey("quote.id"), nullable=False),
    Column("supply_id", String(32), ForeignKey("supply.id")),  # nullable — custom lines
    Column("description", Text),
    Column("qty", Float, default=1),
    Column("unit_cost_usd", Float),
    Column("unit_price_usd", Float),
    Column("line_total_usd", Float),
    *_common(),
)
```

- [ ] **Step 4: Add the migration** (`migrate.py`) — note the guarded ALTER

```python
from sqlalchemy import insert, inspect, select, text
from .schema import (..., vendor, supply, price_event, quote, quote_line, contract_line)

def _cpq(engine: Engine) -> None:
    for t in (vendor, supply, price_event, quote, quote_line):
        t.create(engine, checkfirst=True)
    # additive column on an existing table: create_all can't add it. Guard on the
    # inspector so this is a no-op on a fresh DB (004 already made the column) and
    # an ADD on a DB that reached 004 before supply_id existed. Soft ref -> no FK,
    # so the ALTER is identical on SQLite and Postgres.
    cols = {c["name"] for c in inspect(engine).get_columns("contract_line")}
    if "supply_id" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE contract_line ADD COLUMN supply_id VARCHAR(32)"))

MIGRATIONS: dict[str, callable] = {
    "001_core": _create_all,
    "002_research": _research,
    "003_conversation": _conversation,
    "004_postsale": _postsale,
    "005_cpq": _cpq,
}
```

- [ ] **Step 5: Run tests** — `uv run pytest tests/test_migrate_cpq.py -v` — Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add zerocrm/schema.py zerocrm/migrate.py tests/test_migrate_cpq.py
git commit -m "feat(schema): CPQ tables + contract_line.supply_id (005, guarded ALTER)"
```

---

### Task 2: `pricing.py` — supplies, price changes, impact

**Files:**
- Create: `zerocrm/pricing.py`
- Test: `tests/test_pricing.py`

**Interfaces:**
- Produces:
  - `add_vendor(engine, *, name, actor, workspace_id="default") -> str`
  - `add_supply(engine, *, name, vendor_id=None, sku=None, unit=None, current_cost_usd=None, actor, workspace_id="default") -> str`
  - `record_price_change(engine, supply_id, new_cost_usd, *, effective_on=None, source=None, actor, workspace_id="default") -> str` — one `price_event` + supply cost update, atomic; returns event id.
  - `price_impacts(engine, supply_id) -> list[dict]` — active contracts referencing the supply, `{contract_id, line_id, description, qty, old_cost, new_cost, delta}` (old/new from the latest price_event).
  - `flag_price_impacts(engine, supply_id) -> int` — one `add_notice` per affected contract; returns count flagged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pricing.py
from sqlalchemy import insert, select
from zerocrm import contracts, pricing
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company, location, price_event, supply
from zerocrm.warmup import pop_notices

A = {"kind": "human", "id": "sid"}

def _engine():
    e = make_engine("sqlite://"); migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="co1", name="Acme"))
        c.execute(insert(location).values(id="loc1", company_id="co1", name="HQ"))
    return e

def test_record_price_change_writes_event_and_bumps_cost():
    e = _engine()
    sid = pricing.add_supply(e, name="wax", current_cost_usd=10.0, actor=A)
    pricing.record_price_change(e, sid, 12.0, actor=A)
    with e.connect() as c:
        assert c.execute(select(supply.c.current_cost_usd).where(supply.c.id == sid)).scalar() == 12.0
        ev = c.execute(select(price_event).where(price_event.c.supply_id == sid)).mappings().first()
    assert ev["old_cost_usd"] == 10.0 and ev["new_cost_usd"] == 12.0

def test_price_impacts_lists_active_contracts_only():
    e = _engine()
    sid = pricing.add_supply(e, name="wax", current_cost_usd=10.0, actor=A)
    active = contracts.create_contract(e, company_id="co1", location_id="loc1", actor=A)
    ended = contracts.create_contract(e, company_id="co1", location_id="loc1", actor=A)
    contracts.set_field(e, ended, "status", "ended", actor=A)
    for cid in (active, ended):
        lid = contracts.add_line(e, cid, kind="material", description="floor wax", actor=A)
        with e.begin() as c:
            from zerocrm.schema import contract_line
            c.execute(contract_line.update().where(contract_line.c.id == lid).values(supply_id=sid))
    pricing.record_price_change(e, sid, 12.0, actor=A)
    impacts = pricing.price_impacts(e, sid)
    assert {i["contract_id"] for i in impacts} == {active}
    assert impacts[0]["delta"] == 2.0

def test_flag_price_impacts_adds_notices():
    e = _engine()
    sid = pricing.add_supply(e, name="wax", current_cost_usd=10.0, actor=A)
    cid = contracts.create_contract(e, company_id="co1", location_id="loc1", actor=A)
    lid = contracts.add_line(e, cid, kind="material", actor=A)
    from zerocrm.schema import contract_line
    with e.begin() as c:
        c.execute(contract_line.update().where(contract_line.c.id == lid).values(supply_id=sid))
    pricing.record_price_change(e, sid, 12.0, actor=A)
    assert pricing.flag_price_impacts(e, sid) == 1
    assert any("wax" in n or cid in n for n in pop_notices(e))
```

- [ ] **Step 2: Run test to verify it fails.**

- [ ] **Step 3: Write minimal implementation**

```python
# zerocrm/pricing.py
"""Master supply list + append-only vendor price-change history. A price change
writes a price_event and updates the supply cost in ONE txn (they can't disagree).
price_impacts finds ACTIVE contracts using a supply; flag_price_impacts surfaces
the delta as a digest notice — zerocrm flags it, the operator updates their own
invoice (no invoice entity here)."""
from __future__ import annotations

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from .schema import contract, contract_line, price_event, supply, vendor
from .warmup import add_notice

_A = {"kind": "agent", "id": "pricing"}


def add_vendor(engine: Engine, *, name, actor=_A, workspace_id="default") -> str:
    with engine.begin() as conn:
        return conn.execute(insert(vendor).values(
            name=name, actor=actor, workspace_id=workspace_id)).inserted_primary_key[0]


def add_supply(engine: Engine, *, name, vendor_id=None, sku=None, unit=None,
               current_cost_usd=None, actor=_A, workspace_id="default") -> str:
    with engine.begin() as conn:
        return conn.execute(insert(supply).values(
            name=name, vendor_id=vendor_id, sku=sku, unit=unit,
            current_cost_usd=current_cost_usd, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]


def record_price_change(engine: Engine, supply_id: str, new_cost_usd: float, *,
                        effective_on=None, source=None, actor=_A, workspace_id="default") -> str:
    with engine.begin() as conn:
        old = conn.execute(select(supply.c.current_cost_usd)
                           .where(supply.c.id == supply_id)).scalar()
        eid = conn.execute(insert(price_event).values(
            supply_id=supply_id, old_cost_usd=old, new_cost_usd=new_cost_usd,
            effective_on=effective_on, source=source, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        conn.execute(supply.update().where(supply.c.id == supply_id)
                     .values(current_cost_usd=new_cost_usd))
    return eid


def _latest_event(conn, supply_id):
    return conn.execute(select(price_event)
                        .where(price_event.c.supply_id == supply_id)
                        .order_by(price_event.c.ts.desc())).mappings().first()


def price_impacts(engine: Engine, supply_id: str) -> list[dict]:
    with engine.connect() as conn:
        ev = _latest_event(conn, supply_id)
        if not ev:
            return []
        rows = conn.execute(
            select(contract_line.c.id, contract_line.c.contract_id,
                   contract_line.c.description, contract_line.c.qty)
            .join(contract, contract.c.id == contract_line.c.contract_id)
            .where(contract_line.c.supply_id == supply_id,
                   contract.c.status == "active")).mappings().all()
    return [dict(contract_id=r["contract_id"], line_id=r["id"],
                 description=r["description"], qty=r["qty"],
                 old_cost=ev["old_cost_usd"], new_cost=ev["new_cost_usd"],
                 delta=(ev["new_cost_usd"] or 0) - (ev["old_cost_usd"] or 0))
            for r in rows]


def flag_price_impacts(engine: Engine, supply_id: str) -> int:
    impacts = price_impacts(engine, supply_id)
    with engine.connect() as conn:
        name = conn.execute(select(supply.c.name).where(supply.c.id == supply_id)).scalar()
    for i in impacts:
        add_notice(engine, f"price: {name} {i['old_cost']}->{i['new_cost']} "
                           f"(+{i['delta']}) affects contract {i['contract_id']} "
                           f"line '{i['description']}' — update invoice")
    return len(impacts)
```

- [ ] **Step 4: Run tests** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add zerocrm/pricing.py tests/test_pricing.py
git commit -m "feat(pricing): supplies + append-only price events + active-contract impact notices"
```

---

### Task 3: `quotes.py` — build a quote from supplies + margin

**Files:**
- Create: `zerocrm/quotes.py`
- Test: `tests/test_quotes.py`

**Interfaces:**
- Produces:
  - `create_quote(engine, *, company_id=None, contract_id=None, deal_id=None, margin_pct=0.0, actor, workspace_id="default") -> str` — requires at least one of company/contract/deal (ValueError otherwise).
  - `add_quote_line(engine, quote_id, *, supply_id=None, description=None, qty=1, unit_cost_usd=None, actor, workspace_id="default") -> str` — if `supply_id`, snapshot `unit_cost_usd` from `supply.current_cost_usd`; `unit_price_usd = unit_cost x (1 + quote.margin_pct)`, `line_total_usd = unit_price x qty`; then recompute + store the quote `total_usd`.
  - `quote_total(engine, quote_id) -> float` — sum of `line_total_usd`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_quotes.py
import pytest
from sqlalchemy import insert, select
from zerocrm import pricing, quotes
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company, quote as quote_t

A = {"kind": "human", "id": "sid"}

def _engine():
    e = make_engine("sqlite://"); migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="co1", name="Acme"))
    return e

def test_line_from_supply_snapshots_cost_and_applies_margin():
    e = _engine()
    sid = pricing.add_supply(e, name="wax", current_cost_usd=10.0, actor=A)
    qid = quotes.create_quote(e, company_id="co1", margin_pct=0.5, actor=A)
    quotes.add_quote_line(e, qid, supply_id=sid, qty=2, actor=A)
    with e.connect() as c:
        total = c.execute(select(quote_t.c.total_usd).where(quote_t.c.id == qid)).scalar()
    # cost 10 * (1+0.5) = 15 unit price; * qty 2 = 30
    assert quotes.quote_total(e, qid) == 30.0 and total == 30.0

def test_custom_line_uses_passed_cost():
    e = _engine()
    qid = quotes.create_quote(e, company_id="co1", margin_pct=0.0, actor=A)
    quotes.add_quote_line(e, qid, description="travel", qty=1, unit_cost_usd=25.0, actor=A)
    assert quotes.quote_total(e, qid) == 25.0

def test_create_requires_a_parent():
    e = _engine()
    with pytest.raises(ValueError):
        quotes.create_quote(e, margin_pct=0.1, actor=A)
```

- [ ] **Step 2: Run test to verify it fails.**

- [ ] **Step 3: Write minimal implementation**

```python
# zerocrm/quotes.py
"""Basic CPQ: assemble a customer quote from supplies + a header margin. Unit cost
is snapshotted at line time (a later price change doesn't silently move a sent
quote). unit_price = unit_cost x (1 + margin); the header total_usd is the sum of
line totals, recomputed on every line add."""
from __future__ import annotations

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine

from .schema import quote, quote_line, supply


def create_quote(engine: Engine, *, company_id=None, contract_id=None, deal_id=None,
                 margin_pct: float = 0.0, actor, workspace_id="default") -> str:
    if not (company_id or contract_id or deal_id):
        raise ValueError("a quote needs a company, contract, or deal")
    with engine.begin() as conn:
        return conn.execute(insert(quote).values(
            company_id=company_id, contract_id=contract_id, deal_id=deal_id,
            margin_pct=margin_pct, total_usd=0.0, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]


def add_quote_line(engine: Engine, quote_id: str, *, supply_id=None, description=None,
                   qty: float = 1, unit_cost_usd=None, actor, workspace_id="default") -> str:
    with engine.begin() as conn:
        margin = conn.execute(select(quote.c.margin_pct)
                              .where(quote.c.id == quote_id)).scalar() or 0.0
        if supply_id and unit_cost_usd is None:
            unit_cost_usd = conn.execute(select(supply.c.current_cost_usd)
                                         .where(supply.c.id == supply_id)).scalar()
        cost = unit_cost_usd or 0.0
        unit_price = cost * (1 + margin)
        line_total = unit_price * qty
        lid = conn.execute(insert(quote_line).values(
            quote_id=quote_id, supply_id=supply_id, description=description, qty=qty,
            unit_cost_usd=unit_cost_usd, unit_price_usd=unit_price,
            line_total_usd=line_total, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        total = conn.execute(select(func.coalesce(func.sum(quote_line.c.line_total_usd), 0.0))
                             .where(quote_line.c.quote_id == quote_id)).scalar()
        conn.execute(quote.update().where(quote.c.id == quote_id).values(total_usd=total))
    return lid


def quote_total(engine: Engine, quote_id: str) -> float:
    with engine.connect() as conn:
        return conn.execute(select(func.coalesce(func.sum(quote_line.c.line_total_usd), 0.0))
                            .where(quote_line.c.quote_id == quote_id)).scalar()
```

- [ ] **Step 4: Run tests** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add zerocrm/quotes.py tests/test_quotes.py
git commit -m "feat(quotes): basic CPQ — quote from supplies + header margin, snapshot cost"
```

---

### Task 4: CLI verbs + full-slice run

**Files:**
- Modify: `zerocrm/cli.py`
- Test: covered by module tests; this wires the operator surface.

**Interfaces:**
- Produces: `add-vendor`, `add-supply`, `price-change` (records + flags impacts), `new-quote`, `quote-line`.

- [ ] **Step 1: Add commands** (mirror the slice-2 verbs; `--dsn` top-level). Example:

```python
def _cmd_price_change(args):
    from .pricing import record_price_change, flag_price_impacts
    record_price_change(make_engine(args.dsn), args.supply, args.cost,
                        actor={"kind": "human", "id": "cli"})
    n = flag_price_impacts(make_engine(args.dsn), args.supply)
    print(f"price recorded; {n} contract(s) flagged"); return 0

def _cmd_add_supply(args):
    from .pricing import add_supply
    print(add_supply(make_engine(args.dsn), name=args.name, vendor_id=args.vendor,
                     current_cost_usd=args.cost, actor={"kind": "human", "id": "cli"}))
    return 0
```

Register (in `main`), e.g.:

```python
    pc = sub.add_parser("price-change", help="record a vendor price change + flag impacts")
    pc.add_argument("--supply", required=True)
    pc.add_argument("--cost", type=float, required=True)
    pc.set_defaults(func=_cmd_price_change)
```

Add `add-vendor`, `add-supply` (`--name`, `--vendor`, `--cost`), `new-quote`
(`--company`/`--contract`/`--deal`/`--margin`), `quote-line`
(`--quote`, `--supply`, `--description`, `--qty`, `--cost`) the same way.

- [ ] **Step 2: Smoke-test**

```bash
uv run python -c "
from zerocrm.cli import main
import tempfile, os
db=tempfile.mktemp(suffix='.db'); dsn=f'sqlite:///{db}'
main(['--dsn', dsn, 'migrate'])
sid_out = main(['--dsn', dsn, 'add-supply', '--name', 'wax', '--cost', '10'])
os.remove(db); print('CLI OK')"
```

- [ ] **Step 3: Full-slice + full-suite run + commit**

```bash
uv run pytest tests/test_migrate_cpq.py tests/test_pricing.py tests/test_quotes.py -v
uv run pytest -q   # whole suite stays green (schema + migrate touched shared code)
git add zerocrm/cli.py
git commit -m "feat(cli): CPQ verbs (vendor, supply, price-change, quote, quote-line)"
```

---

## Deferred to a follow-up (not this plan)

- **Digest render of price notices** — `flag_price_impacts` uses `add_notice`, which the digest already surfaces; a richer per-contract card is a thin add.
- **Per-line margin override** — one header margin now; add a `quote_line.margin_pct` only when a customer needs mixed margins.
- **Quote → document generation** — render a quote as a `document` (slice 2 `generate_document`) so it is deliverable/auditable; a small bridge, not built here.
- **Paused-contract impacts** — `price_impacts` is active-only; surface paused on request.

## Self-review notes

- Spec coverage: supply list + vendors (T2), price-change history (T2), price-impact visibility via digest notice (T2), basic CPQ from supplies + margin (T3), `contract_line.supply_id` link (T1), operator surface (T4). All spec sections map to a task.
- Types consistent: creators return `str` ids; `price_impacts` returns `list[dict]`; `flag_price_impacts`/`quote_total` return `int`/`float`. `record_price_change` old/new come from the supply + arg, event stores both.
- Migration risk flagged + handled in-task: the additive column needs a guarded raw ALTER (soft ref, no FK) — the ONE non-`Table.create` migration step in the repo; idempotent via the inspector guard.
- Snapshot-at-line-time is deliberate (T3): a later price change must not silently move an already-sent quote — asserted by `test_line_from_supply_snapshots_cost`.
