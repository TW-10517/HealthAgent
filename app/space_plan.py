"""Helix — Conversational cleanup planner.

User says "I need 10 GB free" → `build_plan(10)` returns a per-group plan
with sizes drawn from existing readers (temp scan, recycle bin, browser
cache, old files, large files). Narrate-only: every row carries the
existing cleanup surface the user can hit — this module never deletes.

Shape of `build_plan()`:
    {
      "ok": bool,
      "target_gb": float,
      "current_free_gb": float | None,
      "needed_gb": float,
      "groups": [
        {"id": "temp"|"recycle"|"browser"|"old"|"large",
         "label": str,
         "size_gb": float,
         "items": int,
         "action": str,         # "cleanup_files" | "empty_recycle_bin"
                                #  | "clean_browser_cache" | "open_page"
         "target": str | None,  # browser name, page id, etc.
         "paths": [str, ...] | None,
         "safe": bool},
        ...
      ],
      "total_available_gb": float,
      "can_meet_target": bool,
    }
"""
from __future__ import annotations
import os
import shutil


_BYTES_PER_GB = 1024.0 ** 3


def _free_gb() -> float | None:
    try:
        total, used, free = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
        return round(free / _BYTES_PER_GB, 2)
    except Exception:
        return None


def _group_temp() -> dict:
    try:
        from scanner import scan_temp
    except Exception:
        return {"size_gb": 0.0, "items": 0, "paths": []}
    try:
        items = scan_temp() or []
    except Exception:
        items = []
    total = 0
    paths = []
    for it in items:
        sz = it.get("size", 0) or 0
        total += sz
        p = it.get("path")
        if p:
            paths.append(p)
    return {"size_gb": round(total / _BYTES_PER_GB, 2),
            "items": len(items),
            "paths": paths}


def _group_recycle() -> dict:
    try:
        import ctypes
        import ctypes.wintypes as wt

        class _RBINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD),
                         ("i64Size", ctypes.c_int64),
                         ("i64NumItems", ctypes.c_int64)]
        info = _RBINFO()
        info.cbSize = ctypes.sizeof(_RBINFO)
        if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
            return {"size_gb": round(info.i64Size / _BYTES_PER_GB, 2),
                    "items": int(info.i64NumItems)}
    except Exception:
        pass
    return {"size_gb": 0.0, "items": 0}


def _group_browser() -> dict:
    try:
        import sys_executor as sx
        res = sx.get_browser_cache_sizes() or {}
    except Exception:
        return {"size_gb": 0.0, "items": 0, "browsers": {}}
    browsers = res.get("browsers") or {}
    total = 0
    for b, info in browsers.items():
        total += int(info.get("size_bytes", 0) or 0)
    return {"size_gb": round(total / _BYTES_PER_GB, 2),
            "items": len(browsers),
            "browsers": {b: int(i.get("size_bytes", 0) or 0)
                         for b, i in browsers.items()}}


def _group_old(days: int = 180) -> dict:
    try:
        import sys_executor as sx
        res = sx.get_old_files(days=days, limit=50) or {}
    except Exception:
        return {"size_gb": 0.0, "items": 0}
    files = res.get("files") or []
    total_mb = sum(float(f.get("size_mb", 0) or 0) for f in files)
    return {"size_gb": round(total_mb / 1024.0, 2),
            "items": len(files)}


def _group_large(min_mb: int = 100) -> dict:
    try:
        import sys_executor as sx
        res = sx.get_large_files(min_size_mb=min_mb, limit=50) or {}
    except Exception:
        return {"size_gb": 0.0, "items": 0}
    files = res.get("files") or []
    total_mb = sum(float(f.get("size_mb", 0) or 0) for f in files)
    return {"size_gb": round(total_mb / 1024.0, 2),
            "items": len(files)}


def build_plan(target_gb: float) -> dict:
    try:
        target = max(0.0, float(target_gb))
    except Exception:
        target = 0.0
    free = _free_gb()
    needed = target
    groups: list[dict] = []

    t = _group_temp()
    if t["size_gb"] > 0.01 or t["items"]:
        groups.append({
            "id": "temp",
            "label": "Temp files & caches",
            "size_gb": t["size_gb"],
            "items": t["items"],
            "action": "cleanup_files",
            "target": None,
            "paths": t.get("paths") or [],
            "safe": True,
        })

    r = _group_recycle()
    if r["size_gb"] > 0.01 or r["items"]:
        groups.append({
            "id": "recycle",
            "label": "Recycle Bin",
            "size_gb": r["size_gb"],
            "items": r["items"],
            "action": "empty_recycle_bin",
            "target": None,
            "paths": None,
            "safe": True,
        })

    b = _group_browser()
    if b["size_gb"] > 0.01:
        groups.append({
            "id": "browser",
            "label": "Browser caches",
            "size_gb": b["size_gb"],
            "items": b["items"],
            "action": "clean_browser_cache",
            "target": None,                 # UI picks per-browser
            "paths": None,
            "safe": True,
            "browsers": b.get("browsers") or {},
        })

    o = _group_old()
    if o["size_gb"] > 0.01:
        groups.append({
            "id": "old",
            "label": "Old files (6+ months untouched)",
            "size_gb": o["size_gb"],
            "items": o["items"],
            "action": "open_page",
            "target": "storage",
            "paths": None,
            "safe": False,                  # user review required
        })

    lg = _group_large()
    if lg["size_gb"] > 0.01:
        groups.append({
            "id": "large",
            "label": "Large files (≥100 MB each)",
            "size_gb": lg["size_gb"],
            "items": lg["items"],
            "action": "open_page",
            "target": "storage",
            "paths": None,
            "safe": False,
        })

    groups.sort(key=lambda g: -g["size_gb"])
    total = round(sum(g["size_gb"] for g in groups), 2)
    return {
        "ok": True,
        "target_gb": round(target, 2),
        "current_free_gb": free,
        "needed_gb": round(needed, 2),
        "groups": groups,
        "total_available_gb": total,
        "can_meet_target": total + 0.01 >= target,
    }


def plan_narration(plan: dict) -> str:
    """Short plain-English summary for assistant reply."""
    if not plan.get("ok"):
        return "I couldn't build a cleanup plan right now."
    tgt = plan.get("target_gb") or 0.0
    avail = plan.get("total_available_gb") or 0.0
    free = plan.get("current_free_gb")
    groups = plan.get("groups") or []
    if not groups:
        return (f"Nothing obvious to clean — your disk already looks tidy."
                f" Current free: {free:.1f} GB." if free is not None
                else "Nothing obvious to clean — your disk already looks tidy.")
    tops = ", ".join(f"{g['label']} ({g['size_gb']:.1f} GB)" for g in groups[:3])
    if plan.get("can_meet_target"):
        base = f"I can free about {avail:.1f} GB — enough to hit your {tgt:.0f} GB goal."
    else:
        short = max(0.0, tgt - avail)
        base = (f"I can free about {avail:.1f} GB, short of your {tgt:.0f} GB goal "
                f"by roughly {short:.1f} GB.")
    return f"{base} Biggest wins: {tops}."
