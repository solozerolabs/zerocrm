"""Slice 2: the LLM copy step that fills the `icebreaker_fn` seam.

The per-lead {{icebreaker}} is the only un-templatable line in the cold sequence
(copy.py owns the 4-touch campaign template). This turns it voice-perfect: one
Claude call per lead, fed the private voice profile + the learned rules ledger +
the best evidence we hold on that company. The deterministic ladder in
copy.py stays the FLOOR — any miss (no key, no profile, API error, or slop in the
output) returns None so draft_run falls back to it. Nothing here sends.

The voice profile is INJECTED (a param, sourced from a deploy secret), never
committed: this is a public MIT repo and the profile is private. Bring your own.
"""

from __future__ import annotations

import os

import httpx

from .copy import clean_company, lint_copy, strip_leading_name

BASE = "https://api.anthropic.com/v1/messages"
_DEFAULT_MODEL = "claude-sonnet-5"

_TASK = (
    "Write the opening of a cold email to {first} at {company}. Return EXACTLY two "
    "lines and nothing else:\n"
    "SUBJECT: a 2-5 word lowercase noun phrase naming the SAME specific thing the "
    "line below is about (no verbs, no greeting)\n"
    "ICEBREAKER: one sentence, under 20 words, that says something specific and true "
    "about their company from the evidence\n\n"
    "Framing rules (strict): ++/-- gains framing, never a gotcha. Point at what they "
    "could gain or stop babysitting, never mock their stack or imply they're behind. "
    "No greeting, no name, no compliment, no links, no em dashes, no marketing words, "
    "no 'you should', no 'in 2025'. Plain lowercase-ish register.\n\n"
    "Evidence:\n{evidence}"
)


def build_icebreaker_fn(voice_profile: str | None, rules: list[str] | None = None,
                        *, api_key: str | None = None, model: str | None = None,
                        complete=None):
    """Return a copy fn(hook, first, company, candidate) -> {"icebreaker","subject"}
    or None. None when the step can't run (no key/profile) OR the output can't be
    made lint-clean — either way draft_run keeps the deterministic floor. Subject
    and icebreaker come from ONE call so they cite the same evidence (can't clash).
    `complete(system, user) -> str` is injectable for tests."""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not (voice_profile and (key or complete)):
        return None
    call = complete or _anthropic(key, model or os.environ.get("ZEROCRM_COPY_MODEL", _DEFAULT_MODEL))
    system = _system(voice_profile, rules or [])

    def copy_fn(hook: dict | None, first: str, company: str,
                candidate: dict | None = None) -> dict | None:
        evidence = _evidence(hook, candidate)
        if not evidence:
            return None  # nothing specific to say -> let the floor's company line win
        user = _TASK.format(first=first or "there",
                            company=clean_company(company) or "your team",
                            evidence=evidence)
        try:
            raw = call(system, user)
        except Exception:  # noqa: BLE001 — any failure falls back to the floor
            return None
        return _parse_and_lint(raw, first)

    return copy_fn


def _system(voice_profile: str, rules: list[str]) -> str:
    parts = [voice_profile.strip(),
             "You are writing the opening line of a cold sales email in this voice."]
    if rules:
        parts.append("Learned corrections to honor:\n" + "\n".join(f"- {r}" for r in rules))
    return "\n\n".join(parts)


def _evidence(hook: dict | None, candidate: dict | None) -> str:
    """The facts the model may draw on, strongest first: the cited research hook,
    then the enrichment we already paid for. Empty -> caller returns None."""
    lines: list[str] = []
    if hook and hook.get("text"):
        lines.append(f"Research: {str(hook['text']).strip()}")
    ce = (candidate or {}).get("company_enrichment") or {}
    gh = ce.get("github") or {}
    if gh.get("found") and gh.get("top_languages"):
        lines.append(f"GitHub: {gh.get('public_repos')} public repos, "
                     f"mostly {gh['top_languages'][0]}")
    if ce.get("technologies"):
        lines.append("Tech stack: " + ", ".join(str(t) for t in ce["technologies"][:5]))
    kws = [str(k) for k in (ce.get("keywords") or []) if "," not in str(k)]
    if kws:
        lines.append("Keywords: " + ", ".join(kws[:5]))
    if ce.get("industry"):
        lines.append(f"Industry: {ce['industry']}")
    return "\n".join(lines)


def _parse_and_lint(raw: str | None, first: str) -> dict | None:
    """Parse the SUBJECT/ICEBREAKER lines, repair the name-leak, then lint. Any
    surviving violation -> None (the clean deterministic floor wins). This is the
    write-time enforcement layer: bad copy never reaches the DB."""
    if not raw:
        return None
    subject = icebreaker = ""
    for ln in raw.splitlines():
        s = ln.strip()
        low = s.lower()
        if low.startswith("subject:"):
            subject = s.split(":", 1)[1].strip().strip('"').strip("'").strip()
        elif low.startswith("icebreaker:"):
            icebreaker = s.split(":", 1)[1].strip().strip('"').strip("'").strip()
    icebreaker = strip_leading_name(icebreaker, first)  # repair before judging
    if lint_copy(subject, icebreaker, first):
        return None
    return {"icebreaker": icebreaker, "subject": subject}


def llm_complete(system: str, user: str, *, model: str | None = None) -> str | None:
    """Shared LLM seam for the conversation-memory synthesizers (brief, review).
    Activates only when ANTHROPIC_API_KEY is set; returns None on no-key or any
    error so callers fall back to their deterministic floor. Nothing here sends."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        out = _anthropic(key, model or _DEFAULT_MODEL)(system, user)
        return out or None
    except Exception:
        return None


def _anthropic(api_key: str, model: str):
    client = httpx.Client(timeout=30)

    def complete(system: str, user: str) -> str:
        r = client.post(
            BASE,
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            # generous ceiling for two short lines; a hit means the model rambled,
            # so a truncated (mid-word) line is rejected rather than shipped.
            json={"model": model, "max_tokens": 512, "system": system,
                  "messages": [{"role": "user", "content": user}]},
        )
        r.raise_for_status()
        body = r.json() or {}
        if body.get("stop_reason") == "max_tokens":
            return ""  # cut off -> drop to the deterministic floor
        blocks = body.get("content") or []
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    return complete
