"""The autonomy curve.

Every human decision on a draft is a signal: a clean approve advances the
channel's streak; a skip or edit (a correction) resets it. Streak thresholds
promote the level along approve_all -> sample_50 -> auto_digest.

Capturing the signal is what's built now — the level accrues from real behavior,
whether the decision came from a digest reply or a one-tap button. ACTING on a
promoted level (auto-sending without asking) is Phase 2 and stays gated behind
sending_enabled; nothing here sends. This only records the curve so the history
exists when that gate is flipped.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from .schema import autonomy

_LADDER = ["approve_all", "sample_50", "auto_digest"]
_PROMOTE_AT = {"approve_all": 10, "sample_50": 20}  # clean approvals to advance a level


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_outcome(conn, channel: str, approved: bool, workspace_id: str = "default") -> dict:
    """Advance/reset a channel's autonomy streak on the given connection (so it
    shares the caller's transaction). Returns {level, streak}. approved=True
    bumps and may promote; False (skip/edit) resets the streak to 0."""
    row = conn.execute(
        select(autonomy.c.id, autonomy.c.level, autonomy.c.streak).where(
            autonomy.c.workspace_id == workspace_id, autonomy.c.channel == channel
        )
    ).first()
    aid, level, streak = (row[0], row[1], row[2]) if row else (None, "approve_all", 0)

    if approved:
        streak += 1
        need = _PROMOTE_AT.get(level)
        if need is not None and streak >= need:
            level = _LADDER[min(_LADDER.index(level) + 1, len(_LADDER) - 1)]
            streak = 0  # fresh streak at the new level
    else:
        streak = 0  # a correction breaks the streak

    if aid is None:
        conn.execute(autonomy.insert().values(
            channel=channel, level=level, streak=streak, workspace_id=workspace_id))
    else:
        conn.execute(autonomy.update().where(autonomy.c.id == aid).values(
            level=level, streak=streak, updated_at=_now()))
    return {"level": level, "streak": streak}
