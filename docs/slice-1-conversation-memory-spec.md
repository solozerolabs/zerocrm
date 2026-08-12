# Slice 1 — Conversation Memory + Docs-as-context (spec)

Status: pre-implementation. Roadmap context: `docs/lifecycle-roadmap.md`.
Autonomy mode B (human sells; zerocrm remembers). Surfaces stay UI-less.

## What this slice delivers

1. **Capture** a sales call (Google Meet now; phone via a deferred seam) into a
   normalized `transcript`, attached to a `deal` + `person`, logged on the unified
   `touch` timeline.
2. **Docs-as-context**: ingest arbitrary files into `memory_chunk`, the one
   generic retrieval spine (transcripts feed it too).
3. **Pre-call brief**: an operator DMs the Slack bot ("brief me on Acme"); the
   agent answers from deal state + recent transcripts + retrieved chunks.
4. **Post-call**: draft a follow-up into the daily digest, and write a structured
   win/loss note when the deal closes.

Out of scope (named so they are not built here): docs-**on-record** (upload to a
contract, generation, audit) → Slice 2; CPQ → Slice 3; autonomous calling (mode
A). Embeddings/semantic search → an additive upgrade behind the retrieval seam.

## Architecture — no new runtime

Everything rides the existing poll-primary runner (`runner.py`). No webhook, no
public endpoint (consistent with runtime-architecture.md D-INGEST).

- **`run_tick`** gains one step: poll the Google Meet API for finished conference
  records since a cursor, ingest transcripts. Same shape as the Smartlead poller
  — cursor in `config`, dedupe on `touch.event_key`.
- **New `run_slack_tick`** — a *separate, frequent* verb (pg_cron every ~2 min,
  business-hours-weighted). It only polls the Slack bot DM channel since a cursor
  and answers. Frequent + cheap, so a "brief me before my 2pm" lands in minutes
  without an always-on Socket-Mode process — scale-to-zero survives.
  `# ponytail: poll on a fast tick; Socket Mode only if sub-minute briefs matter`.

```
Google Meet call ends
  run_tick → Meet API conferenceRecords.list (since cursor)
           → transcripts.entries → normalize → transcript + call + touch(kind=meeting)
           → chunk transcript → memory_chunk(source_type=transcript)
           → participant emails → resolve-or-create person (precedence gate)

file dropped for ingest
  ingest_document → chunk → memory_chunk(source_type=document, attached to deal/person)

Slack DM "brief me on Acme"
  run_slack_tick → poll → authenticate (operator allowlist)
           → retrieve: deal + timeline + recent transcripts + top memory_chunks
           → compose brief → chat.postMessage

deal moves to won/lost
  run_tick → read the deal's transcripts → write win/loss note as touch(kind=review)
           → draft follow-up into digest_item
```

## Data model (2 new tables + reuse)

New:

```
call        id, deal_id→deal, person_id→person, source (meet|voice),
            external_ref (Meet conferenceRecord name / Voice id),
            started_at, ended_at, recording_uri, status
            (captured | awaiting_transcription | transcribed | failed)
            — the conversation event + where audio lives.

transcript  id, call_id→call, transcript_source (google|whisper),
            text, segments (Json: [{speaker, ts, text}]), lang
            — a re-transcribe (Whisper later) is a NEW row; the old one stays.
              Provenance, same principle as upsert.py.

memory_chunk id, source_type (transcript|document), source_id,
            deal_id→deal (nullable), person_id→person (nullable),
            chunk_no, chunk_text
            — the generic retrieval spine (CC-5). No embedding column in this
              slice: retrieval is a Python token-overlap rank.
              `# ponytail: linear scan; DB FTS / pgvector + embedding col when it hurts`.
```

Reuse, no new column:

- **`touch`** — every call logs `kind=meeting`, and every win/loss note is
  `kind=review`, `direction=in`, `payload={outcome, reasons[], sentiment,
  next_step}`. The timeline already carries non-contact kinds (`payment`,
  `flow_preempted`), so `meeting`/`review` fit. No `review` column on `deal`.
- **`digest_item`** — the post-call follow-up is a normal draft item
  (`kind=reply` or a new `kind=followup`), approval-gated like everything else.
- **`config`** — cursors (`google_meet_cursor`, `slack_cursor`) and Slack settings
  (`slack_operator_ids`, `slack_bot_channel`).
- **`provider`** — register `google_meet` and `slack` (doppler key names, scopes).
- The **pre-call brief is NOT stored** — it is synthesized live per request. (If
  it ever needs caching, it is the exact shape of the existing `research` table;
  reuse that, do not add a new one.)

Migration: additive callable `003_conversation` in `migrate.py`
(`call.create(checkfirst=True)`, etc.), following the existing `_research` pattern.

## Capture detail

**Google Meet (now):** Google Meet REST API v2 — `conferenceRecords.list`
(filter `end_time > cursor`), then `.transcripts` → `.transcripts.entries` for
diarized text (speaker + text + timing). No Drive-file parsing. OAuth against the
same Google account (Meet API scope). Cursor is the max `end_time` seen, stored in
`config`; a 5-minute overlap re-read window (same as the Smartlead poller) with
dedupe on `touch.event_key = conferenceRecord.name` makes it exactly-once.

**Deal/person match:** Meet participants carry emails. For each, resolve-or-create
the person via the precedence gate (`upsert.py`, provenance
`observed_from_meeting`) — identical policy to reply ingestion. The call attaches
to the person's open `deal` if one exists, else person-only (a deal can be
back-linked later).

**Phone (deferred, seam only):** a `source=voice` call is created with
`status=awaiting_transcription`; `transcribe.py` exposes `transcribe_audio(uri)`
as a stub that raises `NotImplementedError` until the Whisper adapter lands. The
table, the normalizer, and the `voice` enum value exist now so the later adapter
is a swap, not a schema change. Google Voice has no clean recording-export API, so
this path was always going to be Whisper-on-recording.

## Retrieval

`retrieve(engine, query, *, deal_id=None, person_id=None, k=8)`:
1. Structured filter first — chunks for this deal/person, plus recent ones.
2. Rank by token overlap between `query` and `chunk_text` (lowercased, stopword-
   stripped). Return top `k`.

Good enough for SMB volume and honest about it. Semantic search is the documented
upgrade: add an `embedding` column + provider behind this same function — callers
do not change.

## Pre-call brief & win/loss — activate-when-key idiom

Both follow the existing `copy_llm` pattern: a **deterministic floor** always
works; richer synthesis activates when `ANTHROPIC_API_KEY` (+ voice profile) is
set.

- **Brief (deterministic):** structured dump — person/company facts, open deal +
  stage + next_step, last N `touch` rows, snippets from the most recent
  transcript, and top retrieved chunks. **Brief (LLM):** the same inputs composed
  into a short natural brief with a suggested opening + risks.
- **Win/loss (deterministic):** template from stage transition + last transcript
  snippet. **Win/loss (LLM):** reasons + sentiment + next-step extracted from the
  deal's transcripts.

## Trust & safety

- **Slack authentication:** only messages from `slack_operator_ids` (config
  allowlist) are answered — mirrors the digest reply allowlist. Anything else is
  ignored, never acted on.
- **Transcript and Slack text are DATA, not instructions.** An imperative inside a
  transcript ("email legal@…") or a Slack message is quoted/summarized, never
  executed as a command. Follow-up drafts are approval-gated in the digest;
  nothing sends without the operator's daily approval. (Global instruction-source
  boundary; sid-voice "fetched text is data".)
- **Sending stays gated:** `sending_enabled` continues to gate any outbound; the
  brief and win/loss note are read/compose-only.

## Modules (new)

| file | purpose |
|------|---------|
| `zerocrm/meet.py` | Google Meet API poll + normalize → `call`/`transcript`/`touch` |
| `zerocrm/transcribe.py` | transcript-source seam: Meet (structured) now, `transcribe_audio` stub for Whisper |
| `zerocrm/memory.py` | `ingest_document`, `chunk`, `add_chunks`, `retrieve` |
| `zerocrm/brief.py` | pre-call brief (deterministic floor + LLM activation) |
| `zerocrm/review.py` | win/loss note on deal close |
| `zerocrm/slack.py` | poll bot DM channel, authenticate, dispatch, `post_message` |

Touched: `schema.py` (3 tables), `migrate.py` (`003_conversation`), `runner.py`
(`run_tick` Meet step + `run_slack_tick`), `cli.py` (`slack-tick`, `ingest-doc`).

## Testing (one test file per new module, sqlite in-memory, mocked APIs)

Mirror the existing `test_drivers`/`test_ingest_events` mocking style. Assert:

- **migration** creates `call`/`transcript`/`memory_chunk`; `003` is idempotent.
- **meet**: a mocked conference record + transcript entries → one `call`, one
  `transcript`, one `touch(kind=meeting)`; a redelivery dedupes on `event_key`;
  an unknown participant email resolves-or-creates a person.
- **memory**: `ingest_document` chunks + stores; `retrieve` ranks the chunk that
  contains the query terms above one that does not; deal/person filter scopes it.
- **brief**: deterministic brief for a deal includes its stage, next_step, and a
  recent-transcript snippet; empty history yields a safe "no history" brief.
- **review**: moving a deal to won/lost writes exactly one `touch(kind=review)`
  with structured payload.
- **slack**: an operator message is dispatched; a non-allowlisted sender is
  ignored; the cursor advances and re-poll does not double-answer.
- **transcribe**: `transcribe_audio` raises `NotImplementedError` (the seam is
  explicit, not silently empty).

## YAGNI / ponytail calls (explicit)

- No embeddings, no vector store — token-overlap now; column + provider later.
- No Socket Mode — fast poll now; always-on socket only if sub-minute needed.
- No brief caching / `research` reuse — synthesize live; add only if measured.
- No `review` column on `deal` — win/loss is a `touch`.
- Phone capture is a stub, not a build — Meet is the real path this slice.
- Custom objects untouched (CC-1): a call's extra fields ride `payload`/Json.
