"""Warmup watcher — auto-enables cold sending once warmup completes, so the
operator never has to track "is warmup done yet?".

Runs on every tick. When ALL configured sending mailboxes clear the readiness
bar (reputation, zero spam, ramp-complete send volume) AND we're past the
earliest-allowed date, it flips `sending_enabled` on and drops a headline notice
into the next digest. Fail-safe by design: the date floor stops it firing early,
and a wrong criterion errs toward staying OFF, never toward sending prematurely.

Defaults are conservative; every threshold is overridable in config.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.engine import Engine

from .config import get_config, sending_enabled, set_config

_DEFAULTS = {
    "auto_enable_on_warmup": True,
    "warmup_min_reputation": 95,
    "warmup_max_spam": 0,
    "warmup_min_sent": 150,          # evidence the ramp actually ran
    "warmup_earliest_date": "2026-09-01",  # hard floor: never enable before this
}


def _cfg(engine: Engine, key: str):
    return get_config(engine, key, _DEFAULTS[key])


def warmup_ready(engine: Engine, smartlead, *, today: date | None = None) -> tuple[bool, str]:
    """True only if the date floor has passed AND every sending mailbox clears
    the bar. Returns (ready, reason)."""
    today = today or date.today()
    floor = date.fromisoformat(_cfg(engine, "warmup_earliest_date"))
    if today < floor:
        return False, f"before earliest date {floor}"

    accounts = get_config(engine, "warmup_accounts", None)
    if not accounts:
        accounts = [a.get("id") for a in smartlead.list_email_accounts() if a.get("id")]
    if not accounts:
        return False, "no sending mailboxes configured"

    min_rep = _cfg(engine, "warmup_min_reputation")
    max_spam = _cfg(engine, "warmup_max_spam")
    min_sent = _cfg(engine, "warmup_min_sent")
    for aid in accounts:
        detail = smartlead.account(aid)
        rep = ((detail.get("warmup_details") or {}).get("warmup_reputation")) or 0
        stats = smartlead.warmup_stats(aid)
        sent = int(stats.get("sent_count") or 0)
        spam = int(stats.get("spam_count") or 0)
        if rep < min_rep or spam > max_spam or sent < min_sent:
            return False, f"account {aid} not ready (rep={rep}, spam={spam}, sent={sent})"
    return True, "all mailboxes ready"


def maybe_enable_sending(engine: Engine, smartlead, *, today: date | None = None) -> str:
    """Auto-flip sending_enabled when warmup completes. Returns the outcome:
    'already_enabled' | 'auto_disabled' | 'not_ready:<reason>' | 'enabled'."""
    if sending_enabled(engine):
        return "already_enabled"
    if not get_config(engine, "auto_enable_on_warmup", _DEFAULTS["auto_enable_on_warmup"]):
        return "auto_disabled"
    ready, reason = warmup_ready(engine, smartlead, today=today)
    if not ready:
        return f"not_ready:{reason}"
    set_config(engine, "sending_enabled", True)
    set_config(engine, "sending_enabled_at", (today or date.today()).isoformat())
    add_notice(engine, "Warmup complete — cold sending was auto-enabled. "
                       "Kill switch: zerocrm set-config sending_enabled false")
    return "enabled"


# --- digest notices (rendered at the top of the next digest, then cleared) ---
def add_notice(engine: Engine, message: str) -> None:
    notices = get_config(engine, "notices", []) or []
    set_config(engine, "notices", notices + [message])


def pop_notices(engine: Engine) -> list[str]:
    notices = get_config(engine, "notices", []) or []
    if notices:
        set_config(engine, "notices", [])
    return notices
