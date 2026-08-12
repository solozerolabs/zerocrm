"""Prospeo email-finder driver (dormant fallback tier of the waterfall)."""

import httpx

from zerocrm.drivers import Prospeo


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_prospeo_find_by_linkedin():
    def h(req):
        assert req.url.path.endswith("/linkedin-email-finder")
        return httpx.Response(200, json={"response": {"email": "Jane@Acme.com"}})
    assert Prospeo(api_key="k", client=_client(h)).find_by_linkedin("li/jane") == "jane@acme.com"


def test_prospeo_find_email():
    p = Prospeo(api_key="k", client=_client(lambda r: httpx.Response(200, json={"response": {"email": "j@x.com"}})))
    assert p.find_email("Jane", "Doe", "x.com") == "j@x.com"


def test_prospeo_none_on_error_or_missing():
    assert Prospeo(api_key="k", client=_client(lambda r: httpx.Response(402, json={}))).find_email("a", "b", "x.com") is None
    assert Prospeo(api_key="k", client=_client(lambda r: httpx.Response(200, json={"response": {}}))).find_email("a", "b", "x.com") is None


def test_prospeo_needs_inputs():
    p = Prospeo(api_key="k", client=_client(lambda r: httpx.Response(200, json={"response": {"email": "x@y.com"}})))
    assert p.find_email("", "", "") is None and p.find_by_linkedin("") is None


def test_prospeo_find_wrapper_prefers_linkedin():
    seen = {}
    def h(req):
        seen["path"] = req.url.path
        return httpx.Response(200, json={"response": {"email": "j@x.com"}})
    p = Prospeo(api_key="k", client=_client(h))
    assert p.find("Jane", "Doe", "x.com", "li/jane") == "j@x.com"
    assert seen["path"].endswith("/linkedin-email-finder")  # linkedin tried first
