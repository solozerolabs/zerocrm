from zerocrm import runner
from zerocrm.db import make_engine
from zerocrm.migrate import migrate


def _engine():
    e = make_engine("sqlite://")
    migrate(e)
    return e


def test_slack_tick_is_noop_without_client(monkeypatch):
    e = _engine()
    monkeypatch.setattr(runner, "_build_slack_client", lambda eng: None)
    assert runner.run_slack_tick(e) == {"slack_handled": 0}


def test_tick_reports_meet_ingested_key(monkeypatch):
    e = _engine()
    monkeypatch.setattr(runner, "_build_meet_client", lambda eng: None)
    monkeypatch.setattr(runner, "maybe_enable_sending", lambda *a, **k: {})
    monkeypatch.setattr(runner, "poll_smartlead", lambda *a, **k: {})
    monkeypatch.setattr(runner, "process_replies", lambda *a, **k: {})
    monkeypatch.setattr("zerocrm.reply.process_reply_forwards", lambda *a, **k: {"forwarded": 0})
    monkeypatch.setattr("zerocrm.rules.promote_rules", lambda *a, **k: [])
    out = runner.run_tick(e, smartlead=object())
    assert out["meet_ingested"] == 0
