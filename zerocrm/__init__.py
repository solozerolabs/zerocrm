"""zerocrm — agent-native CRM + outreach engine for zero-hour companies."""

from .db import make_engine
from .migrate import migrate
from .upsert import RANK, resolve_person, upsert_person

__all__ = ["make_engine", "migrate", "upsert_person", "resolve_person", "RANK"]
__version__ = "0.1.0"
