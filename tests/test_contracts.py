import pytest
from sqlalchemy import func, insert, select

from zerocrm import contracts
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import company, contract, contract_event, location

A = {"kind": "human", "id": "sid"}


def _engine():
    e = make_engine("sqlite://")
    migrate(e)
    with e.begin() as c:
        c.execute(insert(company).values(id="co1", name="Acme"))
        c.execute(insert(location).values(id="loc1", company_id="co1", name="HQ"))
    return e


def test_create_writes_created_event():
    e = _engine()
    cid = contracts.create_contract(e, company_id="co1", location_id="loc1",
                                    contract_type="janitorial", scope="nightly", actor=A)
    hist = contracts.contract_history(e, cid)
    assert len(hist) == 1 and hist[0]["kind"] == "created"


def test_set_field_records_before_and_after():
    e = _engine()
    cid = contracts.create_contract(e, company_id="co1", location_id="loc1", scope="nightly", actor=A)
    contracts.set_field(e, cid, "scope", "nightly + weekend", actor=A)
    changed = [h for h in contracts.contract_history(e, cid) if h["kind"] == "scope_changed"]
    assert changed and changed[0]["payload"]["before"] == "nightly"
    assert changed[0]["payload"]["after"] == "nightly + weekend"


def test_set_field_rejects_unknown_field():
    e = _engine()
    cid = contracts.create_contract(e, company_id="co1", location_id="loc1", actor=A)
    with pytest.raises(ValueError):
        contracts.set_field(e, cid, "id", "hacked", actor=A)


def test_mutation_on_missing_contract_leaves_no_orphan_event():
    # a bad FK on the event must roll back the whole set_field txn (no partial audit)
    e = _engine()
    with pytest.raises(Exception):
        contracts.set_field(e, "does-not-exist", "scope", "x", actor=A)
    with e.connect() as c:
        assert c.execute(select(func.count()).select_from(contract_event)).scalar() == 0
        assert c.execute(select(func.count()).select_from(contract)).scalar() == 0
