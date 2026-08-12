"""The digest loop orchestration: send pending drafts, then process replies.

send_pending_digest  -> renders draft items, emails them, records the digest
                        Message-ID as a known thread.
process_replies      -> fetches replies, authenticates each (DKIM+allowlist+
                        thread), parses decisions, applies them, then executes
                        the now-approved items through the given sender.

Only `approved` items are executed. An `edited` item waits for a re-draft (the
Phase-2 LLM step) before it can be sent — its instruction is already captured as
a correction.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .config import get_config, set_config
from .digest import apply_decisions, authenticate_reply, build_cards, parse_reply, render, render_html
from .executor import execute_item
from .mailbox import fetch_replies, send_digest
from .schema import digest_item
from .sender import Sender

_MIDS_KEY = "digest_mids"


def send_pending_digest(engine: Engine, config: dict, subject_prefix: str = "[zerocrm]") -> str | None:
    from .draft_run import lint_payload
    from .warmup import pop_notices
    notices = pop_notices(engine)
    with engine.connect() as conn:
        items = [dict(r) for r in conn.execute(
            select(digest_item).where(digest_item.c.status == "draft")
        ).mappings()]
    # Copy gate: a draft that fails lint is NEVER surfaced for approval/send. Hold
    # it back (stays draft; recopy fixes it) and name it loudly so it gets fixed,
    # rather than putting a bad line in front of a prospect via one-tap approve.
    held = [it for it in items if lint_payload(it["kind"], it["payload"])]
    if held:
        items = [it for it in items if it not in held]
        bad = ", ".join(f"#{it['item_no']} ({'; '.join(lint_payload(it['kind'], it['payload']))})"
                        for it in held)
        notices = list(notices) + [f"{len(held)} draft(s) HELD by the copy gate — "
                                   f"run `zerocrm recopy` to fix: {bad}"]
    if not items and not notices:
        return None  # nothing to say today
    header = ""
    if notices:
        header += "".join(f"** {n}\n" for n in notices) + "\n"
    parts = []
    html = None
    if items:
        header += f"{len(items)} to review.\n"
        hint = f"Reply e.g.:  #{items[0]['item_no']} ok · #{items[0]['item_no']} ok but <edit> · #{items[0]['item_no']} skip"
        parts.append(render(items, header=header) + "\n\n" + hint)
        # Rich HTML with one-tap buttons when a public worker URL is configured
        # (our deployment). Public-repo default has none -> plain text, replies only.
        base_url = config.get("action_base_url")
        if base_url:
            html = render_html(build_cards(engine, items), base_url=base_url, notices=notices)
    else:
        parts.append(header.rstrip())
    subject = f"{subject_prefix} daily digest: {len(items)} to review" if items else f"{subject_prefix} notice"
    mid = send_digest(subject, "\n".join(parts), to=config.get("digest_to"),
                      sender=config.get("digest_from"), html=html)
    _append_mid(engine, mid)
    return mid


def process_replies(engine: Engine, config: dict, sender: Sender,
                    worker_id: str = "loop", execute: bool = True, reply_sender=None) -> dict:
    """Fetch + authenticate + parse + apply operator replies. If execute is True,
    also send the now-approved items via `sender`. When sending is not yet
    activated, pass execute=False: decisions are still recorded, approved items
    just wait in the queue until the first live tick drains them."""
    mids = set(get_known_mids(engine))
    replies = fetch_replies(mids)
    summary = {"rejected": [], "applied": [], "executed": 0, "ambiguous": []}
    for r in replies:
        ok, reason = authenticate_reply(r, config)
        if not ok:
            summary["rejected"].append({"from": r["from"], "reason": reason})
            continue
        decisions, ambiguous = parse_reply(r["body"])
        applied = apply_decisions(engine, decisions, actor={"kind": "human", "id": r["from"]})
        summary["applied"].append(applied)
        summary["ambiguous"].extend(ambiguous)
    if execute:
        summary["executed"] = _execute_approved(engine, sender, worker_id, reply_sender)
    return summary


def _execute_approved(engine: Engine, sender: Sender, worker_id: str, reply_sender=None) -> int:
    """Drain approved items. warm_reply items send from the apex mailbox
    (ReplySender); enrollments go through the provider sender. Both run the same
    claim-first executor, so exactly-once holds across channels."""
    from .mailbox import ReplySender
    reply_sender = reply_sender or ReplySender()
    with engine.connect() as conn:
        ready = conn.execute(
            select(digest_item.c.item_no, digest_item.c.kind).where(digest_item.c.status == "approved")
        ).all()
    sent = 0
    for item_no, kind in ready:
        s = reply_sender if kind == "warm_reply" else sender
        if s is None:
            continue  # no provider sender yet (pre-live); leave it queued
        if execute_item(engine, item_no, s, worker_id) in ("sent", "already_sent"):
            sent += 1
    return sent


# --- known-thread persistence (config table) -------------------------------
def _append_mid(engine: Engine, mid: str, keep: int = 60) -> None:
    mids = get_known_mids(engine)
    set_config(engine, _MIDS_KEY, ([mid] + [m for m in mids if m != mid])[:keep])


def get_known_mids(engine: Engine) -> list[str]:
    return get_config(engine, _MIDS_KEY, []) or []
