"""Live protected-apps list.

Three layers:
  1. CRITICAL_PROCESSES — hardcoded floor (lsass, csrss, …). Never editable.
  2. Shipping IT defaults — editable by the user through Settings; may be
     cleared entirely on a non-work PC that has no ESET / Optima / Acall.
  3. User extras — comma-separated keywords added by the user.

Everything is matched case-insensitive, by substring of the process /
executable basename (same semantics as the old frozenset check).

Settings keys:
  protected_apps_defaults  "true"/"false"  — include shipping IT defaults
  protected_apps_extra     "ekrn,optima"   — comma-separated user keywords
"""
from scanner import IT_PATTERN, CRITICAL_PROCESSES

import db


def _parse_list(raw: str) -> list:
    if not raw:
        return []
    out = []
    for part in raw.replace(";", ",").split(","):
        kw = part.strip().lower()
        if kw and kw not in out:
            out.append(kw)
    return out


def get_user_extras() -> list:
    return _parse_list(db.get_setting("protected_apps_extra"))


def defaults_enabled() -> bool:
    return (db.get_setting("protected_apps_defaults") or "true").lower() != "false"


def get_protected_keywords() -> frozenset:
    """Return the live set of blocked substrings for close_app / scan skip."""
    out = set(CRITICAL_PROCESSES)
    if defaults_enabled():
        out |= set(IT_PATTERN)
    out |= set(get_user_extras())
    return frozenset(out)


def get_config() -> dict:
    return {
        "critical":        sorted(CRITICAL_PROCESSES),
        "defaults":        sorted(IT_PATTERN),
        "defaults_on":     defaults_enabled(),
        "extras":          get_user_extras(),
    }


def set_config(defaults_on: bool, extras: list) -> dict:
    db.set_setting("protected_apps_defaults", "true" if defaults_on else "false")
    cleaned = _parse_list(",".join(str(x) for x in (extras or [])))
    db.set_setting("protected_apps_extra", ",".join(cleaned))
    return get_config()
