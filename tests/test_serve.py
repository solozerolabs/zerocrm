import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from sqlalchemy import insert

from zerocrm import serve
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import person, staff


@pytest.fixture()
def server_url(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    migrate(engine)
    with engine.begin() as conn:
        conn.execute(insert(staff).values(id="u1", full_name="Ana", api_token="secret-token"))
        conn.execute(insert(staff).values(id="u2", full_name="Off", api_token="secret-token", active=False))
        conn.execute(insert(person).values(id="p1", full_name="Jamie Lead"))
    monkeypatch.setattr(serve, "make_engine", lambda *a, **k: engine)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join()


def _get(base_url: str, path: str, token: str | None = None):
    req = urllib.request.Request(f"{base_url}{path}")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_export_401_when_authorization_header_missing(server_url):
    status, body = _get(server_url, "/contacts/export?staff_id=u1")
    assert status == 401
    assert body == {"error": "unauthorized"}


def test_export_401_when_token_does_not_match_stored_token(server_url):
    status, body = _get(server_url, "/contacts/export?staff_id=u1", token="wrong-token")
    assert status == 401
    assert body == {"error": "unauthorized"}


def test_export_401_for_deactivated_staff_even_with_matching_token(server_url):
    status, body = _get(server_url, "/contacts/export?staff_id=u2", token="secret-token")
    assert status == 401
    assert body == {"error": "unauthorized"}


def test_export_200_with_contacts_when_token_matches(server_url):
    status, body = _get(server_url, "/contacts/export?staff_id=u1", token="secret-token")
    assert status == 200
    assert body["contacts"] == [{"id": "p1", "full_name": "Jamie Lead", "job_title": None, "company_id": None}]
