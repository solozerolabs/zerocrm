"""Pre-call brief — the deterministic floor always runs; LLM synthesis activates
only when ANTHROPIC_API_KEY is set (copy_llm.llm_complete). Synthesized live per
request, never stored (if it ever needs caching, reuse the `research` table)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .copy_llm import llm_complete
from .memory import retrieve
from .schema import call, deal, person, touch, transcript

_OPEN_DEAL_EXCLUDES = ("won", "lost", "closed")


def precall_brief(engine: Engine, *, person_id=None, deal_id=None, query: str = "",
                  workspace_id: str = "default") -> str:
    lines: list[str] = []
    with engine.connect() as conn:
        if person_id:
            p = conn.execute(select(person).where(person.c.id == person_id)).mappings().first()
            if p:
                lines.append(f"Person: {p['full_name'] or person_id}"
                             + (f" — {p['job_title']}" if p["job_title"] else ""))
        if deal_id:
            d = conn.execute(select(deal).where(deal.c.id == deal_id)).mappings().first()
            if d:
                lines.append(f"Deal: stage={d['stage']}"
                             + (f", value=${d['value_usd']:.0f}" if d["value_usd"] else "")
                             + (f", next: {d['next_step']}" if d["next_step"] else ""))
        if person_id:
            recent = list(conn.execute(
                select(touch).where(touch.c.person_id == person_id)
                .order_by(touch.c.ts.desc()).limit(5)).mappings())
            if recent:
                lines.append("Recent: " + "; ".join(f"{t['kind']}/{t['direction']}" for t in recent))
            last = conn.execute(
                select(transcript.c.text).join(call, transcript.c.call_id == call.c.id)
                .where(call.c.person_id == person_id)
                .order_by(transcript.c.created_at.desc()).limit(1)).first()
            if last and last[0]:
                lines.append("Last call: " + last[0][:280])
    if query:
        hits = retrieve(engine, query, deal_id=deal_id, person_id=person_id, k=3)
        if hits:
            lines.append("Relevant: " + " | ".join(h["chunk_text"][:160] for h in hits))

    floor = "\n".join(lines) if lines else "No history for this contact yet."
    synth = llm_complete(
        "Write a 5-line pre-call brief for a sales rep. Be concrete, no fluff.",
        f"Facts:\n{floor}\nFocus: {query or 'general'}",
    )
    return synth or floor
