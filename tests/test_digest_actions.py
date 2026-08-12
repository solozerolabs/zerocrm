"""HTML digest (rich cards + one-tap buttons), the dev-shop ICP gate, and the
apply->autonomy wiring."""

from sqlalchemy import select

from zerocrm.digest import apply_decisions, render_html
from zerocrm.draft_run import _is_dev_shop
from zerocrm.schema import autonomy


# --- HTML digest ------------------------------------------------------------
def test_render_html_shows_info_and_buttons():
    cards = [{
        "item": {"item_no": 3, "kind": "enroll", "channel": "email",
                 "payload": {"preview": {"subject": "quick one", "body": "hi"},
                             "hook": {"text": "hiring SDRs", "recency_days": 4},
                             "lead": {"email": "j@acme.com"}}},
        "person": {"full_name": "Jane Doe", "job_title": "Founder", "company_id": "c1",
                   "enrichment": {"city": "SF", "state": "CA"}},
        "company": {"name": "Acme", "domain": "acme.com", "size": "12",
                    "enrichment": {"technologies": ["OpenAI", "Next.js"],
                                   "github": {"found": True, "public_repos": 9,
                                              "top_languages": ["Go"], "confidence": "high"}}},
    }]
    html = render_html(cards, base_url="https://w.fly.dev", notices=["warmup done"])
    # prospect info the operator judges on
    assert "Jane Doe" in html and "Founder" in html and "Acme" in html
    assert "12 ppl" in html and "SF, CA" in html
    assert "OpenAI" in html and "GitHub: 9 repos" in html
    assert "why:</b> hiring SDRs" in html
    assert "quick one" in html            # the drafted copy is shown
    # one-tap buttons to the endpoint
    assert "/act?item=3&action=ok" in html and "action=skip" in html
    assert "warmup done" in html          # notice surfaced


def test_render_html_escapes_content():
    cards = [{"item": {"item_no": 1, "kind": "enroll", "channel": "email",
                       "payload": {"preview": {"subject": "<script>x</script>", "body": "b"}}},
              "person": {"full_name": "A&B", "company_id": None}, "company": {}}]
    html = render_html(cards, base_url="https://w")
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html
    assert "A&amp;B" in html


# --- dev-shop ICP gate ------------------------------------------------------
def test_is_dev_shop_keeps_dev_drops_marketing():
    cfg = {"dev_keywords": ["software development", "web development", "app development"],
           "marketing_keywords": ["marketing", "seo", "advertising", "branding"]}
    dev = {"company_enrichment": {"keywords": ["software development", "web development"],
                                  "industry": "software"}}
    mkt = {"company_enrichment": {"keywords": ["digital marketing", "seo", "branding"],
                                  "industry": "marketing"}}
    tie_dev_wins = {"company_enrichment": {"keywords": ["web development",
                                                        "software development", "marketing"]}}
    assert _is_dev_shop(dev, cfg) is True
    assert _is_dev_shop(mkt, cfg) is False
    assert _is_dev_shop(tie_dev_wins, cfg) is True   # dev_hits 2 >= mkt_hits 1
    assert _is_dev_shop({}, {}) is True              # unconfigured -> pass-through


# --- apply feeds the autonomy curve (button + reply share this path) --------
def test_apply_decisions_feeds_autonomy(engine, mk_item):
    n = mk_item(status="draft", channel="email")
    apply_decisions(engine, {n: {"action": "approve"}}, actor={"kind": "human", "id": "t"})
    with engine.connect() as c:
        r = c.execute(select(autonomy.c.level, autonomy.c.streak)
                      .where(autonomy.c.channel == "email")).first()
    assert r == ("approve_all", 1)
