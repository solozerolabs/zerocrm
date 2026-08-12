# zerocrm

An agent-native CRM + outreach engine for zero-hour companies. No UI, no data
entry, no seats. Your entire surface is one daily digest email; an agent drives
everything else through the database and provider APIs.

The engine is public. **Your data stays in your own database** — SQLite by
default (clone and run), Postgres when you want it.

## Why

The #1 CRM complaint in 2026 is manual data entry. zerocrm never asks you to
update it. The agent writes every record; you approve outreach once a day by
replying to an email.

## Quickstart

```bash
uv sync
uv run zerocrm migrate                      # creates zerocrm.db (SQLite)
uv run zerocrm add-person jane@acme.com --fields '{"full_name":"Jane","job_title":"Founder"}' --provenance human
```

## Design guarantees

- **Write precedence** — enrichment can never overwrite a fact you typed or a
  prospect stated in a reply. (`fill NULL at any rank; overwrite only equal-or-lower rank`.)
- **Identity resolution** — one human across email / LinkedIn / X / site visit.
- **Immortal funnels** — quarterly/yearly re-triggers survive; freshness is a
  just-in-time re-verification, never a delete.
- **Digest is the only human surface** — no new outreach enters any channel
  without a daily approval.

Status: Phase 1 (email slice). See the spec for the full roadmap.

MIT.
