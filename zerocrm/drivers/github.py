"""GitHub buildability probe — the ICP's real qualifier: does this company
actually ship code we could run an agent against?

Matching is precision-first (name collisions are the enemy): try the org login
guessed from the domain root, then a name search, and only accept a candidate
whose profile blog/website matches the company domain (or an exact login match).
Returns repo count, top languages, and recency — the dev signal Apollo lacks.

Uses the `gh` CLI (authenticated). Fails soft: any error -> {"found": False}.
"""

from __future__ import annotations

import json
import re
import subprocess


def _gh(path: str) -> dict | list | None:
    try:
        out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None


def _domain_root(domain: str) -> str:
    d = re.sub(r"^www\.", "", (domain or "").lower())
    return d.split(".")[0] if d else ""


def _blog_matches(blog: str, domain: str) -> bool:
    return bool(blog) and _domain_root(domain) and _domain_root(domain) in blog.lower()


def github_probe(name: str, domain: str) -> dict:
    """Probe GitHub for the org account belonging to a company, precision-first.

    Tries the org login guessed from the domain root, then falls back to a
    name search (orgs only). A candidate is only accepted if its profile
    blog/website matches ``domain``, or its login exactly matches the domain
    root and it has at least one public repo — otherwise it's treated as a
    name collision and skipped.

    Args:
        name: The company's display name, used as a fallback GitHub org
            search query when the domain-derived login guess doesn't match.
        domain: The company's website domain (e.g. "example.com"), used to
            derive the candidate org login and to verify a candidate's
            profile blog/website.

    Returns:
        A dict shaped one of two ways:

        - No match: ``{"found": False}``
        - Match: ``{"found": True, "login": str, "public_repos": int,
          "top_languages": list[str], "last_push": str | None,
          "confidence": "high" | "medium"}``, where ``top_languages`` are
          the distinct languages of the org's most recently pushed repos
          (most recent first) and ``last_push`` is the ISO 8601 timestamp
          of the most recently pushed repo, or ``None`` if unavailable.
    """
    root = _domain_root(domain)
    candidates: list[str] = []
    if root:
        candidates.append(root)  # most orgs use their brand as the login

    # name search fallback (orgs only)
    if name:
        res = _gh(f"search/users?q={requote(name)}+type:org&per_page=5")
        if isinstance(res, dict):
            candidates += [u["login"] for u in res.get("items", []) if u.get("login")]

    seen: set[str] = set()
    for login in candidates:
        if login in seen:
            continue
        seen.add(login)
        prof = _gh(f"users/{login}")
        if not isinstance(prof, dict) or prof.get("message"):
            continue
        blog = prof.get("blog") or ""
        repos = prof.get("public_repos", 0)
        # accept only real matches: blog verifies the domain (high), or a
        # login match that actually ships code (medium). A 0-repo login guess
        # with no domain link is a name collision — skip it.
        if _blog_matches(blog, domain):
            confidence = "high"
        elif login == root and repos > 0:
            confidence = "medium"
        else:
            continue
        return {
            "found": True,
            "login": login,
            "public_repos": repos,
            "top_languages": _top_languages(login),
            "last_push": _last_push(login),
            "confidence": confidence,
        }
    return {"found": False}


def _top_languages(login: str, limit: int = 10) -> list[str]:
    repos = _gh(f"users/{login}/repos?sort=pushed&per_page={limit}")
    if not isinstance(repos, list):
        return []
    langs = [r.get("language") for r in repos if r.get("language")]
    # de-dupe preserving order
    seen, out = set(), []
    for lang in langs:
        if lang not in seen:
            seen.add(lang)
            out.append(lang)
    return out


def _last_push(login: str) -> str | None:
    repos = _gh(f"users/{login}/repos?sort=pushed&per_page=1")
    if isinstance(repos, list) and repos:
        return repos[0].get("pushed_at")
    return None


def requote(s: str) -> str:
    return re.sub(r"\s+", "+", s.strip())
