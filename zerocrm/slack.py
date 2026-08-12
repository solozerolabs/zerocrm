"""Slack DM surface — the on-demand clock. Poll-primary (no Socket Mode, no public
endpoint), driven by a fast run_slack_tick. Operator-allowlisted; message text is
DATA, never an instruction. A brief is only sent for an UNAMBIGUOUS contact match —
briefing on the wrong account is worse than saying "which one?"."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .brief import precall_brief
from .config import get_config, set_config
from .schema import deal, person

_OPEN_DEAL_EXCLUDES = ("won", "lost", "closed")


def _operators(engine: Engine) -> set:
    return set(get_config(engine, "slack_operator_ids", []) or [])


def _needle(text: str) -> str:
    t = text.lower()
    for prefix in ("brief me on", "brief on", "brief"):
        if prefix in t:
            t = t.split(prefix, 1)[1]
            break
    return t.strip(" ?.,:")


def _matches(engine: Engine, needle: str) -> list[dict]:
    """People whose name contains the needle. Substring, not fuzzy — exact enough
    to avoid false matches; ambiguity is surfaced, never guessed."""
    if not needle:
        return []
    out = []
    with engine.connect() as conn:
        for p in conn.execute(select(person)).mappings():
            name = (p["full_name"] or "").lower()
            if name and needle in name:
                out.append(dict(p))
    return out


def _open_deal_id(engine: Engine, person_id: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(deal.c.id)
            .where(deal.c.person_id == person_id, deal.c.stage.notin_(_OPEN_DEAL_EXCLUDES))
            .order_by(deal.c.stage_entered_at.desc())).first()
    return row[0] if row else None


def handle_message(engine: Engine, text: str, user_id: str, *, client,
                   workspace_id: str = "default") -> str | None:
    if user_id not in _operators(engine):
        return None  # trust gate — mirrors the digest reply allowlist
    if "brief" not in text.lower():
        return None  # unknown intent — stay silent rather than guess
    hits = _matches(engine, _needle(text))
    if not hits:
        reply = "No matching contact. Try the full name."
    elif len(hits) > 1:
        names = ", ".join(h["full_name"] or h["id"] for h in hits[:6])
        reply = f"Multiple contacts match: {names}. Which one? (use the full name)"
    else:
        p = hits[0]
        reply = precall_brief(engine, person_id=p["id"],
                              deal_id=_open_deal_id(engine, p["id"]), query=text)
    client.post_message(reply)
    return reply


def poll_slack(engine: Engine, client, *, workspace_id: str = "default") -> int:
    """Handle new operator messages since the stored cursor. Slack ts are fixed-
    width epoch strings, so a lexicographic compare is a correct, precision-safe
    high-water mark (no float rounding)."""
    cursor = get_config(engine, "slack_cursor", "") or ""
    handled = 0
    high = cursor
    for m in client.history(cursor):
        ts = str(m["ts"])
        if ts <= cursor:
            continue
        handle_message(engine, m["text"], m["user"], client=client, workspace_id=workspace_id)
        handled += 1
        if ts > high:
            high = ts
    if high > cursor:
        set_config(engine, "slack_cursor", high)
    return handled
