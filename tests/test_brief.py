from sqlalchemy import insert

from zerocrm import brief
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import call, deal, person, transcript


def _engine():
    e = make_engine("sqlite://")
    migrate(e)
    return e


def test_brief_includes_stage_next_step_and_transcript_snippet():
    e = _engine()
    with e.begin() as c:
        c.execute(insert(person).values(id="p1", full_name="Jane"))
        c.execute(insert(deal).values(id="d1", person_id="p1", stage="negotiation", next_step="send quote"))
        c.execute(insert(call).values(id="c1", person_id="p1", deal_id="d1", source="meet", status="transcribed"))
        c.execute(insert(transcript).values(id="t1", call_id="c1", transcript_source="google",
                                            text="they pushed back on pricing"))
    out = brief.precall_brief(e, person_id="p1", deal_id="d1", query="pricing")
    assert "negotiation" in out and "send quote" in out and "pricing" in out


def test_brief_handles_no_history():
    e = _engine()
    with e.begin() as c:
        c.execute(insert(person).values(id="p2", full_name="New Lead"))
    out = brief.precall_brief(e, person_id="p2")
    assert "New Lead" in out


def test_brief_no_contact_is_safe():
    e = _engine()
    assert "No history" in brief.precall_brief(e, person_id="ghost")
