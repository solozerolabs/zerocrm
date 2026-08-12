from sqlalchemy import func, insert, select

from zerocrm import contracts, marketing
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company, deal, flow_state, list_member, location, person

A = {"kind": "human", "id": "sid"}


def _engine():
    e = make_engine("sqlite://")
    migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="cust", name="Customer Co"))
        c.execute(insert(company).values(id="cold", name="Cold Co"))
        c.execute(insert(location).values(id="loc", company_id="cust", name="HQ"))
        c.execute(insert(person).values(id="p_won", full_name="Won Lead"))
        c.execute(insert(person).values(id="p_contract", full_name="Contract Contact", company_id="cust"))
        c.execute(insert(person).values(id="p_cold", full_name="Cold Prospect", company_id="cold"))
        c.execute(insert(deal).values(id="d_won", person_id="p_won", stage="won"))
    contracts.create_contract(e, company_id="cust", location_id="loc", actor=A)  # active
    return e


def test_audience_is_customers_only():
    e = _engine()
    assert set(marketing.customer_audience(e)) == {"p_won", "p_contract"}


def test_build_list_creates_one_member_per_person():
    e = _engine()
    lid = marketing.build_customer_list(e, name="customers", actor=A)
    with e.connect() as c:
        n = c.execute(select(func.count()).select_from(list_member)
                      .where(list_member.c.list_id == lid)).scalar()
    assert n == 2


def test_enrol_is_scheduled_and_idempotent():
    e = _engine()
    lid = marketing.build_customer_list(e, name="customers", actor=A)
    cid = marketing.create_nurture_campaign(e, name="q3-nurture", actor=A)
    assert marketing.enrol_list(e, lid, cid, actor=A) == 2
    assert marketing.enrol_list(e, lid, cid, actor=A) == 0
    with e.connect() as c:
        rows = c.execute(select(flow_state).where(flow_state.c.campaign_id == cid)).mappings().all()
    assert len(rows) == 2 and all(r["status"] == "scheduled" for r in rows)
