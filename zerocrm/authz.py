"""Per-user API token check for read-only export endpoints.

`staff` rows are the customer's own users (see schema.py); each may carry an
opaque `api_token`. A caller must present that exact token, as a bearer
credential, to act as that user — constant-time compare so timing can't leak
the stored value.
"""

from __future__ import annotations

import hmac

from sqlalchemy import select
from sqlalchemy.engine import Engine

from .schema import staff


def verify_api_token(engine: Engine, staff_id: str | None, token: str | None) -> bool:
    """True iff `token` is non-empty and matches the stored api_token for an
    ACTIVE staff_id. A deactivated staff row's token is rejected even if it
    still matches, so offboarding revokes export access immediately."""
    if not staff_id or not token:
        return False
    with engine.connect() as conn:
        row = conn.execute(
            select(staff.c.api_token, staff.c.active).where(staff.c.id == staff_id)
        ).first()
    if row is None or not row.active or not row.api_token:
        return False
    return hmac.compare_digest(row.api_token, token)
