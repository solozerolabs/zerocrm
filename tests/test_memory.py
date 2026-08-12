from sqlalchemy import insert

from zerocrm import memory
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import deal


def _engine():
    e = make_engine("sqlite://")
    migrate(e)
    return e


def test_chunk_splits_long_text():
    parts = memory.chunk_text("word " * 1000, size=200)
    assert len(parts) > 1 and all(len(p) <= 200 for p in parts)


def test_chunk_empty_is_empty():
    assert memory.chunk_text("   ") == []


def test_retrieve_ranks_matching_chunk_first():
    e = _engine()
    memory.add_chunks(e, source_type="document", source_id="d1",
                      texts=["pricing was the sticking point on renewal",
                             "the weather in the demo was pleasant"])
    hits = memory.retrieve(e, "why did pricing block the renewal", k=1)
    assert hits and "pricing" in hits[0]["chunk_text"]


def test_retrieve_scopes_to_deal():
    e = _engine()
    with e.begin() as c:
        c.execute(insert(deal).values(id="D1", stage="prospect"))
        c.execute(insert(deal).values(id="D2", stage="prospect"))
    memory.add_chunks(e, source_type="document", source_id="a", texts=["alpha budget"], deal_id="D1")
    memory.add_chunks(e, source_type="document", source_id="b", texts=["beta budget"], deal_id="D2")
    hits = memory.retrieve(e, "budget", deal_id="D1")
    assert hits and all(h["source_id"] == "a" for h in hits)


def test_retrieve_empty_query_returns_nothing():
    e = _engine()
    memory.add_chunks(e, source_type="document", source_id="d1", texts=["anything"])
    assert memory.retrieve(e, "the and of") == []
