"""Loop orchestration tests that don't need live IMAP/SMTP: mid persistence
and approved-item execution."""

from sqlalchemy import select

from zerocrm.loop import _execute_approved, _append_mid, get_known_mids
from zerocrm.schema import digest_item
from zerocrm.sender import FakeSender


def test_mid_roundtrip_and_dedup(engine):
    _append_mid(engine, "<a>")
    _append_mid(engine, "<b>")
    _append_mid(engine, "<a>")   # re-append moves to front, no dup
    mids = get_known_mids(engine)
    assert mids[0] == "<a>" and mids.count("<a>") == 1 and "<b>" in mids


def test_execute_approved_only(engine, mk_item):
    approved = mk_item(status="approved", person_id=None)
    mk_item(status="draft")      # not executed
    mk_item(status="edited")     # awaits redraft, not executed
    s = FakeSender()
    n = _execute_approved(engine, s, "w1")
    assert n == 1 and s.sent == [approved]
    with engine.connect() as conn:
        st = conn.execute(
            select(digest_item.c.status).where(digest_item.c.item_no == approved)
        ).scalar_one()
    assert st == "sent"


def _enroll_payload(icebreaker, subj="your go repos", first="Jane"):
    return {"summary": "Jane @ Acme", "lead": {"email": "jane@acme.com", "first_name": first,
            "custom_fields": {"icebreaker": icebreaker, "subject_hook": subj}},
            "preview": {"subject": subj, "body": icebreaker}}


def test_digest_gate_holds_violating_drafts(engine, mk_item, monkeypatch):
    import zerocrm.loop as loop
    captured = {}
    def fake_send(subject, body, to=None, sender=None, html=None):
        captured.update(subject=subject, body=body, html=html)
        return "<mid-1>"
    monkeypatch.setattr(loop, "send_digest", fake_send)

    clean = mk_item(status="draft", payload=_enroll_payload("saw acme ships go"))
    dirty = mk_item(status="draft", payload=_enroll_payload("let's leverage synergy"))  # slop

    mid = loop.send_pending_digest(engine, {"digest_to": "op@x.io", "digest_from": "op@x.io"})
    assert mid == "<mid-1>"
    body = captured["body"]
    assert f"#{clean}" in body                      # clean draft surfaced
    assert "HELD by the copy gate" in body          # dirty one flagged, not surfaced
    assert f"#{dirty}" in body                       # named in the notice
    assert "synergy" not in body                     # the bad line itself never shown


def test_lint_warm_allows_links_and_greeting_but_not_slop():
    from zerocrm.copy import lint_warm
    assert lint_warm("Thanks Jane. Grab a time: https://book.syndai.ai") == []
    assert any("slop" in v for v in lint_warm("let's leverage this"))
    assert any("placeholder" in v for v in lint_warm("book here [BOOKING LINK REQUIRED]"))
    assert "empty body" in lint_warm("")
