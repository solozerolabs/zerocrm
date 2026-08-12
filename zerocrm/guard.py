"""Low-balance guard — closes the one soft human dependency (credit top-ups)
into a clean, observable stop.

Providers expose balance() (credits; -1 = unknown). When a provider is at/below
its floor we DON'T silently drop prospects — we pause that tier for the run and
drop a notice into the next digest so the operator gets pinged to top up. An
unknown balance (-1) stays in and fails open on the read: the provider's own
CREDITS_INSUFFICIENT / 402 still stops a truly-empty account loudly.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine

from .warmup import add_notice


def usable(engine: Engine, name: str, driver, floor: int) -> bool:
    """True if the tier may run. Posts a digest notice (once) when it's paused."""
    bal = driver.balance()
    if 0 <= bal <= floor:
        add_notice(engine, f"{name} credits low ({bal}, floor {floor}). "
                           f"This tier is paused until you top up.")
        return False
    return True
