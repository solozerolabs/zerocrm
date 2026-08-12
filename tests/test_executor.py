"""Exactly-once executor tests. The kill-test is the one the review mandated:
a worker death between the provider send and the DB confirm must NOT produce a
second cold email on redelivery."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from zerocrm.executor import execute_item, sweep_stuck_claims
from zerocrm.schema import digest_item
from zerocrm.sender import FakeSender


def _status(engine, item_no):
    with engine.connect() as conn:
        return conn.execute(
            select(digest_item.c.status).where(digest_item.c.item_no == item_no)
        ).scalar_one()


def test_happy_path_sends_once(engine, mk_item):
    n = mk_item(status="approved")
    s = FakeSender()
    assert execute_item(engine, n, s, "w1") == "sent"
    assert s.sent == [n]
    assert _status(engine, n) == "sent"


def test_kill_between_send_and_confirm_sends_once(engine, mk_item):
    """CRITICAL (review D7): crash after provider send, before DB confirm."""
    n = mk_item(status="approved")
    s = FakeSender(crash_after_send=True)
    with pytest.raises(RuntimeError):
        execute_item(engine, n, s, "w1")     # claimed + sent, then crashed
    assert s.sent == [n]
    assert _status(engine, n) == "sending"   # stuck in claim

    # redelivery: same worker recovers, no longer crashing
    s.crash_after_send = False
    assert execute_item(engine, n, s, "w1") == "already_sent"
    assert s.sent == [n]                      # NOT re-sent
    assert _status(engine, n) == "sent"


def test_kill_before_send_then_redelivery_sends_once(engine, mk_item):
    """Crash after claim, before the provider send: redelivery must send once."""
    n = mk_item(status="approved")
    # simulate a claim that never got to send()
    with engine.begin() as conn:
        conn.execute(
            update(digest_item).where(digest_item.c.item_no == n)
            .values(status="sending", claim_token="w1", claimed_at=datetime.now(timezone.utc))
        )
    s = FakeSender()  # nothing sent yet
    assert execute_item(engine, n, s, "w1") == "sent"
    assert s.sent == [n]
    assert _status(engine, n) == "sent"


def test_other_worker_cannot_double_claim(engine, mk_item):
    n = mk_item(status="approved")
    with engine.begin() as conn:
        conn.execute(
            update(digest_item).where(digest_item.c.item_no == n)
            .values(status="sending", claim_token="w1", claimed_at=datetime.now(timezone.utc))
        )
    s = FakeSender()
    assert execute_item(engine, n, s, "w2") == "not_claimable"
    assert s.sent == []


def test_already_sent_item_not_resent(engine, mk_item):
    n = mk_item(status="sent")
    s = FakeSender()
    assert execute_item(engine, n, s, "w1") == "already_sent"
    assert s.sent == []


def test_draft_item_not_sendable(engine, mk_item):
    n = mk_item(status="draft")
    s = FakeSender()
    assert execute_item(engine, n, s, "w1") == "not_claimable"
    assert s.sent == []


def test_sweeper_confirms_landed_send(engine, mk_item):
    n = mk_item(status="approved")
    with engine.begin() as conn:
        conn.execute(
            update(digest_item).where(digest_item.c.item_no == n)
            .values(status="sending", claim_token="dead",
                    claimed_at=datetime.now(timezone.utc) - timedelta(minutes=30))
        )
    s = FakeSender()
    s.sent.append(n)  # provider actually took it; worker died before confirm
    assert sweep_stuck_claims(engine, s) == 1
    assert _status(engine, n) == "auto_sent"


def test_sweeper_releases_unsent_claim(engine, mk_item):
    n = mk_item(status="approved")
    with engine.begin() as conn:
        conn.execute(
            update(digest_item).where(digest_item.c.item_no == n)
            .values(status="sending", claim_token="dead",
                    claimed_at=datetime.now(timezone.utc) - timedelta(minutes=30))
        )
    s = FakeSender()  # never sent
    assert sweep_stuck_claims(engine, s) == 1
    assert _status(engine, n) == "approved"   # released for a fresh cycle
