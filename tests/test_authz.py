from sqlalchemy import insert

from zerocrm.authz import verify_api_token
from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import staff


def _engine():
    e = make_engine("sqlite://")
    migrate(e)
    with e.begin() as conn:
        conn.execute(insert(staff).values(id="u1", full_name="Ana", api_token="secret-token"))
        conn.execute(insert(staff).values(id="u2", full_name="Bo", api_token=None))
        conn.execute(insert(staff).values(id="u3", full_name="Cy", api_token="secret-token", active=False))
    return e


def test_verify_api_token_true_for_matching_token():
    assert verify_api_token(_engine(), "u1", "secret-token") is True


def test_verify_api_token_false_for_wrong_token():
    assert verify_api_token(_engine(), "u1", "nope") is False


def test_verify_api_token_false_for_missing_token():
    assert verify_api_token(_engine(), "u1", "") is False
    assert verify_api_token(_engine(), "u1", None) is False


def test_verify_api_token_false_for_unknown_staff():
    assert verify_api_token(_engine(), "does-not-exist", "secret-token") is False


def test_verify_api_token_false_when_staff_has_no_token_set():
    assert verify_api_token(_engine(), "u2", "anything") is False


def test_verify_api_token_false_for_deactivated_staff_even_with_matching_token():
    assert verify_api_token(_engine(), "u3", "secret-token") is False
