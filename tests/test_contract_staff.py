from sqlalchemy import func, insert, select

from zerocrm import contracts
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company, contract_staff, location, staff

A = {"kind": "human", "id": "sid"}


def _engine():
    e = make_engine("sqlite://")
    migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="co1", name="Acme"))
        c.execute(insert(location).values(id="loc1", company_id="co1", name="HQ"))
        c.execute(insert(staff).values(id="s_old", full_name="Pat"))
        c.execute(insert(staff).values(id="s_new", full_name="Robin"))
    return e


def test_swap_preserves_history_and_writes_one_event():
    e = _engine()
    cid = contracts.create_contract(e, company_id="co1", location_id="loc1", actor=A)
    contracts.assign_staff(e, cid, "s_old", actor=A)
    contracts.swap_staff(e, cid, "s_old", "s_new", actor=A)

    assert [a["staff_id"] for a in contracts.active_staff(e, cid)] == ["s_new"]
    kinds = [h["kind"] for h in contracts.contract_history(e, cid)]
    assert kinds.count("staff_swapped") == 1
    with e.connect() as c:
        total = c.execute(select(func.count()).select_from(contract_staff)
                          .where(contract_staff.c.contract_id == cid)).scalar()
    assert total == 2  # removed assignment row preserved, not deleted


def test_remove_soft_deletes():
    e = _engine()
    cid = contracts.create_contract(e, company_id="co1", location_id="loc1", actor=A)
    contracts.assign_staff(e, cid, "s_old", actor=A)
    contracts.remove_staff(e, cid, "s_old", actor=A)
    assert contracts.active_staff(e, cid) == []
