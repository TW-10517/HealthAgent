"""Physical-disk health probe (SupportAssist-style).

Uses `Get-PhysicalDisk` which returns Windows Storage Management's
own HealthStatus for every physical disk on the box. That status is
derived from the drive's SMART data by Windows itself — we don't have
to parse SMART attributes directly, which keeps the dependency surface
small and avoids shipping smartctl.

Fallback chain:
  1. `Get-PhysicalDisk | Select FriendlyName, HealthStatus, OperationalStatus,
     MediaType, BusType, Size, Usage`.
  2. Legacy `wmic diskdrive get Model,Status,Size,InterfaceType /format:csv`
     when the Storage Management cmdlet is unavailable (older/stripped
     Windows installs).
  3. Empty list if both fail. The UI is tolerant.

HealthStatus values from the API: 'Healthy', 'Warning', 'Unhealthy',
'Unknown'. We normalise to lower-case and map Warning/Unhealthy to a
single 'caution' badge so the UI only has to render three states.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any

from app import run_hidden

_PS_PRIMARY = (
    "Get-PhysicalDisk | Select-Object FriendlyName, SerialNumber, HealthStatus, "
    "OperationalStatus, MediaType, BusType, Size, Usage | ConvertTo-Json -Compress"
)

_PS_WMIC = (
    "Get-CimInstance Win32_DiskDrive | "
    "Select-Object Model,SerialNumber,Status,Size,InterfaceType,MediaType | "
    "ConvertTo-Json -Compress"
)


def _run_ps(cmd: str) -> str | None:
    try:
        r = run_hidden.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", cmd],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    return out or None


def _as_list(obj: Any) -> list:
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    return [obj]


def _fmt_gb(bytes_: Any) -> str:
    try:
        gb = int(bytes_) / (1024 ** 3)
    except Exception:
        return ""
    if gb >= 1000:
        return f"{gb / 1024:.1f} TB"
    return f"{gb:.0f} GB"


def _norm_health(raw: str) -> str:
    """Return one of: healthy | caution | bad | unknown."""
    s = (raw or "").strip().lower()
    if s in ("healthy", "ok"):
        return "healthy"
    if s in ("warning", "degraded", "predicted failure", "pred fail"):
        return "caution"
    if s in ("unhealthy", "error", "fail", "failed"):
        return "bad"
    return "unknown"


def _probe_primary() -> list[dict]:
    raw = _run_ps(_PS_PRIMARY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    out = []
    for d in _as_list(data):
        if not isinstance(d, dict):
            continue
        health = _norm_health(d.get("HealthStatus") or "")
        out.append({
            "model":   (d.get("FriendlyName") or "").strip() or "Disk",
            "serial":  (d.get("SerialNumber") or "").strip(),
            "health":  health,
            "health_raw": d.get("HealthStatus") or "Unknown",
            "op_status": (d.get("OperationalStatus") or "").strip(),
            "media":   (d.get("MediaType") or "").strip(),
            "bus":     (d.get("BusType") or "").strip(),
            "size":    _fmt_gb(d.get("Size")),
            "usage":   (d.get("Usage") or "").strip(),
            "source":  "storage-api",
        })
    return out


def _probe_wmic() -> list[dict]:
    raw = _run_ps(_PS_WMIC)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    out = []
    for d in _as_list(data):
        if not isinstance(d, dict):
            continue
        health = _norm_health(d.get("Status") or "")
        out.append({
            "model":   (d.get("Model") or "").strip() or "Disk",
            "serial":  (d.get("SerialNumber") or "").strip(),
            "health":  health,
            "health_raw": d.get("Status") or "Unknown",
            "op_status": "",
            "media":   (d.get("MediaType") or "").strip(),
            "bus":     (d.get("InterfaceType") or "").strip(),
            "size":    _fmt_gb(d.get("Size")),
            "usage":   "",
            "source":  "wmic",
        })
    return out


def get_disks() -> dict:
    """Return `{ok, disks:[...], overall}`.

    `overall` is the worst status across all disks ('bad' > 'caution'
    > 'healthy' > 'unknown'), so the Storage page can show a single
    top-line badge without the UI having to compute it.
    """
    disks = _probe_primary() or _probe_wmic()
    if not disks:
        return {"ok": False, "disks": [], "overall": "unknown",
                "msg": "Could not query disk health."}
    rank = {"bad": 3, "caution": 2, "healthy": 1, "unknown": 0}
    overall = max((d["health"] for d in disks), key=lambda h: rank.get(h, 0))
    return {"ok": True, "disks": disks, "overall": overall}
