# Slice 4 — Marketing (spec)

Status: pre-implementation. Roadmap context: `docs/lifecycle-roadmap.md`.
Depends on Slices 2–3 (contracts define who a customer is). Surfaces stay UI-less.

## Why this slice exists

The buyer: "we don't run campaigns today, but I'd like the option to grow into it
on the same platform." So the job is not to run marketing now — it is to make the
capability exist, reusing the outbound engine already built. Their named growth
motions are customer-facing: "selling larger deals to existing customers,
retargeting warm leads for follow-ups." That is nurture/expansion to the customer
base, not new cold acquisition.

## The insight: this is a thin slice, by design

zerocrm's outbound spine is already multi-channel and campaign-driven:
`campaign` (with `flow_kind: cold|warm|retarget|nurture`), `list` + `list_member`,
`flow_state`, `audience_sync`, and the digest approval loop. Marketing to the
customer base is **turning that spine on for a customer audience**, not new
architecture. Slice 4 adds one module that assembles a customer audience and
enrolls it into a nurture campaign — every table it touches already exists.

## What this slice delivers

1. **Customer audience** — the set of people who are existing customers: contacts
   at a company with an ACTIVE contract, plus contacts on a WON deal.
2. **Build a marketing list** from that audience (`list` + `list_member`).
3. **Stand up a nurture campaign** (`campaign`, `flow_kind='nurture'`).
4. **Enrol the list** into the campaign (`flow_state`, `status='scheduled'`), so the
   existing digest/approval + send loop carries it — nothing new sends.

Out of scope (named): new copy generation for nurture (reuses the existing copy
seam / template — a nurture template is content, added when the operator actually
runs one); paid-ads/audience_sync push (the table exists; wiring a provider is a
deferred live-verify like the other drivers); analytics dashboards (no UI, CC-3).

## Architecture — one module over the existing spine, no new runtime

```
customer_audience(engine)      -> people at active-contract companies + won-deal people
build_customer_list(name)      -> list + list_member rows (reuses existing tables)
create_nurture_campaign(name)  -> campaign{flow_kind='nurture', channel}
enrol_list(list_id, campaign)  -> flow_state{status='scheduled'} per member
                                  (scheduled, not active — respects the one-active-
                                   flow-per-(person,channel) index; the send loop
                                   and digest approval already gate the rest)
```

No new tables. No new poller. The `run_tick`/digest machinery already advances and
approves flows; a scheduled nurture enrolment rides it exactly like any campaign.

## Who is a customer (the audience query)

Union, deduped by person id:
- persons with a `deal` where `stage = 'won'`; and
- persons whose `company_id` is a company with a `contract` where
  `status = 'active'`.

This is the "existing customers / expansion" target the buyer named. Cold
prospects (no won deal, no active contract) are excluded — marketing here is to the
base, not acquisition (acquisition is the existing cold spine).

## Data model

None new. Reuses `campaign`, `list`, `list_member`, `flow_state`, `person`, `deal`,
`contract`. `flow_kind='nurture'` and `list.purpose='audience'` are existing
free-text enum values — no migration.

## Trust & safety

- Enrolment writes `flow_state` as `status='scheduled'` — it does NOT send. The
  existing `sending_enabled` gate + daily digest approval still govern every touch.
- No new secrets, no external calls this slice.

## Modules (new)

| file | purpose |
|------|---------|
| `zerocrm/marketing.py` | `customer_audience`, `build_customer_list`, `create_nurture_campaign`, `enrol_list` |

Touched: `cli.py` (a `nurture` verb that runs the four steps end to end).

## Testing (one test file, sqlite in-memory)

- `customer_audience` returns a won-deal person and an active-contract-company
  person; excludes a cold prospect and an ended-contract-only company.
- `build_customer_list` creates a `list` + one `list_member` per audience person
  (deduped).
- `create_nurture_campaign` writes a `campaign` with `flow_kind='nurture'`.
- `enrol_list` writes one `flow_state` per member with `status='scheduled'`;
  re-running does not double-enrol (idempotent on (person, campaign)).

## YAGNI / ponytail calls (explicit)

- No new tables, no new runtime — marketing IS the existing spine, turned on.
- Enrol as `scheduled`, never `active` — the send loop + digest already own sending.
- Nurture COPY (templates) is content, added when a real campaign runs, not now.
- `audience_sync` to ad platforms stays a deferred driver (table exists).
- No analytics UI (CC-3) — campaign performance is a SQL/digest question.

## Open questions (resolve at implementation)

- Whether to also include `warm`/`retarget` flow_kinds in the same module now
  (leaning: nurture only — it's the buyer's stated motion; the others are a
  one-line addition when needed).
- De-dup against a person already in an ACTIVE cold flow on the same channel
  (leaning: enrol anyway as scheduled — the active-flow index only guards `active`,
  and the operator approves before anything sends).
