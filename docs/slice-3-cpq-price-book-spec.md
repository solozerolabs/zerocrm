# Slice 3 — CPQ / price book (spec)

Status: pre-implementation. Roadmap context: `docs/lifecycle-roadmap.md`.
Depends on Slice 2 (`contract`/`contract_line`). Surfaces stay UI-less.

## Why this slice exists

The buyer's inventory/pricing ask: "we buy supplies from vendors against a master
supply list, and we need visibility into vendor price changes so we can reflect
them on customer invoices. Basic CPQ would cover this." Two jobs:
1. A **master supply list** with per-vendor cost, and **visibility when a vendor
   price changes** — so the operator can update the customer's price.
2. **Basic CPQ** — assemble a customer quote from supplies + a margin.

## What this slice delivers

1. **Vendors + master supply list** (`vendor`, `supply`) — cost per supply.
2. **Price-change history** (`price_event`) — every vendor cost change is recorded
   (append-only, same discipline as `contract_event`) and updates the supply's
   current cost atomically.
3. **Price-impact visibility** — when a supply's cost changes, surface which
   ACTIVE contracts use it and the per-line delta, as a digest notice. This is the
   "reflect on customer invoices" ask: zerocrm flags the delta; the operator
   updates their invoice (zerocrm is not the invoicing system, so there is no
   invoice entity — YAGNI).
4. **Basic CPQ** (`quote`, `quote_line`) — build a customer quote from supplies +
   a margin; unit cost is snapshotted at quote time, price = cost x (1 + margin).

Out of scope (named so they are not built): an invoice entity / accounting
integration (zerocrm surfaces deltas, does not invoice); a configurator UI (agent
assembles quotes, per the no-UI product); marketing (Slice 4).

## The Slice-2 link

`contract_line` gains a soft `supply_id` reference so a materials/supply line can
point at a `supply`. Price-impact detection joins `price_event.supply_id →
contract_line.supply_id → contract` (active only). This is the "Slice 3 wires
`contract_line.unit_cost` to a vendor supply" seam the roadmap named.

## Architecture — data + a digest notice, no new runtime

CRUD + one derived query + one digest surfacing. No poller, no new clock.

```
record price change   → pricing.record_price_change
   write price_event {old, new} + update supply.current_cost  (one txn)
   → price_impacts(supply_id): active contracts whose lines use it + per-line delta
   → flag_price_impacts: add_notice(...) so the next digest shows the deltas

build a quote         → quotes.create_quote(margin) + add_quote_line(supply|custom)
   line: unit_cost snapshot (from supply.current_cost if supply_id) ,
         unit_price = unit_cost x (1 + margin), line_total = unit_price x qty
   → quote_total recomputes the header total
```

## Data model (migration 005)

New tables (all carry `_pk()` + `_common()`):

```
vendor      id, name, contact (Json), meta (Json)

supply      id, vendor_id→vendor, sku, name, unit, current_cost_usd
            — the master supply list; current_cost_usd is the latest cost.

price_event id, supply_id→supply, old_cost_usd, new_cost_usd,
            effective_on (Text ISO date), source, ts
            — APPEND-ONLY vendor price-change history (mirrors contract_event).

quote       id, company_id→company, contract_id→contract (nullable),
            deal_id→deal (nullable), status (draft|sent|accepted|rejected),
            margin_pct (Float), total_usd (Float)

quote_line  id, quote_id→quote, supply_id→supply (nullable — custom lines allowed),
            description, qty (Float), unit_cost_usd, unit_price_usd, line_total_usd
```

Additive column (migration 005, guarded ALTER):

```
contract_line.supply_id  (String(32), SOFT reference — no DB FK, so the ALTER is
                          cross-backend simple; app-enforced. Fresh DBs get it in
                          the 004 create; existing-at-004 DBs get the ALTER.)
```

Migration: `005_cpq` creates the 5 tables (`Table.create(checkfirst=True)`) and
adds `contract_line.supply_id` via an inspector-guarded `ALTER TABLE ... ADD
COLUMN` (skipped when the column already exists — SQLite can't add a column with a
FK, hence the soft reference).

## Price change + impact detail

```
record_price_change(engine, supply_id, new_cost_usd, *, effective_on=None,
                    source=None, actor) -> str:
  with engine.begin() as conn:
    old = supply.current_cost_usd
    insert price_event {old_cost_usd: old, new_cost_usd: new, effective_on, source}
    update supply.current_cost_usd = new
  # both in one txn — the event can never disagree with the supply's cost
```

```
price_impacts(engine, supply_id) -> list[dict]:
  # active contracts whose lines reference this supply, with the delta
  join contract_line (supply_id) -> contract (status='active')
  return [{contract_id, line_id, description, qty, old_cost, new_cost, delta}]
```

`flag_price_impacts(engine, supply_id)` composes a one-line notice per affected
contract and calls the existing `warmup.add_notice` so the next digest headlines
it. No invoice is written — the operator updates their own invoicing system.

## CPQ detail

- `create_quote(engine, *, company_id=None, contract_id=None, deal_id=None,
  margin_pct=0.0, actor) -> str`.
- `add_quote_line(engine, quote_id, *, supply_id=None, description=None, qty=1,
  unit_cost_usd=None, actor) -> str` — if `supply_id`, snapshot `unit_cost_usd`
  from `supply.current_cost_usd`; compute `unit_price_usd = unit_cost x (1 +
  quote.margin_pct)` and `line_total_usd = unit_price x qty`; then recompute and
  store the quote header `total_usd`.
- `quote_total(engine, quote_id) -> float` — sum of line totals (source of truth
  for the header).

Margin lives on the quote header (one margin per quote — the common case); a
per-line margin override is a documented upgrade, not built (YAGNI).

## Trust & safety

- CRUD is CLI/agent-driven; when surfaced through Slack, rides the slice-1
  operator allowlist.
- Price impacts are a NOTICE, not an outbound action — nothing sends; the operator
  acts on their own invoicing system.
- No new secrets, no external calls this slice.

## Modules (new)

| file | purpose |
|------|---------|
| `zerocrm/pricing.py` | vendor/supply CRUD; `record_price_change` (event + cost update); `price_impacts`; `flag_price_impacts` |
| `zerocrm/quotes.py` | `create_quote`, `add_quote_line` (snapshot + margin math), `quote_total` |

Touched: `schema.py` (5 tables + `contract_line.supply_id`), `migrate.py`
(`005_cpq` incl guarded ALTER), `cli.py` (vendor/supply/price/quote verbs).

## Testing (one test file per module, sqlite in-memory)

- **migration**: `005` creates the 5 tables AND `contract_line` has a `supply_id`
  column; idempotent (re-run adds nothing, ALTER is skipped).
- **pricing**: `record_price_change` writes one `price_event` with the old cost and
  bumps `supply.current_cost_usd`; a mid-txn failure leaves neither changed
  (rollback). `price_impacts` returns only ACTIVE contracts referencing the supply,
  with the correct delta; an ended contract is excluded.
- **quotes**: `add_quote_line` from a supply snapshots the current cost and applies
  the margin (cost 10, margin 0.5 → unit_price 15); `quote_total` equals the sum of
  line totals; a custom line (no supply_id) uses the passed `unit_cost_usd`.

## YAGNI / ponytail calls (explicit)

- No invoice entity — zerocrm surfaces deltas; it is not the invoicing system.
- `contract_line.supply_id` is a SOFT reference (no DB FK) so the ALTER is
  cross-backend trivial — app-enforced, like other cross-cutting soft links.
- Price-change history = append-only `price_event`, same shape as `contract_event`.
- One margin per quote (header), no per-line override until asked.
- No configurator UI — the agent assembles quotes (product is no-UI).
- Impact surfacing reuses `add_notice` + the digest — no new alerting path.

## Open questions (resolve at implementation)

- Whether `price_impacts` should also flag NON-active (paused) contracts (leaning:
  active only for the digest headline; paused surfaced on request).
- Whether a quote can exist with neither company/contract/deal (leaning: require at
  least one, app-side, like the document parent check).
