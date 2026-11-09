"""Enable / disable Windows logon entries the way Task Manager does.

Windows does not delete a Run value when an entry is switched off in Task
Manager. It records the decision under

    SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StartupApproved\\<kind>

as a binary blob whose first byte carries the state (0x02 = enabled,
0x03 = disabled) followed by a FILETIME of the moment it was switched off.
Flipping that byte is what this module does, because it is:

  * reversible -- the Run value is left intact, so switching an entry back on
    does not depend on having kept a copy of its command anywhere;
  * durable -- an installer that rewrites its own Run value does not silently
    re-enable itself, which deletion cannot prevent;
  * the only mechanism that works for Startup-folder shortcuts, which have no
    registry value to delete in the first place;
  * the same record `scanner.scan_startup()` already reads, so a change made
    here shows up in the next scan with no extra bookkeeping.

The previous implementation deleted the HKCU Run value and stashed the command
in a JSON archive. That covered one of the five places Windows looks for logon
entries, and the archive went stale if the app was reinstalled in between.
Entries switched off by that older build are still restored (`_legacy_restore`)
so nothing a user disabled before is stranded.

Scope note: HKLM entries are machine-wide and Windows requires elevation to
change them. Helix never elevates itself, so those calls return ok=False with
needs_admin=True and the UI says so, rather than failing with a bare
"Access is denied".
"""
from __future__ import annotations

import os
import time
import winreg
from typing import Any

import db as _db

_APPROVED_BASE = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer"
                  r"\StartupApproved")

# Keyed by the `source` label that scanner.scan_startup() stamps on every
# entry, so the UI hands that string straight back and no second lookup table
# has to be kept in sync with the scanner.
#   approved_*  : the StartupApproved key that holds the on/off bit
#   run         : (hive, subkey) of the Run key, or None for folder sources
#   folder      : (env var, relative path) for Startup-folder sources
#   needs_admin : True when the write targets HKLM
_SOURCES: dict[str, dict[str, Any]] = {
    "HKCU\\Run": {
        "approved_hive": winreg.HKEY_CURRENT_USER,
        "approved_sub": _APPROVED_BASE + r"\Run",
        "run": (winreg.HKEY_CURRENT_USER,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
        "folder": None,
        "needs_admin": False,
    },
    "HKLM\\Run": {
        "approved_hive": winreg.HKEY_LOCAL_MACHINE,
        "approved_sub": _APPROVED_BASE + r"\Run",
        "run": (winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
        "folder": None,
        "needs_admin": True,
    },
    "HKLM\\Run (32-bit)": {
        "approved_hive": winreg.HKEY_LOCAL_MACHINE,
        "approved_sub": _APPROVED_BASE + r"\Run32",
        "run": (winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
        "folder": None,
        "needs_admin": True,
    },
    "Startup folder": {
        "approved_hive": winreg.HKEY_CURRENT_USER,
        "approved_sub": _APPROVED_BASE + r"\StartupFolder",
        "run": None,
        "folder": ("APPDATA", r"Microsoft\Windows\Start Menu\Programs\Startup"),
        "needs_admin": False,
    },
    "Startup folder (all users)": {
        "approved_hive": winreg.HKEY_LOCAL_MACHINE,
        "approved_sub": _APPROVED_BASE + r"\StartupFolder",
        "run": None,
        "folder": ("PROGRAMDATA",
                   r"Microsoft\Windows\Start Menu\Programs\Startup"),
        "needs_admin": True,
    },
}

# Windows epoch (1601-01-01) to Unix epoch, in seconds.
_FILETIME_EPOCH_DELTA = 11644473600


def sources() -> list:
    """The source labels this module can act on."""
    return list(_SOURCES)


def is_admin() -> bool:
    """True when the process can write HKLM. Never raises."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _entry(name: str, source: str = ""):
    """The scanner's own row for this entry, or None.

    Reusing `scanner.scan_startup()` rather than re-reading the registry here
    means the IT/system classification the UI displayed is the exact one the
    refusal below is based on. Two separate classifications of the same entry
    is how the startup counts came to disagree in the first place.
    """
    try:
        import scanner
        low = name.lower()
        for a in scanner.scan_startup():
            if a.get("name", "").lower() != low:
                continue
            if source and a.get("source", "") != source:
                continue
            return a
    except Exception:
        return None
    return None


def block_for_row(row) -> str:
    """Why Helix will not switch this entry off: '' | 'admin' | 'it'.

    'it' covers the managed security and compliance agents CLAUDE.md puts
    off-limits (ESET, OPTIMA, Acall, Eye"247", and the Microsoft agents in
    IT_PATTERN). On a domain-joined machine, switching one of those off is not
    the user's call to make through a cleanup utility.

    Takes a `scanner.scan_startup()` row, so a caller that already has the
    list (the scan payload builder) spends nothing, and `toggle_block` below
    looks the row up for callers that only have a name.
    """
    if (row or {}).get("category") == "it":
        return "it"
    if not can_toggle((row or {}).get("source", "")):
        return "admin"
    return ""


def toggle_block(name: str, source: str = "") -> str:
    """`block_for_row` for a caller that has only the entry's name."""
    row = _entry(name, source)
    if row is None:
        return "admin" if not can_toggle(source) else ""
    return block_for_row(row)


def can_toggle(source: str = "") -> bool:
    """True when this process can change that source's entries.

    HKLM sources need elevation. The UI calls this so a row can be labelled
    up front instead of offering a button that is guaranteed to fail.
    """
    spec = _SOURCES.get(source)
    if spec is None:
        return True  # Unknown label: let the attempt decide and report.
    return (not spec["needs_admin"]) or is_admin()


def _blob(enabled: bool) -> bytes:
    """The 12-byte StartupApproved value for an on/off state.

    Byte 0 carries the state and the remaining bytes are a FILETIME that Task
    Manager fills in when switching an entry off. `scanner._read_approved`
    reads this back as `val[0] & 1`, so the low bit is the part that matters;
    the timestamp is written anyway so the value is indistinguishable from one
    Task Manager produced.
    """
    if enabled:
        return b"\x02" + b"\x00" * 11
    ft = int((time.time() + _FILETIME_EPOCH_DELTA) * 10_000_000)
    return b"\x03\x00\x00\x00" + ft.to_bytes(8, "little")


def _folder_path(spec: dict) -> str:
    env, rel = spec["folder"]
    base = os.environ.get(env) or ""
    return os.path.join(base, rel) if base else ""


def _approved_value_name(spec: dict, name: str):
    """The exact string StartupApproved uses as the value name for `name`.

    Run entries are keyed by the registry value name, which can differ from
    what the UI shows only in case. Startup-folder entries are keyed by the
    file name *including* its extension while the UI shows the stem, so the
    folder is searched for a file whose stem matches.
    """
    low = name.lower()
    if spec["run"]:
        hive, sub = spec["run"]
        try:
            with winreg.OpenKey(hive, sub) as key:
                i = 0
                while True:
                    try:
                        vn, _, _ = winreg.EnumValue(key, i)
                        i += 1
                    except OSError:
                        break
                    if vn and vn.lower() == low:
                        return vn
        except OSError:
            return None
        return None

    folder = _folder_path(spec)
    if not folder or not os.path.isdir(folder):
        return None
    try:
        for e in os.scandir(folder):
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if e.name.lower() == low or os.path.splitext(e.name)[0].lower() == low:
                return e.name
    except OSError:
        return None
    return None


def _resolve(name: str, source: str = ""):
    """Find the source holding `name`. Returns (label, spec, approved_name)."""
    if source and source in _SOURCES:
        an = _approved_value_name(_SOURCES[source], name)
        if an:
            return source, _SOURCES[source], an
        # The caller's label is stale (entry removed, or moved between hives);
        # fall through and look everywhere rather than reporting it missing.
    for label, spec in _SOURCES.items():
        an = _approved_value_name(spec, name)
        if an:
            return label, spec, an
    return None, None, None


def read_state(name: str, source: str = ""):
    """True if the entry runs at logon, False if switched off, None if absent.

    An entry with no StartupApproved record has never been touched and
    therefore runs -- the same rule `scanner._read_approved` applies.
    """
    label, spec, approved_name = _resolve(name, source)
    if spec is None:
        return None
    try:
        with winreg.OpenKey(spec["approved_hive"], spec["approved_sub"]) as key:
            val, _ = winreg.QueryValueEx(key, approved_name)
            return not bool(val[0] & 1)
    except FileNotFoundError:
        return True
    except (OSError, TypeError, IndexError):
        return True


def _legacy_restore(name: str, legacy_path):
    """Put back a Run value deleted by the pre-1.1.7 disable path.

    Returns None when there is no archive record, so the caller carries on with
    the normal StartupApproved flip. Returns an error dict only when a record
    exists but could not be restored.
    """
    import json
    try:
        if not legacy_path or not os.path.isfile(str(legacy_path)):
            return None
        with open(str(legacy_path), encoding="utf-8") as f:
            data = json.loads(f.read() or "{}")
    except (OSError, ValueError):
        return None
    entry = (data or {}).get(name)
    if not isinstance(entry, dict) or "value" not in entry:
        return None
    try:
        hive = (winreg.HKEY_CURRENT_USER if entry.get("hive", "HKCU") == "HKCU"
                else winreg.HKEY_LOCAL_MACHINE)
        with winreg.OpenKey(hive, entry["subkey"], 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, name, 0, int(entry.get("type", winreg.REG_SZ)),
                              entry["value"])
    except OSError as e:
        return {"ok": False, "msg": f"Could not restore '{name}': {e}"}
    data.pop(name, None)
    try:
        with open(str(legacy_path), "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
    except OSError:
        pass  # The value is back; a stale archive entry is harmless.
    return None


def set_enabled(name: str, source: str = "", enabled: bool = True,
                legacy_path=None) -> dict:
    """Switch one logon entry on or off, and verify it took.

    Always returns a dict. `needs_admin` is True when the entry lives in HKLM
    and this process is not elevated.
    """
    action = "enable" if enabled else "disable"
    name = (name or "").strip()
    if not name or any(c in name for c in "\\/\x00"):
        return {"ok": False, "msg": "Invalid name."}

    if enabled and legacy_path:
        legacy_err = _legacy_restore(name, legacy_path)
        if legacy_err:
            _db.log_action("startup", action, name, "error",
                           legacy_err.get("msg", ""))
            return legacy_err

    label, spec, approved_name = _resolve(name, source)
    if spec is None:
        return {"ok": False,
                "msg": f"'{name}' is not a logon entry on this PC."}

    # Switching a managed agent off is refused here, not just hidden in the UI,
    # so the chat assistant and any future caller get the same answer.
    # Switching one back ON is allowed -- that only restores the IT baseline.
    if not enabled and toggle_block(name, label) == "it":
        _db.log_action("startup", action, name, "blocked", f"IT-managed ({label})")
        return {"ok": False, "it_managed": True, "source": label,
                "msg": (f"'{name}' is an IT-managed agent. Helix will not "
                        "switch it off -- ask your IT team if it needs to "
                        "change.")}

    if spec["needs_admin"] and not is_admin():
        _db.log_action("startup", action, name, "blocked",
                       f"{label} requires administrator")
        return {"ok": False, "needs_admin": True, "source": label,
                "msg": (f"'{name}' is a machine-wide entry ({label}). "
                        "Windows requires administrator rights to change it, "
                        "and Helix does not elevate itself.")}

    try:
        with winreg.CreateKeyEx(spec["approved_hive"], spec["approved_sub"],
                                0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, approved_name, 0, winreg.REG_BINARY,
                              _blob(enabled))
    except PermissionError as e:
        _db.log_action("startup", action, name, "error", f"denied: {e}")
        return {"ok": False, "needs_admin": spec["needs_admin"],
                "source": label, "msg": f"Windows refused the change: {e}"}
    except OSError as e:
        _db.log_action("startup", action, name, "error", str(e))
        return {"ok": False, "source": label, "msg": str(e)}

    # Read the bit back rather than assuming the write landed.
    state = read_state(name, label)
    if state is not enabled:
        _db.log_action("startup", action, name, "error",
                       f"verify failed (reads {state})")
        return {"ok": False, "source": label,
                "msg": (f"The change did not stick -- '{name}' still reads "
                        f"{'enabled' if state else 'disabled'}.")}

    _db.log_action("startup", action, name, "ok", label)
    return {"ok": True, "name": name, "source": label, "enabled": enabled,
            "msg": (f"'{name}' will run at logon." if enabled
                    else f"'{name}' will no longer run at logon.")}


def row_for_ui(a: dict) -> dict:
    """One logon entry in the shape the Startup table renders.

    Defined once so the rows baked into a scan payload and the rows returned
    by `entries()` after a toggle are identical -- including `toggleBlock`,
    which the UI must never work out for itself.
    """
    return {"name": a.get("name", ""),
            "cmd": (a.get("command") or "")[:80],
            "it": bool(a.get("it_managed")),
            "enabled": a.get("enabled", True),
            "category": a.get("category", "personal"),
            "source": a.get("source", ""),
            "toggleBlock": block_for_row(a)}


def entries() -> dict:
    """Fresh logon entries plus counts, for refreshing the page after a toggle.

    Re-reads the registry rather than reusing the last scan, because the point
    of calling it is that the registry just changed.
    """
    try:
        import scanner
        rows = scanner.scan_startup()
        return {"ok": True,
                "counts": scanner.startup_counts(rows),
                "apps": [row_for_ui(a) for a in rows]}
    except Exception as e:  # pragma: no cover - registry/psutil failure
        return {"ok": False, "apps": [], "counts": {}, "error": str(e)}


def list_disabled(legacy_path=None) -> dict:
    """Every entry currently switched off, across all five sources."""
    items = []
    for label, spec in _SOURCES.items():
        try:
            with winreg.OpenKey(spec["approved_hive"], spec["approved_sub"]) as key:
                i = 0
                while True:
                    try:
                        vn, val, _ = winreg.EnumValue(key, i)
                        i += 1
                    except OSError:
                        break
                    if not vn:
                        continue
                    try:
                        if not (val[0] & 1):
                            continue
                    except (TypeError, IndexError):
                        continue
                    items.append({"name": vn, "source": label,
                                  "method": "StartupApproved"})
        except OSError:
            continue

    if legacy_path:
        import json
        try:
            with open(str(legacy_path), encoding="utf-8") as f:
                data = json.loads(f.read() or "{}")
            for k, v in (data or {}).items():
                items.append({"name": k,
                              "source": (v or {}).get("hive", "HKCU"),
                              "method": "removed (older Helix)",
                              "disabled_at": (v or {}).get("disabled_at", "")})
        except (OSError, ValueError):
            pass
    return {"ok": True, "items": items}
