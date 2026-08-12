from sqlalchemy import inspect

from zerocrm.db import make_engine
from zerocrm.migrate import migrate


def test_003_creates_conversation_tables():
    engine = make_engine("sqlite://")
    migrate(engine)
    tables = set(inspect(engine).get_table_names())
    assert {"call", "transcript", "memory_chunk"} <= tables


def test_003_is_idempotent():
    engine = make_engine("sqlite://")
    migrate(engine)
    assert migrate(engine) == []  # nothing pending on the second run
