"""GET /contacts: token-gated like the POST verbs, since it discloses PII."""

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from sqlalchemy import insert

from zerocrm import serve
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import person, person_identity


@pytest.fixture()
def contacts_server(monkeypatch, tmp_path):
    monkeypatch.setenv("ZEROCRM_TRIGGER_TOKEN", "s3cret")
    # file-backed (not "sqlite://") so the server thread's connection sees
    # the rows the fixture wrote from the test thread.
    eng = make_engine(f"sqlite:///{tmp_path/'t.db'}")
    migrate(eng)
    with eng.begin() as c:
        c.execute(insert(person).values(id="p1", full_name="Jane Doe"))
        c.execute(insert(person_identity).values(
            person_id="p1", kind="email", value="jane@acme.com"))
    monkeypatch.setattr(serve, "make_engine", lambda *a, **kw: eng)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join()


def test_contacts_requires_token(contacts_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{contacts_server}/contacts")
    assert exc.value.code == 401


def test_contacts_rejects_wrong_token(contacts_server):
    req = urllib.request.Request(
        f"{contacts_server}/contacts", headers={"X-Zerocrm-Token": "wrong"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 401


def test_contacts_returns_page_with_valid_token(contacts_server):
    req = urllib.request.Request(
        f"{contacts_server}/contacts", headers={"X-Zerocrm-Token": "s3cret"})
    resp = urllib.request.urlopen(req)
    body = resp.read().decode()
    assert resp.status == 200
    assert "Jane Doe" in body and "jane@acme.com" in body
    assert '<input id="contact-search"' in body
