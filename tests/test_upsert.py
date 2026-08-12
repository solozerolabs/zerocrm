"""Precedence-gate + identity-resolution tests (review-mandated, §8 of the spec).

Runs on SQLite by default; the same suite runs against Postgres in CI.
"""

import pytest
from sqlalchemy import select

from zerocrm.db import make_engine
from zerocrm.migrate import migrate
from zerocrm.schema import person, person_identity
from zerocrm.upsert import resolve_person, upsert_person

HUMAN = {"kind": "human", "id": "sid"}
BOT = {"kind": "agent", "id": "draft-run"}


@pytest.fixture()
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path/'t.db'}")
    migrate(eng)
    return eng


def _get(engine, person_id):
    with engine.connect() as conn:
        return conn.execute(
            select(person.c.full_name, person.c.job_title, person.c.facts_meta).where(
                person.c.id == person_id
            )
        ).mappings().one()


def test_fill_null_at_any_rank(engine):
    with engine.begin() as conn:
        pid = upsert_person(conn, {"kind": "email", "value": "a@x.com"},
                            {"full_name": "Jane"}, "imported", BOT)
    assert _get(engine, pid)["full_name"] == "Jane"


def test_lower_rank_cannot_overwrite(engine):
    # human writes the title, then enrichment tries a different one -> refused
    with engine.begin() as conn:
        pid = upsert_person(conn, {"kind": "email", "value": "a@x.com"},
                            {"job_title": "Founder"}, "human", HUMAN)
    with engine.begin() as conn:
        upsert_person(conn, {"kind": "email", "value": "a@x.com"},
                      {"job_title": "Junior Dev"}, "enriched", BOT)
    row = _get(engine, pid)
    assert row["job_title"] == "Founder"                 # unchanged
    assert row["facts_meta"]["job_title"]["provenance"] == "human"


def test_equal_or_higher_rank_overwrites(engine):
    with engine.begin() as conn:
        pid = upsert_person(conn, {"kind": "email", "value": "a@x.com"},
                            {"job_title": "Dev"}, "enriched", BOT)
    # equal rank (enriched over enriched) allowed
    with engine.begin() as conn:
        upsert_person(conn, {"kind": "email", "value": "a@x.com"},
                      {"job_title": "Senior Dev"}, "enriched", BOT)
    assert _get(engine, pid)["job_title"] == "Senior Dev"
    # higher rank (human) allowed
    with engine.begin() as conn:
        upsert_person(conn, {"kind": "email", "value": "a@x.com"},
                      {"job_title": "Staff Eng"}, "human", HUMAN)
    assert _get(engine, pid)["job_title"] == "Staff Eng"


def test_null_fill_wins_even_below_stored_rank(engine):
    # human sets full_name; a later enriched write fills the still-empty job_title
    with engine.begin() as conn:
        pid = upsert_person(conn, {"kind": "email", "value": "a@x.com"},
                            {"full_name": "Jane"}, "human", HUMAN)
    with engine.begin() as conn:
        upsert_person(conn, {"kind": "email", "value": "a@x.com"},
                      {"job_title": "CEO"}, "enriched", BOT)
    row = _get(engine, pid)
    assert row["full_name"] == "Jane" and row["job_title"] == "CEO"


def test_facts_meta_stamped(engine):
    with engine.begin() as conn:
        pid = upsert_person(conn, {"kind": "email", "value": "a@x.com"},
                            {"full_name": "Jane"}, "verified_provider", BOT)
    meta = _get(engine, pid)["facts_meta"]["full_name"]
    assert meta["rank"] == 3 and meta["provenance"] == "verified_provider"
    assert meta["actor"] == BOT and "ts" in meta


def test_identity_is_idempotent(engine):
    with engine.begin() as conn:
        pid1 = upsert_person(conn, {"kind": "email", "value": "a@x.com"},
                             {"full_name": "Jane"}, "imported", BOT)
    with engine.begin() as conn:
        pid2 = upsert_person(conn, {"kind": "email", "value": "a@x.com"},
                             {"job_title": "CEO"}, "imported", BOT)
    assert pid1 == pid2
    with engine.connect() as conn:
        n = conn.execute(select(person_identity)).all()
    assert len(n) == 1


def test_unknown_provenance_rejected(engine):
    with engine.begin() as conn:
        with pytest.raises(ValueError):
            upsert_person(conn, {"kind": "email", "value": "a@x.com"},
                          {"full_name": "Jane"}, "guessed", BOT)


def test_resolve_person_none_when_absent(engine):
    with engine.connect() as conn:
        assert resolve_person(conn, "email", "nobody@x.com") is None
