"""The draft run: turn prospects into digest drafts.

Pipeline: acquire candidates (live Apollo search+reveal+ZeroBounce, or seeded)
-> import through the precedence gate -> ensure a campaign -> for each new
person create a scheduled flow_state + a `draft` digest_item carrying the
sequence copy. It never sends; the human approves in the digest, the executor
sends. Idempotent: a person already in the campaign's flow is skipped.

The copy here is a template. Phase 2 swaps the copy step for a nightly LLM copy
run (voice + rules ledger); everything downstream is unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .copy import (clean_company, default_sequence, icebreaker_from_hook,
                   lint_copy, lint_warm, render_preview, subject_hook)
from .ingest import import_apollo_people, stamp_verified
from .research import top_hook
from .schema import campaign as campaign_t
from .schema import company as company_t
from .schema import digest_item, flow_state, person_identity
from .schema import person as person_t
from .upsert import resolve_person


def _now() -> datetime:
    return datetime.now(timezone.utc)


def draft_run(
    engine: Engine,
    config: dict,
    actor: dict,
    *,
    apollo=None,
    verify=None,
    finders: list | None = None,
    icebreaker_fn=None,
    seed_people: list[dict] | None = None,
    workspace_id: str = "default",
) -> list[int]:
    """Returns the item_no of every draft created this run."""
    candidates = seed_people if seed_people is not None else _acquire_live(apollo, verify, config, finders)

    import_apollo_people(engine, candidates, actor, workspace_id)
    # record the verification the gate already did during acquisition
    for c in candidates:
        if c.get("_verify") is not None and c.get("email"):
            stamp_verified(engine, c["email"], actor, workspace_id)
    campaign_id, provider_ref = ensure_campaign(engine, config["campaign"], actor, workspace_id)

    today = _now().date().isoformat()
    drafts: list[int] = []
    with engine.begin() as conn:
        for c in candidates:
            email = c.get("email")
            if not email:
                continue
            person_id = resolve_person(conn, "email", email, workspace_id)
            if person_id is None:
                continue
            if _already_in_flow(conn, person_id, campaign_id, workspace_id):
                continue  # dedup: one enrollment per person per campaign
            conn.execute(
                flow_state.insert().values(
                    person_id=person_id, campaign_id=campaign_id, channel="email",
                    step=0, status="scheduled", next_action_at=_now(),
                    actor=actor, workspace_id=workspace_id,
                )
            )
            hook = top_hook(conn, person_id=person_id,
                            domain=c.get("company_domain"), workspace_id=workspace_id)
            payload = build_enroll_payload(c, provider_ref, config,
                                           icebreaker_fn=icebreaker_fn, hook=hook)
            res = conn.execute(
                digest_item.insert().values(
                    kind="enroll", channel="email", person_id=person_id,
                    campaign_id=campaign_id, payload=payload, status="draft",
                    digest_date=today, actor=actor, workspace_id=workspace_id,
                )
            )
            drafts.append(res.inserted_primary_key[0])
    return drafts


def recopy_drafts(engine: Engine, config: dict, *, icebreaker_fn=None,
                  workspace_id: str = "default") -> list[int]:
    """Rebuild the copy on every PENDING draft in place — no new flow_state, no
    provider spend. Reconstructs each candidate's evidence from the enrichment
    store (person.company_id -> company.enrichment) so the LLM copy step has the
    same signals draft_run had, then reruns build_enroll_payload and overwrites
    payload. Used to upgrade drafts created before the copy step changed."""
    provider_ref = config["campaign"].get("provider_ref")
    with engine.connect() as conn:
        rows = conn.execute(
            select(digest_item.c.item_no, digest_item.c.person_id, digest_item.c.payload)
            .where(digest_item.c.workspace_id == workspace_id,
                   digest_item.c.kind == "enroll", digest_item.c.status == "draft")
        ).all()
    updated: list[int] = []
    for item_no, person_id, payload in rows:
        # one transaction per item: the LLM call is slow, so don't hold a DB txn
        # open across it, and let partial progress survive a mid-run stop.
        with engine.begin() as conn:
            candidate = _rebuild_candidate(conn, person_id, payload, workspace_id)
            if candidate is None:
                continue
            hook = top_hook(conn, person_id=person_id,
                            domain=candidate.get("company_domain"), workspace_id=workspace_id)
        fresh = build_enroll_payload(candidate, provider_ref, config,
                                     icebreaker_fn=icebreaker_fn, hook=hook)
        with engine.begin() as conn:
            conn.execute(digest_item.update()
                         .where(digest_item.c.item_no == item_no)
                         .values(payload=fresh))
        updated.append(item_no)
    return updated


def lint_payload(kind: str, payload: dict) -> list[str]:
    """Kind-aware copy lint for one draft payload. COLD enroll -> strict
    lint_copy on subject+icebreaker; WARM reply -> lint_warm on the body (links
    and a greeting are allowed there). Shared by the digest gate and lint-drafts."""
    payload = payload or {}
    if kind == "enroll":
        lead = payload.get("lead") or {}
        cf = lead.get("custom_fields") or {}
        return lint_copy(cf.get("subject_hook", ""), cf.get("icebreaker", ""),
                         lead.get("first_name", ""))
    if kind == "warm_reply":
        return lint_warm((payload.get("preview") or {}).get("body", ""))
    return []


def lint_pending_drafts(engine: Engine, workspace_id: str = "default") -> list[tuple]:
    """The gate: lint every PENDING draft's stored copy. Returns (item_no,
    [violations]) per offending draft. Used by `zerocrm lint-drafts`, the digest
    send gate, and the test suite so a bad line can't sit undetected in the queue."""
    out: list[tuple] = []
    with engine.connect() as conn:
        rows = conn.execute(
            select(digest_item.c.item_no, digest_item.c.kind, digest_item.c.payload)
            .where(digest_item.c.workspace_id == workspace_id,
                   digest_item.c.status == "draft")
        ).all()
    for item_no, kind, payload in rows:
        violations = lint_payload(kind, payload)
        if violations:
            out.append((item_no, violations))
    return out


def _rebuild_candidate(conn, person_id: str, payload: dict, workspace_id: str) -> dict | None:
    """The candidate dict build_enroll_payload expects, reassembled from stored
    person + company facts. The email comes from the draft's own lead (already
    verified at draft time)."""
    email = (payload.get("lead") or {}).get("email")
    if not email:
        return None
    prow = conn.execute(
        select(person_t.c.full_name, person_t.c.company_id)
        .where(person_t.c.id == person_id)
    ).first()
    if prow is None:
        return None
    full_name, company_id = prow
    name = company = domain = None
    ce: dict = {}
    if company_id:
        crow = conn.execute(
            select(company_t.c.name, company_t.c.domain, company_t.c.enrichment)
            .where(company_t.c.id == company_id)
        ).first()
        if crow:
            name, domain, ce = crow[0], crow[1], (crow[2] or {})
    company = name or (payload.get("lead") or {}).get("company_name")
    return {"fields": {"full_name": full_name or ""}, "company_name": company,
            "company_domain": domain, "company_enrichment": ce, "email": email}


def _acquire_live(apollo, verify, config: dict, finders: list | None = None) -> list[dict]:
    """Search -> ICP filter -> reveal firmographics (Apollo) -> resolve email
    across the waterfall (Apollo's own email, then each finder in order) -> the
    verify gate decides. Only a `send` decision becomes a candidate, so nothing
    unverified touches a warming domain. Finder credits are spent lazily: an
    already-verified Apollo email means the finders are never called."""
    raw = apollo.search_people(per_page=config.get("n", 25), **config.get("apollo_filters", {}))
    out: list[dict] = []
    for hit in icp_filter(raw, config):
        matched = _reveal(apollo, hit) or hit
        # dev-shop gate: reveal exposes company keywords/industry, so drop
        # marketing-led shops BEFORE spending finder/verify credits on them.
        if not _is_dev_shop(matched, config):
            continue
        accepted = _first_verified(matched, verify, finders or [])
        if accepted is not None:
            out.append(accepted)
    return out


def _is_dev_shop(matched: dict, config: dict) -> bool:
    """Keep a company only if its dev signal is at least as strong as its
    marketing signal (drops the 'digital marketing agency that also does web'
    leak). Unconfigured -> pass-through. Signal = keywords + industry + description."""
    dev = [k.lower() for k in config.get("dev_keywords", [])]
    if not dev:
        return True
    mkt = [k.lower() for k in config.get("marketing_keywords", [])]
    ce = matched.get("company_enrichment") or {}
    hay = " ".join(str(x).lower() for x in
                   ((ce.get("keywords") or []), ce.get("industry") or "", ce.get("description") or ""))
    dev_hits = sum(1 for k in dev if k in hay)
    mkt_hits = sum(1 for k in mkt if k in hay)
    return dev_hits >= 1 and dev_hits >= mkt_hits


def _reveal(apollo, hit: dict) -> dict | None:
    """Apollo enrich for firmographics (and its own email guess). Reveal by id
    when present (search omits the domain), else by name + company domain."""
    if hit.get("apollo_id"):
        return apollo.match(apollo_id=hit["apollo_id"])
    name = (hit["fields"].get("full_name") or "").split()
    return apollo.match(
        first_name=name[0] if name else "",
        last_name=" ".join(name[1:]) if len(name) > 1 else "",
        domain=hit.get("company_domain") or "",
    )


def _first_verified(matched: dict, verify, finders: list) -> dict | None:
    for source, email in _email_sources(matched, finders):
        decision, evidence = verify.check(email)
        if decision == "send":
            out = dict(matched)
            out["email"] = email
            out["_email_source"] = source
            out["_verify"] = evidence   # carried so draft_run stamps verified_at
            return out
    return None


def _email_sources(matched: dict, finders: list):
    """Yield (source, email) candidates in waterfall order: Apollo's own email
    first (free — already revealed), then each finder. Lazy, so a finder is only
    called when every cheaper tier missed or failed the gate."""
    if matched.get("email"):
        yield "apollo", matched["email"].lower()
    name = (matched.get("fields", {}).get("full_name") or "").split()
    first = name[0] if name else ""
    last = " ".join(name[1:]) if len(name) > 1 else ""
    domain = matched.get("company_domain") or ""
    linkedin = matched.get("linkedin") or ""
    for f in finders:
        email = f.find(first, last, domain, linkedin)
        if email:
            yield type(f).__name__.lower(), email.lower()


def icp_filter(people: list[dict], config: dict) -> list[dict]:
    """Phase-1 ICP filter: title keyword allowlist if configured, else pass-through.
    (Phase 2 replaces this with icp_score + routing rules.)"""
    kw = [k.lower() for k in config.get("title_keywords", [])]
    if not kw:
        return people
    return [p for p in people
            if any(k in (p["fields"].get("job_title") or "").lower() for k in kw)]


def ensure_campaign(engine: Engine, spec: dict, actor: dict, workspace_id: str) -> tuple[str, str | None]:
    with engine.begin() as conn:
        row = conn.execute(
            select(campaign_t.c.id, campaign_t.c.provider_ref).where(
                campaign_t.c.workspace_id == workspace_id,
                campaign_t.c.name == spec["name"],
            )
        ).first()
        if row:
            return row[0], row[1]
        res = conn.execute(
            campaign_t.insert().values(
                name=spec["name"], channel=spec.get("channel", "email"),
                flow_kind=spec.get("flow_kind", "cold"),
                provider_ref=spec.get("provider_ref"), sender_ref=spec.get("sender_ref"),
                status="active", actor=actor, workspace_id=workspace_id,
            )
        )
        return res.inserted_primary_key[0], spec.get("provider_ref")


def build_enroll_payload(candidate: dict, provider_ref: str | None, config: dict,
                         icebreaker_fn=None, hook: dict | None = None) -> dict:
    """Per-lead enrollment. The 4-touch copy is the CAMPAIGN sequence (copy.py,
    set once via smartlead.set_sequence); here we compute the per-lead
    {{icebreaker}} (from the research hook) + a rendered touch-1 preview for the
    digest. icebreaker_fn is the LLM/dogfood seam for voice-perfect per-lead copy."""
    name = candidate["fields"].get("full_name") or ""
    parts = name.split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""
    company = clean_company(candidate.get("company_name"))
    # LLM seam (copy_llm) returns {"icebreaker","subject"} from ONE call (so they
    # cite the same evidence). A dict is also accepted from injected fakes; a bare
    # str is the legacy icebreaker-only shape. Any miss/error -> the floor.
    icebreaker = subj = None
    if icebreaker_fn is not None:
        gen = icebreaker_fn(hook, first, company, candidate)
        if isinstance(gen, dict):
            icebreaker, subj = gen.get("icebreaker"), gen.get("subject")
        elif isinstance(gen, str):
            icebreaker = gen
    if not icebreaker:
        icebreaker = icebreaker_from_hook(hook, first, company, candidate)
    if not subj:
        subj = subject_hook(candidate, company)
    # last-line defense: whatever the source, a lint failure drops BOTH to the
    # deterministic floor (clean by construction) so no bad pair is ever stored.
    if lint_copy(subj, icebreaker, first):
        icebreaker = icebreaker_from_hook(hook, first, company, candidate)
        subj = subject_hook(candidate, company)
    lead = {"email": candidate["email"], "first_name": first, "last_name": last,
            "company_name": company,
            "custom_fields": {"icebreaker": icebreaker, "subject_hook": subj}}
    preview = render_preview(default_sequence(config)[0],
                             {"first_name": first, "company_name": company,
                              "icebreaker": icebreaker, "subject_hook": subj})
    payload = {
        "summary": f"{name or candidate['email']} @ {company} · {preview['subject']}",
        "campaign_ref": provider_ref or "dry",
        "sequence_ref": config["campaign"]["name"],
        "lead": lead,
        "preview": preview,   # touch 1, merges filled — what the digest shows
    }
    if hook:
        payload["hook"] = hook  # rendered as the digest "why" line, cites the source
    return payload


def _already_in_flow(conn, person_id: str, campaign_id: str, workspace_id: str) -> bool:
    row = conn.execute(
        select(flow_state.c.id).where(
            flow_state.c.workspace_id == workspace_id,
            flow_state.c.person_id == person_id,
            flow_state.c.campaign_id == campaign_id,
        )
    ).first()
    return row is not None
