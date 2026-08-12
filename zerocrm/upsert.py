"""The precedence-gated person upsert — every driver writes through this.

The one rule that stops enrichment from silently overwriting truth (the "30% of
enriched records came back worse" failure class):

    fill a NULL field at ANY provenance rank;
    overwrite a non-NULL field ONLY when incoming_rank >= stored_rank.

So Apollo (enriched) can fill a blank title but can never clobber a title you
typed (human) or one a prospect stated in a reply (observed_from_reply).
Per-field provenance is stamped into person.facts_meta.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection

from .schema import person, person_identity

# higher wins. A write may always fill a NULL; it overwrites only equal-or-lower.
RANK = {
    "human": 5,
    "observed_from_reply": 4,
    "observed_from_meeting": 4,  # a call participant is as trustworthy as a reply
    "verified_provider": 3,
    "enriched": 2,
    "imported": 1,
}

# person columns the precedence gate manages (identity/timestamps handled apart)
_MANAGED = ("full_name", "job_title", "segment", "icp_score", "source", "company_id")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_person(conn: Connection, kind: str, value: str, workspace_id: str = "default") -> str | None:
    row = conn.execute(
        select(person_identity.c.person_id).where(
            person_identity.c.workspace_id == workspace_id,
            person_identity.c.kind == kind,
            person_identity.c.value == value,
            person_identity.c.retired_at.is_(None),
        )
    ).first()
    return row[0] if row else None


def upsert_person(
    conn: Connection,
    identity: dict,          # {"kind": "email", "value": "x@y.com"}
    fields: dict,            # subset of _MANAGED
    provenance: str,         # one of RANK
    actor: dict,             # {"kind": "agent"|"human"|"provider", "id": ..., "session": ...}
    workspace_id: str = "default",
) -> str:
    """Resolve-or-create a person, then apply fields under the precedence gate.

    Returns the person id. Idempotent on identity: the same (kind, value) always
    resolves to the same person.
    """
    if provenance not in RANK:
        raise ValueError(f"unknown provenance {provenance!r}")
    incoming_rank = RANK[provenance]
    now = _now()

    person_id = resolve_person(conn, identity["kind"], identity["value"], workspace_id)
    if person_id is None:
        person_id = _create_person(conn, identity, actor, workspace_id, now)
        meta: dict = {}
        current: dict = {}
    else:
        row = conn.execute(
            select(person.c.facts_meta, *[person.c[c] for c in _MANAGED]).where(
                person.c.id == person_id
            )
        ).mappings().first()
        meta = dict(row["facts_meta"] or {})
        current = {c: row[c] for c in _MANAGED}

    changed: dict = {}
    for field, val in fields.items():
        if field not in _MANAGED or val is None:
            continue
        stored_rank = meta.get(field, {}).get("rank", 0)
        if current.get(field) is None or incoming_rank >= stored_rank:
            changed[field] = val
            meta[field] = {
                "rank": incoming_rank,
                "provenance": provenance,
                "actor": actor,
                "ts": now.isoformat(),
            }

    if changed:
        conn.execute(
            update(person)
            .where(person.c.id == person_id)
            .values(facts_meta=meta, updated_at=now, **changed)
        )
    return person_id


def _create_person(conn: Connection, identity: dict, actor: dict, workspace_id: str, now: datetime) -> str:
    res = conn.execute(
        insert(person).values(workspace_id=workspace_id, actor=actor, facts_meta={},
                              created_at=now, updated_at=now)
    )
    person_id = res.inserted_primary_key[0]
    conn.execute(
        insert(person_identity).values(
            person_id=person_id,
            kind=identity["kind"],
            value=identity["value"],
            source=identity.get("source"),
            workspace_id=workspace_id,
            actor=actor,
            created_at=now,
            updated_at=now,
        )
    )
    return person_id
