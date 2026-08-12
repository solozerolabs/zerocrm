"""The rules ledger: corrections promote to rules at the durability gate."""

from datetime import datetime, timezone

from sqlalchemy import insert

from zerocrm.rules import active_rules, promote_rules
from zerocrm.schema import correction

D1 = datetime(2026, 8, 1, tzinfo=timezone.utc)
D2 = datetime(2026, 8, 2, tzinfo=timezone.utc)


def _corr(engine, right, ts, channel="email"):
    with engine.begin() as c:
        c.execute(insert(correction).values(channel=channel, wrong="w", right=right,
                                             durable=False, ts=ts, workspace_id="default"))


def test_promotes_at_gate_3hits_2surfaces(engine):
    for ts in (D1, D1, D2):                       # 3 hits across 2 days
        _corr(engine, "keep it under 60 words", ts)
    assert "keep it under 60 words" in promote_rules(engine)
    assert "keep it under 60 words" in active_rules(engine, "email")


def test_no_promote_below_gate(engine):
    _corr(engine, "x", D1); _corr(engine, "x", D1)  # 2 hits, 1 surface
    assert promote_rules(engine) == []
    assert active_rules(engine, "email") == []


def test_promote_is_idempotent(engine):
    for ts in (D1, D1, D2):
        _corr(engine, "lead with their stack", ts)
    assert promote_rules(engine) == ["lead with their stack"]
    assert promote_rules(engine) == []           # already promoted, not re-added
