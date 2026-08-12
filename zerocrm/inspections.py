"""Periodic quality inspections, tied to a location. Recurrence reuses the digest
clock (a config-driven notice), NOT a new scheduler — this module stores + reads."""

from __future__ import annotations

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from .schema import inspection


def record_inspection(engine: Engine, *, location_id, contract_id=None, outcome,
                      score=None, performed_at=None, notes="",
                      workspace_id: str = "default") -> str:
    with engine.begin() as conn:
        return conn.execute(insert(inspection).values(
            location_id=location_id, contract_id=contract_id, outcome=outcome,
            score=score, performed_at=performed_at, notes=notes,
            workspace_id=workspace_id)).inserted_primary_key[0]


def location_inspections(engine: Engine, location_id: str) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(inspection).where(inspection.c.location_id == location_id)
            .order_by(inspection.c.created_at.desc())).mappings()]
