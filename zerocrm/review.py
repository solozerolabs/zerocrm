"""Win/loss note on deal close, written as touch(kind=review) — no new deal
column. Deterministic floor always; LLM enriches reasons/sentiment when a key is
set (copy_llm.llm_complete)."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .copy_llm import llm_complete
from .schema import call, deal, touch, transcript

_ACTOR = {"kind": "agent", "id": "review"}


def write_review(engine: Engine, *, deal_id: str, outcome: str,
                 workspace_id: str = "default") -> str | None:
    """Read the deal's transcripts and record one review touch. Returns the touch
    id, or None if the deal is missing or has no person to hang the touch on."""
    with engine.begin() as conn:
        d = conn.execute(select(deal).where(deal.c.id == deal_id)).mappings().first()
        if not d or not d["person_id"]:
            return None
        texts = [r[0] for r in conn.execute(
            select(transcript.c.text).join(call, transcript.c.call_id == call.c.id)
            .where(call.c.deal_id == deal_id)).all() if r[0]]
        payload = _analyze(outcome, texts)
        return conn.execute(touch.insert().values(
            person_id=d["person_id"], channel="internal", direction="in", kind="review",
            payload=payload, actor=_ACTOR)).inserted_primary_key[0]


def _analyze(outcome: str, texts: list[str]) -> dict:
    joined = " ".join(texts)
    floor = {"outcome": outcome, "reasons": [joined[:200]] if joined else [],
             "sentiment": "unknown", "next_step": ""}
    if not joined:
        return floor
    raw = llm_complete(
        "Extract a deal review. Return ONLY JSON: "
        '{"reasons": [string], "sentiment": string, "next_step": string}.',
        f"Outcome: {outcome}\nTranscripts:\n{joined[:3000]}",
    )
    if raw:
        try:
            parsed = json.loads(raw)
            floor.update({k: parsed[k] for k in ("reasons", "sentiment", "next_step")
                          if k in parsed})
        except (json.JSONDecodeError, TypeError):
            pass  # keep the deterministic floor
    return floor
