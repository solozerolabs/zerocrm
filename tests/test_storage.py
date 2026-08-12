import httpx

from zerocrm.storage import Storage


def _fake_bucket():
    store: dict[str, bytes] = {}
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["last"] = request
        path = request.url.path
        if request.method == "PUT":
            store[path] = request.content
            return httpx.Response(200)
        if request.method == "GET":
            return httpx.Response(200, content=store.get(path, b""))
        if request.method == "DELETE":
            store.pop(path, None)
            return httpx.Response(204)
        return httpx.Response(405)

    return store, seen, httpx.Client(transport=httpx.MockTransport(handler))


def _storage(client):
    return Storage(endpoint="https://acct.r2.cloudflarestorage.com", bucket="b",
                   access_key="AK", secret_key="SK", client=client)


def test_put_signs_and_returns_s3_uri():
    _, seen, client = _fake_bucket()
    s = _storage(client)
    uri = s.put("documents/d1/confirm.txt", b"hello", content_type="text/plain")
    assert uri == "s3://b/documents/d1/confirm.txt"
    req = seen["last"]
    assert req.method == "PUT"
    assert req.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AK/")
    assert "x-amz-content-sha256" in req.headers and "x-amz-date" in req.headers


def test_round_trip_put_get_delete():
    store, _, client = _fake_bucket()
    s = _storage(client)
    s.put("k/obj", b"payload")
    assert s.get("k/obj") == b"payload"
    s.delete("k/obj")
    assert s.get("k/obj") == b""  # gone
