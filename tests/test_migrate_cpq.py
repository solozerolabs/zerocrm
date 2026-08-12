from sqlalchemy import inspect

from zerocrm.db import make_engine
from zerocrm.migrate import migrate

_TABLES = {"vendor", "supply", "price_event", "quote", "quote_line"}


def test_005_creates_cpq_tables_and_supply_id_column():
    e = make_engine("sqlite://")
    migrate(e)
    insp = inspect(e)
    assert _TABLES <= set(insp.get_table_names())
    cols = {c["name"] for c in insp.get_columns("contract_line")}
    assert "supply_id" in cols


def test_005_is_idempotent():
    e = make_engine("sqlite://")
    migrate(e)
    assert migrate(e) == []
