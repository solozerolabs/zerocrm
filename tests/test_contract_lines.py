from sqlalchemy import insert

from zerocrm import contracts
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company, location

A = {"kind": "human", "id": "sid"}


def _engine_cid():
    e = make_engine("sqlite://")
    migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="co1", name="Acme"))
        c.execute(insert(location).values(id="loc1", company_id="co1", name="HQ"))
    cid = contracts.create_contract(e, company_id="co1", location_id="loc1", actor=A)
    return e, cid


def test_add_one_off_visit_writes_event_and_line():
    e, cid = _engine_cid()
    contracts.add_line(e, cid, kind="one_off_visit", description="emergency clean",
                       unit_cost_usd=120, actor=A)
    assert len(contracts.lines(e, cid)) == 1
    assert any(h["kind"] == "line_added" for h in contracts.contract_history(e, cid))


def test_remove_line_captures_what_was_removed():
    e, cid = _engine_cid()
    lid = contracts.add_line(e, cid, kind="material", description="floor wax",
                             unit_cost_usd=40, actor=A)
    contracts.remove_line(e, lid, actor=A)
    assert contracts.lines(e, cid) == []
    removed = [h for h in contracts.contract_history(e, cid) if h["kind"] == "line_removed"]
    assert removed and removed[0]["payload"]["description"] == "floor wax"
    assert removed[0]["payload"]["unit_cost_usd"] == 40
