from datetime import datetime, timezone

from zerocrm import meet
from zerocrm.config import get_config
from zerocrm.db import make_engine
from zerocrm.migrate import migrate


class FakeMeet:
    def __init__(self, records):
        self._records = records
        self.seen_since = "unset"

    def finished_records(self, since):
        self.seen_since = since
        return self._records


def _rec(name, text="pricing talk"):
    t = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    return {"name": name, "start_time": t, "end_time": t,
            "participant_emails": ["jane@acme.com"], "transcript_text": text, "segments": []}


def _engine():
    e = make_engine("sqlite://")
    migrate(e)
    return e


def test_poll_ingests_and_sets_cursor():
    e = _engine()
    ids = meet.poll_meet(e, FakeMeet([_rec("conferenceRecords/1")]))
    assert len(ids) == 1
    assert get_config(e, "google_meet_cursor")


def test_poll_dedupes_across_calls():
    e = _engine()
    client = FakeMeet([_rec("conferenceRecords/1")])
    meet.poll_meet(e, client)
    assert meet.poll_meet(e, client) == []  # same record redelivered -> nothing new


def test_poll_passes_cursor_back_to_client():
    e = _engine()
    meet.poll_meet(e, FakeMeet([_rec("conferenceRecords/1")]))
    client = FakeMeet([])
    meet.poll_meet(e, client)
    assert client.seen_since is not None  # second poll reads the stored cursor
