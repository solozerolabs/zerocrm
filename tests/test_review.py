from sqlalchemy import func, insert, select

from zerocrm import review
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import call, deal, person, touch, transcript


def _engine():
    e = make_engine("sqlite://")
    migrate(e)
    return e


def test_write_review_records_one_review_touch():
    e = _engine()
    with e.begin() as c:
        c.execute(insert(person).values(id="p1", full_name="Jane"))
        c.execute(insert(deal).values(id="d1", person_id="p1", stage="lost"))
        c.execute(insert(call).values(id="c1", person_id="p1", deal_id="d1", source="meet", status="transcribed"))
        c.execute(insert(transcript).values(id="t1", call_id="c1", transcript_source="google",
                                            text="price too high vs competitor"))
    tid = review.write_review(e, deal_id="d1", outcome="lost")
    with e.connect() as c:
        n = c.execute(select(func.count()).select_from(touch).where(touch.c.kind == "review")).scalar()
        row = c.execute(select(touch).where(touch.c.id == tid)).mappings().first()
    assert n == 1
    assert row["payload"]["outcome"] == "lost"
    assert row["payload"]["reasons"]  # transcript text captured in the floor


def test_write_review_none_for_personless_deal():
    e = _engine()
    with e.begin() as c:
        c.execute(insert(deal).values(id="d2", stage="won"))
    assert review.write_review(e, deal_id="d2", outcome="won") is None
