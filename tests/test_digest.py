"""Digest render / parse / auth / apply tests."""

from sqlalchemy import select

from zerocrm.digest import apply_decisions, authenticate_reply, parse_reply, render
from zerocrm.schema import correction, digest_item

ACTOR = {"kind": "human", "id": "sid"}
CONFIG = {
    "approver_identities": ["operator@example.com"],
    "approver_domains": ["example.com"],
}


def _good_meta(**over):
    m = {"from": "operator@example.com", "dkim_pass": True, "dkim_domain": "example.com",
         "in_reply_to": "<d1>", "known_thread": True}
    m.update(over)
    return m


# --- parser ----------------------------------------------------------------
def test_parse_approve_skip_edit():
    d, amb = parse_reply("#141 ok\n142 skip\n#143 ok but drop the first line")
    assert d[141] == {"action": "approve"}
    assert d[142] == {"action": "skip"}
    assert d[143] == {"action": "edit", "instruction": "drop the first line"}
    assert amb == []


def test_parse_send_as_is_is_approve():
    d, _ = parse_reply("144 send as-is")
    assert d[144] == {"action": "approve"}


def test_parse_ambiguous_is_held_not_sent():
    d, amb = parse_reply("142 maybe later\nlooks good overall\n143 ok")
    assert 142 not in d                 # unrecognized verb -> held
    assert d[143] == {"action": "approve"}
    assert any("maybe later" in a for a in amb)


def test_parse_no_number_ignored_unless_command():
    d, amb = parse_reply("thanks for this\nskip everything")
    assert d == {}
    assert any("skip everything" in a for a in amb)  # command w/o number -> flagged


# --- auth ------------------------------------------------------------------
def test_auth_accepts_valid_reply():
    ok, reason = authenticate_reply(_good_meta(), CONFIG)
    assert ok and reason == "ok"


def test_auth_rejects_dkim_fail():
    ok, reason = authenticate_reply(_good_meta(dkim_pass=False), CONFIG)
    assert not ok and reason == "dkim_fail"


def test_auth_rejects_misaligned_dkim():
    ok, reason = authenticate_reply(_good_meta(dkim_domain="evil.com"), CONFIG)
    assert not ok and reason == "dkim_misaligned"


def test_auth_rejects_non_allowlisted_sender():
    ok, reason = authenticate_reply(
        _good_meta(**{"from": "attacker@evil.com", "dkim_domain": "evil.com"}), CONFIG
    )
    assert not ok and reason == "sender_not_allowlisted"


def test_auth_rejects_out_of_thread():
    ok, reason = authenticate_reply(_good_meta(known_thread=False), CONFIG)
    assert not ok and reason == "not_in_digest_thread"


def test_auth_accepts_self_authored_reply():
    # a reply composed inside the watched mailbox: from-self, no external auth stamp
    meta = {"from": "operator@example.com", "has_auth_results": False, "known_thread": True}
    ok, reason = authenticate_reply(meta, {"self_address": "operator@example.com"})
    assert ok and reason == "self_authored"


def test_auth_rejects_spoofed_self_from_outside():
    # attacker forges From:operator@example.com externally -> Gmail stamps it, dkim won't align
    meta = {"from": "operator@example.com", "has_auth_results": True, "dkim_pass": True,
            "dkim_domain": "evil.com", "known_thread": True}
    ok, reason = authenticate_reply(meta, {"self_address": "operator@example.com",
                                           "approver_domains": ["example.com"]})
    assert not ok and reason == "dkim_misaligned"


# --- apply -----------------------------------------------------------------
def test_apply_writes_statuses_and_captures_edit(engine, mk_item):
    a = mk_item(status="draft")
    b = mk_item(status="draft")
    c = mk_item(status="draft", channel="li_comment")
    res = apply_decisions(
        engine,
        {a: {"action": "approve"}, b: {"action": "skip"},
         c: {"action": "edit", "instruction": "less salesy"}},
        ACTOR,
    )
    assert res == {"approved": 1, "skipped": 1, "edited": 1, "ignored": 0}
    with engine.connect() as conn:
        rows = {r["item_no"]: r["status"] for r in
                conn.execute(select(digest_item.c.item_no, digest_item.c.status)).mappings()}
        corr = conn.execute(select(correction)).mappings().all()
    assert rows[a] == "approved" and rows[b] == "skipped" and rows[c] == "edited"
    assert len(corr) == 1 and corr[0]["channel"] == "li_comment"
    assert corr[0]["right"] == "less salesy" and corr[0]["durable"] is False


def test_apply_ignores_non_draft(engine, mk_item):
    n = mk_item(status="sent")
    res = apply_decisions(engine, {n: {"action": "approve"}}, ACTOR)
    assert res["ignored"] == 1


# --- render ----------------------------------------------------------------
def test_render_uses_stable_item_numbers(engine, mk_item):
    n = mk_item(status="draft", payload={"summary": "Jane @ Acme"})
    with engine.connect() as conn:
        item = conn.execute(
            select(digest_item).where(digest_item.c.item_no == n)
        ).mappings().one()
    body = render([dict(item)])
    assert f"#{n}" in body and "Jane @ Acme" in body
