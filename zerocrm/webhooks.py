"""Inbound provider webhooks — the low-latency reply path.

Feeds the SAME processing path as the poll (record_provider_event), so webhook
and poll converge on one set of touches and can't double-process (touch dedupes
on event_key; handle_reply dedupes on event_key). The webhook adds what the poll
can't get: the reply BODY, which lets handle_reply classify + draft the warm
reply automatically. Poll stays the reliable backstop — Smartlead webhooks are
unsigned and retry silently, so a missed webhook is caught by the next poll.

Auth is a token in the URL (Smartlead webhooks carry no HMAC), checked in serve.py.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine

from .drivers.smartlead import Smartlead
from .ingest import record_provider_event
from .reply import handle_reply

_ACTOR = {"kind": "agent", "id": "webhook"}


def ingest_provider_webhook(engine: Engine, provider: str, payload: dict, config: dict,
                            sender=None) -> dict:
    """Normalize + record one webhook event, then (for replies) classify+draft.
    Idempotent and safe to receive more than once. `sender` (the apex forwarder)
    is injectable for tests; production uses the default mailbox sender."""
    if provider != "smartlead":
        return {"ignored": f"unknown provider {provider}"}
    ev = Smartlead.normalize_event(payload)
    if not ev:
        return {"ignored": "not a reply/bounce"}

    outcome = record_provider_event(engine, ev, _ACTOR)  # idempotent touch (+ bounce -> blocklist)
    result = {"kind": ev["kind"], "recorded": outcome}

    if ev["kind"] == "reply":
        # handle_reply is idempotent on event_key; if the body field was wrong
        # (unverified webhook shape) it classifies as neutral and just forwards,
        # while the poll backstop still fires — nothing is lost.
        result["reply"] = handle_reply(engine, {
            "email": ev["email"], "event_key": ev["event_key"],
            "message_id": ev.get("message_id"), "body": ev.get("body"),
        }, config, sender=sender)
    return result
