"""Living service contracts + their append-only audit.

Every mutation writes exactly one contract_event in the SAME transaction as the
state change, so the audit can never diverge from the state it describes. This is
NOT the upsert precedence gate — a contract has no competing writers, so the right
audit is an event log, not per-field precedence."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, Engine

from .schema import contract, contract_event, contract_line, contract_staff

_EDITABLE = {"scope", "contract_type", "status", "value_usd", "starts_on", "ends_on"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(conn: Connection, contract_id: str, kind: str, payload: dict, actor: dict,
           workspace_id: str = "default") -> None:
    conn.execute(insert(contract_event).values(
        contract_id=contract_id, kind=kind, payload=payload, actor=actor,
        workspace_id=workspace_id))


# --- contract + field edits ------------------------------------------------
def create_contract(engine: Engine, *, company_id=None, location_id=None,
                    contract_type=None, scope=None, value_usd=None, actor,
                    workspace_id: str = "default") -> str:
    with engine.begin() as conn:
        cid = conn.execute(insert(contract).values(
            company_id=company_id, location_id=location_id, contract_type=contract_type,
            scope=scope, value_usd=value_usd, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        _event(conn, cid, "created",
               {"contract_type": contract_type, "scope": scope}, actor, workspace_id)
    return cid


def set_field(engine: Engine, contract_id: str, field: str, value, *, actor,
              workspace_id: str = "default") -> None:
    if field not in _EDITABLE:
        raise ValueError(f"field {field!r} is not editable")
    with engine.begin() as conn:
        before = conn.execute(
            select(contract.c[field]).where(contract.c.id == contract_id)).scalar()
        conn.execute(update(contract).where(contract.c.id == contract_id)
                     .values({field: value, "updated_at": _now()}))
        _event(conn, contract_id, f"{field}_changed",
               {"before": before, "after": value}, actor, workspace_id)


def contract_history(engine: Engine, contract_id: str) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(contract_event).where(contract_event.c.contract_id == contract_id)
            .order_by(contract_event.c.ts, contract_event.c.created_at)).mappings()]


# --- assigned staff (swaps preserve history) -------------------------------
def assign_staff(engine: Engine, contract_id: str, staff_id: str, *, role=None, actor,
                 workspace_id: str = "default") -> str:
    with engine.begin() as conn:
        rid = conn.execute(insert(contract_staff).values(
            contract_id=contract_id, staff_id=staff_id, role=role, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        _event(conn, contract_id, "staff_added",
               {"staff_id": staff_id, "role": role}, actor, workspace_id)
    return rid


def remove_staff(engine: Engine, contract_id: str, staff_id: str, *, actor,
                 workspace_id: str = "default") -> None:
    with engine.begin() as conn:
        conn.execute(update(contract_staff)
                     .where(contract_staff.c.contract_id == contract_id,
                            contract_staff.c.staff_id == staff_id,
                            contract_staff.c.removed_at.is_(None))
                     .values(removed_at=_now()))
        _event(conn, contract_id, "staff_removed", {"staff_id": staff_id}, actor, workspace_id)


def swap_staff(engine: Engine, contract_id: str, out_staff_id: str, in_staff_id: str, *,
               role=None, actor, workspace_id: str = "default") -> str:
    with engine.begin() as conn:
        conn.execute(update(contract_staff)
                     .where(contract_staff.c.contract_id == contract_id,
                            contract_staff.c.staff_id == out_staff_id,
                            contract_staff.c.removed_at.is_(None))
                     .values(removed_at=_now()))
        rid = conn.execute(insert(contract_staff).values(
            contract_id=contract_id, staff_id=in_staff_id, role=role, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        _event(conn, contract_id, "staff_swapped",
               {"out": out_staff_id, "in": in_staff_id}, actor, workspace_id)
    return rid


def active_staff(engine: Engine, contract_id: str) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(contract_staff).where(contract_staff.c.contract_id == contract_id,
                                         contract_staff.c.removed_at.is_(None))).mappings()]


# --- line items (one-off visits, materials) --------------------------------
def add_line(engine: Engine, contract_id: str, *, kind, description=None, qty=1,
             unit_cost_usd=None, occurred_on=None, actor, workspace_id: str = "default") -> str:
    with engine.begin() as conn:
        lid = conn.execute(insert(contract_line).values(
            contract_id=contract_id, kind=kind, description=description, qty=qty,
            unit_cost_usd=unit_cost_usd, occurred_on=occurred_on, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        _event(conn, contract_id, "line_added",
               {"kind": kind, "description": description, "unit_cost_usd": unit_cost_usd},
               actor, workspace_id)
    return lid


def remove_line(engine: Engine, line_id: str, *, actor, workspace_id: str = "default") -> None:
    with engine.begin() as conn:
        row = conn.execute(select(contract_line).where(
            contract_line.c.id == line_id)).mappings().first()
        if row is None:
            return
        # capture what we removed BEFORE deleting, so the audit isn't lossy
        conn.execute(contract_line.delete().where(contract_line.c.id == line_id))
        _event(conn, row["contract_id"], "line_removed",
               {"line_id": line_id, "kind": row["kind"], "description": row["description"],
                "unit_cost_usd": row["unit_cost_usd"]}, actor, workspace_id)


def lines(engine: Engine, contract_id: str) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(
            select(contract_line).where(contract_line.c.contract_id == contract_id)).mappings()]
