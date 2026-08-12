"""Inbound event ingestion: exactly-once touches + bounce blocklisting.
The inbound analog of the executor kill-test."""

from sqlalchemy import func, select

from zerocrm.ingest import import_apollo_people, record_provider_event
from zerocrm.schema import blocklist, person, touch

ACTOR = {"kind": "agent", "id": "poller"}


def _seed(engine, email="jane@acme.com"):
    import_apollo_people(engine, [{"email": email, "fields": {"source": "apollo"}}], ACTOR)


def _reply(email="jane@acme.com", key="m1"):
    return {"kind": "reply", "email": email, "direction": "in", "event_key": key,
            "campaign_ref": None, "raw": {"body": "interested"}}


def _count(engine, table):
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(table)).scalar_one()


def test_reply_ingested_exactly_once(engine):
    _seed(engine)
    assert record_provider_event(engine, _reply(), ACTOR) == "inserted"
    assert record_provider_event(engine, _reply(), ACTOR) == "duplicate"  # redelivery
    assert _count(engine, touch) == 1


def test_reply_from_unknown_sender_creates_person(engine):
    # nobody seeded — a reply from someone not yet in the CRM still lands
    assert record_provider_event(engine, _reply("stranger@x.com", "m2"), ACTOR) == "inserted"
    assert _count(engine, person) == 1
    assert _count(engine, touch) == 1


def test_bounce_blocklists_identity_once(engine):
    _seed(engine, "bounce@x.com")
    ev = {"kind": "bounce", "email": "bounce@x.com", "direction": "in",
          "event_key": "b1", "campaign_ref": None, "raw": {"reason": "550"}}
    assert record_provider_event(engine, ev, ACTOR) == "inserted"
    with engine.connect() as c:
        bl = c.execute(select(blocklist.c.granularity, blocklist.c.value, blocklist.c.reason)).one()
    assert bl == ("identity", "bounce@x.com", "hard_bounce")
    # redelivery: no dup touch, no dup blocklist
    assert record_provider_event(engine, ev, ACTOR) == "duplicate"
    assert _count(engine, touch) == 1 and _count(engine, blocklist) == 1


def test_two_distinct_events_two_touches(engine):
    _seed(engine)
    assert record_provider_event(engine, _reply(key="m1"), ACTOR) == "inserted"
    assert record_provider_event(engine, _reply(key="m2"), ACTOR) == "inserted"
    assert _count(engine, touch) == 2


def test_event_needs_email_and_key(engine):
    import pytest
    with pytest.raises(ValueError):
        record_provider_event(engine, {"kind": "reply", "email": "", "event_key": ""}, ACTOR)
