from sqlalchemy import inspect

from zerocrm.db import make_engine
from zerocrm.migrate import migrate

_TABLES = {"location", "staff", "contract", "contract_staff",
           "contract_line", "document", "inspection", "contract_event"}


def test_004_creates_postsale_tables():
    e = make_engine("sqlite://")
    migrate(e)
    assert _TABLES <= set(inspect(e).get_table_names())


def test_004_is_idempotent():
    e = make_engine("sqlite://")
    migrate(e)
    assert migrate(e) == []
