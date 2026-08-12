"""Key/value config + operational flags/cursors, stored in the `config` table.

Everything an operator (or their agent) tunes by prompt lives here, plus runtime
state that must survive restarts: the digest thread ids, the Smartlead poll
high-water marks, and the sending_enabled activation flag.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from .schema import config as config_t


def get_config(engine: Engine, key: str, default=None):
    with engine.connect() as conn:
        row = conn.execute(select(config_t.c.value).where(config_t.c.key == key)).first()
    return row[0] if row else default


def set_config(engine: Engine, key: str, value) -> None:
    with engine.begin() as conn:
        exists = conn.execute(select(config_t.c.key).where(config_t.c.key == key)).first()
        if exists:
            conn.execute(update(config_t).where(config_t.c.key == key).values(value=value))
        else:
            conn.execute(config_t.insert().values(key=key, value=value))


def sending_enabled(engine: Engine) -> bool:
    """The activation gate. Defaults OFF — real cold-email enrollment does not
    happen until this is flipped true (after warmup completes AND the live
    Smartlead shapes are verified). Everything else (drafting, the operator
    digest, reply ingestion) runs regardless."""
    return bool(get_config(engine, "sending_enabled", False))
