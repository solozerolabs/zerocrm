"""Master supply list + append-only vendor price-change history. A price change
writes a price_event and updates the supply cost in ONE txn (they can't disagree).
price_impacts finds ACTIVE contracts using a supply; flag_price_impacts surfaces
the delta as a digest notice — zerocrm flags it, the operator updates their own
invoice (no invoice entity here)."""

from __future__ import annotations

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from .schema import contract, contract_line, price_event, supply, vendor
from .warmup import add_notice

_A = {"kind": "agent", "id": "pricing"}


def add_vendor(engine: Engine, *, name, actor=_A, workspace_id: str = "default") -> str:
    with engine.begin() as conn:
        return conn.execute(insert(vendor).values(
            name=name, actor=actor, workspace_id=workspace_id)).inserted_primary_key[0]


def add_supply(engine: Engine, *, name, vendor_id=None, sku=None, unit=None,
               current_cost_usd=None, actor=_A, workspace_id: str = "default") -> str:
    with engine.begin() as conn:
        return conn.execute(insert(supply).values(
            name=name, vendor_id=vendor_id, sku=sku, unit=unit,
            current_cost_usd=current_cost_usd, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]


def record_price_change(engine: Engine, supply_id: str, new_cost_usd: float, *,
                        effective_on=None, source=None, actor=_A,
                        workspace_id: str = "default") -> str:
    with engine.begin() as conn:
        old = conn.execute(select(supply.c.current_cost_usd)
                           .where(supply.c.id == supply_id)).scalar()
        eid = conn.execute(insert(price_event).values(
            supply_id=supply_id, old_cost_usd=old, new_cost_usd=new_cost_usd,
            effective_on=effective_on, source=source, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        conn.execute(supply.update().where(supply.c.id == supply_id)
                     .values(current_cost_usd=new_cost_usd))
    return eid


def _latest_event(conn, supply_id):
    return conn.execute(select(price_event).where(price_event.c.supply_id == supply_id)
                        .order_by(price_event.c.ts.desc())).mappings().first()


def price_impacts(engine: Engine, supply_id: str) -> list[dict]:
    with engine.connect() as conn:
        ev = _latest_event(conn, supply_id)
        if not ev:
            return []
        rows = conn.execute(
            select(contract_line.c.id, contract_line.c.contract_id,
                   contract_line.c.description, contract_line.c.qty)
            .join(contract, contract.c.id == contract_line.c.contract_id)
            .where(contract_line.c.supply_id == supply_id,
                   contract.c.status == "active")).mappings().all()
    old, new = ev["old_cost_usd"] or 0, ev["new_cost_usd"] or 0
    return [dict(contract_id=r["contract_id"], line_id=r["id"],
                 description=r["description"], qty=r["qty"],
                 old_cost=ev["old_cost_usd"], new_cost=ev["new_cost_usd"],
                 delta=new - old) for r in rows]


def flag_price_impacts(engine: Engine, supply_id: str) -> int:
    impacts = price_impacts(engine, supply_id)
    with engine.connect() as conn:
        name = conn.execute(select(supply.c.name).where(supply.c.id == supply_id)).scalar()
    for i in impacts:
        add_notice(engine, f"price: {name} {i['old_cost']}->{i['new_cost']} "
                           f"(+{i['delta']}) affects contract {i['contract_id']} "
                           f"line '{i['description']}' — update invoice")
    return len(impacts)
