"""Pipe driver output into the DB through the precedence gate and identity table.

Keeps all writes-from-providers in one place so provenance ranks are applied
consistently: Apollo -> enriched, ZeroBounce -> verified_provider.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from .schema import blocklist, campaign, company, person, person_identity, touch
from .upsert import resolve_person, upsert_person


def _now() -> datetime:
    return datetime.now(timezone.utc)


def import_apollo_people(engine: Engine, people: list[dict], actor: dict,
                         workspace_id: str = "default") -> dict:
    """Upsert normalized Apollo people. Skips those without an email (no identity
    key). Returns {"imported": n, "skipped_no_email": m}."""
    imported = skipped = 0
    with engine.begin() as conn:
        for p in people:
            email = p.get("email")
            if not email:
                skipped += 1
                continue
            company_id = _upsert_company(conn, p, actor, workspace_id)
            fields = dict(p["fields"])
            if company_id:
                fields["company_id"] = company_id
            person_id = upsert_person(
                conn,
                identity={"kind": "email", "value": email, "source": "apollo"},
                fields=fields,
                provenance="enriched",
                actor=actor,
                workspace_id=workspace_id,
            )
            if p.get("enrichment"):
                conn.execute(
                    update(person).where(person.c.id == person_id)
                    .values(enrichment=p["enrichment"], enriched_at=_now())
                )
            if p.get("linkedin"):
                _add_identity(conn, email, "linkedin", p["linkedin"], workspace_id, actor)
            imported += 1
    return {"imported": imported, "skipped_no_email": skipped}


def enrich_github(engine: Engine, probe=None) -> dict:
    """Probe GitHub for each company with a domain and merge the buildability
    signal into company.enrichment['github']. probe is injectable for tests."""
    if probe is None:
        from .drivers.github import github_probe as probe
    checked = found = 0
    with engine.begin() as conn:
        rows = conn.execute(
            select(company.c.id, company.c.name, company.c.domain, company.c.enrichment)
            .where(company.c.domain.isnot(None))
        ).mappings().all()
    for r in rows:
        enr = dict(r["enrichment"] or {})
        if "github" in enr:  # already probed
            continue
        result = probe(r["name"] or "", r["domain"])
        enr["github"] = result
        with engine.begin() as conn:
            conn.execute(update(company).where(company.c.id == r["id"]).values(enrichment=enr))
        checked += 1
        found += 1 if result.get("found") else 0
    return {"checked": checked, "github_found": found}


def mark_verified(engine: Engine, email: str, zb_result: dict, actor: dict,
                  workspace_id: str = "default") -> str:
    """Stamp verified_at on the email identity when ZeroBounce says valid.
    Returns the classification (send | block | risky)."""
    from .drivers.zerobounce import classify

    verdict = classify(zb_result)
    if verdict == "send":
        with engine.begin() as conn:
            conn.execute(
                update(person_identity)
                .where(
                    person_identity.c.workspace_id == workspace_id,
                    person_identity.c.kind == "email",
                    person_identity.c.value == email.lower(),
                )
                .values(verified_at=_now(), updated_at=_now())
            )
    return verdict


def stamp_verified(engine: Engine, email: str, actor: dict,
                   workspace_id: str = "default") -> None:
    """Stamp verified_at on an email identity. Called when the verify gate has
    already decided `send`; provider-agnostic (MillionVerifier and/or ZeroBounce
    made the call upstream). Idempotent."""
    with engine.begin() as conn:
        conn.execute(
            update(person_identity)
            .where(
                person_identity.c.workspace_id == workspace_id,
                person_identity.c.kind == "email",
                person_identity.c.value == email.lower(),
            )
            .values(verified_at=_now(), updated_at=_now())
        )


def record_provider_event(engine: Engine, event: dict, actor: dict,
                          workspace_id: str = "default") -> str:
    """Idempotently record a normalized inbound event (reply/bounce) as a touch.

    Exactly-once via touch.event_key (unique-when-present): a redelivered event
    resolves to the same row and is a no-op. A hard bounce also blocklists the
    address (identity granularity, spec D11.1). Returns 'inserted' | 'duplicate'.
    """
    email = event.get("email")
    event_key = event.get("event_key")
    if not email or not event_key:
        raise ValueError("event needs email and event_key")
    now = _now()
    with engine.begin() as conn:
        # dedupe check first (cheap); the unique index is the race backstop.
        if conn.execute(
            select(touch.c.id).where(
                touch.c.workspace_id == workspace_id, touch.c.event_key == event_key
            )
        ).first():
            return "duplicate"

        person_id = resolve_person(conn, "email", email, workspace_id)
        if person_id is None:
            # a reply from someone not in the CRM: create them (they DID engage)
            person_id = upsert_person(
                conn, {"kind": "email", "value": email, "source": "smartlead"},
                {}, "observed_from_reply", actor, workspace_id,
            )
        campaign_id = None
        if event.get("campaign_ref"):
            campaign_id = conn.execute(
                select(campaign.c.id).where(
                    campaign.c.workspace_id == workspace_id,
                    campaign.c.provider_ref == str(event["campaign_ref"]),
                )
            ).scalar_one_or_none()

        # savepoint so a unique-index race resolves to "duplicate" without
        # poisoning the outer transaction (Postgres aborts a txn on any error).
        try:
            with conn.begin_nested():
                conn.execute(touch.insert().values(
                    person_id=person_id, channel="email", direction="in",
                    kind=event["kind"], campaign_id=campaign_id, event_key=event_key,
                    payload=event.get("raw"), ts=now, actor=actor, workspace_id=workspace_id,
                ))
        except IntegrityError:
            return "duplicate"  # lost a race on the unique index

        if event["kind"] == "bounce":
            _blocklist_identity(conn, email, "hard_bounce", actor, workspace_id)
    return "inserted"


def _blocklist_identity(conn, email: str, reason: str, actor: dict, workspace_id: str) -> None:
    try:
        with conn.begin_nested():
            conn.execute(blocklist.insert().values(
                granularity="identity", value=email.lower(), reason=reason,
                actor=actor, workspace_id=workspace_id,
            ))
    except IntegrityError:
        pass  # already blocked


def _upsert_company(conn, p: dict, actor: dict, workspace_id: str) -> str | None:
    domain = (p.get("company_domain") or "").lower() or None
    if not domain:
        return None
    ce = p.get("company_enrichment") or {}
    industry = ce.get("industry")
    size = str(ce["employees"]) if ce.get("employees") is not None else None
    vals = {"name": p.get("company_name"), "industry": industry, "size": size,
            "enrichment": ce, "enriched_at": _now()}
    row = conn.execute(
        select(company.c.id).where(
            company.c.workspace_id == workspace_id, company.c.domain == domain
        )
    ).first()
    if row:
        conn.execute(update(company).where(company.c.id == row[0]).values(**vals))
        return row[0]
    res = conn.execute(
        company.insert().values(domain=domain, actor=actor, workspace_id=workspace_id, **vals)
    )
    return res.inserted_primary_key[0]


def _add_identity(conn, email: str, kind: str, value: str, workspace_id: str, actor: dict) -> None:
    pid = conn.execute(
        select(person_identity.c.person_id).where(
            person_identity.c.workspace_id == workspace_id,
            person_identity.c.kind == "email",
            person_identity.c.value == email.lower(),
        )
    ).scalar_one_or_none()
    if pid is None:
        return
    exists = conn.execute(
        select(person_identity.c.id).where(
            person_identity.c.workspace_id == workspace_id,
            person_identity.c.kind == kind,
            person_identity.c.value == value,
        )
    ).first()
    if exists:
        return
    conn.execute(
        person_identity.insert().values(
            person_id=pid, kind=kind, value=value, source="apollo",
            workspace_id=workspace_id, actor=actor,
        )
    )
