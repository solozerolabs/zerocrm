"""Research briefs — derived, TTL'd per-prospect/company research that feeds a
cited hook into the draft copy and the digest's "why" line.

Kept OUT of person/company enrichment: enrichment is the precedence-gated store
for canonical facts; a brief is synthesized, expires, and is re-derivable without
re-paying retrieval. Company briefs dedup by NORMALIZED DOMAIN (agencies share a
domain -> one brief); person briefs key on person_id.

Slice 1 is the persistence + read seam only. The content that fills these rows
(executor web search, later Exa/Firecrawl behind the driver seam) is Slice 2 —
and whatever fills them must work AROUND LinkedIn, never resolving a linkedin.com
URL (docs/research-pipeline-plan.md).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.engine import Connection

from .schema import research as research_t

_TTL_DAYS = {"company": 30, "person": 14}  # people change roles faster than firmographics


def _now() -> datetime:
    return datetime.now(timezone.utc)


def default_ttl(scope: str) -> int:
    return _TTL_DAYS.get(scope, 14)


def normalize_domain(value: str) -> str:
    """'acme.com' from 'https://www.Acme.com/about', 'ACME.com:443', etc. -> ''
    if empty. Company briefs key on this so agencies sharing a domain collapse."""
    if not value:
        return ""
    v = value.strip().lower()
    if "//" not in v:
        v = "//" + v
    host = (urlparse(v).netloc or "").split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def upsert_research(engine, *, scope: str, subject_key: str, brief: dict,
                    sources: list | None = None, provider: str = "executor",
                    ttl_days: int | None = None, company_id: str | None = None,
                    person_id: str | None = None, actor: dict | None = None,
                    workspace_id: str = "default") -> str:
    """UPSERT one brief on (workspace_id, scope, subject_key). A refetch overwrites
    in place — never a second row — so per-domain dedup holds. Returns the row id."""
    now = _now()
    ttl = default_ttl(scope) if ttl_days is None else ttl_days
    vals = dict(brief=brief, sources=sources or [], provider=provider,
                company_id=company_id, person_id=person_id, fetched_at=now,
                expires_at=now + timedelta(days=ttl), actor=actor, updated_at=now)
    with engine.begin() as conn:
        row = conn.execute(
            select(research_t.c.id).where(
                research_t.c.workspace_id == workspace_id,
                research_t.c.scope == scope,
                research_t.c.subject_key == subject_key,
            )
        ).first()
        if row:
            conn.execute(research_t.update().where(research_t.c.id == row[0]).values(**vals))
            return row[0]
        res = conn.execute(research_t.insert().values(
            scope=scope, subject_key=subject_key, workspace_id=workspace_id, **vals))
        return res.inserted_primary_key[0]


def get_fresh(conn_or_engine, scope: str, subject_key: str,
              workspace_id: str = "default") -> dict | None:
    """The brief dict if a non-expired row exists, else None (missing OR stale)."""
    row = _first(conn_or_engine, select(research_t.c.brief, research_t.c.expires_at).where(
        research_t.c.workspace_id == workspace_id,
        research_t.c.scope == scope,
        research_t.c.subject_key == subject_key,
    ))
    if not row:
        return None
    brief, expires_at = row
    if expires_at is not None:
        # SQLite hands back naive datetimes; treat naive as UTC for the compare.
        exp = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
        if exp <= _now():
            return None
    return brief


def top_hook(conn_or_engine, *, person_id: str | None = None,
             domain: str | None = None, workspace_id: str = "default") -> dict | None:
    """The best cited hook for a candidate: person brief first, then company (by
    normalized domain). Fresh only. Returns one hook dict or None."""
    for scope, key in (("person", person_id), ("company", normalize_domain(domain or ""))):
        if not key:
            continue
        hooks = (get_fresh(conn_or_engine, scope, key, workspace_id) or {}).get("hooks") or []
        if hooks:
            return hooks[0]
    return None


def _first(conn_or_engine, query):
    if isinstance(conn_or_engine, Connection):
        return conn_or_engine.execute(query).first()
    with conn_or_engine.connect() as conn:
        return conn.execute(query).first()
