import pytest
from sqlalchemy import insert

from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import digest_item


@pytest.fixture()
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path/'t.db'}")
    migrate(eng)
    return eng


@pytest.fixture()
def mk_item(engine):
    def _mk(status="approved", channel="email", kind="enroll", person_id=None,
            payload=None, digest_date="2026-08-08"):
        with engine.begin() as conn:
            res = conn.execute(
                insert(digest_item).values(
                    kind=kind, channel=channel, person_id=person_id,
                    payload=payload or {"summary": "hi there"},
                    status=status, digest_date=digest_date,
                )
            )
            return res.inserted_primary_key[0]
    return _mk
