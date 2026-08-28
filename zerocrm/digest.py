"""The digest: the only human surface.

- render()            builds the numbered email body from pending items
- parse_reply()       maps a terse reply back to per-item decisions (lenient;
                      anything ambiguous is HELD, never guessed into a send)
- authenticate_reply() the trust gate: DKIM-aligned + allowlisted + in-thread
- apply_decisions()   writes decisions onto digest_item + captures edits as
                      corrections (the learning loop's input)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import html as _html

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from .actions import action_url
from .autonomy import record_outcome
from .schema import company, correction, digest_item, person

# reply verbs
_APPROVE = {"ok", "okay", "yes", "y", "approve", "approved", "send", "go", "ship"}
_SKIP = {"skip", "no", "n", "drop", "kill", "pass"}
# words that mean "no real instruction follows" after an approve verb
_FILLER = {"as-is", "as", "is", "it", "this", "please", "thanks", "asis", "now", ""}

_LINE = re.compile(r"^\s*#?(\d+)\b[\s:.\-]*(.*)$")


def parse_reply(text: str) -> tuple[dict[int, dict], list[str]]:
    """Return (decisions, ambiguous_lines).

    decisions: {item_no: {"action": "approve"|"skip"|"edit", "instruction": str?}}
    Lines without a leading number or a recognizable verb are ambiguous and are
    returned for logging, NEVER turned into a send.
    """
    decisions: dict[int, dict] = {}
    ambiguous: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LINE.match(line)
        if not m:
            if _looks_like_a_command(line):
                ambiguous.append(raw)
            continue
        item_no = int(m.group(1))
        rest = m.group(2).strip()
        first = rest.split()[0].lower().rstrip(",.") if rest else ""
        remainder = rest[len(rest.split()[0]):].strip(" ,.-") if rest else ""

        if first in _SKIP:
            decisions[item_no] = {"action": "skip"}
        elif first in _APPROVE:
            instr = _strip_but(remainder)
            if instr and instr.lower() not in _FILLER:
                decisions[item_no] = {"action": "edit", "instruction": instr}
            else:
                decisions[item_no] = {"action": "approve"}
        elif first in ("edit", "change", "reword"):
            decisions[item_no] = {"action": "edit", "instruction": remainder or rest}
        else:
            ambiguous.append(raw)  # numbered but unrecognized verb -> HELD
    return decisions, ambiguous


def _strip_but(s: str) -> str:
    s = s.strip()
    for lead in ("but ", "however ", "though "):
        if s.lower().startswith(lead):
            return s[len(lead):].strip()
    return s


def _looks_like_a_command(line: str) -> bool:
    w = line.split()[0].lower() if line.split() else ""
    return w in _APPROVE | _SKIP | {"edit", "change", "reword"}


def authenticate_reply(meta: dict, config: dict) -> tuple[bool, str]:
    """Gate a reply before any decision is honored (D4).

    meta: {"from": addr, "dkim_pass": bool, "dkim_domain": domain,
           "has_auth_results": bool, "in_reply_to": message_id, "known_thread": bool}
    config: {"approver_identities": [...], "approver_domains": [...],
             "self_address": addr}   # the watched mailbox itself

    Two paths:
      - self-authored: a reply From the watched mailbox with NO external auth
        stamp never left the account (Gmail stamps all externally-received mail),
        so it is provably internal — accept. A spoofed From:self from outside
        WOULD carry an auth stamp (dkim=fail) and falls through to the external
        path, where it is rejected.
      - external: must pass DKIM alignment + be allowlisted.
    Returns (accepted, reason).
    """
    frm = (meta.get("from") or "").lower()
    domain = frm.split("@")[-1] if "@" in frm else ""
    ids = {a.lower() for a in config.get("approver_identities", [])}
    domains = {d.lower() for d in config.get("approver_domains", [])}
    self_addr = (config.get("self_address") or "").lower()

    if not meta.get("known_thread"):
        return False, "not_in_digest_thread"
    if frm and frm == self_addr and not meta.get("has_auth_results"):
        return True, "self_authored"
    if not meta.get("dkim_pass"):
        return False, "dkim_fail"
    if meta.get("dkim_domain", "").lower() != domain:
        return False, "dkim_misaligned"
    if frm not in ids and domain not in domains:
        return False, "sender_not_allowlisted"
    return True, "ok"


def apply_decisions(engine: Engine, decisions: dict[int, dict], actor: dict) -> dict:
    """Write decisions onto digest_item; capture edits as corrections. Only
    items currently in `draft` are actionable — a decision on an already-sent
    item is ignored (idempotent re-processing of a redelivered reply)."""
    now = datetime.now(timezone.utc)
    applied = {"approved": 0, "skipped": 0, "edited": 0, "ignored": 0}
    for item_no, d in decisions.items():
        with engine.begin() as conn:
            row = conn.execute(
                select(digest_item).where(digest_item.c.item_no == item_no)
            ).mappings().first()
            if not row or row["status"] != "draft":
                applied["ignored"] += 1
                continue
            action = d["action"]
            ws = row.get("workspace_id", "default")
            if action == "approve":
                conn.execute(_set(item_no, status="approved", now=now))
                record_outcome(conn, row["channel"], approved=True, workspace_id=ws)
                applied["approved"] += 1
            elif action == "skip":
                conn.execute(_set(item_no, status="skipped", now=now))
                record_outcome(conn, row["channel"], approved=False, workspace_id=ws)
                applied["skipped"] += 1
            elif action == "edit":
                conn.execute(
                    update(digest_item)
                    .where(digest_item.c.item_no == item_no)
                    .values(status="edited", diff={"instruction": d["instruction"]},
                            actor=actor, updated_at=now)
                )
                conn.execute(
                    correction.insert().values(
                        channel=row["channel"],
                        wrong=str(row["payload"]),
                        right=d["instruction"],
                        durable=False,   # durability gate (Phase 2) decides persistence
                        ts=now,
                        actor=actor,
                        workspace_id=ws,
                    )
                )
                record_outcome(conn, row["channel"], approved=False, workspace_id=ws)  # a correction breaks the streak
                applied["edited"] += 1
    return applied


def _set(item_no: int, *, status: str, now: datetime):
    return (
        update(digest_item)
        .where(digest_item.c.item_no == item_no)
        .values(status=status, updated_at=now)
    )


def render(items: list[dict], header: str = "") -> str:
    """Numbered, channel-grouped digest body. Item numbers are the stable
    digest_item.item_no, so replies never misattribute across days."""
    lines: list[str] = [header] if header else []
    by_channel: dict[str, list[dict]] = {}
    for it in items:
        by_channel.setdefault(it["channel"], []).append(it)
    for channel, group in by_channel.items():
        lines.append(f"\n{channel.upper()}")
        for it in group:
            summary = _summarize(it["payload"])
            lines.append(f"  #{it['item_no']} {it['kind']} · {summary}")
            why = _why(it["payload"])
            if why:
                lines.append(f"      why: {why}")
    return "\n".join(lines)


def build_cards(engine: Engine, items: list[dict]) -> list[dict]:
    """Enrich draft items with the person + company facts the HTML digest shows,
    so the operator can judge each prospect without leaving the email."""
    cards: list[dict] = []
    with engine.connect() as conn:
        for it in items:
            p = co = None
            if it.get("person_id"):
                p = conn.execute(select(person).where(person.c.id == it["person_id"])).mappings().first()
            if p and p["company_id"]:
                co = conn.execute(select(company).where(company.c.id == p["company_id"])).mappings().first()
            cards.append({"item": it, "person": dict(p) if p else {}, "company": dict(co) if co else {}})
    return cards


def render_html(cards: list[dict], *, base_url: str, notices: list[str] | None = None) -> str:
    """A scannable HTML digest: one card per prospect with the facts that decide
    fit, the drafted copy, the cited 'why', and Approve/Skip buttons that hit the
    worker's /act endpoint (one tap, no reply). Table + inline styles for email."""
    n = len(cards)
    head = "".join(f'<p style="margin:0 0 6px;padding:8px 12px;background:#fff8e1;'
                   f'border-left:3px solid #f5a623">{_html.escape(x)}</p>' for x in (notices or []))
    body = [f'<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;'
            f'margin:0 auto;color:#1a1a1a">{head}'
            f'<h2 style="font-size:18px">{n} to review</h2>']
    for c in cards:
        body.append(_card_html(c, base_url))
    body.append('<p style="color:#888;font-size:12px;margin-top:24px">One tap approves or '
                'skips. You can still reply in-thread (e.g. <code>#N ok</code> / '
                '<code>#N skip</code> / <code>#N ok but &lt;edit&gt;</code>).</p></div>')
    return "".join(body)


def _card_html(card: dict, base_url: str) -> str:
    it, p, co = card["item"], card["person"], card["company"]
    pe, ce = (p.get("enrichment") or {}), (co.get("enrichment") or {})
    payload = it.get("payload") or {}
    e = _html.escape

    name = e(p.get("full_name") or (payload.get("lead") or {}).get("email") or "?")
    title = e(p.get("job_title") or pe.get("headline") or "")
    comp = e(co.get("name") or "")
    dom = e(co.get("domain") or "")
    loc = e(", ".join(x for x in (pe.get("city"), pe.get("state")) if x))
    emp = e(str(co.get("size") or ce.get("employees") or ""))
    industry = e(co.get("industry") or "")

    # AI-adopter / tech signal (the dev-tool targeting tell)
    techs = ce.get("technologies") or []
    ai = [t for t in techs if any(k in (t or "").lower()
          for k in ("anthropic", "openai", "claude", "vercel", "langchain", "next.js", "github"))]
    tech = e(", ".join(ai[:4]) or ", ".join((ce.get("keywords") or [])[:3]))
    gh = ce.get("github") or {}
    gh_line = ""
    if gh.get("found"):
        gh_line = e(f"GitHub: {gh.get('public_repos','?')} repos · "
                    f"{', '.join(gh.get('top_languages') or [])} · {gh.get('confidence','')}")

    facts = " · ".join(x for x in [f"{emp} ppl" if emp else "", loc, industry] if x)
    copy_html = ""
    why = _why(payload)
    why_html = (f'<p style="margin:8px 0;padding:6px 10px;background:#eef6ff;border-radius:4px">'
                f'<b>why:</b> {e(why)}</p>') if why else ""

    # touch 1, merges filled (the rest of the 4-touch sequence is campaign-level)
    prev = payload.get("preview") or {}
    if prev:
        copy_html = (f'<div style="margin:6px 0"><b>{e(prev.get("subject",""))}</b><br>'
                     f'<span style="white-space:pre-wrap">{e(prev.get("body",""))}</span></div>'
                     f'<div style="font-size:11px;color:#999;margin-top:4px">+ 3 follow-ups in sequence</div>')

    ok = action_url(base_url, it["item_no"], "ok")
    skip = action_url(base_url, it["item_no"], "skip")
    btn = ('padding:9px 18px;border-radius:6px;text-decoration:none;font-weight:600;'
           'display:inline-block;font-size:14px')
    tech_html = f'<div style="font-size:13px;color:#555">{tech}</div>' if tech else ""
    gh_html = f'<div style="font-size:12px;color:#777">{gh_line}</div>' if gh_line else ""
    return (
        f'<div style="border:1px solid #e2e2e2;border-radius:8px;padding:16px;margin:12px 0">'
        f'<div style="font-size:12px;color:#888">#{it["item_no"]} · {e(it.get("kind",""))} · {e(it.get("channel",""))}</div>'
        f'<div style="font-size:16px;font-weight:700;margin-top:2px">{name}'
        f'{" — " + title if title else ""}</div>'
        f'<div style="font-size:14px;color:#333">{comp}'
        f'{" (" + dom + ")" if dom else ""}</div>'
        f'<div style="font-size:13px;color:#555;margin:4px 0">{facts}</div>'
        f'{tech_html}'
        f'{gh_html}'
        f'{why_html}'
        f'<div style="background:#fafafa;border-radius:6px;padding:10px;margin:8px 0;font-size:13px">{copy_html}</div>'
        f'<div style="margin-top:12px">'
        f'<a href="{ok}" style="{btn};background:#16a34a;color:#fff">✓ Approve</a>&nbsp;&nbsp;'
        f'<a href="{skip}" style="{btn};background:#f1f1f1;color:#333">✕ Skip</a>'
        f'</div></div>'
    )


def _why(payload: dict) -> str | None:
    """The cited research hook, one line, so the operator verifies before pasting."""
    if not isinstance(payload, dict):
        return None
    hook = payload.get("hook")
    if not isinstance(hook, dict) or not hook.get("text"):
        return None
    src = hook.get("evidence_title") or hook.get("evidence_url") or ""
    rec = hook.get("recency_days")
    tail = f" · {src}" if src else ""
    tail += f", {rec}d ago" if isinstance(rec, int) else ""
    return f"{hook['text']}{tail}"


def _summarize(payload: dict) -> str:
    if not isinstance(payload, dict):
        return str(payload)[:120]
    return (payload.get("summary") or payload.get("subject") or str(payload))[:120]
