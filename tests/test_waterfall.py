"""The acquisition waterfall: Apollo email first (lazy), then finders in order,
verify gate decides. Finder credits are only spent when cheaper tiers miss/fail."""

from zerocrm.draft_run import _acquire_live


class _Apollo:
    def __init__(self, match_email=None):
        self._e = match_email
    def search_people(self, **k):
        return [{"apollo_id": "a1", "fields": {"full_name": "Jane Doe", "job_title": "Founder"},
                 "linkedin": "li/jane", "company_domain": "acme.com", "company_name": "Acme"}]
    def match(self, **k):
        return {"email": self._e, "fields": {"full_name": "Jane Doe"},
                "linkedin": "li/jane", "company_domain": "acme.com", "company_name": "Acme"}


class _Finder:
    def __init__(self, email="jane@acme.com"):
        self.email = email
        self.called = False
    def find(self, first, last, domain, linkedin):
        self.called = True
        return self.email


class _V:
    """Verify gate stub: block listed emails, send everything else."""
    def __init__(self, block=()):
        self.block = set(block)
    def check(self, email):
        return (("block" if email in self.block else "send"), {"email": email})


def test_waterfall_uses_apollo_email_when_verified():
    f = _Finder()
    out = _acquire_live(_Apollo(match_email="direct@acme.com"), _V(), {"n": 10}, [f])
    assert out[0]["email"] == "direct@acme.com" and out[0]["_email_source"] == "apollo"
    assert f.called is False  # cheapest tier won, finder credit never spent


def test_waterfall_falls_to_finder_when_apollo_missing():
    f = _Finder()
    out = _acquire_live(_Apollo(match_email=None), _V(), {"n": 10}, [f])
    assert out[0]["email"] == "jane@acme.com" and out[0]["_email_source"] == "_finder"
    assert f.called


def test_waterfall_falls_to_finder_when_apollo_email_blocked():
    f = _Finder()
    out = _acquire_live(_Apollo(match_email="bad@acme.com"), _V(block={"bad@acme.com"}), {"n": 10}, [f])
    assert out[0]["email"] == "jane@acme.com" and out[0]["_email_source"] == "_finder"
    assert f.called  # apollo email failed the gate, so the finder was consulted


def test_waterfall_drops_person_when_every_tier_fails():
    f = _Finder(email="also-bad@acme.com")
    out = _acquire_live(_Apollo(match_email="bad@acme.com"),
                        _V(block={"bad@acme.com", "also-bad@acme.com"}), {"n": 10}, [f])
    assert out == []


def test_waterfall_carries_verify_evidence():
    out = _acquire_live(_Apollo(match_email="a@acme.com"), _V(), {"n": 10}, [])
    assert out[0]["_verify"] == {"email": "a@acme.com"}
