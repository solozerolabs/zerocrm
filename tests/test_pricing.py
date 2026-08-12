from sqlalchemy import insert, select

from zerocrm import contracts, pricing
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company, contract_line, location, price_event, supply
from zerocrm.warmup import pop_notices

A = {"kind": "human", "id": "sid"}


def _engine():
    e = make_engine("sqlite://")
    migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="co1", name="Acme"))
        c.execute(insert(location).values(id="loc1", company_id="co1", name="HQ"))
    return e


def _link(e, line_id, supply_id):
    with e.begin() as c:
        c.execute(contract_line.update().where(contract_line.c.id == line_id)
                  .values(supply_id=supply_id))


def test_record_price_change_writes_event_and_bumps_cost():
    e = _engine()
    sid = pricing.add_supply(e, name="wax", current_cost_usd=10.0, actor=A)
    pricing.record_price_change(e, sid, 12.0, actor=A)
    with e.connect() as c:
        assert c.execute(select(supply.c.current_cost_usd).where(supply.c.id == sid)).scalar() == 12.0
        ev = c.execute(select(price_event).where(price_event.c.supply_id == sid)).mappings().first()
    assert ev["old_cost_usd"] == 10.0 and ev["new_cost_usd"] == 12.0


def test_price_impacts_lists_active_contracts_only():
    e = _engine()
    sid = pricing.add_supply(e, name="wax", current_cost_usd=10.0, actor=A)
    active = contracts.create_contract(e, company_id="co1", location_id="loc1", actor=A)
    ended = contracts.create_contract(e, company_id="co1", location_id="loc1", actor=A)
    contracts.set_field(e, ended, "status", "ended", actor=A)
    for cid in (active, ended):
        lid = contracts.add_line(e, cid, kind="material", description="floor wax", actor=A)
        _link(e, lid, sid)
    pricing.record_price_change(e, sid, 12.0, actor=A)
    impacts = pricing.price_impacts(e, sid)
    assert {i["contract_id"] for i in impacts} == {active}
    assert impacts[0]["delta"] == 2.0


def test_flag_price_impacts_adds_notices():
    e = _engine()
    sid = pricing.add_supply(e, name="wax", current_cost_usd=10.0, actor=A)
    cid = contracts.create_contract(e, company_id="co1", location_id="loc1", actor=A)
    lid = contracts.add_line(e, cid, kind="material", actor=A)
    _link(e, lid, sid)
    pricing.record_price_change(e, sid, 12.0, actor=A)
    assert pricing.flag_price_impacts(e, sid) == 1
    assert any(cid in n for n in pop_notices(e))
