"""Driver unit tests against httpx.MockTransport + ingest integration.
No live network — the same driver code runs live against Doppler keys."""

import httpx
import pytest
from sqlalchemy import select

from zerocrm.drivers import Apollo, Smartlead, ZeroBounce
from zerocrm.drivers.zerobounce import classify
from zerocrm.ingest import import_apollo_people, mark_verified
from zerocrm.schema import company, person, person_identity


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- Apollo ----------------------------------------------------------------
def test_apollo_search_normalizes():
    def handler(req):
        assert req.headers["X-Api-Key"] == "k"
        return httpx.Response(200, json={"people": [
            {"first_name": "Jane", "last_name": "Doe", "title": "Founder",
             "email": "Jane@Acme.com", "linkedin_url": "li/jane",
             "organization": {"name": "Acme", "primary_domain": "acme.com"}},
            {"name": "No Email", "title": "CTO", "organization": {}},
        ]})
    a = Apollo(api_key="k", client=_client(handler))
    people = a.search_people(person_titles=["Founder"])
    assert people[0]["email"] == "jane@acme.com"          # lowercased
    assert people[0]["fields"]["full_name"] == "Jane Doe"
    assert people[0]["company_domain"] == "acme.com"
    assert people[1]["email"] is None


def test_apollo_match_reveals_email():
    def handler(req):
        assert req.url.path.endswith("/people/match")
        body = req.read()
        assert b"reveal_personal_emails" in body
        return httpx.Response(200, json={"person": {
            "name": "Jane Doe", "title": "Founder", "email": "jane@acme.com",
            "organization": {"name": "Acme", "primary_domain": "acme.com"}}})
    a = Apollo(api_key="k", client=_client(handler))
    p = a.match(first_name="Jane", last_name="Doe", domain="acme.com")
    assert p["email"] == "jane@acme.com"


# --- ZeroBounce ------------------------------------------------------------
def test_zerobounce_validate_and_credits():
    def handler(req):
        if req.url.path.endswith("/validate"):
            assert req.url.params["email"] == "a@x.com"
            return httpx.Response(200, json={"status": "valid", "sub_status": ""})
        return httpx.Response(200, json={"Credits": "5100"})
    z = ZeroBounce(api_key="k", client=_client(handler))
    assert z.validate("a@x.com")["status"] == "valid"
    assert z.credits() == 5100


def test_zerobounce_classify():
    assert classify({"status": "valid"}) == "send"
    assert classify({"status": "invalid"}) == "block"
    assert classify({"status": "catch-all"}) == "risky"


# --- Smartlead -------------------------------------------------------------
def test_smartlead_list_accounts_and_warmup():
    def handler(req):
        if req.url.path.endswith("/email-accounts/"):
            return httpx.Response(200, json=[{"id": 21823640, "from_email": "operator@example.com"}])
        if "warmup-stats" in req.url.path:
            return httpx.Response(200, json={"sent_count": "44", "spam_count": "0"})
        return httpx.Response(404)
    s = Smartlead(api_key="k", client=_client(handler))
    assert s.list_email_accounts()[0]["from_email"] == "operator@example.com"
    assert s.warmup_stats(21823640)["spam_count"] == "0"


def test_smartlead_send_enrolls():
    calls = {}
    def handler(req):
        calls["path"] = req.url.path
        return httpx.Response(200, json={"upload_count": 1})
    s = Smartlead(api_key="k", client=_client(handler))
    item = {"item_no": 1, "payload": {"campaign_ref": "77", "lead": {"email": "a@x.com"}}}
    assert s.send(item) == "1"
    assert calls["path"].endswith("/campaigns/77/leads")


def test_smartlead_already_sent_checks_membership():
    def handler(req):
        return httpx.Response(200, json={"campaign_ids": [77, 88]})
    s = Smartlead(api_key="k", client=_client(handler))
    item = {"item_no": 1, "payload": {"campaign_ref": 77, "lead": {"email": "a@x.com"}}}
    assert s.already_sent(item) is True
    item["payload"]["campaign_ref"] = 99
    assert s.already_sent(item) is False


def test_smartlead_normalize_event_reply_bounce_only():
    r = Smartlead.normalize_event({"event_type": "EMAIL_REPLY", "to_email": "A@X.com",
                                   "campaign_id": 77, "message_id": "m1"})
    assert r["kind"] == "reply" and r["direction"] == "in" and r["event_key"] == "m1"
    assert Smartlead.normalize_event({"event_type": "EMAIL_BOUNCE", "lead_email": "a@x.com"})["direction"] == "in"
    # KISS: opens/clicks/sends are dropped at the normalizer
    assert Smartlead.normalize_event({"event_type": "EMAIL_OPEN"}) is None
    assert Smartlead.normalize_event({"event_type": "EMAIL_CLICK"}) is None
    assert Smartlead.normalize_event({"event_type": "UNKNOWN"}) is None
    # deterministic fallback event_key when no message id
    ev = Smartlead.normalize_event({"event_type": "EMAIL_REPLY", "lead_email": "b@x.com", "time": "2026-08-08"})
    assert ev["event_key"] == "b@x.com:reply:2026-08-08"


def test_smartlead_poll_events_normalizes():
    def handler(req):
        assert "leads-statistics" in req.url.path
        # Live 2026-10-02: Smartlead 400s `"event_time_lt" is not allowed` — lower bound only.
        assert req.url.params["event_time_gt"] == "2026-08-01"
        assert "event_time_lt" not in req.url.params
        return httpx.Response(200, json={"data": [
            {"lead_email": "a@x.com", "replied_at": "2026-08-08T10:00:00Z", "message_id": "m9"},
            {"lead_email": "b@x.com", "bounced_at": "2026-08-08T11:00:00Z"},
            {"lead_email": "c@x.com"},  # no reply/bounce -> ignored
        ]})
    s = Smartlead(api_key="k", client=_client(handler))
    events = s.poll_events(77, "2026-08-01")
    kinds = sorted(e["kind"] for e in events)
    assert kinds == ["bounce", "reply"]



def test_smartlead_poll_events_reads_every_page():
    offsets = []

    def handler(req):
        off = int(req.url.params.get("offset", "0"))
        offsets.append(off)
        row = ({"lead_email": "a@x.com", "replied_at": "2026-08-08T10:00:00Z"} if off == 0
               else {"lead_email": "b@x.com", "bounced_at": "2026-08-08T11:00:00Z"})
        return httpx.Response(200, json={"hasMore": off == 0, "data": [row], "skip": off, "limit": 100})
    s = Smartlead(api_key="k", client=_client(handler))
    assert sorted(e["kind"] for e in s.poll_events(77, "2026-08-01")) == ["bounce", "reply"]
    assert offsets == [0, 100]

# --- ingest integration ----------------------------------------------------
def test_import_apollo_people_through_precedence(engine):
    people = [
        {"email": "jane@acme.com",
         "fields": {"full_name": "Jane Doe", "job_title": "Founder", "source": "apollo"},
         "linkedin": "li/jane", "company_domain": "acme.com", "company_name": "Acme"},
        {"email": None, "fields": {"full_name": "No Email"}},
    ]
    res = import_apollo_people(engine, people, actor={"kind": "agent", "id": "apollo"})
    assert res == {"imported": 1, "skipped_no_email": 1}
    with engine.connect() as conn:
        assert conn.execute(select(person.c.full_name)).scalar_one() == "Jane Doe"
        assert conn.execute(select(company.c.domain)).scalar_one() == "acme.com"
        kinds = {r[0] for r in conn.execute(select(person_identity.c.kind))}
    assert kinds == {"email", "linkedin"}


def test_mark_verified_stamps_identity(engine):
    import_apollo_people(engine, [
        {"email": "jane@acme.com", "fields": {"source": "apollo"}}
    ], actor={"kind": "agent", "id": "apollo"})
    verdict = mark_verified(engine, "jane@acme.com", {"status": "valid"},
                            actor={"kind": "agent", "id": "zb"})
    assert verdict == "send"
    with engine.connect() as conn:
        va = conn.execute(
            select(person_identity.c.verified_at).where(person_identity.c.kind == "email")
        ).scalar_one()
    assert va is not None


def test_import_stores_enrichment(engine):
    from zerocrm.schema import company
    import_apollo_people(engine, [{
        "email": "jane@acme.com",
        "fields": {"full_name": "Jane", "job_title": "Founder", "source": "apollo"},
        "company_domain": "acme.com", "company_name": "Acme",
        "enrichment": {"headline": "we build web apps", "seniority": "founder", "city": "SF"},
        "company_enrichment": {"industry": "software", "employees": 8,
                               "website": "http://acme.com", "keywords": ["web development"]},
    }], actor={"kind": "agent", "id": "t"})
    with engine.connect() as c:
        pe = c.execute(select(person.c.enrichment)).scalar_one()
        co = c.execute(select(company.c.industry, company.c.size, company.c.enrichment)).mappings().one()
    assert pe["headline"] == "we build web apps" and pe["seniority"] == "founder"
    assert co["industry"] == "software" and co["size"] == "8"
    assert co["enrichment"]["website"] == "http://acme.com"


def test_enrich_github_stores_signal(engine):
    from zerocrm.ingest import enrich_github
    from zerocrm.schema import company
    import_apollo_people(engine, [{
        "email": "j@acme.com", "fields": {"source": "apollo"},
        "company_domain": "acme.com", "company_name": "Acme",
        "company_enrichment": {"industry": "software"},
    }], actor={"kind": "agent", "id": "t"})
    fake = lambda name, domain: {"found": True, "login": "acme", "public_repos": 12,
                                 "top_languages": ["TypeScript", "Go"], "confidence": "high"}
    res = enrich_github(engine, probe=fake)
    assert res == {"checked": 1, "github_found": 1}
    with engine.connect() as c:
        gh = c.execute(select(company.c.enrichment)).scalar_one()["github"]
    assert gh["public_repos"] == 12 and gh["top_languages"] == ["TypeScript", "Go"]


def test_mark_verified_block_does_not_stamp(engine):
    import_apollo_people(engine, [
        {"email": "bad@x.com", "fields": {"source": "apollo"}}
    ], actor={"kind": "agent", "id": "apollo"})
    assert mark_verified(engine, "bad@x.com", {"status": "invalid"},
                         actor={"kind": "agent", "id": "zb"}) == "block"
