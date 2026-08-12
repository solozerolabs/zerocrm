# Slice 2 — Post-sale system-of-record (spec)

Status: pre-implementation. Roadmap context: `docs/lifecycle-roadmap.md`.
Depends on Slice 1 (`memory_chunk` retrieval spine). Surfaces stay UI-less.

## Why this slice exists

This is the buyer's actual stuck point — the r/CRM thread that seeded the
re-scope. Close already wins their sales side; the post-sale mess is what breaks
Excel: service contracts tied to a customer location, living documents (staff
swaps, one-off visits, added materials), a full change audit, uploaded
confirmation docs, and periodic quality inspections tracked back to the location.

## What this slice delivers

1. **Location** as a first-class entity — contracts and inspections hang off a
   customer *location*, not the company.
2. **Living contracts** — scope, contract type, assigned staff, line items
   (one-off visits, materials). Every mutation is an audited edit.
3. **Full change audit** — an append-only `contract_event` log renders the whole
   history of a contract in one query. This is the buyer's #1 ask.
4. **Docs-on-record** — confirmation files attached to a contract (distinct from
   docs-as-context; a text doc MAY also chunk into `memory_chunk` for retrieval).
5. **Document generation** — contracts/paperwork rendered from a template + the
   contract record.
6. **Inspections** — periodic quality checks tied to a location, with outcome.

Out of scope (named so they are not built here): vendor price book / price-change
→ invoice deltas → **Slice 3 (CPQ)**; marketing → Slice 4; autonomous calling →
mode A. Blob-store *upload wiring* (how a file reaches object storage) is a
deferred live-verify task, same staging as Slice 1's Google/Slack driver classes
— the schema, audit, generation, and retrieval hookup are what this slice builds.

## Architecture — data + audit, no new runtime

No poller, no new clock. These are operator/agent-driven CRUD operations exposed
through the CLI and (thin follow-on) Slack intents. The one non-obvious design
choice is the audit mechanism:

**Audit is an append-only event log, NOT the person precedence gate.** `upsert.py`
runs per-field precedence because a person's facts are written by *competing
enrichment providers* and truth must not be clobbered. A contract has no
competing writers — a human or the agent edits it. So the right audit is a
`contract_event` row per mutation (who, when, what changed), written in the SAME
transaction as the mutation. One query renders the living document's history.
Reusing the precedence gate here would be applying a pattern where its problem
does not exist.

```
create/mutate contract  → contracts.py
   every mutation writes contract_event {kind, before, after, actor} in-txn
   (staff swap, one-off visit, material add, scope edit, status change)

attach document         → documents.py
   record in `document`; if text, also chunk into memory_chunk (slice-1 spine)

generate document       → documents.py
   template + contract record → rendered text → `document` (generated=true)

record inspection       → inspections.py  (FK location; outcome + score)

"show Acme's contract history"  → one SELECT over contract_event
"report contracts by location"  → SQL join, answered in Slack (CC-3, no dashboards)
```

## Data model (migration 004)

New entities. Every table carries `_common()` (`workspace_id`/`actor`/timestamps)
and a `_pk()` uuid, same as slice 1.

```
location    id, company_id→company, name, address (Json), meta (Json)
            — contracts + inspections hang off this, not company.

staff       id, full_name, email, role, active (bool)
            — the customer's OWN employees assigned to service work. A SEPARATE
              table from `person`: employees are a different population than
              prospects/customers; overloading `person` would muddy every
              pipeline query. (Roadmap open question resolved: own table.)

contract    id, company_id→company, location_id→location,
            contract_type, scope (Text), status (active|paused|ended),
            value_usd, starts_on, ends_on
            — the living record. Field edits + collection changes are all audited.

contract_staff  id, contract_id→contract, staff_id→staff, role,
                assigned_at, removed_at (nullable — a swap sets removed_at on the
                old row and inserts a new one; history is preserved, never deleted)

contract_line   id, contract_id→contract, kind (one_off_visit|material|supply),
                description, qty, unit_cost_usd, occurred_on
                — one-off visits and added materials. unit_cost_usd is a captured
                  snapshot NOW; Slice 3 links it to a vendor supply + price_event.

document    id, contract_id→contract (nullable), location_id→location (nullable),
            filename, uri, doc_type, generated (bool), chunked (bool)
            — docs-on-RECORD (the record of truth). `uri` points at storage; a
              text doc may ALSO feed memory_chunk (chunked=true) for retrieval.

inspection  id, location_id→location, contract_id→contract (nullable),
            scheduled_for, performed_at, outcome (pass|fail|partial),
            score (Integer), notes (Text)

contract_event  id, contract_id→contract, kind, payload (Json: {before, after}
                or {field, value}), actor, ts
                — APPEND-ONLY audit trail. One query = the whole living-document
                  history. kinds: created | scope_changed | type_changed |
                  status_changed | staff_added | staff_removed | staff_swapped |
                  line_added | line_removed | doc_attached | doc_generated
```

Reuse: `memory_chunk` (slice 1) for optional document retrieval; `company` as the
contract's account. No changes to slice-1 tables.

Migration: additive callable `004_postsale` in `migrate.py`, `Table.create(engine,
checkfirst=True)` for each, following the `003_conversation` pattern.

## Audit detail

`contracts.py` exposes one mutation surface, and **every mutation writes exactly
one `contract_event` in the same transaction as the change** — atomic, so an
audit row can never diverge from the state it describes. Example:

```
swap_staff(engine, contract_id, out_staff_id, in_staff_id, actor):
  with engine.begin() as conn:
    conn.execute(update(contract_staff)
        .where(contract_id, staff_id==out, removed_at.is_(None))
        .values(removed_at=now))
    conn.execute(insert(contract_staff).values(contract_id, staff_id=in, ...))
    conn.execute(insert(contract_event).values(
        contract_id, kind="staff_swapped",
        payload={"out": out_staff_id, "in": in_staff_id}, actor=actor))
```

`contract_history(engine, contract_id) -> list[dict]` = `SELECT * FROM
contract_event WHERE contract_id=? ORDER BY ts`. That render IS the audit trail
the buyer asked for; test it against a staff swap + a one-off visit (the exact
scenario from the thread).

## Documents

- **Attach:** `attach_document(engine, *, contract_id|location_id, filename, uri,
  doc_type, text=None)` — writes the `document` record; if `text` is given, calls
  slice-1 `memory.ingest_document(source_id=document.id, ...)` and sets
  `chunked=true`. The blob upload (getting the file to `uri`) is caller-supplied;
  the storage driver is deferred.
- **Generate:** `generate_document(engine, *, contract_id, template) -> id` —
  renders a template against the contract record (stdlib `string.Template` /
  simple field substitution) to text/HTML, stores it as `document(generated=true)`,
  writes a `doc_generated` contract_event. PDF rendering is a documented upgrade
  behind this function; do not pull a PDF dependency in this slice.
  `# ponytail: text/HTML template now; PDF lib only when a customer needs it`

## Inspections

`record_inspection(engine, *, location_id, contract_id=None, outcome, score=None,
performed_at=None, notes="")` — one row, FK to location. Scheduling recurrence
(the "periodic" part) is a stored cadence the agent surfaces in the digest, not a
new scheduler — reuse the existing pg_cron/digest clock. Slice 2 stores and
records inspections; the reminder cadence is a thin config-driven digest notice.

## Trust & safety

- Mutations are CLI/agent-driven; when exposed through Slack later, they ride the
  same `slack_operator_ids` allowlist as the slice-1 brief.
- Document text ingested for retrieval is DATA, never instructions (same rule as
  transcripts).
- No outbound/sending in this slice — it is system-of-record only.

## Modules (new)

| file | purpose |
|------|---------|
| `zerocrm/contracts.py` | create + mutate contracts; staff assign/swap; line add/remove; `contract_history`. Every mutation writes a `contract_event`. |
| `zerocrm/documents.py` | `attach_document` (record + optional memory_chunk), `generate_document` (template → record) |
| `zerocrm/inspections.py` | `record_inspection`, cadence-notice helper |

Touched: `schema.py` (8 tables), `migrate.py` (`004_postsale`), `cli.py`
(contract/document/inspection verbs).

## Testing (one test file per module, sqlite in-memory)

- **migration**: `004` creates all 8 tables; idempotent.
- **contracts**: create writes a `created` event; `swap_staff` sets `removed_at`
  on the old row, inserts the new, and writes ONE `staff_swapped` event;
  `add_line` (one-off visit) writes a `line_added` event; `contract_history`
  returns the events in ts order. The staff-swap + one-off-visit run is the
  buyer's scenario — assert the history reads cleanly.
- **documents**: `attach_document` with text also creates `memory_chunk` rows and
  sets `chunked=true`; `attach_document` without text does not; `generate_document`
  stores a `generated=true` record and writes a `doc_generated` event.
- **inspections**: `record_inspection` writes one row FK'd to the location.
- **audit atomicity**: a mutation that raises mid-transaction leaves NO event and
  NO partial state (assert rollback).

## YAGNI / ponytail calls (explicit)

- Audit = append-only event log, NOT the person precedence gate (no competing
  writers on a contract).
- `staff` is its own table, not a `person.segment` overload.
- Document generation is text/HTML from a template — no PDF dependency yet.
- No blob-store abstraction — `document.uri` is a string; the upload driver is
  deferred like slice 1's Google/Slack clients.
- No custom-object registry (CC-1) — a contract's extra fields ride `scope`/Json.
- Reporting stays SQL-over-Slack (CC-3) — no dashboards, no report builder.
- Inspection recurrence reuses the digest clock — no new scheduler.

## Open questions (resolve at implementation)

- Whether `contract_line` unit costs should be nullable until Slice 3 wires the
  vendor supply (leaning: nullable — a one-off visit may have no material cost).
- Whether a document can attach to a `location` with no contract (leaning: yes —
  a site photo predates a contract), hence both FKs nullable with a CHECK that at
  least one is set (app-side, since the schema has no CHECK constraints).
