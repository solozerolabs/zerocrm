"""Cold-email copy: the 4-touch sequence + per-lead personalization.

Two layers, matching how Smartlead actually works:
  1. default_sequence() — the CAMPAIGN-level template: 4 touches with widening
     delays, merge tags, spintax (deliverability), one-click unsubscribe. Set
     ONCE on the Smartlead campaign via smartlead.set_sequence.
  2. icebreaker_from_hook() — the PER-LEAD variable: one specific researched line
     that fills {{icebreaker}} in touch 1, so every send is un-templatable.

Rules encoded here come from .claude/skills/sid-voice/playbooks/cold-email.md
(the authority) + 2026 copy research: under-80-word touch 1, one claim/one proof/
soft reply-CTA, beta-invite offer (not a sale), same-thread follow-ups that ADD
(never "just following up"), day-0/3/8/18 cadence, breakup last, no links in cold
touches, no open tracking, unsubscribe every send. Anti-slop: no em dashes, no
"leverage/streamline/delve/seamless", no rule-of-three, contractions, lowercase,
short varied sentences. The voice-perfect 1:1 rewrite is the LLM copy step
(icebreaker_fn / the dogfood draft run) that reads the private voice profile.
"""

from __future__ import annotations

import re

# merge tags Smartlead fills per-lead; {{icebreaker}} is our per-lead custom field
_UNSUB = "{{unsubscribe_link}}"

# widening cadence: day 0, 3, 8, 18 (breakup clusters late per cold-email.md)
_DELAYS = [0, 3, 5, 10]


def default_sequence(config: dict | None = None) -> list[dict]:
    """The 4-touch campaign template. `sender_address` (config) is CAN-SPAM
    required; a missing one renders a loud placeholder so it can't ship unseen."""
    config = config or {}
    addr = config.get("sender_address", "[POSTAL ADDRESS REQUIRED]")
    foot = f"\n\nNot for you? {_UNSUB}\n{addr}"
    # Register: lowercase SUBJECTS (the measured +21%), normal sentences in the
    # body (AGENTS.md founder register: first person, stakes, real numbers).
    steps = [
        {  # touch 1 — relevance hook + one claim + one proof + soft beta CTA
            # subject is per-lead: a "[their-thing]" noun phrase custom field
            "subject": "{{subject_hook}}",
            "body": (
                # framing is deliberately ++ / -- (gains), not +- / -+ (warnings):
                # "turns a prompt into a PR" (++ more shipped), "no babysitting" +
                # "hands back" (-- less of the bad). No "or else you lose X" warning.
                "{{icebreaker}}\n\n"
                "Syndai turns a prompt into a production ready PR, no babysitting the "
                "agent. You ship more and get your hands back for the work only you "
                "can do.\n\n"
                "Solo founder, ex Meta staff eng. I'm giving a handful of agencies "
                "free credits to run it on their own repos before launch.\n\n"
                # soft try-it offer, NOT a demo/pilot ask; the receipt is the proof,
                # so no meeting is required to say yes. The call (if any) comes later.
                "{Want in|Worth a look}? I'll send a receipt, not a pitch.\n\nSid"
            ),
        },
        {  # touch 2 — same thread, NEW angle: the described receipt (how it works)
            "subject": "",
            "body": (
                "One more thing, {{first_name}}. The 20 second version of how it "
                "works:\n\n"
                "You point it at a repo, say what you want changed, and it comes back "
                "with a PR you'd actually merge. Tests included, plus a short note on "
                "what it touched. Nothing to sit and watch.\n\n"
                "Want me to run it on one of your repos?\n\nSid"
            ),
        },
        {  # touch 3 — same thread, peer proof + the accountability term
            "subject": "",
            "body": (
                "Last useful thing I'll send. I ran it on a {comparable|similar} "
                "agency's repo last week and it shipped a working PR first pass, no "
                "edits from them.\n\n"
                "If it doesn't clear the bar on yours, you owe me nothing and I'll "
                "tell you so myself. Still worth a look?\n\nSid"
            ),
        },
        {  # touch 4 — breakup, permission-to-close, zero pressure, no link
            "subject": "",
            "body": (
                "I'll close this out, {{first_name}}. Timing's probably off and "
                "that's fine.\n\n"
                "If agent written PRs ever get useful to you, just reply here and "
                "I'll pick it back up.\n\nSid"
            ),
        },
    ]
    for i, s in enumerate(steps):
        s["seq_number"] = i + 1
        s["delay_days"] = _DELAYS[i]
        s["body"] += foot
    return steps


_AI_TECH = ("anthropic", "claude", "openai", "vercel", "next.js", "langchain", "react")


def clean_company(name: str | None) -> str:
    """'Cappital - Custom App Development Firm' -> 'Cappital'. Trims the marketing
    suffix so icebreakers and merge tags read like a human wrote the name."""
    if not name:
        return ""
    for sep in (" | ", " – ", " — ", " - ", ", a ", ", A "):
        if sep in name:
            name = name.split(sep)[0]
    return name.strip()


def _usable_keyword(kw: str) -> bool:
    # skip Apollo industry-taxonomy strings ("technology, information & internet")
    return bool(kw) and "," not in kw and "&" not in kw and len(kw.split()) <= 3


_DEV_TERMS = ("develop", "software", "app", "engineering", "code", "devops",
              "modernization", "integration", "cloud", "api", "web design")


def _pick_keyword(ce: dict) -> str | None:
    """First DEV-flavored usable keyword, else first usable. 'inbound marketing'
    never headlines an email about shipping code."""
    kws = [str(k) for k in (ce.get("keywords") or []) if _usable_keyword(str(k))]
    for k in kws:
        if any(t in k.lower() for t in _DEV_TERMS):
            return k
    return kws[0] if kws else None


def icebreaker_from_hook(hook: dict | None, first: str, company: str,
                         candidate: dict | None = None) -> str:
    """The per-lead opener, best available evidence first:
      1. a cited research hook (Slice 2) -> the strongest, freshest line
      2. held enrichment (GitHub probe, tech stack, keywords) -> still specific,
         derived from data we already paid for
      3. a light company line (never a generic compliment) -> the floor
    The LLM copy step (icebreaker_fn) replaces all of this with voice-perfect
    per-lead copy; this deterministic ladder is the floor, not the ceiling."""
    them = company or "your team"
    if hook and hook.get("text"):
        t = str(hook["text"]).strip().rstrip(".")
        return f"Saw {them} is {t}."
    line = _from_enrichment(candidate or {}, them)
    if line:
        return line
    return f"Been looking at what {them} ships."


def _from_enrichment(candidate: dict, them: str) -> str | None:
    ce = candidate.get("company_enrichment") or {}
    gh = ce.get("github") or {}
    if gh.get("found") and gh.get("top_languages"):
        return (f"Saw {them}'s GitHub, {gh.get('public_repos')} public repos, "
                f"mostly {gh['top_languages'][0]}.")
    techs = [t for t in (ce.get("technologies") or [])
             if any(k in str(t).lower() for k in _AI_TECH)]
    if techs:
        return f"Saw {them} builds on {techs[0]}."
    kw = _pick_keyword(ce)
    if kw:
        return f"Saw {them} does {kw.lower()} for clients."
    return None


def subject_hook(candidate: dict, company: str) -> str:
    """Per-lead subject: a lowercase '[their-thing]' noun phrase (the 2026 winner),
    never empty (we fill the custom field at enroll, so no blank-subject risk)."""
    ce = (candidate or {}).get("company_enrichment") or {}
    techs = [t for t in (ce.get("technologies") or [])
             if any(k in str(t).lower() for k in _AI_TECH)]
    if techs:
        return f"your {str(techs[0]).lower()} builds"
    kw = _pick_keyword(ce)
    if kw:
        return f"your {kw.lower()} work"
    return f"{(clean_company(company) or 'your shop').lower()} + agents"


def warm_reply(first: str, config: dict | None = None, rules: list[str] | None = None) -> dict:
    """The apex (sid@syndai.ai) reply to an INTERESTED prospect. Sent same-thread
    from the human lane after a positive reply — so links are allowed here (unlike
    the cold sequence). Leads with the zero-friction path (send the receipt, no
    call), offers two plain-text windows + the booking link as the fallback. The
    `rules` (active learned rules) feed the LLM rewrite seam; the deterministic
    body below is the floor. booking_url unset -> loud placeholder."""
    config = config or {}
    booking = config.get("booking_url", "[BOOKING LINK REQUIRED]")
    site = config.get("site_url", "syndai.ai")
    body = (
        f"Thanks {first or 'there'}. Quickest path: point me at one repo and I'll "
        f"run it and send back the PR. No call needed, you'll see a real result.\n\n"
        f"If you'd rather talk it through first, I'm open Thursday or Friday "
        f"afternoon PT, or grab a time here: {booking}\n\n"
        f"More at {site} whenever you want it.\n\nSid"
    )
    return {"subject": "", "body": body}  # same-thread reply


# --- rendering --------------------------------------------------------------
def render_preview(step: dict, lead: dict) -> dict:
    """Fill merge tags + collapse spintax to the first option so the digest shows
    the ACTUAL first email the prospect would get. Preview only."""
    subj = _fill(step.get("subject") or "", lead)
    body = _fill(step.get("body") or "", lead).replace(_UNSUB, "unsubscribe")
    return {"subject": subj or "(same thread)", "body": body}


def _fill(text: str, lead: dict) -> str:
    out = _despin(text)
    for tag in ("first_name", "company_name", "icebreaker", "subject_hook"):
        out = out.replace("{{" + tag + "}}", str(lead.get(tag) or "").strip())
    return out


def _despin(text: str) -> str:
    """{a|b|c} -> a (first option). Non-nested; runs left to right."""
    return re.sub(r"\{([^{}|]+(?:\|[^{}|]+)+)\}", lambda m: m.group(1).split("|")[0], text)


# --- Smartlead API payload --------------------------------------------------
def to_smartlead(steps: list[dict]) -> dict:
    """Map to the /campaigns/{id}/sequences shape, VERIFIED live 2026-08-09:
    flat single-variant steps (seq_number + seq_delay_details + subject +
    email_body). The API rejects a `variants` array; per-send variation comes
    from inline spintax, not A/B variants (add seq_variants only when actually
    A/B testing). Blank subject on a follow-up = same-thread reply."""
    return {
        "sequences": [
            {
                "seq_number": s["seq_number"],
                "seq_delay_details": {"delay_in_days": s["delay_days"]},
                "subject": s.get("subject") or "",
                "email_body": s["body"],
            }
            for s in steps
        ]
    }


# --- anti-slop guard (used by tests; also handy at draft time) ---------------
_SLOP = ["—", "–", "leverage", "streamline", "delve", "seamless", "robust",
         "elevate", "unlock", "cutting-edge", "hope this finds you well",
         "just following up", "circling back", "i came across", "i noticed you"]


def slop_hits(text: str) -> list[str]:
    low = text.lower()
    return [w for w in _SLOP if w in low]


# --- draft-copy linter (the structural gate) --------------------------------
# One deterministic validator, enforced at THREE layers so a bad line can't ship:
#   1. write time  — copy_llm rejects/repairs LLM output that fails this
#   2. draft time  — build_enroll_payload falls back to the clean floor on a fail
#   3. a gate      — `zerocrm lint-drafts` + tests scan stored drafts / templates
# Tone (condescension) is primarily a PROMPT control; the tells below are the
# few high-precision ones worth a hard fail. Everything else lives in the prompt.
_CONDESCENSION = ["feels backwards", "duct tape", "in 2025", "in 2026",
                  "no cdn", "amateur", "sloppy", "you should", "obviously"]
_MAX_ICEBREAKER = 200


def strip_leading_name(text: str, first: str) -> str:
    """'edgar, saw acme...' -> 'saw acme...'. The icebreaker is not a greeting;
    the sequence handles the name. Repairs the common LLM name-leak in place."""
    if not (text and first):
        return text
    m = re.match(rf"\s*{re.escape(first)}\s*[,:\-]\s*", text, re.IGNORECASE)
    return text[m.end():] if m else text


def _links(text: str) -> bool:
    low = text.lower()
    return "http://" in low or "https://" in low or "www." in low


def lint_copy(subject: str, icebreaker: str, first: str = "") -> list[str]:
    """COLD per-lead icebreaker+subject rules (strict): short, no greeting, no
    links. Empty list == clean. Shared by the write-time guard, the draft-time
    fallback, and the lint-drafts gate."""
    v: list[str] = []
    ice = (icebreaker or "").strip()
    subj = (subject or "").strip()
    if not ice:
        v.append("empty icebreaker")
    if not subj:
        v.append("empty subject")
    if len(ice) > _MAX_ICEBREAKER:
        v.append(f"icebreaker too long ({len(ice)} > {_MAX_ICEBREAKER})")
    for s in slop_hits(ice) + slop_hits(subj):
        v.append(f"slop: {s!r}")
    if first and re.match(rf"\s*{re.escape(first)}\s*[,:\-]", ice, re.IGNORECASE):
        v.append(f"opens with the lead's name ({first!r})")
    low = ice.lower()
    for tell in _CONDESCENSION:
        if tell in low:
            v.append(f"condescension tell: {tell!r}")
    if _links(ice):
        v.append("link in cold copy")  # links belong in the WARM reply only
    return v


def lint_warm(body: str) -> list[str]:
    """WARM apex-reply rules (loose): links + a name greeting are ALLOWED here
    (unlike cold); only slop and emptiness fail. Placeholders that must be filled
    ([BOOKING LINK REQUIRED]) also fail so a template never sends unseen."""
    v: list[str] = []
    b = (body or "").strip()
    if not b:
        v.append("empty body")
    for s in slop_hits(b):
        v.append(f"slop: {s!r}")
    if "REQUIRED]" in b:
        v.append("unfilled placeholder")
    return v
