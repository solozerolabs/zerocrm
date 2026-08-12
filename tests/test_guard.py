"""Low-balance guard: pause a tier and notify rather than silently dropping."""

from zerocrm.guard import usable
from zerocrm.warmup import pop_notices


class _D:
    def __init__(self, bal):
        self._b = bal
    def balance(self):
        return self._b


def test_usable_ok_above_floor(engine):
    assert usable(engine, "Findymail", _D(100), 20) is True
    assert pop_notices(engine) == []


def test_usable_pauses_and_notifies_when_low(engine):
    assert usable(engine, "Findymail", _D(5), 20) is False
    notices = pop_notices(engine)
    assert len(notices) == 1 and "Findymail" in notices[0]


def test_usable_unknown_balance_stays_in(engine):
    # -1 (unknown) fails open on the read; the API's own 402 still stops empty
    assert usable(engine, "MillionVerifier", _D(-1), 500) is True
    assert pop_notices(engine) == []
