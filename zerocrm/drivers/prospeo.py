"""Prospeo — waterfall email finder. Runs only when the primary source (Apollo)
returns no email, lifting coverage on the long tail. Finds by LinkedIn URL first
(highest precision) then by name + company domain. Found emails still pass
ZeroBounce before entering a sequence.

Key from PROSPEO_API_KEY. Fails soft: any error -> None (the person is skipped).
"""

from __future__ import annotations

import os

import httpx

BASE = "https://api.prospeo.io"


class Prospeo:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key or os.environ.get("PROSPEO_API_KEY", "")
        self._client = client or httpx.Client(timeout=30)

    def _headers(self) -> dict:
        return {"X-KEY": self.api_key, "Content-Type": "application/json"}

    def find_by_linkedin(self, linkedin_url: str) -> str | None:
        if not linkedin_url:
            return None
        return self._email(self._client.post(
            f"{BASE}/linkedin-email-finder", headers=self._headers(),
            json={"url": linkedin_url}))

    def find_email(self, first_name: str, last_name: str, domain: str) -> str | None:
        if not domain or not (first_name or last_name):
            return None
        return self._email(self._client.post(
            f"{BASE}/email-finder", headers=self._headers(),
            json={"first_name": first_name, "last_name": last_name, "company": domain}))

    def find(self, first_name: str, last_name: str, domain: str,
             linkedin_url: str = "") -> str | None:
        """Uniform waterfall finder interface. LinkedIn first, then name+domain."""
        return (self.find_by_linkedin(linkedin_url)
                or self.find_email(first_name, last_name, domain))

    def balance(self) -> int:
        return -1  # not wired; dormant fallback tier (its own errors fail soft)

    @staticmethod
    def _email(r: httpx.Response) -> str | None:
        if r.status_code != 200:
            return None
        resp = (r.json() or {}).get("response") or {}
        email = resp.get("email")
        return email.lower() if email else None
