"""MillionVerifier — cheap bulk-grade email verification. Stage 1 of the verify
gate: it grades most addresses at a fraction of ZeroBounce's price. Only its
`catch_all` results escalate to ZeroBounce's confidence scoring (see verify.py),
which is the one signal that grades an un-provable catch-all mailbox.

Key from MILLIONVERIFIER_API_KEY. Balance is read from the `credits` field the
API returns on every validate (no separate credits endpoint), so balance() is
only meaningful after at least one validate this run.
"""

from __future__ import annotations

import os

import httpx

BASE = "https://api.millionverifier.com/api/v3/"  # trailing slash: the API 301s without it


class MillionVerifier:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key or os.environ.get("MILLIONVERIFIER_API_KEY", "")
        self._client = client or httpx.Client(timeout=30)
        self._last_credits = -1

    def validate(self, email: str, timeout: int = 20) -> dict:
        r = self._client.get(BASE, params={"api": self.api_key, "email": email, "timeout": timeout})
        r.raise_for_status()
        res = r.json()
        try:
            self._last_credits = int(res.get("credits", self._last_credits))
        except (ValueError, TypeError):
            pass
        return res

    @staticmethod
    def classify(res: dict) -> str:
        """Map a validate() response to send | block | catchall | risky."""
        result = (res.get("result") or "").lower()
        if result == "ok":
            return "send"
        if result in ("invalid", "disposable"):
            return "block"
        if result == "catch_all":
            return "catchall"          # escalate to ZeroBounce scoring
        return "risky"                 # unknown / error / empty

    def balance(self) -> int:
        """Credits from the last validate this run; -1 until one runs. Exhaustion
        also surfaces loudly in the validate response, so this is a soft guard."""
        return self._last_credits
