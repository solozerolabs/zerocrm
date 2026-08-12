# Slice 4 — Marketing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make marketing-to-customers a capability: assemble a customer audience and enrol it into a `nurture` campaign, reusing the existing outbound spine. No new tables, no new sends.

**Architecture:** One module, `marketing.py`, over `campaign`/`list`/`list_member`/`flow_state`/`person`/`deal`/`contract`. Enrolment writes `flow_state` as `status='scheduled'` — the digest approval + `sending_enabled` gate still own every touch.

**Tech Stack:** Python 3, SQLAlchemy Core, stdlib, pytest sqlite in-memory.

## Global Constraints

- No new tables, no migration. `flow_kind='nurture'` and `list.purpose='audience'` are existing free-text values.
- Enrol as `status='scheduled'`, never `active` — the send loop and daily digest already gate sending; scheduled also respects the one-active-flow-per-(person,channel) partial index.
- Customer = person with a WON deal OR at a company with an ACTIVE contract. Cold prospects excluded.
- `.inserted_primary_key[0]` for ids; one test file. Tests create real parent rows (FK on).

---

### Task 1: `marketing.py` — audience, list, campaign, enrol

**Files:**
- Create: `zerocrm/marketing.py`
- Test: `tests/test_marketing.py`

**Interfaces:**
- Produces:
  - `customer_audience(engine, workspace_id="default") -> list[str]` — deduped person ids (won-deal OR active-contract-company).
  - `build_customer_list(engine, *, name, actor, workspace_id="default") -> str` — creates a `list(purpose='audience')` + one `list_member` per audience person; returns list id.
  - `create_nurture_campaign(engine, *, name, channel="email", actor, workspace_id="default") -> str` — `campaign(flow_kind='nurture', status='active')`.
  - `enrol_list(engine, list_id, campaign_id, *, actor, workspace_id="default") -> int` — one `flow_state(status='scheduled')` per member; idempotent on (person, campaign); returns count enrolled.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_marketing.py
from sqlalchemy import func, insert, select
from zerocrm import contracts, marketing
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company, deal, flow_state, list_member, location, person

A = {"kind": "human", "id": "sid"}

def _engine():
    e = make_engine("sqlite://"); migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="cust", name="Customer Co"))
        c.execute(insert(company).values(id="cold", name="Cold Co"))
        c.execute(insert(location).values(id="loc", company_id="cust", name="HQ"))
        c.execute(insert(person).values(id="p_won", full_name="Won Lead"))
        c.execute(insert(person).values(id="p_contract", full_name="Contract Contact", company_id="cust"))
        c.execute(insert(person).values(id="p_cold", full_name="Cold Prospect", company_id="cold"))
        c.execute(insert(deal).values(id="d_won", person_id="p_won", stage="won"))
    contracts.create_contract(e, company_id="cust", location_id="loc", actor=A)  # active
    return e

def test_audience_is_customers_only():
    e = _engine()
    aud = set(marketing.customer_audience(e))
    assert aud == {"p_won", "p_contract"}  # cold prospect excluded

def test_build_list_creates_one_member_per_person():
    e = _engine()
    lid = marketing.build_customer_list(e, name="customers", actor=A)
    with e.connect() as c:
        n = c.execute(select(func.count()).select_from(list_member)
                      .where(list_member.c.list_id == lid)).scalar()
    assert n == 2

def test_enrol_is_scheduled_and_idempotent():
    e = _engine()
    lid = marketing.build_customer_list(e, name="customers", actor=A)
    cid = marketing.create_nurture_campaign(e, name="q3-nurture", actor=A)
    assert marketing.enrol_list(e, lid, cid, actor=A) == 2
    assert marketing.enrol_list(e, lid, cid, actor=A) == 0  # idempotent
    with e.connect() as c:
        rows = c.execute(select(flow_state).where(flow_state.c.campaign_id == cid)).mappings().all()
    assert len(rows) == 2 and all(r["status"] == "scheduled" for r in rows)
```

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/test_marketing.py -v`.

- [ ] **Step 3: Write minimal implementation**

```python
# zerocrm/marketing.py
"""Marketing = the existing outbound spine, turned on for the customer base. No new
tables, no new sends: an audience is built from customers (won deals + active
contracts) and enrolled into a nurture campaign as `scheduled`; the digest approval
and sending_enabled gate still own every touch."""
from __future__ import annotations

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from .schema import (campaign, contract, deal, flow_state, list_ as list_t,
                     list_member, person)


def customer_audience(engine: Engine, workspace_id: str = "default") -> list[str]:
    with engine.connect() as conn:
        won = select(deal.c.person_id).where(deal.c.stage == "won",
                                             deal.c.person_id.isnot(None))
        active_companies = select(contract.c.company_id).where(
            contract.c.status == "active", contract.c.company_id.isnot(None))
        by_contract = select(person.c.id).where(person.c.company_id.in_(active_companies))
        ids = {r[0] for r in conn.execute(won)} | {r[0] for r in conn.execute(by_contract)}
    return sorted(i for i in ids if i)


def build_customer_list(engine: Engine, *, name, actor, workspace_id: str = "default") -> str:
    people = customer_audience(engine, workspace_id)
    with engine.begin() as conn:
        lid = conn.execute(insert(list_t).values(
            name=name, purpose="audience", actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        for pid in people:
            conn.execute(insert(list_member).values(
                list_id=lid, person_id=pid, actor=actor, workspace_id=workspace_id))
    return lid


def create_nurture_campaign(engine: Engine, *, name, channel: str = "email", actor,
                            workspace_id: str = "default") -> str:
    with engine.begin() as conn:
        return conn.execute(insert(campaign).values(
            name=name, channel=channel, flow_kind="nurture", status="active",
            actor=actor, workspace_id=workspace_id)).inserted_primary_key[0]


def enrol_list(engine: Engine, list_id: str, campaign_id: str, *, actor,
               workspace_id: str = "default") -> int:
    with engine.begin() as conn:
        channel = conn.execute(select(campaign.c.channel)
                               .where(campaign.c.id == campaign_id)).scalar() or "email"
        members = [r[0] for r in conn.execute(
            select(list_member.c.person_id).where(list_member.c.list_id == list_id))]
        enrolled = 0
        for pid in members:
            exists = conn.execute(select(flow_state.c.id).where(
                flow_state.c.person_id == pid,
                flow_state.c.campaign_id == campaign_id)).first()
            if exists:
                continue  # idempotent on (person, campaign)
            conn.execute(insert(flow_state).values(
                person_id=pid, campaign_id=campaign_id, channel=channel, step=0,
                status="scheduled", actor=actor, workspace_id=workspace_id))
            enrolled += 1
    return enrolled
```

- [ ] **Step 4: Run tests** — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add zerocrm/marketing.py tests/test_marketing.py
git commit -m "feat(marketing): customer audience + nurture enrolment over the existing spine"
```

---

### Task 2: CLI `nurture` verb + full suite

**Files:**
- Modify: `zerocrm/cli.py`

**Interfaces:**
- Produces: `nurture --name <campaign>` — builds the customer list, creates a nurture campaign, enrols, prints the counts. One operator command for the whole motion.

- [ ] **Step 1: Add the command**

```python
def _cmd_nurture(args):
    from .marketing import build_customer_list, create_nurture_campaign, enrol_list
    engine = make_engine(args.dsn)
    lid = build_customer_list(engine, name=f"{args.name}-audience", actor=_CLI_ACTOR)
    cid = create_nurture_campaign(engine, name=args.name, channel=args.channel, actor=_CLI_ACTOR)
    n = enrol_list(engine, lid, cid, actor=_CLI_ACTOR)
    print(f"nurture '{args.name}': {n} customer(s) enrolled (scheduled)"); return 0
```

Register:

```python
    nu = sub.add_parser("nurture", help="enrol the customer base into a nurture campaign")
    nu.add_argument("--name", required=True)
    nu.add_argument("--channel", default="email")
    nu.set_defaults(func=_cmd_nurture)
```

- [ ] **Step 2: Smoke-test + full suite + commit**

```bash
uv run python -c "
from zerocrm.cli import main
import tempfile, os
db=tempfile.mktemp(suffix='.db'); dsn=f'sqlite:///{db}'
main(['--dsn', dsn, 'migrate']); main(['--dsn', dsn, 'nurture', '--name', 'q3'])
os.remove(db); print('CLI OK')"
uv run pytest -q
git add zerocrm/cli.py
git commit -m "feat(cli): nurture verb — enrol the customer base in one command"
```

---

## Deferred to a follow-up (not this plan)

- **Nurture copy templates** — content, authored when a real campaign runs (reuses the existing copy seam).
- **`audience_sync` to ad platforms** — the table exists; wiring a Meta/Google audience push is a deferred driver, like the other providers.
- **warm/retarget kinds** — a one-line addition to `create_nurture_campaign` when needed.

## Self-review notes

- Spec coverage: customer audience (T1), list build (T1), nurture campaign (T1), scheduled enrolment (T1), operator surface (T2). All map to a task.
- Types consistent: `customer_audience -> list[str]`, builders return `str` ids, `enrol_list -> int`.
- Reuse-only: no new tables, no migration — confirmed the touched tables (`campaign`, `list`, `list_member`, `flow_state`) all exist from `001`.
- Idempotency asserted (T1): re-enrol returns 0; scheduled (not active) respects the one-active-flow index.
