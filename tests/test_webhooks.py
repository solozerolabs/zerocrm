"""Webhook ingest: reply -> touch + warm-reply draft; bounce -> blocklist;
converges with the poll path (no double-processing)."""

from sqlalchemy import func, select

from zerocrm.ingest import import_apollo_people, record_provider_event
from zerocrm.reply import process_reply_forwards
from zerocrm.schema import blocklist, digest_item, touch
from zerocrm.webhooks import ingest_provider_webhook

ACTOR = {"kind": "agent", "id": "t"}
CFG = {"digest_to": "sid@syndai.ai", "booking_url": "https://book.syndai.ai"}


def test_reply_webhook_records_touch_and_drafts(engine):
    import_apollo_people(engine, [{"email": "jane@acme.com",
                                   "fields": {"full_name": "Jane Doe", "source": "apollo"}}], ACTOR)
    payload = {"event_type": "EMAIL_REPLY", "lead_email": "jane@acme.com",
               "message_id": "m1", "reply_body": "yes, interested, send it over"}
    res = ingest_provider_webhook(engine, "smartlead", payload, CFG,
                                  sender=lambda *a, **k: None)
    assert res["kind"] == "reply" and res["reply"] == "interested"
    with engine.connect() as c:
        assert c.execute(select(func.count()).select_from(touch)
                         .where(touch.c.kind == "reply")).scalar_one() == 1
        item = c.execute(select(digest_item).where(digest_item.c.kind == "warm_reply")).mappings().one()
    assert item["payload"]["in_reply_to"] == "m1"
    assert "book.syndai.ai" in item["payload"]["preview"]["body"]


def test_reply_webhook_uses_from_email_as_prospect(engine):
    # a real EMAIL_REPLY carries from_email=prospect, to_email=OUR mailbox, no lead_email
    import_apollo_people(engine, [{"email": "jane@acme.com", "fields": {"source": "apollo"}}], ACTOR)
    ingest_provider_webhook(engine, "smartlead",
                            {"event_type": "EMAIL_REPLY", "from_email": "jane@acme.com",
                             "to_email": "sid@trysyndai.com", "message_id": "mx",
                             "reply_body": "<p>interested</p>"}, CFG, sender=lambda *a, **k: None)
    with engine.connect() as c:
        item = c.execute(select(digest_item).where(digest_item.c.kind == "warm_reply")).mappings().one()
    assert item["payload"]["lead"]["email"] == "jane@acme.com"   # prospect, not our mailbox


def test_bounce_webhook_blocklists(engine):
    payload = {"event_type": "EMAIL_BOUNCE", "lead_email": "bob@x.com", "message_id": "b1"}
    ingest_provider_webhook(engine, "smartlead", payload, CFG)
    with engine.connect() as c:
        bl = c.execute(select(blocklist.c.value, blocklist.c.reason)).mappings().all()
    assert any(b["value"] == "bob@x.com" and b["reason"] == "hard_bounce" for b in bl)


def test_webhook_ignores_non_reply_bounce(engine):
    assert ingest_provider_webhook(engine, "smartlead",
                                   {"event_type": "EMAIL_OPEN", "lead_email": "a@x.com"}, CFG)["ignored"]


def test_webhook_then_poll_does_not_double_forward(engine):
    import_apollo_people(engine, [{"email": "jane@acme.com", "fields": {"source": "apollo"}}], ACTOR)
    # webhook drafts the interested reply
    ingest_provider_webhook(engine, "smartlead",
                            {"event_type": "EMAIL_REPLY", "lead_email": "jane@acme.com",
                             "message_id": "m9", "reply_body": "interested"}, CFG,
                            sender=lambda *a, **k: None)
    # poll later ingests the same reply as a touch (no body) ...
    record_provider_event(engine, {"email": "jane@acme.com", "event_key": "m9",
                                   "kind": "reply", "raw": {}}, ACTOR)
    sent = []
    out = process_reply_forwards(engine, CFG, sender=lambda *a, **k: sent.append(1))
    assert out["forwarded"] == 0 and sent == []   # webhook already handled event m9
