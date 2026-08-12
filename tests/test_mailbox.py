"""Pure reply-parsing tests (SMTP/IMAP IO is thin and proven live)."""

from email.message import EmailMessage

from zerocrm.mailbox import parse_reply_message


def _msg(from_addr, authres, in_reply_to="<digest-1>", refs=None, body="#141 ok"):
    m = EmailMessage()
    m["From"] = from_addr
    m["In-Reply-To"] = in_reply_to
    if refs:
        m["References"] = refs
    if authres is not None:
        m["Authentication-Results"] = authres
    m.set_content(body)
    return m


def test_parses_dkim_pass_and_domain():
    m = _msg("Sid <operator@example.com>",
             "mx.google.com; dkim=pass header.d=example.com; spf=pass")
    r = parse_reply_message(m, {"<digest-1>"})
    assert r["from"] == "operator@example.com"
    assert r["dkim_pass"] is True
    assert r["dkim_domain"] == "example.com"
    assert r["known_thread"] is True
    assert r["body"].strip() == "#141 ok"


def test_dkim_fail_detected():
    m = _msg("operator@example.com", "mx.google.com; dkim=fail header.d=example.com")
    assert parse_reply_message(m, {"<digest-1>"})["dkim_pass"] is False


def test_thread_match_via_references():
    m = _msg("operator@example.com", "dkim=pass header.d=example.com",
             in_reply_to="<other>", refs="<x> <digest-1> <y>")
    assert parse_reply_message(m, {"<digest-1>"})["known_thread"] is True


def test_unknown_thread():
    m = _msg("operator@example.com", "dkim=pass header.d=example.com", in_reply_to="<stranger>")
    assert parse_reply_message(m, {"<digest-1>"})["known_thread"] is False


def test_missing_auth_results_is_not_pass():
    m = _msg("operator@example.com", None)
    r = parse_reply_message(m, {"<digest-1>"})
    assert r["dkim_pass"] is False and r["dkim_domain"] == ""


def test_end_to_end_auth_gate_accepts_valid_reply():
    """parse -> authenticate_reply, the real chain."""
    from zerocrm.digest import authenticate_reply
    m = _msg("operator@example.com", "mx.google.com; dkim=pass header.d=example.com")
    meta = parse_reply_message(m, {"<digest-1>"})
    ok, reason = authenticate_reply(meta, {"approver_identities": ["operator@example.com"],
                                           "approver_domains": ["example.com"]})
    assert ok and reason == "ok"


def test_end_to_end_auth_gate_rejects_spoof():
    from zerocrm.digest import authenticate_reply
    # attacker forges From but Gmail's DKIM won't align to example.com
    m = _msg("operator@example.com", "mx.google.com; dkim=pass header.d=evil.com")
    meta = parse_reply_message(m, {"<digest-1>"})
    ok, reason = authenticate_reply(meta, {"approver_identities": ["operator@example.com"],
                                           "approver_domains": ["example.com"]})
    assert not ok and reason == "dkim_misaligned"
