"""Helix — Proactive morning briefing.

Once-per-calendar-day summary of overnight PC state deltas. Gated by the
`last_briefing_date` setting and the `briefing_enabled` toggle so it only
fires at most once between app launches on a given day.

Shape of `generate_briefing()`:
    {
      "available": True,
      "score_now":        int | None,
      "score_prev":       int | None,
      "score_delta":      int | None,
      "issues_now":       int,
      "issues_prev":      int,
      "disk_free_gb":     float,
      "disk_free_prev":   float | None,
      "overnight_hours":  int,
      "overnight_errors": int,        # count of Error/Critical events in window
      "overnight_crits":  int,
      "top_sources":      [str, …],   # most frequent event sources
      "highlights":       [str, …],   # 2-4 plain-English bullets
      "narrated":         str | None, # Ollama 2-3 sentence summary
      "date":             "YYYY-MM-DD",
    }
"""
from __future__ import annotations
import json
import datetime as _dt
from typing import Any

import db as _db


_OVERNIGHT_HOURS_DEFAULT = 12


def _parse_data(scan: dict) -> dict | None:
    raw = scan.get("data_json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _disk_free_from_scan(data: dict | None) -> float | None:
    """Best-effort extraction of free disk GB from scan payload."""
    if not data:
        return None
    for key in ("disk_free_gb", "free_gb", "disk_free"):
        if key in data:
            try:
                return float(data[key])
            except Exception:
                pass
    du = data.get("disk_usage") or data.get("disk")
    if isinstance(du, dict):
        for key in ("free_gb", "free"):
            if key in du:
                try:
                    v = float(du[key])
                    return v / (1024**3) if v > 1e6 else v
                except Exception:
                    pass
    return None


def _psutil_disk_free_gb() -> float | None:
    try:
        import psutil
        d = psutil.disk_usage(_dt.datetime.now().strftime("C:\\"))
        return round(d.free / (1024**3), 1)
    except Exception:
        return None


def _today() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d")


def should_show_today(force: bool = False) -> bool:
    if force:
        return True
    if (_db.get_setting("briefing_enabled") or "true").lower() != "true":
        return False
    last = _db.get_setting("last_briefing_date") or ""
    return last != _today()


def mark_shown() -> None:
    _db.set_setting("last_briefing_date", _today())


def generate_briefing(overnight_hours: int = _OVERNIGHT_HOURS_DEFAULT,
                      *, narrate: bool = True) -> dict[str, Any]:
    """Build the briefing payload. Caller decides whether to actually show it
    (see `should_show_today`)."""
    out: dict[str, Any] = {
        "available":        True,
        "date":             _today(),
        "overnight_hours":  overnight_hours,
        "score_now":        None,
        "score_prev":       None,
        "score_delta":      None,
        "issues_now":       0,
        "issues_prev":      0,
        "disk_free_gb":     None,
        "disk_free_prev":   None,
        "overnight_errors": 0,
        "overnight_crits":  0,
        "top_sources":      [],
        "highlights":       [],
        "narrated":         None,
    }

    # Scan-score trend: last two completed scans.
    try:
        scans = _db.list_scans(limit=10, status="completed")
    except Exception:
        scans = []
    if scans:
        latest = scans[0]
        out["score_now"] = latest.get("score")
        out["issues_now"] = latest.get("issues") or 0
        out["disk_free_gb"] = _disk_free_from_scan(_db.get_scan_data(latest["id"]))
    if len(scans) >= 2:
        prev = scans[1]
        out["score_prev"] = prev.get("score")
        out["issues_prev"] = prev.get("issues") or 0
        out["disk_free_prev"] = _disk_free_from_scan(_db.get_scan_data(prev["id"]))
    if out["disk_free_gb"] is None:
        out["disk_free_gb"] = _psutil_disk_free_gb()
    if out["score_now"] is not None and out["score_prev"] is not None:
        out["score_delta"] = int(out["score_now"] - out["score_prev"])

    # Overnight Windows events — Critical + Error only (level 1/2).
    try:
        import event_analyzer as _ea
        evs = _ea.get_events(hours=overnight_hours, max_level=2, max_results=100)
    except Exception:
        evs = []
    out["overnight_crits"] = sum(1 for e in evs if e.get("level") == 1)
    out["overnight_errors"] = sum(1 for e in evs if e.get("level") == 2)
    # Top 3 event sources (by frequency).
    from collections import Counter
    freq = Counter(e.get("source", "") for e in evs if e.get("source"))
    out["top_sources"] = [s for s, _ in freq.most_common(3)]

    # Plain-English highlights (facts; narration is optional and additive).
    hl: list[str] = []
    if out["score_delta"] is not None:
        if out["score_delta"] > 0:
            hl.append(f"Health score up {out['score_delta']} points since last scan "
                      f"({out['score_prev']} → {out['score_now']}).")
        elif out["score_delta"] < 0:
            hl.append(f"Health score dropped {-out['score_delta']} points "
                      f"({out['score_prev']} → {out['score_now']}).")
    if out["disk_free_gb"] is not None and out["disk_free_prev"] is not None:
        delta_gb = round(out["disk_free_gb"] - out["disk_free_prev"], 1)
        if delta_gb < -0.5:
            hl.append(f"Disk filled by {-delta_gb} GB since the previous scan.")
        elif delta_gb > 0.5:
            hl.append(f"Disk freed up by {delta_gb} GB since the previous scan.")
    if out["overnight_crits"]:
        hl.append(f"{out['overnight_crits']} critical event(s) in the last "
                  f"{overnight_hours} h — check Problems ▸ Event log.")
    elif out["overnight_errors"] >= 5:
        hl.append(f"{out['overnight_errors']} errors in the last {overnight_hours} h "
                  f"from {', '.join(out['top_sources']) or 'various sources'}.")
    if not hl:
        hl.append("Everything looks quiet overnight — no critical events, no score drop.")
    out["highlights"] = hl[:4]

    # Optional short Ollama narration — strictly additive, 2 sentences max.
    if narrate:
        try:
            import assistant as _asst
            ctx = (
                f"MORNING BRIEFING FACTS (do NOT invent beyond these):\n"
                f"- Health score now: {out['score_now']}  prev: {out['score_prev']}  "
                f"delta: {out['score_delta']}\n"
                f"- Issues now: {out['issues_now']}  prev: {out['issues_prev']}\n"
                f"- Free disk GB now: {out['disk_free_gb']}  prev: {out['disk_free_prev']}\n"
                f"- Overnight critical events: {out['overnight_crits']}\n"
                f"- Overnight error events: {out['overnight_errors']}\n"
                f"- Top event sources: {', '.join(out['top_sources']) or 'none'}\n"
                f"Write a 2-sentence 'good morning' briefing summarizing these "
                f"facts in plain English. If nothing is wrong, say so cheerfully. "
                f"Do NOT invent numbers. Do NOT recommend upgrades unless an issue "
                f"is listed above."
            )
            resp = _asst.ollama_chat("Give me the morning briefing", context=ctx,
                                     lang="en", history=None)
            if resp:
                out["narrated"] = resp
        except Exception:
            pass

    return out


def plain_text(briefing: dict) -> str:
    """Offline-fallback plain-text rendering of the briefing."""
    lines = [f"Good morning. Here's the overnight briefing ({briefing['date']})."]
    for h in briefing.get("highlights", []):
        lines.append(f"• {h}")
    return "\n".join(lines)
