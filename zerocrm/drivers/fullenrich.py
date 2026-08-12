"""FullEnrich — waterfall email finder (it internally cascades ~15-20 providers,
incl. Apollo/Prospeo/Findymail/Hunter, and charges only on a verified hit). Runs
as a later waterfall tier to sweep the long tail Findymail misses.

Async by design: submit a 1-row bulk job, then poll to completion. We poll
(no webhook needed for a single lookup); `sleep` is injectable so tests don't
wait. Found emails still pass zerocrm's verify gate.

Key from FULLENRICH_API_KEY. Fails soft: any error/timeout -> None.
"""

from __future__ import annotations

import os
import time

import httpx

BASE = "https://app.fullenrich.com/api/v2"


class FullEnrich:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None,
                 sleep=None, max_polls: int = 20, poll_wait: float = 3.0):
        self.api_key = api_key or os.environ.get("FULLENRICH_API_KEY", "")
        self._client = client or httpx.Client(timeout=30)
        self._sleep = sleep or time.sleep
        self._max_polls = max_polls
        self._poll_wait = poll_wait

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def find(self, first_name: str, last_name: str, domain: str,
             linkedin_url: str = "") -> str | None:
        if not (linkedin_url or (domain and (first_name or last_name))):
            return None
        enrichment_id = self._submit(first_name, last_name, domain, linkedin_url)
        if not enrichment_id:
            return None
        for _ in range(self._max_polls):
            email, done = self._poll(enrichment_id)
            if done:
                return email
            self._sleep(self._poll_wait)
        return None  # timed out — treat as a miss, waterfall moves on

    def _submit(self, first: str, last: str, domain: str, linkedin: str) -> str | None:
        # wire contract verified live 2026-08-09: key is `data` (not `datas`) and
        # row fields are snake_case (`first_name`); the API 400s otherwise.
        row: dict = {"enrich_fields": ["contact.emails"]}
        if first:
            row["first_name"] = first
        if last:
            row["last_name"] = last
        if domain:
            row["domain"] = domain
        if linkedin:
            row["linkedin_url"] = linkedin
        try:
            r = self._client.post(f"{BASE}/contact/enrich/bulk", headers=self._headers(),
                                  json={"name": "zerocrm", "data": [row]})
        except httpx.HTTPError:
            return None
        if r.status_code not in (200, 201):
            return None
        return (r.json() or {}).get("enrichment_id")

    def _poll(self, enrichment_id: str) -> tuple[str | None, bool]:
        """Returns (email, done). Not-ready -> (None, False); terminal states
        (FINISHED / credits-out / canceled / error) -> (email_or_None, True)."""
        try:
            r = self._client.get(f"{BASE}/contact/enrich/bulk/{enrichment_id}",
                                 headers=self._headers())
        except httpx.HTTPError:
            return None, True
        if r.status_code == 400:      # "not ready, try again"
            return None, False
        if r.status_code != 200:
            return None, True
        body = r.json() or {}
        status = body.get("status")
        if status == "FINISHED":
            return _extract(body), True
        if status in ("CREATED", "IN_PROGRESS", None):
            return None, False
        return None, True             # CANCELED / CREDITS_INSUFFICIENT / RATE_LIMIT / UNKNOWN

    def balance(self) -> int:
        try:
            r = self._client.get(f"{BASE}/account/credits", headers=self._headers())
        except httpx.HTTPError:
            return -1
        if r.status_code != 200:
            return -1
        body = r.json() or {}
        val = body.get("balance", body.get("credits"))
        try:
            return int(val)
        except (ValueError, TypeError):
            return -1


def _extract(body: dict) -> str | None:
    # verified live 2026-08-09: FINISHED body -> {data:[{contact_info:{...}}]},
    # emails are {email, status} objects under contact_info. Work email preferred.
    rows = body.get("data") or body.get("datas") or []
    if not rows:
        return None
    ci = rows[0].get("contact_info") or rows[0].get("contact") or rows[0]
    for key in ("most_probable_work_email", "most_probable_personal_email"):
        obj = ci.get(key)
        if isinstance(obj, dict) and obj.get("email"):
            return obj["email"].lower()
        if isinstance(obj, str) and obj:
            return obj.lower()
    for key in ("work_emails", "personal_emails", "emails"):
        lst = ci.get(key) or []
        if lst:
            first = lst[0]
            email = first.get("email") if isinstance(first, dict) else first
            if email:
                return email.lower()
    return None
