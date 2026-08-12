"""Research briefs: normalize/upsert/TTL/dedup + the draft-hook + digest "why" seam."""

from sqlalchemy import func, select

from zerocrm.digest import render
from zerocrm.draft_run import draft_run
from zerocrm.research import (
    get_fresh, normalize_domain, top_hook, upsert_research,
)
from zerocrm.schema import digest_item
from zerocrm.schema import research as research_t

ACTOR = {"kind": "agent", "id": "research"}


# --- normalize_domain -------------------------------------------------------
def test_normalize_domain():
    assert normalize_domain("https://www.Acme.com/about") == "acme.com"
    assert normalize_domain("ACME.com:443") == "acme.com"
    assert normalize_domain("acme.com") == "acme.com"
    assert normalize_domain("") == ""


# --- upsert / freshness / dedup ---------------------------------------------
def _brief(text="hiring 3 SDRs"):
    return {"summary": "s", "hooks": [{"text": text, "evidence_url": "x", "recency_days": 4}]}


def test_upsert_inserts_then_updates_in_place(engine):
    rid = upsert_research(engine, scope="company", subject_key="acme.com",
                          brief=_brief("v1"), actor=ACTOR)
    rid2 = upsert_research(engine, scope="company", subject_key="acme.com",
                           brief=_brief("v2"), actor=ACTOR)
    assert rid == rid2  # same row, overwritten
    with engine.connect() as c:
        assert c.execute(select(func.count()).select_from(research_t)).scalar_one() == 1
    assert get_fresh(engine, "company", "acme.com")["hooks"][0]["text"] == "v2"


def test_get_fresh_returns_none_when_stale(engine):
    upsert_research(engine, scope="person", subject_key="p1", brief=_brief(),
                    ttl_days=-1, actor=ACTOR)  # already expired
    assert get_fresh(engine, "person", "p1") is None


def test_get_fresh_none_when_missing(engine):
    assert get_fresh(engine, "company", "nope.com") is None


def test_top_hook_prefers_person_then_company(engine):
    upsert_research(engine, scope="company", subject_key="acme.com",
                    brief=_brief("company hook"), actor=ACTOR)
    # no person brief yet -> falls through to company
    assert top_hook(engine, person_id="p1", domain="www.acme.com")["text"] == "company hook"
    upsert_research(engine, scope="person", subject_key="p1",
                    brief=_brief("person hook"), actor=ACTOR)
    assert top_hook(engine, person_id="p1", domain="acme.com")["text"] == "person hook"


def test_top_hook_none_when_nothing_fresh(engine):
    assert top_hook(engine, person_id="p1", domain="acme.com") is None


# --- draft_run attaches the hook; digest renders the "why" line -------------
SEED = [{"email": "jane@acme.com",
         "fields": {"full_name": "Jane Doe", "job_title": "Founder", "source": "apollo"},
         "company_name": "Acme", "company_domain": "acme.com"}]
CONFIG = {"campaign": {"name": "cold", "flow_kind": "cold"}}


def test_draft_run_attaches_research_hook_and_digest_shows_why(engine):
    upsert_research(engine, scope="company", subject_key="acme.com",
                    brief=_brief("shipping 3 SDR roles"), actor=ACTOR)
    draft_run(engine, CONFIG, ACTOR, seed_people=SEED)
    with engine.connect() as c:
        item = c.execute(select(digest_item)).mappings().one()
    assert item["payload"]["hook"]["text"] == "shipping 3 SDR roles"
    body = render([dict(item)])
    assert "why: shipping 3 SDR roles" in body and "4d ago" in body


def test_draft_run_no_hook_when_no_research(engine):
    draft_run(engine, CONFIG, ACTOR, seed_people=SEED)
    with engine.connect() as c:
        item = c.execute(select(digest_item)).mappings().one()
    assert "hook" not in item["payload"]
    assert "why:" not in render([dict(item)])


def test_draft_run_uses_custom_icebreaker_fn(engine):
    seen = {}
    def ib_fn(hook, first, company, candidate=None):
        seen["hook"] = hook
        return "custom opener line."
    upsert_research(engine, scope="company", subject_key="acme.com",
                    brief=_brief("hooktext"), actor=ACTOR)
    draft_run(engine, CONFIG, ACTOR, seed_people=SEED, icebreaker_fn=ib_fn)
    with engine.connect() as c:
        item = c.execute(select(digest_item)).mappings().one()
    assert item["payload"]["lead"]["custom_fields"]["icebreaker"] == "custom opener line."
    assert "custom opener line." in item["payload"]["preview"]["body"]
    assert seen["hook"]["text"] == "hooktext"  # icebreaker_fn received the hook
