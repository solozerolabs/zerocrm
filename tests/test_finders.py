"""Enrichment waterfall drivers: Findymail (sync), FullEnrich (async), and the
MillionVerifier grader."""

import httpx

from zerocrm.drivers import Findymail, FullEnrich, MillionVerifier


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- Findymail --------------------------------------------------------------
def test_findymail_by_linkedin_then_name():
    def h(req):
        assert req.headers["Authorization"] == "Bearer k"
        if req.url.path.endswith("/business-profile"):
            return httpx.Response(200, json={"contact": {"email": "Jane@Acme.com"}})
        return httpx.Response(404)
    assert Findymail(api_key="k", client=_client(h)).find("Jane", "Doe", "acme.com", "li/jane") == "jane@acme.com"


def test_findymail_falls_to_name_when_no_linkedin():
    def h(req):
        assert req.url.path.endswith("/search/name")
        return httpx.Response(200, json={"contact": {"email": "j@acme.com"}})
    assert Findymail(api_key="k", client=_client(h)).find("Jane", "Doe", "acme.com") == "j@acme.com"


def test_findymail_soft_fail_and_credits():
    def h(req):
        if req.url.path.endswith("/api/credits"):
            return httpx.Response(200, json={"credits": 1234})
        return httpx.Response(402, json={})
    fm = Findymail(api_key="k", client=_client(h))
    assert fm.find("A", "B", "x.com") is None      # 402 -> None (soft fail)
    assert fm.balance() == 1234


# --- FullEnrich (async submit -> poll); shapes verified live 2026-08-09 ------
def test_fullenrich_submit_then_poll_to_finished():
    calls = {"gets": 0}
    def h(req):
        if req.method == "POST":
            body = req.read()
            assert b'"data"' in body and b'"first_name"' in body  # real key/field names
            return httpx.Response(200, json={"enrichment_id": "e1"})
        calls["gets"] += 1
        if calls["gets"] == 1:
            return httpx.Response(200, json={"status": "IN_PROGRESS"})   # still processing
        return httpx.Response(200, json={"status": "FINISHED", "data": [
            {"contact_info": {"most_probable_work_email": {"email": "Jane@Acme.com",
                                                            "status": "HIGH_PROBABILITY"}}}]})
    fe = FullEnrich(api_key="k", client=_client(h), sleep=lambda s: None)
    assert fe.find("Jane", "Doe", "acme.com", "li/jane") == "jane@acme.com"
    assert calls["gets"] == 2  # polled through the in-progress state


def test_fullenrich_credits_insufficient_is_a_miss():
    def h(req):
        if req.method == "POST":
            return httpx.Response(200, json={"enrichment_id": "e1"})
        return httpx.Response(200, json={"status": "CREDITS_INSUFFICIENT", "data": []})
    fe = FullEnrich(api_key="k", client=_client(h), sleep=lambda s: None)
    assert fe.find("Jane", "Doe", "acme.com") is None


def test_fullenrich_needs_inputs_and_balance():
    def h(req):
        return httpx.Response(200, json={"balance": 500})
    fe = FullEnrich(api_key="k", client=_client(h))
    assert fe.find("", "", "") is None            # no linkedin, no name+domain -> no submit
    assert fe.balance() == 500


# --- MillionVerifier --------------------------------------------------------
def test_smartlead_set_sequence_posts_correct_shape():
    from zerocrm.copy import default_sequence, to_smartlead
    from zerocrm.drivers import Smartlead
    seen = {}
    def h(req):
        seen["path"] = req.url.path
        seen["body"] = req.read()
        return httpx.Response(200, json={"ok": True})
    Smartlead(api_key="k", client=_client(h)).set_sequence(77, to_smartlead(default_sequence()))
    assert seen["path"].endswith("/campaigns/77/sequences")
    assert b"seq_delay_details" in seen["body"] and b"email_body" in seen["body"]


def test_millionverifier_classify_and_credits():
    def h(req):
        return httpx.Response(200, json={"result": "ok", "credits": 9000})
    mv = MillionVerifier(api_key="k", client=_client(h))
    res = mv.validate("a@x.com")
    assert MillionVerifier.classify(res) == "send"
    assert mv.balance() == 9000  # read from the validate response


def test_millionverifier_classify_map():
    c = MillionVerifier.classify
    assert c({"result": "ok"}) == "send"
    assert c({"result": "invalid"}) == "block"
    assert c({"result": "disposable"}) == "block"
    assert c({"result": "catch_all"}) == "catchall"
    assert c({"result": "unknown"}) == "risky"
    assert c({}) == "risky"
