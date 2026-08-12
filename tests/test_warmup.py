"""Warmup watcher: auto-enable sending only when every mailbox is ready and the
date floor has passed. Fail-safe toward staying OFF."""

from datetime import date

import httpx

from zerocrm.config import sending_enabled, set_config
from zerocrm.drivers.smartlead import Smartlead
from zerocrm.warmup import maybe_enable_sending, pop_notices, warmup_ready

READY = date(2026, 9, 15)   # past the default floor
EARLY = date(2026, 8, 20)   # before the default floor


def _smartlead(rep=100, sent="200", spam="0", accounts=(1, 2)):
    def handler(req):
        p = req.url.path
        if "warmup-stats" in p:
            return httpx.Response(200, json={"sent_count": sent, "spam_count": spam})
        if p.rstrip("/").endswith("email-accounts"):
            return httpx.Response(200, json=[{"id": a} for a in accounts])
        if "/email-accounts/" in p:  # single account
            return httpx.Response(200, json={"warmup_details": {"warmup_reputation": rep}})
        return httpx.Response(404)
    return Smartlead(api_key="k", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_not_ready_before_date_floor(engine):
    ready, why = warmup_ready(engine, _smartlead(), today=EARLY)
    assert not ready and "earliest date" in why


def test_not_ready_low_reputation(engine):
    ready, why = warmup_ready(engine, _smartlead(rep=60), today=READY)
    assert not ready and "not ready" in why


def test_not_ready_spam_seen(engine):
    ready, _ = warmup_ready(engine, _smartlead(spam="2"), today=READY)
    assert not ready


def test_not_ready_ramp_incomplete(engine):
    ready, _ = warmup_ready(engine, _smartlead(sent="10"), today=READY)
    assert not ready


def test_ready_when_all_pass(engine):
    ready, why = warmup_ready(engine, _smartlead(), today=READY)
    assert ready and why == "all mailboxes ready"


def test_maybe_enable_flips_and_notifies(engine):
    assert sending_enabled(engine) is False
    assert maybe_enable_sending(engine, _smartlead(), today=READY) == "enabled"
    assert sending_enabled(engine) is True
    notices = pop_notices(engine)
    assert any("Warmup complete" in n for n in notices)


def test_maybe_enable_respects_date_floor(engine):
    out = maybe_enable_sending(engine, _smartlead(), today=EARLY)
    assert out.startswith("not_ready") and sending_enabled(engine) is False


def test_maybe_enable_auto_disabled(engine):
    set_config(engine, "auto_enable_on_warmup", False)
    assert maybe_enable_sending(engine, _smartlead(), today=READY) == "auto_disabled"
    assert sending_enabled(engine) is False


def test_maybe_enable_idempotent_once_on(engine):
    set_config(engine, "sending_enabled", True)
    assert maybe_enable_sending(engine, _smartlead(), today=READY) == "already_enabled"
