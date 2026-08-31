"""Read-only contact listing for the token-gated export endpoint (serve.py)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .schema import person


def export_contacts(engine: Engine, workspace_id: str = "default") -> list[dict]:
    """All contacts (person rows) in a workspace, as plain dicts."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(person.c.id, person.c.full_name, person.c.job_title, person.c.company_id)
            .where(person.c.workspace_id == workspace_id)
            .order_by(person.c.id)
        ).mappings().all()
    return [dict(r) for r in rows]
