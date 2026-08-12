import pytest
from sqlalchemy import insert, select

from zerocrm import pricing, quotes
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company
from zerocrm.schema import quote as quote_t

A = {"kind": "human", "id": "sid"}


def _engine():
    e = make_engine("sqlite://")
    migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="co1", name="Acme"))
    return e


def test_line_from_supply_snapshots_cost_and_applies_margin():
    e = _engine()
    sid = pricing.add_supply(e, name="wax", current_cost_usd=10.0, actor=A)
    qid = quotes.create_quote(e, company_id="co1", margin_pct=0.5, actor=A)
    quotes.add_quote_line(e, qid, supply_id=sid, qty=2, actor=A)
    with e.connect() as c:
        total = c.execute(select(quote_t.c.total_usd).where(quote_t.c.id == qid)).scalar()
    assert quotes.quote_total(e, qid) == 30.0 and total == 30.0  # 10*(1.5)=15 unit; *2=30


def test_price_change_does_not_move_a_snapshotted_line():
    e = _engine()
    sid = pricing.add_supply(e, name="wax", current_cost_usd=10.0, actor=A)
    qid = quotes.create_quote(e, company_id="co1", margin_pct=0.0, actor=A)
    quotes.add_quote_line(e, qid, supply_id=sid, qty=1, actor=A)
    pricing.record_price_change(e, sid, 99.0, actor=A)  # cost jumps after the quote
    assert quotes.quote_total(e, qid) == 10.0  # snapshot held


def test_custom_line_uses_passed_cost():
    e = _engine()
    qid = quotes.create_quote(e, company_id="co1", margin_pct=0.0, actor=A)
    quotes.add_quote_line(e, qid, description="travel", qty=1, unit_cost_usd=25.0, actor=A)
    assert quotes.quote_total(e, qid) == 25.0


def test_create_requires_a_parent():
    e = _engine()
    with pytest.raises(ValueError):
        quotes.create_quote(e, margin_pct=0.1, actor=A)
