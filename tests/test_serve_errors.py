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


def test_a_failing_verb_logs_one_redacted_line(monkeypatch, capsys):
    import threading
    from http.server import ThreadingHTTPServer

    from zerocrm import serve

    def boom(engine):
        req = httpx.Request("GET", "https://x.test/v1/c?api_key=SECRET1")
        raise httpx.HTTPStatusError(f"Client error '400 Bad Request' for url '{req.url}'",
                                    request=req, response=httpx.Response(400, request=req))

    monkeypatch.setitem(serve._VERBS, "/tick", boom)
    monkeypatch.setattr(serve, "make_engine", lambda: None)
    monkeypatch.setenv("ZEROCRM_TRIGGER_TOKEN", "t")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = httpx.post(f"http://127.0.0.1:{srv.server_port}/tick", headers={"X-Zerocrm-Token": "t"})
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert r.status_code == 500 and "SECRET1" not in r.text
    assert "POST /tick -> 500" in out and "SECRET1" not in out
