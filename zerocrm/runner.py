"""The scheduled runtime: one runner, two verbs.

  run_digest  — once/day: (optionally draft prospects) then email the operator
                the pending digest. Always safe; never sends cold email.
  run_tick    — a few times/day (business-hours-weighted): poll Smartlead for
                replies/bounces into touches, then process the operator's replies
                and — only when sending is activated — execute approved items.

Clocked externally by pg_cron (see deploy/). Idempotent and restart-safe: the
poll cursor lives in config, touches dedupe on event_key, sends are claim-first.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy.engine import Engine

from .config import get_config, sending_enabled, set_config
from .draft_run import draft_run
from .guard import usable
from .ingest import record_provider_event
from .loop import process_replies, send_pending_digest
from .sender import Sender
from .verify import Verifier
from .warmup import add_notice, maybe_enable_sending

_POLLER = {"kind": "agent", "id": "poller"}
_OVERLAP = timedelta(minutes=5)  # re-read window so a boundary event is never missed (dedupe absorbs it)

# Credit floors: below these a tier is paused for the run and a digest notice is
# posted (so the operator tops up) rather than silently dropping prospects.
_FLOORS = {"Findymail": 20, "FullEnrich": 20, "Prospeo": 0,
           "MillionVerifier": 500, "ZeroBounce": 100}


def _build_finders() -> list:
    """Enrichment waterfall, in order, from whichever keys are set. Empty until
    keys land in the environment (then it activates with no code change)."""
    from .drivers import Findymail, FullEnrich, Prospeo
    finders: list = []
    for cls, env in ((Findymail, "FINDYMAIL_API_KEY"),
                     (FullEnrich, "FULLENRICH_API_KEY"),
                     (Prospeo, "PROSPEO_API_KEY")):
        if os.environ.get(env):
            finders.append(cls())
    return finders


def _build_copy_fn(engine: Engine):
    """The voice-perfect LLM icebreaker step, or None (deterministic floor stays).
    Activates only when ANTHROPIC_API_KEY + ZEROCRM_VOICE_PROFILE are both set —
    the profile is private, injected via secret, never committed. Reads the learned
    rules ledger so the copy honors what the operator keeps correcting."""
    from .copy_llm import build_icebreaker_fn
    from .rules import active_rules
    profile = os.environ.get("ZEROCRM_VOICE_PROFILE")
    if not (profile and os.environ.get("ANTHROPIC_API_KEY")):
        return None
    return build_icebreaker_fn(profile, active_rules(engine, "email"))


def _build_verifier() -> Verifier:
    """MillionVerifier bulk gate + ZeroBounce catch-all scoring, from set keys."""
    from .drivers import MillionVerifier, ZeroBounce
    mv = MillionVerifier() if os.environ.get("MILLIONVERIFIER_API_KEY") else None
    zb = ZeroBounce() if os.environ.get("ZEROBOUNCE_API_KEY") else None
    return Verifier(millionverifier=mv, zerobounce=zb)


def _verify_ready(engine: Engine, verify: Verifier) -> bool:
    """Pause any verifier tier that's out of credits (so the other takes over);
    if NEITHER is usable, hold drafting entirely — we never send unverified."""
    if verify.mv is not None and not usable(engine, "MillionVerifier", verify.mv, _FLOORS["MillionVerifier"]):
        verify.mv = None
    if verify.zb is not None and not usable(engine, "ZeroBounce", verify.zb, _FLOORS["ZeroBounce"]):
        verify.zb = None
    if verify.mv is None and verify.zb is None:
        add_notice(engine, "No verifier has credits — drafting paused (won't send "
                           "unverified). Top up MillionVerifier or ZeroBounce.")
        return False
    return True


def load_runtime_config(engine: Engine) -> dict:
    """Operator config for the run, from the config table with our defaults."""
    return {
        "digest_to": get_config(engine, "digest_to", "operator@example.com"),
        "digest_from": get_config(engine, "digest_from", "operator@example.com"),
        "self_address": get_config(engine, "self_address", "operator@example.com"),
        "approver_identities": get_config(engine, "approver_identities", ["operator@example.com"]),
        "approver_domains": get_config(engine, "approver_domains",
                                       ["example.com"]),
        "smartlead_poll_campaigns": get_config(engine, "smartlead_poll_campaigns", []),
        "draft": get_config(engine, "draft", None),  # {"campaign":..., "apollo_filters":..., "n":...}
        "action_base_url": get_config(engine, "action_base_url", None),  # enables one-tap digest buttons
        "booking_url": get_config(engine, "booking_url", None),  # gcal appointment page (warm reply)
        "site_url": get_config(engine, "site_url", "syndai.ai"),
        "workspace_id": get_config(engine, "workspace_id", "default"),
    }


def run_digest(engine: Engine, config: dict | None = None, *, apollo=None,
               verify: Verifier | None = None, finders: list | None = None) -> dict:
    config = config or load_runtime_config(engine)
    drafted = 0
    if config.get("draft") and apollo:
        # Waterfall + verifier assemble from whichever keys are set; each tier
        # activates automatically when its key lands (no code change).
        if verify is None:
            verify = _build_verifier()
        if finders is None:
            finders = _build_finders()
        # balance guard: pause any tier below its floor and notify, rather than
        # silently under-enriching or under-verifying.
        finders = [f for f in finders
                   if usable(engine, type(f).__name__, f, _FLOORS.get(type(f).__name__, 0))]
        if _verify_ready(engine, verify):
            drafted = len(draft_run(engine, config["draft"], _POLLER,
                                    apollo=apollo, verify=verify, finders=finders,
                                    icebreaker_fn=_build_copy_fn(engine)))
    mid = send_pending_digest(engine, config)
    return {"drafted": drafted, "digest_sent": bool(mid), "message_id": mid}


def run_tick(engine: Engine, config: dict | None = None, *, smartlead: Sender | None = None) -> dict:
    config = config or load_runtime_config(engine)
    # watch warmup; auto-enable sending the moment it completes (no manual flip)
    sl_for_checks = smartlead
    if sl_for_checks is None:
        from .drivers.smartlead import Smartlead
        sl_for_checks = Smartlead()
    warmup = maybe_enable_sending(engine, sl_for_checks)
    poll = poll_smartlead(engine, config, smartlead)
    # auto-forward any new prospect reply to the apex for same-day human takeover,
    # and let the rules ledger self-promote from recent corrections.
    from .reply import process_reply_forwards
    from .rules import promote_rules
    forwarded = process_reply_forwards(engine, config)
    promoted = promote_rules(engine, config.get("workspace_id", "default"))
    live = sending_enabled(engine)
    # execute approved items only once sending is activated; until then decisions
    # are still recorded and approved items wait in the queue.
    sender = smartlead
    if live and sender is None:
        from .drivers.smartlead import Smartlead
        sender = Smartlead()
    replies = process_replies(engine, config, sender, execute=live)
    # capture finished Google Meet calls into conversation memory (no-op without creds)
    from .meet import poll_meet
    meet_client = _build_meet_client(engine)
    meet_ingested = len(poll_meet(engine, meet_client)) if meet_client else 0
    return {"warmup": warmup, "poll": poll, "reply_forwards": forwarded,
            "rules_promoted": len(promoted), "sending_enabled": live, "replies": replies,
            "meet_ingested": meet_ingested}


def _build_meet_client(engine: Engine):
    """Google Meet API client, or None when creds absent (the tick no-ops).
    The real driver is a deferred live-verify task; the poller is already tested
    against the client contract."""
    if not os.environ.get("GOOGLE_MEET_CREDENTIALS"):
        return None
    from .drivers.google_meet import GoogleMeetClient  # deferred; imported lazily
    return GoogleMeetClient()


def _build_slack_client(engine: Engine):
    """Slack bot client, or None when SLACK_BOT_TOKEN is absent."""
    if not os.environ.get("SLACK_BOT_TOKEN"):
        return None
    from .drivers.slack import SlackClient  # deferred; imported lazily
    return SlackClient(channel=get_config(engine, "slack_bot_channel"))


def run_slack_tick(engine: Engine) -> dict:
    """The fast on-demand clock: poll the Slack DM surface and answer briefs.
    Clocked separately from run_tick (every ~2 min, business-hours-weighted)."""
    from .slack import poll_slack
    client = _build_slack_client(engine)
    return {"slack_handled": poll_slack(engine, client) if client else 0}


def poll_smartlead(engine: Engine, config: dict, smartlead: Sender | None = None) -> dict:
    campaigns = config.get("smartlead_poll_campaigns") or []
    if not campaigns:
        return {"campaigns": 0, "ingested": 0, "duplicate": 0}
    if smartlead is None:
        from .drivers.smartlead import Smartlead
        smartlead = Smartlead()
    now = datetime.now(timezone.utc)
    ingested = duplicate = 0
    for cid in campaigns:
        since = get_config(engine, f"poll_cursor:{cid}", (now - timedelta(days=7)).isoformat())
        for ev in smartlead.poll_events(cid, since, now.isoformat()):
            outcome = record_provider_event(engine, ev, _POLLER)
            if outcome == "inserted":
                ingested += 1
            else:
                duplicate += 1
        # advance cursor with an overlap so a boundary event is re-read, not lost
        set_config(engine, f"poll_cursor:{cid}", (now - _OVERLAP).isoformat())
    return {"campaigns": len(campaigns), "ingested": ingested, "duplicate": duplicate}
