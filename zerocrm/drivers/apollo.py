"""Apollo — prospect search + enrichment. Data provenance is `enriched`, so the
precedence gate lets it fill blanks but never clobber human/reply-verified facts.
"""

from __future__ import annotations

import os

import httpx

BASE = "https://api.apollo.io/api/v1"


class Apollo:
    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key or os.environ.get("APOLLO_API_KEY", "")
        self._client = client or httpx.Client(timeout=30)

    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    def search_people(self, per_page: int = 25, page: int = 1, **filters) -> list[dict]:
        """POST /mixed_people/api_search (the search endpoint for API callers;
        mixed_people/search is deprecated for API keys). Returns normalized people."""
        body = {"per_page": per_page, "page": page, **filters}
        r = self._client.post(f"{BASE}/mixed_people/api_search", json=body, headers=self._headers())
        r.raise_for_status()
        return [normalize(p) for p in r.json().get("people", [])]

    def match(self, first_name: str = "", last_name: str = "", domain: str = "",
              apollo_id: str = "", email: str = "", reveal: bool = True) -> dict | None:
        """POST /people/match — enriches a person to the full record (headline,
        seniority, location, org industry/size/keywords/website). With reveal it
        also unmasks the email. Match by id > email > name+domain. CONSUMES a
        credit; call only for ICP-qualified people, then verify via ZeroBounce."""
        body = {"reveal_personal_emails": reveal, "reveal_phone_number": False}
        if apollo_id:
            body["id"] = apollo_id
        elif email:
            body["email"] = email
        else:
            body.update({"first_name": first_name, "last_name": last_name,
                         "domain": domain})
        r = self._client.post(f"{BASE}/people/match", json=body, headers=self._headers())
        r.raise_for_status()
        person = r.json().get("person")
        return normalize(person) if person else None


def normalize(p: dict) -> dict:
    """Apollo person -> {identity, fields} for the precedence upsert. Email may be
    masked/absent in search results; callers skip people without one."""
    name = p.get("name") or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
    org = p.get("organization") or {}
    return {
        "email": (p.get("email") or "").lower() or None,
        "apollo_id": p.get("id"),  # reveal by id (search results omit the domain)
        "fields": {
            "full_name": name or None,
            "job_title": p.get("title"),
            "source": "apollo",
        },
        "linkedin": p.get("linkedin_url"),
        "company_domain": (org.get("primary_domain") or "").lower() or None,
        "company_name": org.get("name"),
        # rich data for judging the prospect (only present on match/enrich)
        "enrichment": {
            "headline": p.get("headline"),
            "seniority": p.get("seniority"),
            "city": p.get("city"), "state": p.get("state"), "country": p.get("country"),
            "departments": p.get("departments"),
            "functions": p.get("functions"),
            # buying-intent (populated only when the Apollo Intent add-on is on)
            "intent_strength": p.get("intent_strength"),
            "show_intent": p.get("show_intent"),
            "email_status": p.get("email_status"),   # Apollo's own deliverability grade
            "photo_url": p.get("photo_url"),
            "twitter_url": p.get("twitter_url"),
            "github_url": p.get("github_url"),        # personal dev signal when present
            "employment_history": [
                {"company": h.get("organization_name"), "title": h.get("title"),
                 "current": h.get("current")}
                for h in (p.get("employment_history") or [])[:5]
            ],
            "apollo_id": p.get("id"),
        },
        "company_enrichment": {
            "website": org.get("website_url"),
            "linkedin": org.get("linkedin_url"),      # the LinkedIn company page
            "industry": org.get("industry"),
            "industries": org.get("industries"),
            "employees": org.get("estimated_num_employees"),
            "headcount_growth_6mo": org.get("organization_headcount_six_month_growth"),
            "annual_revenue": org.get("annual_revenue"),
            "total_funding": org.get("total_funding"),
            "latest_funding_stage": org.get("latest_funding_stage"),
            "phone": org.get("phone"),
            "keywords": (org.get("keywords") or [])[:30],
            "technologies": org.get("technology_names"),  # FULL stack — the AI-adopter signal
            "description": org.get("short_description"),
            "founded_year": org.get("founded_year"),
        },
    }
