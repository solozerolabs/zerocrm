"""Runner tests: poll ingestion + the sending_enabled activation gate. Mocked —
no live network, no live IMAP/SMTP."""

import httpx
from sqlalchemy import select

from zerocrm.config import sending_enabled, set_config
from zerocrm.drivers.smartlead import Smartlead
from zerocrm.ingest import import_apollo_people
from zerocrm.runner import run_tick
from zerocrm.schema import digest_item, touch


def _smartlead(reply_rows=None, already_campaigns=None):
    """A Smartlead whose poll returns reply_rows and whose enroll/already_sent are mocked."""
    def handler(req):
        p = req.url.path
        if "leads-statistics" in p:
            return httpx.Response(200, json={"data": reply_rows or []})
        if p.endswith("/leads/"):  # already_sent lookup
            return httpx.Response(200, json={"campaign_ids": already_campaigns or []})
        if "/leads" in p:          # enroll
            return httpx.Response(200, json={"upload_count": 1})
        return httpx.Response(404)
    return Smartlead(api_key="k", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_sending_disabled_by_default(engine):
    assert sending_enabled(engine) is False


def test_tick_polls_and_ingests(engine, monkeypatch):
    monkeypatch.setattr("zerocrm.loop.fetch_replies", lambda mids: [])
    monkeypatch.setattr("zerocrm.reply.process_reply_forwards", lambda *a, **k: {"forwarded": 0})
    set_config(engine, "smartlead_poll_campaigns", [77])
    sl = _smartlead(reply_rows=[{"lead_email": "a@x.com", "replied_at": "2026-08-08T10:00:00Z",
                                 "message_id": "m1"}])
    out = run_tick(engine, smartlead=sl)
    assert out["poll"]["ingested"] == 1
    with engine.connect() as c:
        assert c.execute(select(touch.c.kind)).scalar_one() == "reply"


def test_tick_gates_execution_until_sending_enabled(engine, monkeypatch, mk_item):
    monkeypatch.setattr("zerocrm.loop.fetch_replies", lambda mids: [])
    monkeypatch.setattr("zerocrm.reply.process_reply_forwards", lambda *a, **k: {"forwarded": 0})
    n = mk_item(status="approved", person_id=None,
                payload={"summary": "x", "campaign_ref": "77", "lead": {"email": "a@x.com"}})
    sl = _smartlead()

    # disabled: approved item is NOT executed
    out = run_tick(engine, smartlead=sl)
    assert out["sending_enabled"] is False
    with engine.connect() as c:
        assert c.execute(select(digest_item.c.status).where(digest_item.c.item_no == n)).scalar_one() == "approved"

    # flip on: it drains
    set_config(engine, "sending_enabled", True)
    out = run_tick(engine, smartlead=sl)
    assert out["sending_enabled"] is True and out["replies"]["executed"] == 1
    with engine.connect() as c:
        assert c.execute(select(digest_item.c.status).where(digest_item.c.item_no == n)).scalar_one() == "sent"


def test_tick_poll_is_idempotent_across_ticks(engine, monkeypatch):
    monkeypatch.setattr("zerocrm.loop.fetch_replies", lambda mids: [])
    monkeypatch.setattr("zerocrm.reply.process_reply_forwards", lambda *a, **k: {"forwarded": 0})
    set_config(engine, "smartlead_poll_campaigns", [77])
    rows = [{"lead_email": "a@x.com", "replied_at": "2026-08-08T10:00:00Z", "message_id": "m1"}]
    run_tick(engine, smartlead=_smartlead(reply_rows=rows))
    out2 = run_tick(engine, smartlead=_smartlead(reply_rows=rows))  # same event again
    assert out2["poll"]["duplicate"] == 1 and out2["poll"]["ingested"] == 0
    with engine.connect() as c:
        from sqlalchemy import func
        assert c.execute(select(func.count()).select_from(touch)).scalar_one() == 1
