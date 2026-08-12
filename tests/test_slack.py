from sqlalchemy import insert

from zerocrm import slack
from zerocrm.config import set_config
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import person


class FakeSlack:
    def __init__(self, msgs):
        self._msgs = msgs
        self.posted = []

    def history(self, since):
        return self._msgs

    def post_message(self, text):
        self.posted.append(text)


def _engine(*names):
    e = make_engine("sqlite://")
    migrate(e)
    set_config(e, "slack_operator_ids", ["U_OP"])
    with e.begin() as c:
        for i, n in enumerate(names or ["Acme Jane"]):
            c.execute(insert(person).values(id=f"p{i}", full_name=n))
    return e


def test_operator_brief_is_answered():
    e = _engine("Acme Jane")
    client = FakeSlack([])
    out = slack.handle_message(e, "brief me on Acme", "U_OP", client=client)
    assert out and client.posted


def test_non_operator_is_ignored():
    e = _engine("Acme Jane")
    client = FakeSlack([])
    assert slack.handle_message(e, "brief me on Acme", "U_STRANGER", client=client) is None
    assert not client.posted


def test_ambiguous_match_asks_not_guesses():
    e = _engine("Jane Doe", "Jane Smith")
    client = FakeSlack([])
    out = slack.handle_message(e, "brief me on Jane", "U_OP", client=client)
    assert "Which one" in out
    assert "Jane Doe" in out and "Jane Smith" in out


def test_no_match_says_so():
    e = _engine("Acme Jane")
    client = FakeSlack([])
    out = slack.handle_message(e, "brief me on Zeta", "U_OP", client=client)
    assert "No matching" in out


def test_poll_advances_cursor_and_dedupes():
    e = _engine("Acme Jane")
    msgs = [{"ts": "1699999999.100000", "user": "U_OP", "text": "brief me on Acme"}]
    assert slack.poll_slack(e, FakeSlack(msgs)) == 1
    assert slack.poll_slack(e, FakeSlack(msgs)) == 0  # cursor past that ts
