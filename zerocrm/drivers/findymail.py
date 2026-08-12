"""Findymail — synchronous email finder + verifier. Tier-1 of the enrichment
waterfall: cheap, high coverage, low bounce (it only returns emails it has
already SMTP-verified). Finds by LinkedIn URL first (highest precision), then by
name + company domain. Found emails still pass zerocrm's own verify gate.

Key from FINDYMAIL_API_KEY. Fails soft: any error -> None (person skipped).
"""

from __future__ import annotations

import os

import httpx

BASE = "https://app.findymail.com"


class Findymail:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key or os.environ.get("FINDYMAIL_API_KEY", "")
        self._client = client or httpx.Client(timeout=30)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def find(self, first_name: str, last_name: str, domain: str,
             linkedin_url: str = "") -> str | None:
        """Uniform finder interface (shared by every waterfall tier)."""
        if linkedin_url:
            em = self._post("/api/search/business-profile", {"linkedin_url": linkedin_url})
            if em:
                return em
        if domain and (first_name or last_name):
            name = f"{first_name} {last_name}".strip()
            return self._post("/api/search/name", {"name": name, "domain": domain})
        return None

    def _post(self, path: str, body: dict) -> str | None:
        try:
            r = self._client.post(f"{BASE}{path}", headers=self._headers(), json=body)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        return _email((r.json() or {}))

    def balance(self) -> int:
        """Finder credits remaining; -1 if unknown. The guard pauses this tier
        when it hits the floor (API also 402s loudly at zero)."""
        try:
            r = self._client.get(f"{BASE}/api/credits", headers=self._headers())
        except httpx.HTTPError:
            return -1
        if r.status_code != 200:
            return -1
        try:
            return int((r.json() or {}).get("credits", -1))
        except (ValueError, TypeError):
            return -1


def _email(body: dict) -> str | None:
    contact = body.get("contact") or body
    email = contact.get("email") if isinstance(contact, dict) else None
    return email.lower() if email else None
