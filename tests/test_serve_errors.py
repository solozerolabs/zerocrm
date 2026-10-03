"""A failing verb's 500 body must never echo a credential: httpx errors quote the full
URL, Smartlead/ZeroBounce take api_key in the query, and pg_net stores the body."""

import httpx

from zerocrm.serve import _safe_error


def test_error_reply_redacts_query_secrets():
    req = httpx.Request("GET", "https://x.test/v1/c?api_key=SECRET1&token=SECRET2&event=ok")
    exc = httpx.HTTPStatusError(f"Client error '400 Bad Request' for url '{req.url}'",
                                request=req, response=httpx.Response(400, request=req))
    msg = _safe_error(exc)
    assert "SECRET" not in msg
    assert "api_key=<redacted>" in msg and "event=ok" in msg
