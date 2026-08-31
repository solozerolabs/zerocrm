"""Tiny token-gated HTTP trigger so an external scheduler can wake a
scale-to-zero worker and run one verb. stdlib only — no web framework.

    POST /digest             -> run_digest
    POST /tick               -> run_tick
    POST /contacts/import    -> import_contacts_csv (body is raw CSV text)

Auth: header `X-Zerocrm-Token` must equal env ZEROCRM_TRIGGER_TOKEN (constant-time).
The machine auto-starts on the request and scales back to zero when idle (Fly).
"""

from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .db import make_engine
from .runner import run_digest, run_tick

_VERBS = {"/digest": run_digest, "/tick": run_tick}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/webhook/"):
            return self._webhook()
        if self.path == "/contacts/import":
            return self._import_contacts()
        if not self._trigger_authorized():
            return self._reply(401, {"error": "unauthorized"})
        verb = _VERBS.get(self.path)
        if not verb:
            return self._reply(404, {"error": "unknown verb"})
        try:
            result = verb(make_engine())
            self._reply(200, {"ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001 — return 500 so pg_cron logs the failure
            self._reply(500, {"ok": False, "error": str(exc)})

    def do_HEAD(self) -> None:  # noqa: N802
        # email link-scanners sometimes HEAD the booking URL; mirror the redirect
        # (no body) so they don't see the stdlib server's default 501.
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        target = os.environ.get("BOOK_REDIRECT_URL", "")
        code, loc = (301, target) if (target and host.startswith("book.") and self.path in ("/", "/book")) else (200, None)
        self.send_response(code)
        if loc:
            self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        # book.syndai.ai vanity -> 301 to the Google booking page. Host-gated so
        # the .fly.dev host is unaffected. Target in BOOK_REDIRECT_URL (a secret,
        # so changing the underlying schedule needs no redeploy).
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        target = os.environ.get("BOOK_REDIRECT_URL", "")
        if target and host.startswith("book.") and self.path in ("/", "/book"):
            self.send_response(301)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/health":
            return self._reply(200, {"ok": True})
        if self.path.startswith("/act"):
            return self._act()
        return self._reply(404, {})

    def _webhook(self) -> None:
        """Provider webhook receiver (POST /webhook/<provider>?token=...). Smartlead
        webhooks are unsigned, so the URL token IS the auth (constant-time). Feeds
        the same path as the poll; poll remains the backstop on silent failure."""
        parsed = urlparse(self.path)
        provider = parsed.path.rsplit("/", 1)[-1]
        token = (parse_qs(parsed.query).get("token") or [""])[0]
        secret = os.environ.get("ZEROCRM_WEBHOOK_TOKEN") or os.environ.get("ZEROCRM_TRIGGER_TOKEN", "")
        if not secret or not hmac.compare_digest(secret, token):
            return self._reply(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except (ValueError, json.JSONDecodeError):
            return self._reply(400, {"error": "bad json"})
        # log field NAMES only (not values/PII) so the real Smartlead payload
        # shape can be verified from fly logs; drop once the body field is confirmed.
        print(f"WEBHOOK {provider} keys={sorted(payload.keys())} "
              f"event_type={payload.get('event_type')}", flush=True)
        from .runner import load_runtime_config
        from .webhooks import ingest_provider_webhook
        try:
            engine = make_engine()
            res = ingest_provider_webhook(engine, provider, payload, load_runtime_config(engine))
            self._reply(200, {"ok": True, "result": res})
        except Exception as exc:  # noqa: BLE001 — 500 so the provider (and our poll) knows it failed
            self._reply(500, {"ok": False, "error": str(exc)})

    def _trigger_authorized(self) -> bool:
        token = os.environ.get("ZEROCRM_TRIGGER_TOKEN", "")
        given = self.headers.get("X-Zerocrm-Token", "")
        return bool(token) and hmac.compare_digest(token, given)

    def _import_contacts(self) -> None:
        """POST /contacts/import — body is raw CSV text (header: name,email,phone).
        Same token gate as /digest and /tick. Replies with the import summary
        dict directly (imported/skipped/errors) as the response body."""
        if not self._trigger_authorized():
            return self._reply(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        from .contacts_import import import_contacts_csv
        try:
            body = self.rfile.read(length).decode("utf-8") if length else ""
            summary = import_contacts_csv(make_engine(), body, actor={"kind": "human", "id": "api"})
            self._reply(200, summary)
        except UnicodeDecodeError:
            self._reply(400, {"error": "body must be utf-8 CSV text"})
        except Exception as exc:  # noqa: BLE001 — return 500, mirror /digest and /tick
            self._reply(500, {"ok": False, "error": str(exc)})

    def _act(self) -> None:
        """One-tap approve/skip from a digest button. The HMAC sig in the query
        IS the auth (email links can't carry headers). Idempotent + fail-closed."""
        from .actions import verify
        from .digest import apply_decisions

        q = parse_qs(urlparse(self.path).query)
        try:
            item = int((q.get("item") or ["0"])[0])
        except ValueError:
            item = 0
        action = (q.get("action") or [""])[0]
        sig = (q.get("sig") or [""])[0]
        if not item or action not in ("ok", "skip") or not verify(item, action, sig):
            return self._reply_html(403, "Invalid or expired link.")
        applied = apply_decisions(
            make_engine(), {item: {"action": "approve" if action == "ok" else "skip"}},
            actor={"kind": "human", "id": "one-tap"})
        if applied["approved"] or applied["skipped"]:
            verb = "approved ✓" if action == "ok" else "skipped"
            return self._reply_html(200, f"#{item} {verb}. You can close this tab.")
        return self._reply_html(200, f"#{item} was already handled.")

    def _reply_html(self, code: int, message: str) -> None:
        page = (f'<!doctype html><meta name="viewport" content="width=device-width">'
                f'<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
                f'max-width:420px;margin:15% auto;text-align:center;font-size:18px;color:#1a1a1a">'
                f'{message}</div>').encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def _reply(self, code: int, body: dict) -> None:
        payload = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # keep stdout clean; Fly captures it anyway
        pass


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), _Handler).serve_forever()


if __name__ == "__main__":
    main()
