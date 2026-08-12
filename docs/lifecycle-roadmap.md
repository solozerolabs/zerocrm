# zerocrm — full-lifecycle roadmap

zerocrm started as a cold-outbound engine. This records the re-scope to a
full revenue-lifecycle agent-native CRM (no UI), the slice order, and the
cross-cutting decisions that bind every slice. Pre-production; anything below a
built slice can still change.

## The frame that decides the split

The eventual product runs the whole revenue lifecycle: prospect → converse →
price → close → serve → expand. The **agent does the work; the human approves.**
The surfaces stay UI-less:

- **Daily digest email** — the batch clock. Outbound approvals, follow-up sends,
  proposals. Already built.
- **Slack DM** — the on-demand clock. Real-time asks a daily email can't serve:
  "brief me on Acme before my 2pm." Added in Slice 1.

Everything human-facing is one of those two. No screen. "Configure a page
layout" is replaced by "tell the agent." That substitution is the product's
whole differentiation, not a missing feature.

## Two "document" concepts — do not conflate

Named here because they get merged and they are different builds:

- **Docs-as-context (retrieval / RAG).** Files the agent *reads* to answer
  questions and inform later stages. Lands in `memory_chunk` (Slice 1).
- **Docs-on-record (system-of-record).** Confirmation documents *attached* to a
  contract, generated contracts/paperwork, the change audit. Storage + CRUD +
  audit, not retrieval. Lands in Slice 2.

## Autonomy: B now, A later

- **B (now):** the human sells; zerocrm is memory. It captures calls, transcribes,
  logs, briefs before the call, drafts follow-up, writes the win/loss note.
- **A (later):** the agent places and holds the call itself. B is a strict subset
  of A — the pre-call brief, price book, and transcript store are exactly what an
  autonomous agent reads. Nothing in B is throwaway.

## Slice order

| # | Slice | Delivers | Depends on |
|---|-------|----------|------------|
| 1 | **Conversation Memory + Docs-as-context** | call capture (Meet + phone) → transcript → `memory_chunk`; Slack pre-call brief; post-call follow-up + win/loss note | none (own spec: `slice-1-conversation-memory-spec.md`) |
| 2 | **Post-sale system-of-record** | `location` entity; living contracts; change audit; docs-on-record upload; inspections-by-location; document generation | 1 (retrieval); own spec: `slice-2-post-sale-system-of-record-spec.md` |
| 3 | **CPQ / price book** | vendor supply list; price-change tracking → customer invoice deltas; basic quote | 2; own spec: `slice-3-cpq-price-book-spec.md` |
| 4 | **Marketing** | grow-into-it campaigns on the existing `campaign`/`flow_state` spine | 1–3 |

Slice 1 has its own full spec. Slices 2–4 are roadmap-fidelity below; each is
promoted to its own spec when it is the next thing built. Speccing them to the
letter now is waste — Slice 1 will move their boundaries.

---

## Cross-cutting decisions (bind every slice)

### CC-1 — Custom objects: Json-first, no registry
Custom *fields* already ride the per-field `facts_meta` provenance store and
`enrichment` Json on `person`/`company` — a customer's extra field is a Json key,
no migration. Custom *entity types* (a brand-new object) are **YAGNI** until a
real customer needs one; when they do, the upgrade is a generic `custom_object` +
`custom_record` pair, not a per-customer migration. Do not build it speculatively.

### CC-2 — Audit trail: event log for contracts, provenance for facts
Every table carries `actor` + `created_at`/`updated_at` via `_common()`, and
`upsert.py` writes per-field provenance (`facts_meta: {field: {rank, provenance,
actor, ts}}`) for `person`/`company` — the right tool when *competing enrichment
providers* fight over truth. A contract has no competing writers (a human or the
agent edits it), so Slice 2 audits it with an append-only `contract_event` log,
not the precedence gate. Same actor+timestamp discipline, mechanism matched to
the problem. See `slice-2-post-sale-system-of-record-spec.md`.

### CC-3 — Reporting has no dashboards
No UI means no report builder. "Report on nested objects" is answered by the
agent running SQL + `memory_chunk` retrieval and replying in Slack or the digest.
A saved question is a stored prompt, not a saved view.

### CC-4 — No record ceiling
The engine is the customer's own Postgres (SQLite for clone-and-run). No seats,
no row caps. State it in the pitch; nothing to build.

### CC-5 — One retrieval spine
`memory_chunk` (built in Slice 1) is generic on `source_type`. Transcripts,
docs-as-context, and later contract text all feed the same table and the same
similarity search. There is exactly one retrieval path in the product.

---

## Slice 2 — Post-sale system-of-record

**PROMOTED to a full spec:** `slice-2-post-sale-system-of-record-spec.md`. The
roadmap open questions are resolved there — staff = own table (not `person`);
one-off visits = `contract_line` rows; audit = append-only `contract_event`;
invoice ownership stays in Slice 3.

## Slice 3 — CPQ / price book

**PROMOTED to a full spec:** `slice-3-cpq-price-book-spec.md`. Roadmap open
questions resolved there — margin lives on the quote header (no per-line override
yet); NO invoice entity (zerocrm surfaces price deltas via a digest notice, it is
not the invoicing system); `contract_line` gains a soft `supply_id` reference for
price-impact detection.

## Slice 4 — Marketing

**PROMOTED to a full spec:** `slice-4-marketing-spec.md`. Thinnest slice — one
module (`marketing.py`) over the existing `campaign`/`list`/`flow_state` spine, no
new tables, no new runtime. Builds a customer audience (active-contract companies +
won deals) and enrols it into a `nurture` campaign as `scheduled` (the digest +
`sending_enabled` gate still own every send).
