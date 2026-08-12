"""Claim-first exactly-once executor.

pgmq (and any at-least-once queue) redelivers when a worker dies mid-job. The
worst failure for an outreach product is sending the same cold email twice, so
the executor claims BEFORE it sends and, on redelivery of its own claim,
verifies with the provider instead of re-sending:

    1. atomically flip approved/edited -> sending, stamp claim_token   (the claim)
    2. send via the provider                                            (the effect)
    3. flip sending -> sent, record provider id                         (the confirm)

Crash between 1 and 2  -> redelivery re-runs send under the same claim (1 send).
Crash between 2 and 3  -> redelivery finds already_sent() true, just confirms.
A different worker's message -> claim fails, it returns without sending.

A sweeper resolves claims stranded in `sending` (worker died and never came
back) by asking the provider whether the send landed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.engine import Engine

from .schema import blocklist, digest_item, touch
from .sender import Sender

SENDABLE = ("approved", "edited")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row(conn, item_no: int) -> dict | None:
    r = conn.execute(select(digest_item).where(digest_item.c.item_no == item_no)).mappings().first()
    return dict(r) if r else None


def _confirm_sent(engine: Engine, row: dict, provider_id: str, auto: bool) -> None:
    now = _now()
    with engine.begin() as conn:
        conn.execute(
            update(digest_item)
            .where(digest_item.c.item_no == row["item_no"])
            .values(status="auto_sent" if auto else "sent", updated_at=now)
        )
        if row.get("person_id"):
            conn.execute(
                touch.insert().values(
                    person_id=row["person_id"],
                    channel=row["channel"],
                    direction="out",
                    kind="sent",
                    campaign_id=row.get("campaign_id"),
                    body_ref=provider_id,
                    payload={"digest_item": row["item_no"], "provider_id": provider_id},
                    ts=now,
                    workspace_id=row.get("workspace_id", "default"),
                )
            )


def execute_item(engine: Engine, item_no: int, sender: Sender, worker_id: str, auto: bool = False) -> str:
    """Process one digest item exactly once. Returns the outcome:
    'sent' | 'already_sent' | 'not_claimable'."""
    now = _now()
    # 1. claim: only an unclaimed sendable item flips to `sending`.
    with engine.begin() as conn:
        claimed = conn.execute(
            update(digest_item)
            .where(
                digest_item.c.item_no == item_no,
                digest_item.c.status.in_(SENDABLE),
                digest_item.c.claim_token.is_(None),
            )
            .values(status="sending", claim_token=worker_id, claimed_at=now)
        ).rowcount

    row = None
    if not claimed:
        # Not freshly claimable. Either someone else owns it, it already sent,
        # or this is a redelivery of OUR OWN claim after a crash.
        with engine.begin() as conn:
            row = _row(conn, item_no)
        if not row or row["status"] in ("sent", "auto_sent", "skipped"):
            return "already_sent" if row and row["status"] in ("sent", "auto_sent") else "not_claimable"
        if not (row["status"] == "sending" and row["claim_token"] == worker_id):
            return "not_claimable"  # another worker holds the claim
        # our own claim, crashed before confirm -> verify then continue
        if sender.already_sent(row):
            _confirm_sent(engine, row, row.get("body_ref") or f"recovered-{item_no}", auto)
            return "already_sent"
    else:
        with engine.begin() as conn:
            row = _row(conn, item_no)

    # 1b. blocklist guard — never enroll a suppressed address/domain/person.
    # (spec: every send path joins against blocklist; scopes per outreachr:
    # identity/email, domain, person, global.)
    reason = blocklist_reason(engine, row)
    if reason:
        with engine.begin() as conn:
            conn.execute(
                update(digest_item).where(digest_item.c.item_no == item_no)
                .values(status="skipped", updated_at=_now())
            )
        return f"blocked:{reason}"

    # 2. send (idempotency-probed for our-own-claim redelivery above)
    provider_id = sender.send(row)
    # 3. confirm
    _confirm_sent(engine, row, provider_id, auto)
    return "sent"


def blocklist_reason(engine: Engine, row: dict) -> str | None:
    """Return the block reason if the item's target is suppressed, else None.
    Checks identity (email), domain, person, and global scopes."""
    lead = (row.get("payload") or {}).get("lead") or {}
    email = (lead.get("email") or "").lower()
    ws = row.get("workspace_id", "default")
    checks = [blocklist.c.granularity == "global"]
    if email:
        checks.append((blocklist.c.granularity == "identity") & (blocklist.c.value == email))
        if "@" in email:
            checks.append((blocklist.c.granularity == "domain") & (blocklist.c.value == email.split("@")[1]))
    if row.get("person_id"):
        checks.append((blocklist.c.granularity == "person") & (blocklist.c.value == row["person_id"]))
    with engine.connect() as conn:
        return conn.execute(
            select(blocklist.c.reason).where(blocklist.c.workspace_id == ws, or_(*checks)).limit(1)
        ).scalar_one_or_none()


def sweep_stuck_claims(engine: Engine, sender: Sender, older_than_min: int = 15) -> int:
    """Resolve claims stranded in `sending` (worker died). Returns count resolved."""
    cutoff = _now() - timedelta(minutes=older_than_min)
    with engine.connect() as conn:
        stuck = conn.execute(
            select(digest_item).where(
                digest_item.c.status == "sending",
                digest_item.c.claimed_at < cutoff,
            )
        ).mappings().all()
    resolved = 0
    for r in stuck:
        row = dict(r)
        if sender.already_sent(row):
            _confirm_sent(engine, row, row.get("body_ref") or f"swept-{row['item_no']}", auto=True)
        else:
            # never sent: release the claim so a fresh cycle can re-send
            with engine.begin() as conn:
                conn.execute(
                    update(digest_item)
                    .where(digest_item.c.item_no == row["item_no"])
                    .values(status="approved", claim_token=None, claimed_at=None)
                )
        resolved += 1
    return resolved
