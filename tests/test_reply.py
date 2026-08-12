"""Prospect-reply handling: classify, warm-reply draft, urgent forward, opt-out."""

from sqlalchemy import func, select

from zerocrm.ingest import import_apollo_people, record_provider_event
from zerocrm.reply import classify_reply, handle_reply, process_reply_forwards
from zerocrm.schema import blocklist, digest_item

ACTOR = {"kind": "agent", "id": "t"}


def _seed_person(engine, email="jane@acme.com", name="Jane Doe"):
    import_apollo_people(engine, [{"email": email,
                                   "fields": {"full_name": name, "source": "apollo"}}], ACTOR)


def test_classify_reply():
    assert classify_reply("yes, interested, tell me more") == "interested"
    assert classify_reply("not interested, thanks") == "not_interested"   # neg before pos
    assert classify_reply("please unsubscribe me") == "unsubscribe"
    assert classify_reply("who is this?") == "neutral"


def test_interested_creates_warm_reply_and_forwards(engine):
    _seed_person(engine)
    sent = {}
    def fake(subject, body, to=None, sender=None, html=None):
        sent.update(subject=subject, html=html)
    v = handle_reply(engine, {"email": "jane@acme.com", "event_key": "e1",
                              "message_id": "m1", "body": "yes interested, send it"},
                     {"digest_to": "sid@syndai.ai", "action_base_url": "https://w",
                      "booking_url": "https://cal/x"}, sender=fake)
    assert v == "interested"
    with engine.connect() as c:
        item = c.execute(select(digest_item).where(digest_item.c.kind == "warm_reply")).mappings().one()
    assert item["payload"]["in_reply_to"] == "m1"        # threads onto the reply
    assert "https://cal/x" in item["payload"]["preview"]["body"]  # booking link in draft
    assert "interested reply" in sent["subject"].lower() and sent["html"]  # urgent forward w/ buttons


def test_unsubscribe_blocklists(engine):
    v = handle_reply(engine, {"email": "bob@x.com", "event_key": "e2", "body": "unsubscribe"},
                     {}, sender=lambda *a, **k: None)
    assert v == "unsubscribe"
    with engine.connect() as c:
        bl = c.execute(select(blocklist.c.granularity, blocklist.c.reason)).mappings().all()
    assert any(b["granularity"] == "identity" and b["reason"] == "opt_out" for b in bl)


def test_handle_reply_idempotent(engine):
    _seed_person(engine)
    ev = {"email": "jane@acme.com", "event_key": "e9", "body": "interested"}
    handle_reply(engine, ev, {}, sender=lambda *a, **k: None)
    handle_reply(engine, ev, {}, sender=lambda *a, **k: None)   # same event again
    with engine.connect() as c:
        n = c.execute(select(func.count()).select_from(digest_item)
                      .where(digest_item.c.kind == "warm_reply")).scalar_one()
    assert n == 1


def test_process_reply_forwards_urgent_and_idempotent(engine):
    _seed_person(engine)
    record_provider_event(engine, {"email": "jane@acme.com", "event_key": "r1",
                                   "kind": "reply", "raw": {}}, ACTOR)
    sent = []
    fwd = lambda subject, body, to=None, sender=None, html=None: sent.append(subject)
    out = process_reply_forwards(engine, {"digest_to": "sid@syndai.ai"}, sender=fwd)
    assert out["forwarded"] == 1 and len(sent) == 1
    out2 = process_reply_forwards(engine, {"digest_to": "sid@syndai.ai"}, sender=fwd)
    assert out2["forwarded"] == 0                # deduped on event_key
