"""The two-stage verify gate: MillionVerifier grades, ZeroBounce scores catch-alls."""

from zerocrm.verify import Verifier


class _MV:
    def __init__(self, result):
        self.result = result
    def validate(self, email):
        return {"result": self.result}
    @staticmethod
    def classify(res):
        from zerocrm.drivers import MillionVerifier
        return MillionVerifier.classify(res)


class _ZB:
    def __init__(self, status="valid", score=8):
        self._status = status
        self._score = score
    def validate(self, email):
        return {"status": self._status}
    def score(self, email):
        return self._score


def test_mv_send_passes_without_touching_zb():
    v = Verifier(millionverifier=_MV("ok"), zerobounce=_ZB(score=0))
    decision, ev = v.check("a@x.com")
    assert decision == "send" and "millionverifier" in ev and "zerobounce_score" not in ev


def test_mv_invalid_blocks():
    assert Verifier(millionverifier=_MV("invalid")).check("a@x.com")[0] == "block"


def test_mv_catchall_escalates_to_zb_score_pass():
    v = Verifier(millionverifier=_MV("catch_all"), zerobounce=_ZB(score=9), catchall_min_score=7)
    decision, ev = v.check("a@x.com")
    assert decision == "send" and ev["zerobounce_score"] == 9


def test_mv_catchall_low_score_is_held_risky():
    v = Verifier(millionverifier=_MV("catch_all"), zerobounce=_ZB(score=3), catchall_min_score=7)
    assert v.check("a@x.com")[0] == "risky"


def test_mv_catchall_without_scorer_is_risky():
    assert Verifier(millionverifier=_MV("catch_all")).check("a@x.com")[0] == "risky"


def test_zb_only_path_valid_and_catchall():
    assert Verifier(zerobounce=_ZB(status="valid")).check("a@x.com")[0] == "send"
    assert Verifier(zerobounce=_ZB(status="invalid")).check("a@x.com")[0] == "block"
    # catch-all escalates to the score even on the ZB-only path
    v = Verifier(zerobounce=_ZB(status="catch-all", score=9))
    assert v.check("a@x.com")[0] == "send"
    v2 = Verifier(zerobounce=_ZB(status="catch-all", score=2))
    assert v2.check("a@x.com")[0] == "risky"


def test_no_verifier_fails_closed():
    assert Verifier().check("a@x.com") == ("risky", {})
