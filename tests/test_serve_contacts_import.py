import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from zerocrm import serve
from zerocrm.db import make_engine
from zerocrm.migrate import migrate

TOKEN = "test-token"


@pytest.fixture()
def http_server(tmp_path, monkeypatch):
    dsn = f"sqlite:///{tmp_path / 'serve.db'}"
    monkeypatch.setenv("ZEROCRM_DATABASE_URL", dsn)
    monkeypatch.setenv("ZEROCRM_TRIGGER_TOKEN", TOKEN)
    migrate(make_engine(dsn))

    server = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        thread.join()


def _post(addr, path, body: str, token: str | None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(*addr, timeout=5)
    headers = {"Content-Length": str(len(body))}
    if token is not None:
        headers["X-Zerocrm-Token"] = token
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def test_import_endpoint_returns_summary(http_server):
    csv_body = "name,email,phone\nJane Doe,jane@acme.com,555-1000\n"
    status, data = _post(http_server, "/contacts/import", csv_body, token=TOKEN)

    assert status == 200
    assert data == {"imported": 1, "skipped": 0, "errors": []}


def test_import_endpoint_rejects_bad_token(http_server):
    csv_body = "name,email,phone\nJane Doe,jane@acme.com,555-1000\n"
    status, data = _post(http_server, "/contacts/import", csv_body, token="wrong")

    assert status == 401
    assert data == {"error": "unauthorized"}


def test_import_endpoint_rejects_non_utf8_body(http_server):
    conn = http.client.HTTPConnection(*http_server, timeout=5)
    bad_body = b"name,email,phone\n\xff\xfeJane,jane@acme.com,555\n"
    conn.request("POST", "/contacts/import", body=bad_body,
                 headers={"Content-Length": str(len(bad_body)), "X-Zerocrm-Token": TOKEN})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()

    assert resp.status == 400
    assert "utf-8" in data["error"]


def test_unknown_verb_still_404s(http_server):
    # regression: adding the /contacts/import special-case must not disturb the
    # existing _VERBS dispatch (/digest, /tick) for paths it doesn't recognize.
    status, _data = _post(http_server, "/nope", "", token=TOKEN)
    assert status == 404
    assert set(serve._VERBS) == {"/digest", "/tick"}
