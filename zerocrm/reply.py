"""Prospect replies: the apex-handoff half of the loop.

Two entrypoints:
  process_reply_forwards(engine, config)  — the POLL path that runs today. The
    Smartlead leads-statistics poll tells us a reply HAPPENED but not its text,
    so this urgently forwards a "reply came in, take over" to the apex inbox
    (sid@syndai.ai) for same-day handling. This is the "auto forward on reply".
  handle_reply(engine, event, config)     — the WEBHOOK-ready path: the
    EMAIL_REPLY webhook carries the body, so it classifies (interested / not /
    unsubscribe), drafts a warm reply for interested ones as a `warm_reply`
    digest_item (approve via the same one-tap buttons + autonomy curve), and
    blocklists unsubscribes. Wire it when the webhook lands (memory: ingestion is
    poll-primary today; the webhook is the future upgrade).

Classification here is a deterministic floor; the LLM classifier (Claude off the
webhook, per cold-email.md) is the upgrade. Warm replies SEND from the apex via
the claim-first executor (mailbox.ReplySender), gated by sending_enabled.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .config import get_config, set_config
from .copy import warm_reply
from .ingest import _blocklist_identity
from .rules import active_rules
from .schema import digest_item, person, person_identity, touch
from .upsert import resolve_person

_UNSUB = ("unsubscribe", "opt out", "opt-out", "remove me", "take me off", "stop emailing")
_NEG = ("not interested", "no thanks", "no thank", "all set", "already have",
        "not right now", "please stop", "no need", "not a fit")
_POS = ("interested", " yes", "sure", "sounds good", "tell me more", "send it",
        "send me", "how does", "let's", "lets ", "happy to", " book", "demo",
        "worth a look", "curious", "keen", "open to", "receipt", "a slot", "want in")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def classify_reply(body: str) -> str:
    """interested | not_interested | unsubscribe | neutral. Order matters:
    'not interested' contains 'interested', so negatives are checked first."""
    b = " " + (body or "").lower() + " "
    if any(w in b for w in _UNSUB):
        return "unsubscribe"
    if any(w in b for w in _NEG):
        return "not_interested"
    if any(w in b for w in _POS):
        return "interested"
    return "neutral"


def _forward(config: dict, subject: str, body: str, html: str | None = None, sender=None) -> None:
    """Urgent forward to the apex inbox. `sender` injectable for tests."""
    send = sender or _default_sender()
    send(subject, body, to=config.get("digest_to"), sender=config.get("digest_from"), html=html)


def _default_sender():
    from .mailbox import send_email
    return send_email


def process_reply_forwards(engine: Engine, config: dict, sender=None) -> dict:
    """POLL path: urgently forward each NEW inbound reply to the apex so the human
    can take over from sid@syndai.ai same-day. Deduped on the reply's event_key."""
    notified = set(get_config(engine, "reply_notified", []) or [])
    with engine.connect() as conn:
        replies = conn.execute(
            select(touch.c.event_key, touch.c.person_id)
            .where(touch.c.kind == "reply", touch.c.direction == "in")
        ).mappings().all()
    # skip replies the webhook already drafted (warm_reply exists) so poll and
    # webhook don't both forward the same reply.
    fresh = [r for r in replies if r["event_key"] and r["event_key"] not in notified
             and not _already_handled(engine, r["event_key"])]
    for r in fresh:
        email = _email_for(engine, r["person_id"])
        _forward(config,
                 subject=f"[zerocrm] prospect replied: {email}",
                 body=(f"{email} replied to a cold sequence (it auto-paused).\n\n"
                       "Take over from sid@syndai.ai: open Smartlead Unibox to read it, "
                       "reply from the apex. Booking link + a screen recording are the "
                       "warm-reply moves."),
                 sender=sender)
        notified.add(r["event_key"])
    if fresh:
        set_config(engine, "reply_notified", list(notified)[-500:])  # cap
    return {"forwarded": len(fresh)}


def handle_reply(engine: Engine, event: dict, config: dict, sender=None,
                 actor: dict | None = None) -> str:
    """WEBHOOK path (has the body): classify, then draft+forward an interested
    reply, blocklist an unsubscribe, or notify otherwise. Idempotent on event_key.
    Returns the verdict acted on."""
    actor = actor or {"kind": "agent", "id": "reply"}
    ws = config.get("workspace_id", "default")
    email = (event.get("email") or "").lower()
    evk = event.get("event_key")
    verdict = classify_reply(event.get("body") or "")

    if verdict == "unsubscribe":
        with engine.begin() as conn:
            _blocklist_identity(conn, email, "opt_out", actor, ws)
        return "unsubscribe"

    if _already_handled(engine, evk):
        return f"{verdict}:duplicate"

    if verdict == "interested":
        item_no = _make_warm_reply_item(engine, event, config, actor, ws)
        _forward_draft(engine, item_no, email, event.get("body") or "", config, sender)
        return "interested"

    # not_interested / neutral: surface it, no auto-draft
    _forward(config,
             subject=f"[zerocrm] reply ({verdict}): {email}",
             body=f"{email} replied:\n\n{event.get('body','')}\n\nTake over from the apex if worth it.",
             sender=sender)
    return verdict


def _make_warm_reply_item(engine: Engine, event: dict, config: dict, actor: dict, ws: str) -> int:
    email = (event.get("email") or "").lower()
    with engine.begin() as conn:
        pid = resolve_person(conn, "email", email, ws)
        first = ""
        if pid:
            first = (conn.execute(select(person.c.full_name).where(person.c.id == pid)).scalar() or "").split()
            first = first[0] if first else ""
        draft = warm_reply(first, config, rules=active_rules(engine, "warm_reply", ws))
        payload = {
            "summary": f"reply from {email}",
            "lead": {"email": email, "first_name": first},
            "preview": draft,
            "in_reply_to": event.get("message_id"),
            "reply_event_key": event.get("event_key"),
            "prospect_reply": event.get("body"),
        }
        res = conn.execute(digest_item.insert().values(
            kind="warm_reply", channel="email", person_id=pid, payload=payload,
            status="draft", digest_date=_now().date().isoformat(), actor=actor, workspace_id=ws))
        return res.inserted_primary_key[0]


def _forward_draft(engine, item_no, email, prospect_reply, config, sender=None) -> None:
    from .digest import build_cards, render_html
    with engine.connect() as conn:
        item = dict(conn.execute(select(digest_item).where(digest_item.c.item_no == item_no)).mappings().one())
    body = (f"{email} replied (interested):\n\n{prospect_reply}\n\n"
            f"Drafted apex reply below. Approve to send it from sid@syndai.ai.")
    html = None
    if config.get("action_base_url"):
        html = render_html(build_cards(engine, [item]), base_url=config["action_base_url"],
                           notices=[f"Interested reply from {email}. Approve to send from the apex."])
    _forward(config, subject=f"[zerocrm] interested reply: {email}", body=body, html=html, sender=sender)


def _already_handled(engine, event_key: str | None) -> bool:
    if not event_key:
        return False
    with engine.connect() as conn:
        rows = conn.execute(select(digest_item.c.payload).where(digest_item.c.kind == "warm_reply")).all()
    return any((p or {}).get("reply_event_key") == event_key for (p,) in rows)


def _email_for(engine, person_id) -> str:
    if not person_id:
        return "(unknown)"
    with engine.connect() as conn:
        return conn.execute(
            select(person_identity.c.value).where(
                person_identity.c.person_id == person_id, person_identity.c.kind == "email")
        ).scalar() or "(unknown)"
