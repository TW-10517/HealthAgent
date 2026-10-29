"""Helix — Battery coaching (Feature #11).

Live snapshot of current battery state + top CPU processes as a proxy for
power draw (psutil does not expose per-process watts on Windows). Optionally
asks Ollama for a short plain-English coaching tip grounded in the numbers.

Shape of `coach()`:
    {
      "available":     True,
      "ts":            "YYYY-MM-DD HH:MM:SS",
      "has_battery":   bool,
      "percent":       float | None,
      "plugged":       bool | None,
      "time_left_min": int | None,     # None if plugged or unknown
      "wear_pct":      int | None,     # from WMI design-vs-full-capacity
      "top_drain":     [{pid,name,cpu,mem_mb}, ...],  # up to 5
      "highlights":    [str, ...],
      "narrated":      str | None,
    }

Narrative only — no auto-actions. Degrades gracefully when Ollama is offline
or when the machine has no battery (desktop).
"""
from __future__ import annotations
import os
import time
import datetime as _dt
from typing import Any

_TOP_N = 5
_PRIME_INTERVAL = 0.3


def _now_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _mb(n) -> float:
    try:
        return round(float(n) / (1024 * 1024), 1)
    except Exception:
        return 0.0


def has_battery() -> bool:
    """Cheap check used by the UI to decide whether to show the button."""
    try:
        import psutil
        b = psutil.sensors_battery()
        return b is not None
    except Exception:
        return False


def _wear_pct() -> int | None:
    """WMI design vs full-charge capacity → wear %. None if unavailable.

    Always goes through run_hidden to guarantee CREATE_NO_WINDOW; without
    it we return None rather than fall back to raw subprocess (which would
    risk flashing a console in the --windowed frozen build).
    """
    try:
        import run_hidden as _rh
    except Exception:
        return None
    try:
        ps = (
            "$d=(Get-CimInstance -Namespace root\\WMI -Class BatteryStaticData "
            "-ErrorAction Stop).DesignedCapacity;"
            "$f=(Get-CimInstance -Namespace root\\WMI -Class BatteryFullChargedCapacity "
            "-ErrorAction Stop).FullChargedCapacity;"
            "if($d -and $f){[int](100-($f/$d*100))}"
        )
        r = _rh.run(["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True, timeout=4, encoding="utf-8", errors="replace")
        out = (r.stdout or "").strip()
        if out and out.lstrip("-").isdigit():
            v = int(out)
            if 0 <= v <= 100:
                return v
    except Exception:
        pass
    return None


def snapshot(top_n: int = _TOP_N) -> dict[str, Any]:
    """Collect a point-in-time battery + drain snapshot. ~0.3 s."""
    out: dict[str, Any] = {
        "available":     True,
        "ts":            _now_str(),
        "has_battery":   False,
        "percent":       None,
        "plugged":       None,
        "time_left_min": None,
        "wear_pct":      None,
        "top_drain":     [],
        "highlights":    [],
        "narrated":      None,
    }

    try:
        import psutil
    except Exception:
        out["highlights"] = ["psutil unavailable — cannot read battery state."]
        return out

    b = None
    try:
        b = psutil.sensors_battery()
    except Exception:
        b = None

    if b is None:
        out["has_battery"] = False
        out["highlights"] = ["No battery detected — this looks like a desktop."]
        return out

    out["has_battery"] = True
    out["percent"] = round(float(b.percent), 1)
    out["plugged"] = bool(b.power_plugged)
    secs = getattr(b, "secsleft", None)
    if secs not in (None, psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) and secs > 0 and not out["plugged"]:
        out["time_left_min"] = int(secs // 60)
    out["wear_pct"] = _wear_pct()

    # Top drain by CPU% (proxy for power draw).
    self_pid = os.getpid()
    procs = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            p.cpu_percent(None)
            procs.append(p)
        except Exception:
            pass
    time.sleep(_PRIME_INTERVAL)
    rows = []
    for p in procs:
        try:
            if p.pid == self_pid:
                continue
            cpu = float(p.cpu_percent(None))
            if cpu <= 0:
                continue
            rows.append({
                "pid": p.pid,
                "name": p.info.get("name") or f"pid-{p.pid}",
                "cpu": round(cpu, 1),
                "mem_mb": _mb(p.memory_info().rss),
            })
        except Exception:
            pass
    out["top_drain"] = sorted(rows, key=lambda r: r["cpu"], reverse=True)[:top_n]

    # Highlights (facts; narration is optional and additive).
    hl: list[str] = []
    if out["plugged"]:
        hl.append(f"Plugged in at {out['percent']:.0f}% — charging.")
    else:
        if out["percent"] is not None and out["percent"] <= 20:
            hl.append(f"Battery low at {out['percent']:.0f}%.")
        elif out["percent"] is not None:
            hl.append(f"On battery at {out['percent']:.0f}%.")
        if out["time_left_min"]:
            h = out["time_left_min"] // 60
            m = out["time_left_min"] % 60
            hl.append(f"About {h}h {m}m remaining at the current rate.")
    if out["top_drain"] and not out["plugged"]:
        top = out["top_drain"][0]
        hl.append(f"Biggest drain right now: {top['name']} at {top['cpu']:.0f}% CPU.")
    if out["wear_pct"] is not None and out["wear_pct"] >= 20:
        hl.append(f"Battery wear is {out['wear_pct']}% — capacity is noticeably reduced.")
    if not hl:
        hl.append("Battery looks fine.")
    out["highlights"] = hl[:4]
    return out


def _fmt_proc_line(r: dict) -> str:
    return f"{r['name']} (pid {r['pid']}): {r['cpu']:.0f}% CPU, {r['mem_mb']:.0f} MB RAM"


def _build_context(snap: dict) -> str:
    top = "\n".join(f"  - {_fmt_proc_line(r)}" for r in snap["top_drain"]) or "  - (none)"
    plug = "yes" if snap["plugged"] else "no"
    tl = f"{snap['time_left_min']} min" if snap["time_left_min"] is not None else "unknown"
    wear = f"{snap['wear_pct']}%" if snap["wear_pct"] is not None else "unknown"
    return (
        "LIVE BATTERY SNAPSHOT (do NOT invent beyond these numbers):\n"
        f"- Charge: {snap['percent']}%\n"
        f"- Plugged in: {plug}\n"
        f"- Time remaining: {tl}\n"
        f"- Battery wear: {wear}\n"
        "Top CPU processes (proxy for power draw):\n"
        f"{top}\n"
        "Write 2-3 sentences in plain English coaching the user about their "
        "battery right now. Name the specific drain processes above if the "
        "machine is unplugged. If plugged in, keep it short. Do NOT recommend "
        "shell commands, auto-close, or hardware upgrades. Do NOT invent numbers."
    )


def narrate(snap: dict) -> str | None:
    if not snap.get("has_battery"):
        return None
    try:
        import assistant as _asst
        resp = _asst.ollama_chat("Coach me on my battery right now.",
                                 context=_build_context(snap),
                                 lang="en", history=None)
        return resp or None
    except Exception:
        return None


def coach(narrate_: bool = True, top_n: int = _TOP_N) -> dict[str, Any]:
    snap = snapshot(top_n=top_n)
    if narrate_ and snap.get("has_battery"):
        snap["narrated"] = narrate(snap)
    return snap


def plain_text(snap: dict) -> str:
    lines = [f"Battery coach ({snap['ts']}):"]
    for h in snap.get("highlights", []):
        lines.append(f"- {h}")
    if snap.get("top_drain"):
        lines.append("Top drain:")
        for r in snap["top_drain"]:
            lines.append(f"  {_fmt_proc_line(r)}")
    return "\n".join(lines)
