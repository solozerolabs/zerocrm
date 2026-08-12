"""Send-path blocklist guard (adopted from outreachr's scoped suppressions):
a suppressed address/domain/person/global is never enrolled."""

from sqlalchemy import select

from zerocrm.executor import execute_item
from zerocrm.schema import blocklist, digest_item
from zerocrm.sender import FakeSender

ACTOR = {"kind": "agent", "id": "t"}


def _approved(mk_item, email="a@x.com"):
    return mk_item(status="approved", person_id=None,
                   payload={"summary": "x", "campaign_ref": "77", "lead": {"email": email}})


def _block(engine, granularity, value, reason="opt_out"):
    with engine.begin() as c:
        c.execute(blocklist.insert().values(granularity=granularity, value=value,
                                            reason=reason, actor=ACTOR, workspace_id="default"))


def _status(engine, n):
    with engine.connect() as c:
        return c.execute(select(digest_item.c.status).where(digest_item.c.item_no == n)).scalar_one()


def test_identity_block_prevents_enroll(engine, mk_item):
    n = _approved(mk_item, "blocked@x.com")
    _block(engine, "identity", "blocked@x.com", "hard_bounce")
    s = FakeSender()
    assert execute_item(engine, n, s, "w1") == "blocked:hard_bounce"
    assert s.sent == [] and _status(engine, n) == "skipped"


def test_domain_block_prevents_enroll(engine, mk_item):
    n = _approved(mk_item, "anyone@banned.com")
    _block(engine, "domain", "banned.com")
    s = FakeSender()
    assert execute_item(engine, n, s, "w1").startswith("blocked")
    assert s.sent == []


def test_global_block_pauses_everything(engine, mk_item):
    n = _approved(mk_item, "a@x.com")
    _block(engine, "global", "*", "kill_switch")
    s = FakeSender()
    assert execute_item(engine, n, s, "w1") == "blocked:kill_switch"
    assert s.sent == []


def test_unblocked_still_sends(engine, mk_item):
    n = _approved(mk_item, "fine@x.com")
    s = FakeSender()
    assert execute_item(engine, n, s, "w1") == "sent"
    assert s.sent == [n]
