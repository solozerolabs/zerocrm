"""The one retrieval spine (CC-5). Transcripts and docs both feed memory_chunk.

ponytail: retrieval is a Python token-overlap rank over the candidate rows. At
SMB chunk counts a linear scan is nothing; the documented upgrade is a DB FTS /
pgvector index + an embedding column behind retrieve() — callers do not change.
"""

from __future__ import annotations

import re

from sqlalchemy.engine import Connection, Engine

from .schema import memory_chunk

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "or", "was", "is", "on", "in", "to", "of",
         "did", "why", "it", "for", "with", "at", "this", "that", "we", "they"}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


def chunk_text(text: str, size: int = 1200) -> list[str]:
    """Split on a fixed character window. Deliberately dumb — good enough for a
    linear-scan retriever; a semantic splitter is only worth it with embeddings."""
    text = text.strip()
    if not text:
        return []
    return [text[i:i + size] for i in range(0, len(text), size)]


def add_chunks(engine: Engine, *, source_type: str, source_id: str, texts,
               deal_id=None, person_id=None, workspace_id: str = "default",
               conn: Connection | None = None) -> int:
    """Insert chunk rows. Pass `conn` to write inside a caller's transaction
    (so a call and its chunks commit atomically)."""
    rows = [dict(source_type=source_type, source_id=source_id, chunk_no=i,
                 chunk_text=t, deal_id=deal_id, person_id=person_id,
                 workspace_id=workspace_id)
            for i, t in enumerate(texts) if t and t.strip()]
    if not rows:
        return 0
    if conn is not None:
        conn.execute(memory_chunk.insert(), rows)
    else:
        with engine.begin() as c:
            c.execute(memory_chunk.insert(), rows)
    return len(rows)


def ingest_document(engine: Engine, *, text: str, source_id: str,
                    deal_id=None, person_id=None, workspace_id: str = "default") -> int:
    return add_chunks(engine, source_type="document", source_id=source_id,
                      texts=chunk_text(text), deal_id=deal_id, person_id=person_id,
                      workspace_id=workspace_id)


def retrieve(engine: Engine, query: str, *, deal_id=None, person_id=None,
             k: int = 8, workspace_id: str = "default") -> list[dict]:
    q = memory_chunk.select().where(memory_chunk.c.workspace_id == workspace_id)
    if deal_id:
        q = q.where(memory_chunk.c.deal_id == deal_id)
    elif person_id:
        q = q.where(memory_chunk.c.person_id == person_id)
    qtok = _tokens(query)
    if not qtok:
        return []
    scored: list[tuple[int, dict]] = []
    with engine.connect() as conn:
        for r in conn.execute(q).mappings():
            overlap = len(qtok & _tokens(r["chunk_text"]))
            if overlap:
                scored.append((overlap, dict(chunk_text=r["chunk_text"],
                               source_type=r["source_type"],
                               source_id=r["source_id"], score=overlap)))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [d for _, d in scored[:k]]
