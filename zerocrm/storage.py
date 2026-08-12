"""S3-compatible blob storage (Cloudflare R2) for docs-on-record — the deferred
Slice-2 storage seam. No boto3: the repo already ships httpx, and SigV4 for a
PUT/GET/DELETE is a focused, live-verifiable signer. BYO bucket via env, activates
only when configured (same idiom as the other drivers).

Env: ZEROCRM_S3_ENDPOINT, ZEROCRM_S3_BUCKET, ZEROCRM_S3_ACCESS_KEY_ID,
ZEROCRM_S3_SECRET_ACCESS_KEY (region is 'auto' for R2)."""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx

_ALG = "AWS4-HMAC-SHA256"
_UNSIGNED = "UNSIGNED-PAYLOAD"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    k = _hmac(("AWS4" + secret).encode(), datestamp)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


class Storage:
    """Path-style S3 client. `client` is injectable for tests (httpx.MockTransport)."""

    def __init__(self, *, endpoint, bucket, access_key, secret_key, region="auto",
                 client: httpx.Client | None = None):
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.host = urlsplit(self.endpoint).netloc
        self._client = client or httpx.Client(timeout=30)

    def _signed(self, method: str, key: str, *, body: bytes = b"", content_type=None,
                payload_hash: str | None = None) -> httpx.Request:
        now = datetime.now(timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        canonical_uri = "/" + self.bucket + "/" + quote(key, safe="/")
        ph = payload_hash or _sha256_hex(body)
        headers = {"host": self.host, "x-amz-content-sha256": ph, "x-amz-date": amzdate}
        if content_type:
            headers["content-type"] = content_type
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
        canonical_request = "\n".join(
            [method, canonical_uri, "", canonical_headers, signed_headers, ph])
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [_ALG, amzdate, scope, _sha256_hex(canonical_request.encode())])
        sig = hmac.new(_signing_key(self.secret_key, datestamp, self.region, "s3"),
                       string_to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            f"{_ALG} Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig}")
        url = f"{self.endpoint}/{self.bucket}/{quote(key, safe='/')}"
        return self._client.build_request(method, url, headers=headers, content=body)

    def put(self, key: str, data: bytes, *, content_type="application/octet-stream") -> str:
        req = self._signed("PUT", key, body=data, content_type=content_type,
                           payload_hash=_sha256_hex(data))
        self._client.send(req).raise_for_status()
        return f"s3://{self.bucket}/{key}"

    def get(self, key: str) -> bytes:
        req = self._signed("GET", key, payload_hash=_UNSIGNED)
        r = self._client.send(req)
        r.raise_for_status()
        return r.content

    def delete(self, key: str) -> None:
        req = self._signed("DELETE", key, payload_hash=_UNSIGNED)
        self._client.send(req).raise_for_status()


def enabled() -> bool:
    return all(os.environ.get(k) for k in
               ("ZEROCRM_S3_ENDPOINT", "ZEROCRM_S3_BUCKET",
                "ZEROCRM_S3_ACCESS_KEY_ID", "ZEROCRM_S3_SECRET_ACCESS_KEY"))


def from_env(client: httpx.Client | None = None) -> "Storage | None":
    """A Storage from env, or None when not configured (callers fall back to a
    caller-supplied uri — storage stays optional, same as the other drivers)."""
    if not enabled():
        return None
    return Storage(endpoint=os.environ["ZEROCRM_S3_ENDPOINT"],
                   bucket=os.environ["ZEROCRM_S3_BUCKET"],
                   access_key=os.environ["ZEROCRM_S3_ACCESS_KEY_ID"],
                   secret_key=os.environ["ZEROCRM_S3_SECRET_ACCESS_KEY"],
                   client=client)
