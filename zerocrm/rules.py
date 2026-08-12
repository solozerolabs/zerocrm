"""The rules ledger (tacitry-style): corrections in, rules out.

Every human edit on a draft is recorded as a `correction` (see digest.apply_decisions).
When the SAME correction pattern recurs enough to be durable, it promotes to a
`rule` — a standing instruction that shapes future drafts. This is the learning
half of the autonomy curve: autonomy.py tracks WHETHER the human keeps approving;
this tracks WHAT they keep changing, so the copy gets better instead of just more
trusted.

Durability gate (mirrors the sid-voice loop): a correction promotes at >= 3 hits
across >= 2 distinct surfaces (here: >= 2 distinct days). active_rules() is read
at draft time by the LLM copy step (icebreaker_fn / warm_reply rules=) to apply
what's been learned. Nothing here sends or auto-edits; it curates instructions.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from .schema import correction, rule

_PROMOTE_HITS = 3
_PROMOTE_SURFACES = 2  # distinct days the pattern was corrected on


def _now() -> datetime:
    return datetime.now(timezone.utc)


def active_rules(engine, channel: str, workspace_id: str = "default") -> list[str]:
    """Promoted, still-active rules for a channel — the instructions a draft should
    honor. Read at draft time (the LLM copy step applies them)."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(rule.c.text).where(
                rule.c.workspace_id == workspace_id, rule.c.channel == channel,
                rule.c.promoted_at.isnot(None),
            ).order_by(rule.c.promoted_at.desc())
        ).all()
    return [r[0] for r in rows]


def promote_rules(engine, workspace_id: str = "default") -> list[str]:
    """Scan corrections; promote any pattern past the durability gate to a rule.
    Idempotent: a pattern already promoted is skipped. Returns newly promoted
    rule texts. Run on a cadence (each tick) so the ledger self-maintains."""
    now = _now()
    promoted: list[str] = []
    with engine.begin() as conn:
        # group corrections by (channel, right) — the instruction the human gave
        groups = conn.execute(
            select(correction.c.channel, correction.c.right,
                   func.count().label("hits"),
                   func.count(func.distinct(func.date(correction.c.ts))).label("surfaces"))
            .where(correction.c.workspace_id == workspace_id)
            .group_by(correction.c.channel, correction.c.right)
        ).all()
        for channel, instruction, hits, surfaces in groups:
            if hits < _PROMOTE_HITS or surfaces < _PROMOTE_SURFACES:
                continue
            already = conn.execute(
                select(rule.c.id).where(
                    rule.c.workspace_id == workspace_id, rule.c.channel == channel,
                    rule.c.text == instruction)
            ).first()
            if already:
                continue
            conn.execute(rule.insert().values(
                channel=channel, text=instruction, hits=hits, surfaces=surfaces,
                promoted_at=now, workspace_id=workspace_id))
            promoted.append(instruction)
    return promoted
