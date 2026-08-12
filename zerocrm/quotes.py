"""Basic CPQ: assemble a customer quote from supplies + a header margin. Unit cost
is snapshotted at line time (a later price change doesn't silently move a sent
quote). unit_price = unit_cost x (1 + margin); the header total_usd is the sum of
line totals, recomputed on every line add."""

from __future__ import annotations

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine

from .schema import quote, quote_line, supply


def create_quote(engine: Engine, *, company_id=None, contract_id=None, deal_id=None,
                 margin_pct: float = 0.0, actor, workspace_id: str = "default") -> str:
    if not (company_id or contract_id or deal_id):
        raise ValueError("a quote needs a company, contract, or deal")
    with engine.begin() as conn:
        return conn.execute(insert(quote).values(
            company_id=company_id, contract_id=contract_id, deal_id=deal_id,
            margin_pct=margin_pct, total_usd=0.0, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]


def add_quote_line(engine: Engine, quote_id: str, *, supply_id=None, description=None,
                   qty: float = 1, unit_cost_usd=None, actor,
                   workspace_id: str = "default") -> str:
    with engine.begin() as conn:
        margin = conn.execute(select(quote.c.margin_pct)
                              .where(quote.c.id == quote_id)).scalar() or 0.0
        if supply_id and unit_cost_usd is None:
            unit_cost_usd = conn.execute(select(supply.c.current_cost_usd)
                                         .where(supply.c.id == supply_id)).scalar()
        cost = unit_cost_usd or 0.0
        unit_price = cost * (1 + margin)
        line_total = unit_price * qty
        lid = conn.execute(insert(quote_line).values(
            quote_id=quote_id, supply_id=supply_id, description=description, qty=qty,
            unit_cost_usd=unit_cost_usd, unit_price_usd=unit_price,
            line_total_usd=line_total, actor=actor,
            workspace_id=workspace_id)).inserted_primary_key[0]
        total = conn.execute(select(func.coalesce(func.sum(quote_line.c.line_total_usd), 0.0))
                             .where(quote_line.c.quote_id == quote_id)).scalar()
        conn.execute(quote.update().where(quote.c.id == quote_id).values(total_usd=total))
    return lid


def quote_total(engine: Engine, quote_id: str) -> float:
    with engine.connect() as conn:
        return conn.execute(select(func.coalesce(func.sum(quote_line.c.line_total_usd), 0.0))
                            .where(quote_line.c.quote_id == quote_id)).scalar()
