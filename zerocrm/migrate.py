"""Standalone migration runner — deliberately NOT on any product release train.

ponytail: migration 001 is `create_all` because the schema is greenfield and
create_all is idempotent. Later, additive changes get numbered functions in
MIGRATIONS below (a new version key + a callable that ALTERs). Don't reach for
Alembic until incremental DDL actually hurts.
"""

from __future__ import annotations

from sqlalchemy import insert, inspect, select, text
from sqlalchemy.engine import Engine

from .schema import (metadata, research, schema_migrations, call, transcript,
                     memory_chunk, location, staff, contract, contract_staff,
                     contract_line, document, inspection, contract_event,
                     vendor, supply, price_event, quote, quote_line)


def _create_all(engine: Engine) -> None:
    metadata.create_all(engine)


def _research(engine: Engine) -> None:
    # additive: create just the research table on DBs already at 001 (fresh DBs
    # got it from create_all; checkfirst makes this a no-op there).
    research.create(engine, checkfirst=True)


def _conversation(engine: Engine) -> None:
    # additive: create the slice-1 conversation-memory tables on DBs already at
    # 002 (fresh DBs got them from create_all; checkfirst makes this a no-op there).
    for t in (call, transcript, memory_chunk):
        t.create(engine, checkfirst=True)


def _postsale(engine: Engine) -> None:
    # additive: the slice-2 post-sale system-of-record tables.
    for t in (location, staff, contract, contract_staff, contract_line,
              document, inspection, contract_event):
        t.create(engine, checkfirst=True)


def _cpq(engine: Engine) -> None:
    for t in (vendor, supply, price_event, quote, quote_line):
        t.create(engine, checkfirst=True)
    # additive COLUMN on an existing table: create_all can't add it. Guard on the
    # inspector so this no-ops on a fresh DB (001 create_all already made the
    # column) and ADDs on a DB that reached 004 before supply_id existed. Soft ref
    # (no FK) so the ALTER is identical on SQLite and Postgres.
    cols = {c["name"] for c in inspect(engine).get_columns("contract_line")}
    if "supply_id" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE contract_line ADD COLUMN supply_id VARCHAR(32)"))


def _staff_api_token(engine: Engine) -> None:
    # additive COLUMN on an existing table (staff was created at 004_postsale);
    # same guarded-ALTER pattern as _cpq's supply_id.
    cols = {c["name"] for c in inspect(engine).get_columns("staff")}
    if "api_token" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE staff ADD COLUMN api_token TEXT"))


# version -> callable(engine). Ordered by key.
MIGRATIONS: dict[str, callable] = {
    "001_core": _create_all,
    "002_research": _research,
    "003_conversation": _conversation,
    "004_postsale": _postsale,
    "005_cpq": _cpq,
    "006_staff_api_token": _staff_api_token,
}


def applied_versions(engine: Engine) -> set[str]:
    # schema_migrations is created by create_all; on a fresh DB it won't exist yet.
    if not inspect(engine).has_table("schema_migrations"):
        return set()
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(select(schema_migrations.c.version))}


def migrate(engine: Engine) -> list[str]:
    """Apply every pending migration in order. Returns the versions applied now."""
    done = applied_versions(engine)
    ran: list[str] = []
    for version, fn in MIGRATIONS.items():
        if version in done:
            continue
        fn(engine)
        with engine.begin() as conn:
            conn.execute(insert(schema_migrations).values(version=version))
        ran.append(version)
    return ran
