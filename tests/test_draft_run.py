"""draft_run pipeline tests (seeded — no live Apollo)."""

from sqlalchemy import func, select

from zerocrm.draft_run import draft_run, icp_filter
from zerocrm.schema import campaign as campaign_t
from zerocrm.schema import digest_item, flow_state, person

ACTOR = {"kind": "agent", "id": "draft-run"}
CONFIG = {"campaign": {"name": "cold-founders-q3", "flow_kind": "cold"}}

SEED = [
    {"email": "jane@acme.com",
     "fields": {"full_name": "Jane Doe", "job_title": "Founder", "source": "apollo"},
     "company_name": "Acme", "company_domain": "acme.com"},
    {"email": "bob@beta.io",
     "fields": {"full_name": "Bob Lin", "job_title": "CEO", "source": "apollo"},
     "company_name": "Beta", "company_domain": "beta.io"},
]


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_draft_run_creates_people_campaign_flows_drafts(engine):
    drafts = draft_run(engine, CONFIG, ACTOR, seed_people=SEED)
    assert len(drafts) == 2
    assert _count(engine, person) == 2
    assert _count(engine, campaign_t) == 1
    assert _count(engine, flow_state) == 2
    with engine.connect() as conn:
        items = conn.execute(select(digest_item)).mappings().all()
    assert all(it["status"] == "draft" and it["kind"] == "enroll" for it in items)
    # payload carries the sequence copy + a lead for Smartlead enrollment
    p0 = items[0]["payload"]
    assert p0["lead"]["email"] in ("jane@acme.com", "bob@beta.io")
    assert p0["lead"]["custom_fields"]["icebreaker"]          # per-lead personalization slot
    assert p0["preview"]["subject"] and p0["preview"]["body"]  # rendered touch 1
    assert "—" not in p0["preview"]["body"]                    # no em dashes (voice/anti-slop)


def test_draft_run_is_idempotent(engine):
    draft_run(engine, CONFIG, ACTOR, seed_people=SEED)
    again = draft_run(engine, CONFIG, ACTOR, seed_people=SEED)
    assert again == []                     # nobody re-enrolled
    assert _count(engine, flow_state) == 2  # no duplicate flows
    assert _count(engine, digest_item) == 2


def test_draft_run_skips_no_email(engine):
    seed = SEED + [{"email": None, "fields": {"full_name": "No Email"}}]
    drafts = draft_run(engine, CONFIG, ACTOR, seed_people=seed)
    assert len(drafts) == 2


def test_icp_filter_by_title_keyword():
    people = [
        {"fields": {"job_title": "Founder"}},
        {"fields": {"job_title": "Sales Intern"}},
        {"fields": {"job_title": None}},
    ]
    kept = icp_filter(people, {"title_keywords": ["founder", "ceo"]})
    assert len(kept) == 1


def test_icp_filter_passthrough_when_unconfigured():
    people = [{"fields": {"job_title": "anything"}}]
    assert icp_filter(people, {}) == people


def test_recopy_rebuilds_drafts_in_place_from_stored_evidence(engine):
    from zerocrm.draft_run import recopy_drafts
    seed = [{"email": "jane@acme.com",
             "fields": {"full_name": "Jane Doe", "job_title": "Founder", "source": "apollo"},
             "company_name": "Acme", "company_domain": "acme.com",
             "company_enrichment": {"industry": "Software",
                                    "github": {"found": True, "public_repos": 9,
                                               "top_languages": ["Go"]}}}]
    draft_run(engine, CONFIG, ACTOR, seed_people=seed)

    seen = {}
    def fake_fn(hook, first, company, candidate=None):
        seen["candidate"] = candidate
        return "voice line about go repos"

    updated = recopy_drafts(engine, CONFIG, icebreaker_fn=fake_fn)
    assert len(updated) == 1
    # the fn was handed the reconstructed evidence, not an empty candidate
    assert seen["candidate"]["company_enrichment"]["github"]["public_repos"] == 9
    assert seen["candidate"]["email"] == "jane@acme.com"
    # payload overwritten in place; no new items/flows created
    assert _count(engine, digest_item) == 1
    assert _count(engine, flow_state) == 1
    with engine.connect() as conn:
        p = conn.execute(select(digest_item.c.payload)).scalar_one()
    assert p["lead"]["custom_fields"]["icebreaker"] == "voice line about go repos"


def test_recopy_falls_back_to_floor_when_no_fn(engine):
    from zerocrm.draft_run import recopy_drafts
    seed = [{"email": "bob@beta.io",
             "fields": {"full_name": "Bob Lin", "job_title": "CEO", "source": "apollo"},
             "company_name": "Beta", "company_domain": "beta.io",
             "company_enrichment": {"technologies": ["Next.js"]}}]
    draft_run(engine, CONFIG, ACTOR, seed_people=seed)
    updated = recopy_drafts(engine, CONFIG, icebreaker_fn=None)
    assert len(updated) == 1
    with engine.connect() as conn:
        p = conn.execute(select(digest_item.c.payload)).scalar_one()
    assert "Next.js" in p["lead"]["custom_fields"]["icebreaker"]  # enrichment floor
