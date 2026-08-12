"""One-tap action links for the digest.

A digest button is an HMAC-signed (item_no, action) link to the worker's /act
endpoint, so the operator approves/skips without composing a reply. The signature
IS the auth — email clients can't attach custom headers, so a per-item, unguessable,
constant-time-checked token in the URL is the pragmatic equivalent of the
reply-path's DKIM+allowlist gate. Downstream apply is idempotent, so a re-clicked
link is a no-op.

Secret: ZEROCRM_ACTION_SECRET, falling back to ZEROCRM_TRIGGER_TOKEN (already set
on the worker). No secret -> empty signatures, and verify() fails closed.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from urllib.parse import quote


def _secret() -> str:
    return os.environ.get("ZEROCRM_ACTION_SECRET") or os.environ.get("ZEROCRM_TRIGGER_TOKEN", "")


def sign(item_no: int, action: str, secret: str | None = None) -> str:
    key = (secret if secret is not None else _secret()).encode()
    if not key:
        return ""  # no secret -> unsigned; verify() will reject
    return hmac.new(key, f"{item_no}:{action}".encode(), hashlib.sha256).hexdigest()[:32]


def verify(item_no: int, action: str, sig: str, secret: str | None = None) -> bool:
    expected = sign(item_no, action, secret)
    return bool(expected) and hmac.compare_digest(expected, sig or "")


def action_url(base_url: str, item_no: int, action: str, secret: str | None = None) -> str:
    """A ready-to-click link: {base}/act?item=N&action=ok|skip&sig=..."""
    sig = quote(sign(item_no, action, secret))
    return f"{base_url.rstrip('/')}/act?item={item_no}&action={quote(action)}&sig={sig}"
