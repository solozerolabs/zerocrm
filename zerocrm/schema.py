"""zerocrm schema — migration 001 (the full v1 schema).

One MetaData definition drives both backends: SQLite (public default) and
Postgres (our deployment). jsonb columns degrade to plain JSON on SQLite via
the dialect variant below; every table carries workspace_id / actor / timestamps.

ponytail: PK strategy is deliberately mixed. Most tables use app-generated
uuid-hex string PKs (portable, no server default needed). digest_item alone
uses an autoincrement integer PK *as* its item_no, because the reply grammar
needs a stable, monotonic, never-reused human number and that is exactly what
an autoincrement PK already is — no second counter to keep.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()

# jsonb on Postgres, JSON (TEXT-backed) on SQLite.
Json = JSON().with_variant(JSONB, "postgresql")


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _common() -> list[Column]:
    """Fresh column objects per table (Columns cannot be shared across Tables)."""
    return [
        Column("workspace_id", Text, nullable=False, default="default"),
        Column("actor", Json, nullable=True),
        Column("created_at", DateTime(timezone=True), default=_now, nullable=False),
        Column("updated_at", DateTime(timezone=True), default=_now, onupdate=_now, nullable=False),
    ]


def _pk() -> Column:
    return Column("id", String(32), primary_key=True, default=_uuid)


# --- migration bookkeeping -------------------------------------------------
schema_migrations = Table(
    "schema_migrations",
    metadata,
    Column("version", Text, primary_key=True),
    Column("applied_at", DateTime(timezone=True), default=_now, nullable=False),
)

# --- core entities ---------------------------------------------------------
company = Table(
    "company",
    metadata,
    _pk(),
    Column("domain", Text),
    Column("name", Text),
    Column("size", Text),
    Column("industry", Text),
    Column("enriched_at", DateTime(timezone=True)),
    Column("facts_meta", Json, default=dict),
    # rich source data: website, keywords, technologies, description, founded_year
    Column("enrichment", Json, default=dict),
    *_common(),
    UniqueConstraint("workspace_id", "domain", name="uq_company_domain"),
)

person = Table(
    "person",
    metadata,
    _pk(),
    Column("full_name", Text),
    Column("job_title", Text),
    Column("segment", Text),
    Column("icp_score", Integer),
    Column("source", Text),
    Column("company_id", String(32), ForeignKey("company.id")),
    Column("enriched_at", DateTime(timezone=True)),
    # per-field provenance: {field: {rank, provenance, actor, ts}} — see upsert.py
    Column("facts_meta", Json, default=dict),
    # rich source data for judging a prospect: headline, seniority, location,
    # departments, apollo_id (supplementary; not precedence-gated).
    Column("enrichment", Json, default=dict),
    *_common(),
)

person_identity = Table(
    "person_identity",
    metadata,
    _pk(),
    Column("person_id", String(32), ForeignKey("person.id"), nullable=False),
    Column("kind", Text, nullable=False),  # email | linkedin | x | phone | visit_fingerprint
    Column("value", Text, nullable=False),
    Column("source", Text),
    Column("verified_at", DateTime(timezone=True)),
    Column("retired_at", DateTime(timezone=True)),
    *_common(),
    UniqueConstraint("workspace_id", "kind", "value", name="uq_identity"),
)

deal = Table(
    "deal",
    metadata,
    _pk(),
    Column("person_id", String(32), ForeignKey("person.id")),
    Column("company_id", String(32), ForeignKey("company.id")),
    Column("stage", Text, nullable=False, default="prospect"),
    Column("value_usd", Float),
    Column("next_step", Text),
    Column("stage_entered_at", DateTime(timezone=True), default=_now),
    *_common(),
)

# --- conversation memory (slice 1) -----------------------------------------
# A sales call captured (Meet now; phone via a deferred Whisper seam). Dedup key
# is external_ref (always written), NOT the touch event_key — an emailless call
# writes no touch, so touch-keyed dedup would double-insert on the overlap re-read.
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
    Index("uq_call_external_ref", "workspace_id", "external_ref",
          unique=True,
          sqlite_where=text("external_ref IS NOT NULL"),
          postgresql_where=text("external_ref IS NOT NULL")),
)

# A re-transcribe (Whisper later) is a NEW row; the old one stays. Provenance,
# same principle as upsert.py.
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

# The one generic retrieval spine (CC-5): transcripts and docs both feed it.
# ponytail: retrieval is a Python token-overlap scan; DB FTS / pgvector + an
# embedding column is the documented upgrade behind memory.retrieve().
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

# --- post-sale system of record (slice 2) ----------------------------------
location = Table(
    "location", metadata, _pk(),
    Column("company_id", String(32), ForeignKey("company.id")),
    Column("name", Text, nullable=False),
    Column("address", Json, default=dict),
    Column("meta", Json, default=dict),
    *_common(),
)

# the customer's OWN employees — a SEPARATE population from person (prospects),
# so every pipeline query doesn't have to exclude staff.
staff = Table(
    "staff", metadata, _pk(),
    Column("full_name", Text, nullable=False),
    Column("email", Text),
    Column("role", Text),
    Column("active", Boolean, nullable=False, default=True),
    *_common(),
)

# a living service contract: field edits and collection changes are all audited
# via contract_event (append-only), NOT the person precedence gate.
contract = Table(
    "contract", metadata, _pk(),
    Column("company_id", String(32), ForeignKey("company.id")),
    Column("location_id", String(32), ForeignKey("location.id")),
    Column("contract_type", Text),
    Column("scope", Text),
    Column("status", Text, nullable=False, default="active"),  # active|paused|ended
    Column("value_usd", Float),
    Column("starts_on", Text),   # ISO date as text -> SQLite/PG identical
    Column("ends_on", Text),
    *_common(),
)

contract_staff = Table(
    "contract_staff", metadata, _pk(),
    Column("contract_id", String(32), ForeignKey("contract.id"), nullable=False),
    Column("staff_id", String(32), ForeignKey("staff.id"), nullable=False),
    Column("role", Text),
    Column("assigned_at", DateTime(timezone=True), default=_now),
    Column("removed_at", DateTime(timezone=True)),  # a swap sets this; never deletes
    *_common(),
)

contract_line = Table(
    "contract_line", metadata, _pk(),
    Column("contract_id", String(32), ForeignKey("contract.id"), nullable=False),
    Column("kind", Text, nullable=False),          # one_off_visit|material|supply
    Column("description", Text),
    Column("qty", Float, default=1),
    Column("unit_cost_usd", Float),                # nullable; Slice 3 links to a supply
    Column("supply_id", String(32)),              # soft ref -> supply (slice 3); no DB FK (ALTER-safe)
    Column("occurred_on", Text),
    *_common(),
)

# docs-on-RECORD (record of truth). `uri` points at storage; `body` holds
# generated/inline text; a text doc may ALSO feed memory_chunk (chunked=true).
document = Table(
    "document", metadata, _pk(),
    Column("contract_id", String(32), ForeignKey("contract.id")),
    Column("location_id", String(32), ForeignKey("location.id")),
    Column("filename", Text, nullable=False),
    Column("uri", Text),
    Column("body", Text),
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

# --- research: derived, TTL'd per-prospect/company briefs ------------------
# Kept OUT of person/company.enrichment: enrichment is the precedence-gated store
# for CANONICAL facts; a brief is synthesized, expires, and is re-derivable. A
# company brief dedups by NORMALIZED DOMAIN (agencies share a domain -> one row);
# a person brief keys on person_id. Nothing that fills these rows may resolve a
# linkedin.com URL (see docs/research-pipeline-plan.md).
research = Table(
    "research",
    metadata,
    _pk(),
    Column("scope", Text, nullable=False),         # company | person
    Column("subject_key", Text, nullable=False),   # company: normalized domain; person: person_id
    Column("company_id", String(32), ForeignKey("company.id")),
    Column("person_id", String(32), ForeignKey("person.id")),
    # {summary, angle, hooks:[{text, evidence_url, evidence_title, recency_days, confidence}]}
    Column("brief", Json, nullable=False, default=dict),
    Column("sources", Json, default=list),         # [{url, title, fetched_at, provider}]
    Column("provider", Text),                      # executor | exa | firecrawl | ...
    Column("fetched_at", DateTime(timezone=True), default=_now, nullable=False),
    Column("expires_at", DateTime(timezone=True)),
    *_common(),
    UniqueConstraint("workspace_id", "scope", "subject_key", name="uq_research_subject"),
    Index("ix_research_expiry", "workspace_id", "scope", "expires_at"),
)

# --- campaigns / lists / flows ---------------------------------------------
campaign = Table(
    "campaign",
    metadata,
    _pk(),
    Column("name", Text, nullable=False),
    Column("channel", Text, nullable=False),      # email | li_dm | li_invite | li_comment | li_post | x | ads
    Column("flow_kind", Text, nullable=False),    # cold | warm | retarget | nurture
    Column("provider_ref", Text),                 # e.g. Smartlead campaign id
    Column("sender_ref", Text),                   # mailbox / account
    Column("status", Text, nullable=False, default="active"),
    *_common(),
)

list_ = Table(
    "list",
    metadata,
    _pk(),
    Column("name", Text, nullable=False),
    Column("purpose", Text),  # sequence | audience
    *_common(),
)

list_member = Table(
    "list_member",
    metadata,
    _pk(),
    Column("list_id", String(32), ForeignKey("list.id"), nullable=False),
    Column("person_id", String(32), ForeignKey("person.id"), nullable=False),
    *_common(),
    UniqueConstraint("list_id", "person_id", name="uq_list_member"),
)

flow_state = Table(
    "flow_state",
    metadata,
    _pk(),
    Column("person_id", String(32), ForeignKey("person.id"), nullable=False),
    Column("campaign_id", String(32), ForeignKey("campaign.id"), nullable=False),
    Column("channel", Text, nullable=False),
    Column("step", Integer, nullable=False, default=0),
    Column("status", Text, nullable=False, default="scheduled"),  # active|scheduled|dormant|blocked
    Column("next_action_at", DateTime(timezone=True)),
    Column("parked_reason", Text),
    *_common(),
    # one ACTIVE flow per (person, channel); partial index below (PG). SQLite
    # honours partial indexes too (>=3.8), so this is cross-backend.
    Index(
        "uq_active_flow",
        "workspace_id", "person_id", "channel",
        unique=True,
        sqlite_where=text("status = 'active'"),
        postgresql_where=text("status = 'active'"),
    ),
)

# --- the unified timeline ---------------------------------------------------
touch = Table(
    "touch",
    metadata,
    _pk(),
    Column("person_id", String(32), ForeignKey("person.id"), nullable=False),
    Column("channel", Text, nullable=False),
    Column("direction", Text, nullable=False),  # out | in
    Column("kind", Text, nullable=False),       # sent|reply|click|open|bounce|li_engage|visit|meeting_booked|payment|flow_preempted
    Column("campaign_id", String(32), ForeignKey("campaign.id")),
    Column("body_ref", Text),
    Column("payload", Json),
    Column("ts", DateTime(timezone=True), default=_now, nullable=False),
    # provider event id / message id. Unique-when-present so any ingestion path
    # (poll or a future webhook) is exactly-once on redelivery — the inbound
    # analog of the executor's claim. NULL for internally-generated touches.
    Column("event_key", Text),
    *_common(),
    Index("ix_touch_person_ts", "person_id", "ts"),
    Index("uq_touch_event_key", "workspace_id", "event_key",
          unique=True,
          sqlite_where=text("event_key IS NOT NULL"),
          postgresql_where=text("event_key IS NOT NULL")),
)

# --- routing / audiences / providers ---------------------------------------
routing_rule = Table(
    "routing_rule",
    metadata,
    _pk(),
    Column("name", Text, nullable=False),
    Column("predicate", Json, nullable=False),   # matched against touch/person state
    Column("action", Json, nullable=False),      # assign/move flow, set segment, flag digest
    Column("priority", Integer, nullable=False, default=100),
    Column("specificity", Integer, nullable=False, default=0),
    Column("enabled", Boolean, nullable=False, default=True),
    *_common(),
)

audience_sync = Table(
    "audience_sync",
    metadata,
    _pk(),
    Column("list_id", String(32), ForeignKey("list.id"), nullable=False),
    Column("provider", Text, nullable=False),  # meta | linkedin | google
    Column("synced_at", DateTime(timezone=True)),
    Column("match_count", Integer),
    *_common(),
)

provider = Table(
    "provider",
    metadata,
    _pk(),
    Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("doppler_key_name", Text),
    Column("scopes", Json),
    Column("status", Text, nullable=False, default="active"),
    *_common(),
    UniqueConstraint("workspace_id", "name", name="uq_provider_name"),
)

# --- the only permanent exit ------------------------------------------------
blocklist = Table(
    "blocklist",
    metadata,
    _pk(),
    # granularity (scopes adopted from outreachr's suppressions):
    #   identity (a bounced/opted-out address) | domain | person | company | global
    Column("granularity", Text, nullable=False),
    Column("value", Text, nullable=False),      # the address / person_id / domain
    Column("reason", Text, nullable=False),     # hard_bounce | complaint | opt_out
    *_common(),
    UniqueConstraint("workspace_id", "granularity", "value", name="uq_blocklist"),
)

# --- the only human surface -------------------------------------------------
digest_item = Table(
    "digest_item",
    metadata,
    # item_no IS the PK: monotonic, never reused, referenced by the reply grammar.
    # BigInteger on PG (bigserial); plain INTEGER on SQLite so it aliases rowid
    # and actually autoincrements (BIGINT PK does not on SQLite).
    Column("item_no", BigInteger().with_variant(Integer, "sqlite"),
           primary_key=True, autoincrement=True),
    Column("kind", Text, nullable=False),       # enroll | comment | dm | post | reply | proposal
    Column("channel", Text, nullable=False),
    Column("person_id", String(32), ForeignKey("person.id")),
    Column("campaign_id", String(32), ForeignKey("campaign.id")),
    Column("payload", Json, nullable=False),    # draft copy (all steps for an enroll)
    Column("status", Text, nullable=False, default="draft"),  # draft|approved|edited|skipped|sending|sent|auto_sent
    Column("diff", Json),                        # human edit captured for the correction loop
    Column("digest_date", Text, nullable=False), # YYYY-MM-DD the item first appeared
    Column("claim_token", Text),                 # claim-first executor
    Column("claimed_at", DateTime(timezone=True)),
    *_common(),
    Index("ix_digest_status", "digest_date", "status"),
)

# --- the learning loop (tacitry) — DB is canonical -------------------------
correction = Table(
    "correction",
    metadata,
    _pk(),
    Column("channel", Text, nullable=False),
    Column("wrong", Text, nullable=False),
    Column("right", Text, nullable=False),
    Column("durable", Boolean, nullable=False, default=False),
    Column("ts", DateTime(timezone=True), default=_now, nullable=False),
    *_common(),
)

rule = Table(
    "rule",
    metadata,
    _pk(),
    Column("channel", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("hits", Integer, nullable=False, default=1),
    Column("surfaces", Integer, nullable=False, default=1),
    Column("promoted_at", DateTime(timezone=True)),
    *_common(),
)

autonomy = Table(
    "autonomy",
    metadata,
    _pk(),
    Column("channel", Text, nullable=False),
    Column("level", Text, nullable=False, default="approve_all"),  # approve_all|sample_50|auto_digest
    Column("streak", Integer, nullable=False, default=0),
    *_common(),
    UniqueConstraint("workspace_id", "channel", name="uq_autonomy_channel"),
)

# --- config: everything an operator customizes by prompt --------------------
config = Table(
    "config",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Json, nullable=False),
    Column("workspace_id", Text, nullable=False, default="default"),
    Column("updated_at", DateTime(timezone=True), default=_now, onupdate=_now, nullable=False),
)
