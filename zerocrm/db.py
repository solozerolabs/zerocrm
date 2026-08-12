"""Engine factory. SQLite is the public default; Postgres is opt-in via DSN.

Our deployment points ZEROCRM_DATABASE_URL at a managed Postgres and sets
ZEROCRM_PG_SCHEMA=zerocrm so every connection runs under a schema-scoped role
inside the zerocrm schema — the public repo never hardcodes any of that.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine


def make_engine(dsn: str | None = None, pg_schema: str | None = None) -> Engine:
    dsn = dsn or os.environ.get("ZEROCRM_DATABASE_URL") or "sqlite:///zerocrm.db"
    pg_schema = pg_schema or os.environ.get("ZEROCRM_PG_SCHEMA")
    engine = create_engine(dsn, future=True)

    if engine.dialect.name == "sqlite":
        # enforce FK constraints (off by default in SQLite)
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    elif engine.dialect.name == "postgresql" and pg_schema:
        # Tables are schema-unqualified so SQLite works. On Postgres we pin them
        # to pg_schema via schema_translate_map, which makes SQLAlchemy emit
        # fully-qualified `<schema>.<table>` in every statement — deterministic,
        # and immune to the search_path-lost-on-rollback trap. (The role's
        # server-side `search_path` covers any raw SQL / reflection.)
        engine = engine.execution_options(schema_translate_map={None: pg_schema})

    return engine
