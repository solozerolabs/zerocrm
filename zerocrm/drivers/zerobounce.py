"""ZeroBounce — email verification. A `valid` result stamps person_identity
verified_at (provenance verified_provider); the freshness gate reads that.
"""

from __future__ import annotations

import os

import httpx

BASE = "https://api.zerobounce.net/v2"

# statuses that mean "safe to send" for gate purposes
GOOD = {"valid"}
# statuses that should never enter a sequence
BAD = {"invalid", "spamtrap", "abuse", "do_not_mail"}


class ZeroBounce:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key or os.environ.get("ZEROBOUNCE_API_KEY", "")
        self._client = client or httpx.Client(timeout=30)

    def validate(self, email: str, ip: str = "") -> dict:
        r = self._client.get(
            f"{BASE}/validate",
            params={"api_key": self.api_key, "email": email, "ip_address": ip},
        )
        r.raise_for_status()
        return r.json()

    def score(self, email: str) -> int:
        """AI confidence 1-10 (0 = unknown) via /v2/scoring — the graded signal
        the verify gate thresholds a catch-all on. Not exposed by /validate."""
        r = self._client.get(f"{BASE}/scoring", params={"api_key": self.api_key, "email": email})
        r.raise_for_status()
        try:
            return int(float(r.json().get("score", 0)))
        except (ValueError, TypeError):
            return 0

    def credits(self) -> int:
        r = self._client.get(f"{BASE}/getcredits", params={"api_key": self.api_key})
        r.raise_for_status()
        try:
            return int(r.json().get("Credits", -1))
        except (ValueError, TypeError):
            return -1

    def balance(self) -> int:
        return self.credits()


def classify(result: dict) -> str:
    """Map a validate() response to send | block | risky."""
    status = (result.get("status") or "").lower()
    if status in GOOD:
        return "send"
    if status in BAD:
        return "block"
    return "risky"  # catch-all, unknown, greylisted — allow but flag
