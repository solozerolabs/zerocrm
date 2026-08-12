"""Signed one-tap action links: the signature is the auth, so it must be
tamper-proof and fail closed with no secret."""

from zerocrm.actions import action_url, sign, verify

SECRET = "s3cr3t"


def test_sign_verify_roundtrip():
    sig = sign(5, "ok", SECRET)
    assert sig and verify(5, "ok", sig, SECRET)


def test_verify_rejects_tamper():
    sig = sign(5, "ok", SECRET)
    assert not verify(5, "skip", sig, SECRET)   # action swapped
    assert not verify(6, "ok", sig, SECRET)     # item swapped
    assert not verify(5, "ok", "deadbeef", SECRET)


def test_empty_secret_fails_closed():
    assert sign(5, "ok", "") == ""
    assert not verify(5, "ok", "", "")          # no secret -> never verifies


def test_action_url_shape():
    u = action_url("https://w.fly.dev/", 7, "ok", SECRET)
    assert u.startswith("https://w.fly.dev/act?item=7&action=ok&sig=")
