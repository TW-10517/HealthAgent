"""Helix — "Why is my PC slow right now?" live triage.

On-demand snapshot of what's eating resources this instant. Pure read-only:
top CPU processes, top RAM processes, overall CPU/RAM, disk busy %,
network throughput. Optionally asks Ollama for a short plain-English
narration grounded in the exact numbers.

Shape of `triage_live()`:
    {
      "available":     True,
      "ts":            "YYYY-MM-DD HH:MM:SS",
      "cpu_overall":   float,  # 0..100
      "ram_overall":   float,  # 0..100
      "disk_busy_pct": float | None,
      "net_mbps":      float,
      "top_cpu":       [{pid,name,cpu,mem_mb}, ...],  # up to 5
      "top_ram":       [{pid,name,cpu,mem_mb}, ...],  # up to 5
      "highlights":    [str, ...],
      "narrated":      str | None,
    }

Narrative-only. No action list. Users act via existing Boost / close_app.
"""
from __future__ import annotations
import os
import time
import datetime as _dt
from typing import Any

_TOP_N = 5
_PRIME_INTERVAL = 0.3  # seconds between the two psutil cpu_percent calls


def _now_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _mb(n: int | float) -> float:
    try:
        return round(float(n) / (1024 * 1024), 1)
    except Exception:
        return 0.0


def snapshot(top_n: int = _TOP_N) -> dict[str, Any]:
    """Collect a point-in-time resource snapshot.

    Primes per-process cpu_percent, sleeps briefly, then reads real values.
    Takes ~0.3 s. Caller should run this off the UI thread if that matters.
    """
    import psutil
    out: dict[str, Any] = {
        "available":     True,
        "ts":            _now_str(),
        "cpu_overall":   0.0,
        "ram_overall":   0.0,
        "disk_busy_pct": None,
        "net_mbps":      0.0,
        "top_cpu":       [],
        "top_ram":       [],
        "highlights":    [],
        "narrated":      None,
    }

    self_pid = os.getpid()

    # Overall CPU + RAM.
    try:
        out["cpu_overall"] = float(psutil.cpu_percent(interval=None))
    except Exception:
        pass
    try:
        out["ram_overall"] = float(psutil.virtual_memory().percent)
    except Exception:
        pass

    # Prime per-process CPU meters. First call always returns 0.
    procs: list = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            p.cpu_percent(None)
            procs.append(p)
        except Exception:
            pass

    # Disk + net sample #1.
    try:
        d1 = psutil.disk_io_counters()
        n1 = psutil.net_io_counters()
    except Exception:
        d1 = n1 = None
    t1 = time.perf_counter()

    time.sleep(_PRIME_INTERVAL)

    # Sample #2 → deltas.
    try:
        d2 = psutil.disk_io_counters()
        n2 = psutil.net_io_counters()
    except Exception:
        d2 = n2 = None
    dt = max(time.perf_counter() - t1, 0.05)

    if d1 and d2:
        busy_ms = (d2.busy_time - d1.busy_time) if hasattr(d2, "busy_time") else None
        if busy_ms is not None:
            out["disk_busy_pct"] = round(min(100.0, (busy_ms / (dt * 1000.0)) * 100.0), 1)
    if n1 and n2:
        bytes_delta = (n2.bytes_sent - n1.bytes_sent) + (n2.bytes_recv - n1.bytes_recv)
        out["net_mbps"] = round((bytes_delta * 8) / (dt * 1_000_000), 2)

    # Second CPU read + collect rows.
    rows: list[dict] = []
    for p in procs:
        try:
            if p.pid == self_pid:
                continue
            cpu = float(p.cpu_percent(None))
            mem_mb = _mb(p.memory_info().rss)
            name = p.info.get("name") or f"pid-{p.pid}"
            rows.append({"pid": p.pid, "name": name, "cpu": round(cpu, 1), "mem_mb": mem_mb})
        except Exception:
            pass

    # Top-N by CPU and by RAM, independently.
    out["top_cpu"] = sorted(rows, key=lambda r: r["cpu"], reverse=True)[:top_n]
    out["top_ram"] = sorted(rows, key=lambda r: r["mem_mb"], reverse=True)[:top_n]

    # Plain-English highlights (facts; narration is optional and additive).
    hl: list[str] = []
    if out["cpu_overall"] >= 85:
        who = out["top_cpu"][0]["name"] if out["top_cpu"] else "something"
        hl.append(f"CPU is at {out['cpu_overall']:.0f}% — {who} is leading the load.")
    elif out["cpu_overall"] >= 60:
        hl.append(f"CPU is busy at {out['cpu_overall']:.0f}%.")
    if out["ram_overall"] >= 90:
        hl.append(f"RAM at {out['ram_overall']:.0f}% — close something or run Boost.")
    elif out["ram_overall"] >= 80:
        hl.append(f"RAM at {out['ram_overall']:.0f}% — getting tight.")
    if out["disk_busy_pct"] is not None and out["disk_busy_pct"] >= 80:
        hl.append(f"Disk is {out['disk_busy_pct']:.0f}% busy — I/O-bound right now.")
    if out["net_mbps"] >= 50:
        hl.append(f"Network pushing {out['net_mbps']:.0f} Mbps.")
    if not hl:
        hl.append("Nothing looks pinned right now — the system is relatively idle.")
    out["highlights"] = hl[:4]

    return out


def _fmt_proc_line(r: dict) -> str:
    return f"{r['name']} (pid {r['pid']}): {r['cpu']:.0f}% CPU, {r['mem_mb']:.0f} MB RAM"


def _build_context(snap: dict) -> str:
    top_cpu = "\n".join(f"  - {_fmt_proc_line(r)}" for r in snap["top_cpu"]) or "  - (none)"
    top_ram = "\n".join(f"  - {_fmt_proc_line(r)}" for r in snap["top_ram"]) or "  - (none)"
    disk = f"{snap['disk_busy_pct']}%" if snap["disk_busy_pct"] is not None else "unknown"
    return (
        "LIVE PC SNAPSHOT (do NOT invent beyond these numbers):\n"
        f"- Overall CPU: {snap['cpu_overall']:.0f}%\n"
        f"- Overall RAM: {snap['ram_overall']:.0f}%\n"
        f"- Disk busy:   {disk}\n"
        f"- Network:     {snap['net_mbps']:.1f} Mbps\n"
        f"Top CPU processes:\n{top_cpu}\n"
        f"Top RAM processes:\n{top_ram}\n"
        "Write 2-3 sentences in plain English explaining what is slowing the PC "
        "right now. Name the specific apps from the lists above. If nothing is "
        "pinned, say the system looks idle. Do NOT recommend auto-closing or "
        "give shell commands. Do NOT invent numbers or process names."
    )


def narrate(snap: dict) -> str | None:
    """Optional 2-3 sentence Ollama narration. Returns None if unavailable."""
    try:
        import assistant as _asst
        resp = _asst.ollama_chat("Why is my PC slow right now?",
                                 context=_build_context(snap),
                                 lang="en", history=None)
        return resp or None
    except Exception:
        return None


def triage_live(narrate_: bool = True, top_n: int = _TOP_N) -> dict[str, Any]:
    """Public entry: snapshot + optional narration."""
    snap = snapshot(top_n=top_n)
    if narrate_:
        snap["narrated"] = narrate(snap)
    return snap


def plain_text(snap: dict) -> str:
    """Offline fallback rendering."""
    lines = [f"Live triage ({snap['ts']}):"]
    for h in snap.get("highlights", []):
        lines.append(f"- {h}")
    if snap.get("top_cpu"):
        lines.append("Top CPU:")
        for r in snap["top_cpu"]:
            lines.append(f"  {_fmt_proc_line(r)}")
    return "\n".join(lines)
