"""Smartlead — email send lane. Implements the Sender protocol.

Under the entry-gate model (spec D9) a "send" is enrolling a person into a
Smartlead campaign; Smartlead then owns step advancement, rotation, scheduling.
already_sent() checks campaign membership so the executor's crash-recovery path
never double-enrolls.

Read-only endpoints here (list_email_accounts, warmup_stats, set_warmup) were
probed live on the Base tier 2026-08-08. enroll/already_sent shapes are unit-
tested against mocks and verified live when the first real enrollment runs.
"""

from __future__ import annotations

import os

import httpx

BASE = "https://server.smartlead.ai/api/v1"
channel = "email"


class Smartlead:
    channel = "email"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key or os.environ.get("SMARTLEAD_API_KEY", "")
        self._client = client or httpx.Client(timeout=30)

    def _params(self, **extra) -> dict:
        return {"api_key": self.api_key, **extra}

    # --- read-only (probed live) -------------------------------------------
    def list_email_accounts(self) -> list[dict]:
        r = self._client.get(f"{BASE}/email-accounts/", params=self._params())
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])

    def account(self, account_id: int) -> dict:
        """Single-account GET — carries warmup_details.warmup_reputation. The LIST
        endpoint reports nulls for warmup fields (live-learned), so read here."""
        r = self._client.get(f"{BASE}/email-accounts/{account_id}/", params=self._params())
        r.raise_for_status()
        return r.json()

    def warmup_stats(self, account_id: int) -> dict:
        r = self._client.get(f"{BASE}/email-accounts/{account_id}/warmup-stats",
                             params=self._params())
        r.raise_for_status()
        return r.json()

    def set_warmup(self, account_id: int, total_per_day: int,
                   reply_rate: int = 30, ramp: int = 5) -> dict:
        # daily_rampup floor is 5; warmup_min_count is derived, not settable (learned live).
        r = self._client.post(
            f"{BASE}/email-accounts/{account_id}/warmup",
            params=self._params(),
            json={"warmup_enabled": True, "total_warmup_per_day": total_per_day,
                  "daily_rampup": ramp, "reply_rate_percentage": reply_rate},
        )
        r.raise_for_status()
        return r.json()

    def set_sequence(self, campaign_id: int, sequences_payload: dict) -> dict:
        """Set the campaign's 4-touch sequence (delays + variants + spintax). This
        REPLACES the whole sequence; the campaign must NOT be ACTIVE when called.
        Payload shape from copy.to_smartlead — verify variant field names live
        before the first real POST (see copy.to_smartlead docstring)."""
        r = self._client.post(
            f"{BASE}/campaigns/{campaign_id}/sequences",
            params=self._params(), json=sequences_payload,
        )
        r.raise_for_status()
        return r.json()

    # --- Sender protocol ---------------------------------------------------
    def send(self, item: dict) -> str:
        """Enroll the item's lead into its campaign. payload must carry
        {"campaign_ref": <id>, "lead": {"email":..., "first_name":..., ...}}."""
        payload = item["payload"]
        campaign_id = payload["campaign_ref"]
        lead = payload["lead"]
        r = self._client.post(
            f"{BASE}/campaigns/{campaign_id}/leads",
            params=self._params(),
            json={"lead_list": [lead]},
        )
        r.raise_for_status()
        body = r.json()
        return str(body.get("upload_count") or body.get("id") or "enrolled")

    def already_sent(self, item: dict) -> bool:
        payload = item["payload"]
        campaign_id = payload["campaign_ref"]
        email = payload["lead"]["email"]
        r = self._client.get(f"{BASE}/leads/", params=self._params(email=email))
        if r.status_code == 404:
            return False
        r.raise_for_status()
        lead = r.json()
        # a lead already in this campaign carries it in its campaign associations
        campaigns = lead.get("campaign_ids") or [
            c.get("campaign_id") for c in lead.get("campaigns", [])
        ]
        return campaign_id in campaigns

    # --- inbound event normalizer (shared by poll AND any future webhook) --
    # KISS: we ingest ONLY reply + bounce. Opens/clicks/sends would flood the
    # timeline with no decision value; they are dropped at the normalizer.
    @staticmethod
    def normalize_event(event: dict) -> dict | None:
        """Map a Smartlead reply/bounce (webhook OR poll row) to a touch dict.
        Returns None for anything we don't ingest. `event_key` is the dedupe key
        the idempotent ingest upserts on."""
        kind_map = {"EMAIL_REPLY": "reply", "EMAIL_BOUNCE": "bounce"}
        etype = event.get("event_type")
        kind = kind_map.get(etype)
        if not kind:
            return None
        # the PROSPECT's email. lead_email is unambiguous if present (poll path);
        # else it depends on direction: on a REPLY the prospect is the SENDER
        # (from_email; to_email is our mailbox), on a BOUNCE it's who we sent TO
        # (to_email). Verified against Smartlead's documented EMAIL_REPLY payload
        # 2026-08-09; confirm the exact prospect field on the first real webhook.
        if event.get("lead_email"):
            email = event["lead_email"].lower()
        elif kind == "reply":
            email = (event.get("from_email") or event.get("to_email") or "").lower()
        else:
            email = (event.get("to_email") or event.get("from_email") or "").lower()
        # stable provider id for exactly-once. message_id preferred; fall back to
        # an event id, else a deterministic composite (email+kind+timestamp).
        ts = (event.get("time") or event.get("event_timestamp")
              or event.get("time_replied") or event.get("stats_id") or "")
        event_key = str(
            event.get("message_id") or event.get("event_id") or event.get("stats_id")
            or f"{email}:{kind}:{ts}"
        )
        # reply body (webhook only; poll rows carry none). Tolerant across field
        # names because the exact EMAIL_REPLY key is UNVERIFIED against a live
        # payload — confirm before relying on auto-draft; poll is the backstop.
        body = None
        for k in ("reply_body", "reply_message", "reply_text", "email_body",
                  "message_body", "message", "body", "stats_reply_text"):
            if event.get(k):
                body = event[k]
                break
        return {
            "kind": kind,
            "email": email,
            "direction": "in",
            "event_key": event_key,
            "message_id": event.get("message_id") or event.get("reply_message_id") or None,
            "body": body,
            "campaign_ref": str(event.get("campaign_id")) if event.get("campaign_id") else None,
            "raw": event,
        }

    # back-compat alias (parse_webhook was the old name)
    parse_webhook = normalize_event

    # Smartlead requires a non-empty reply-category filter; this is the full valid
    # set (verified live 2026-08-09) = catch every reply. Smartlead webhooks are
    # UNSIGNED, so the receiver authenticates via a token in the URL, not HMAC.
    REPLY_CATEGORIES = ("Interested", "Meeting Request", "Not Interested",
                        "Information Request", "Out Of Office", "Wrong Person",
                        "Do Not Contact")

    def register_webhook(self, campaign_id: int, url: str, name: str = "zerocrm",
                         event_types=("EMAIL_REPLY",), categories=None) -> dict:
        """Register a webhook on a campaign (applied at campaign creation). Verified
        live: `categories` is required non-empty; defaults to all reply categories."""
        r = self._client.post(
            f"{BASE}/campaigns/{campaign_id}/webhooks", params=self._params(),
            json={"name": name, "webhook_url": url, "event_types": list(event_types),
                  "categories": list(categories or self.REPLY_CATEGORIES)},
        )
        r.raise_for_status()
        return r.json()

    def poll_events(self, campaign_id: int, since_iso: str) -> list[dict]:
        """Poll replies/bounces for a campaign since a cursor, normalized like
        normalize_event. Lower bound only: live 2026-10-02 Smartlead 400s
        `"event_time_lt" is not allowed`, which failed every tick. Rate limit is
        60 req/60s."""
        r = self._client.get(
            f"{BASE}/campaigns/{campaign_id}/leads-statistics",
            params=self._params(event_time_gt=since_iso),
        )
        r.raise_for_status()
        rows = r.json().get("data", r.json()) if isinstance(r.json(), dict) else r.json()
        out: list[dict] = []
        for row in rows or []:
            email = (row.get("lead_email") or row.get("to_email") or "").lower()
            for field, etype in (("replied_at", "EMAIL_REPLY"), ("bounced_at", "EMAIL_BOUNCE")):
                if row.get(field):
                    ev = self.normalize_event({
                        "event_type": etype, "lead_email": email,
                        "campaign_id": campaign_id, "time": row[field],
                        "message_id": row.get("message_id"),
                    })
                    if ev:
                        out.append(ev)
        return out
