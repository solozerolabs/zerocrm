"""The autonomy curve: approvals advance the streak + promote levels; a
correction resets it. (Signal capture only — nothing here sends.)"""

from sqlalchemy import select

from zerocrm.autonomy import record_outcome
from zerocrm.schema import autonomy


def _state(engine, channel="email"):
    with engine.connect() as c:
        r = c.execute(select(autonomy.c.level, autonomy.c.streak)
                      .where(autonomy.c.channel == channel)).first()
    return (r[0], r[1]) if r else (None, None)


def test_streak_bumps_on_approve(engine):
    with engine.begin() as c:
        out = record_outcome(c, "email", True)
    assert out == {"level": "approve_all", "streak": 1}
    assert _state(engine) == ("approve_all", 1)


def test_correction_resets_streak(engine):
    with engine.begin() as c:
        record_outcome(c, "email", True)
        record_outcome(c, "email", True)
        record_outcome(c, "email", False)  # skip/edit
    assert _state(engine) == ("approve_all", 0)


def test_promotes_at_threshold(engine):
    with engine.begin() as c:
        for _ in range(10):
            out = record_outcome(c, "email", True)
    assert out == {"level": "sample_50", "streak": 0}
    assert _state(engine) == ("sample_50", 0)
