from sqlalchemy import insert

from zerocrm import inspections
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company, location


def _engine():
    e = make_engine("sqlite://")
    migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="co1", name="Acme"))
        c.execute(insert(location).values(id="loc1", company_id="co1", name="HQ"))
    return e


def test_record_and_list_by_location():
    e = _engine()
    inspections.record_inspection(e, location_id="loc1", outcome="pass", score=9)
    got = inspections.location_inspections(e, "loc1")
    assert len(got) == 1 and got[0]["outcome"] == "pass" and got[0]["score"] == 9
