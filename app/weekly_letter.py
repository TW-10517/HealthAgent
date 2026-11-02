"""Helix — Weekly health letter.

Once-per-calendar-week narrative recap of what changed over the last 7 days.
Gated by `weekly_letter_last` + the `weekly_letter_enabled` toggle so it
only fires at most once between app launches in a given week.

Shape of `generate_letter()`:
    {
      "available":       bool,
      "reason":          str | None,     # when not available
      "week_start":      "YYYY-MM-DD",
      "week_end":        "YYYY-MM-DD",
      "scans_done":      int,
      "score_first":     int | None,
      "score_last":      int | None,
      "score_delta":     int | None,
      "disk_delta_gb":   float | None,
      "actions_count":   int,
      "actions_by_cat":  {category: count, ...},
      "events_errors":   int,
      "events_crits":    int,
      "top_sources":     [str, ...],
      "highlights":      [str, ...],     # 3-5 plain-English bullets
      "narrated":        str | None,     # optional Ollama summary
    }

Pure read of local SQLite tables (scans + action_log) + a Windows event
pull via event_analyzer. Narration is optional (ai_enabled gate inside).
"""
from __future__ import annotations
import datetime as _dt

import db as _db


_WEEK_DAYS = 7
_LAST_KEY = "weekly_letter_last"


def _parse_data(scan: dict) -> dict | None:
    sid = scan.get("id")
    if not sid:
        return None
    try:
        return _db.get_scan_data(sid)
    except Exception:
        return None


def _disk_free(data: dict | None) -> float | None:
    if not data:
        return None
    disk = data.get("disk")
    if isinstance(disk, dict):
        for k in ("free_gb", "freeGB", "free"):
            v = disk.get(k)
            if isinstance(v, (int, float)):
                return float(v)
    return None


def _bullets(info: dict) -> list[str]:
    out = []
    sd = info.get("score_delta")
    if sd is not None:
        if sd > 2:
            out.append(f"Health score climbed {sd} points this week "
                       f"({info['score_first']} → {info['score_last']}).")
        elif sd < -2:
            out.append(f"Health score dropped {abs(sd)} points this week "
                       f"({info['score_first']} → {info['score_last']}).")
        else:
            out.append(f"Health score steady around {info['score_last']}.")
    dd = info.get("disk_delta_gb")
    if dd is not None:
        if dd < -2:
            out.append(f"Disk C: lost {abs(dd):.1f} GB of free space over the week.")
        elif dd > 2:
            out.append(f"Disk C: gained {dd:.1f} GB of free space over the week.")
    ac = info.get("actions_count") or 0
    if ac:
        cats = info.get("actions_by_cat") or {}
        top = sorted(cats.items(), key=lambda x: -x[1])[:2]
        parts = ", ".join(f"{c} ({n})" for c, n in top)
        out.append(f"{ac} action{'s' if ac != 1 else ''} logged this week — mostly {parts}.")
    ec = info.get("events_crits") or 0
    ee = info.get("events_errors") or 0
    if ec:
        out.append(f"{ec} critical event{'s' if ec != 1 else ''} "
                   f"({ee} total errors) — check Problems → Event log.")
    elif ee:
        out.append(f"{ee} error event{'s' if ee != 1 else ''} this week — "
                   f"not critical, but worth a glance.")
    if info.get("scans_done", 0) == 0:
        out.append("No scans ran this week — consider enabling scheduled scans.")
    return out[:5]


def _collect_events(hours: int) -> dict:
    try:
        import event_analyzer as ev
        items = ev.get_events(hours=hours, max_level=3) or []
    except Exception:
        return {"errors": 0, "crits": 0, "top_sources": []}
    errors = 0
    crits = 0
    sources = {}
    for it in items:
        # Windows event levels: 1=Critical, 2=Error, 3=Warning. Some sources
        # may hand back a string instead of an int.
        lvl = it.get("level")
        if isinstance(lvl, (int, float)):
            if int(lvl) == 1: crits += 1
            if int(lvl) in (1, 2): errors += 1
        else:
            lvl_s = (str(lvl or "")).lower()
            if lvl_s.startswith("crit"):
                crits += 1
            if lvl_s.startswith(("err", "crit")):
                errors += 1
        src = (it.get("source") or "?").strip()
        if src:
            sources[src] = sources.get(src, 0) + 1
    top = [k for k, _ in sorted(sources.items(), key=lambda x: -x[1])[:5]]
    return {"errors": errors, "crits": crits, "top_sources": top}


def _collect_actions(start_iso: str) -> dict:
    try:
        rows = _db.list_action_log(limit=500, category=None) or []
    except Exception:
        return {"count": 0, "by_cat": {}}
    count = 0
    by_cat: dict[str, int] = {}
    for r in rows:
        if (r.get("ts") or "") < start_iso:
            continue
        count += 1
        cat = r.get("category") or "?"
        by_cat[cat] = by_cat.get(cat, 0) + 1
    return {"count": count, "by_cat": by_cat}


def mark_shown() -> None:
    _db.set_setting(_LAST_KEY, _dt.datetime.now().strftime("%Y-%m-%d"))


def _due_this_week() -> tuple[bool, str]:
    last = _db.get_setting(_LAST_KEY) or ""
    today = _dt.date.today()
    if not last:
        return True, ""
    try:
        last_d = _dt.datetime.strptime(last, "%Y-%m-%d").date()
    except Exception:
        return True, ""
    delta = (today - last_d).days
    if delta >= _WEEK_DAYS:
        return True, ""
    return False, f"Already shown this week (last: {last}, {_WEEK_DAYS - delta}d to go)."


def generate_letter(narrate: bool = True) -> dict:
    due, reason = _due_this_week()
    today = _dt.date.today()
    week_start = today - _dt.timedelta(days=_WEEK_DAYS)
    week_end = today
    out: dict = {
        "available": False,
        "reason": None,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
    }
    if not due:
        out["reason"] = reason
        return out
    # Pull last 20 scans; narrow to this week.
    try:
        scans = _db.list_scans(limit=20) or []
    except Exception:
        scans = []
    week_start_iso = f"{week_start.isoformat()} 00:00:00"
    week_scans = [s for s in scans if (s.get("start_time") or "") >= week_start_iso]
    score_first = score_last = disk_first = disk_last = None
    if week_scans:
        week_scans_sorted = sorted(week_scans, key=lambda s: s.get("start_time") or "")
        first = week_scans_sorted[0]
        last = week_scans_sorted[-1]
        score_first = first.get("score")
        score_last = last.get("score")
        disk_first = _disk_free(_parse_data(first))
        disk_last = _disk_free(_parse_data(last))
    score_delta = None
    if isinstance(score_first, int) and isinstance(score_last, int):
        score_delta = score_last - score_first
    disk_delta = None
    if disk_first is not None and disk_last is not None:
        disk_delta = round(disk_last - disk_first, 1)
    act = _collect_actions(week_start_iso)
    evt = _collect_events(hours=_WEEK_DAYS * 24)
    info = {
        "scans_done":    len(week_scans),
        "score_first":   score_first,
        "score_last":    score_last,
        "score_delta":   score_delta,
        "disk_delta_gb": disk_delta,
        "actions_count": act["count"],
        "actions_by_cat": act["by_cat"],
        "events_errors": evt["errors"],
        "events_crits":  evt["crits"],
        "top_sources":   evt["top_sources"],
    }
    info["highlights"] = _bullets(info)
    info["available"] = True
    info["week_start"] = week_start.isoformat()
    info["week_end"] = week_end.isoformat()
    info["narrated"] = None
    if narrate and _db.get_setting("ai_enabled") != "false":
        try:
            import assistant as _asst
            prompt = _build_narration_prompt(info)
            nar = _asst.ollama_chat("Give me this week's PC-health summary",
                                    context=prompt, lang="en", history=None)
            if nar:
                info["narrated"] = nar.strip()
        except Exception:
            pass
    return info


def _build_narration_prompt(info: dict) -> str:
    lines = [
        "Summarise this past week's PC-health deltas:",
        f"- scans run: {info.get('scans_done', 0)}",
    ]
    sd = info.get("score_delta")
    if sd is not None:
        lines.append(f"- health score: {info['score_first']} → {info['score_last']} ({sd:+d})")
    dd = info.get("disk_delta_gb")
    if dd is not None:
        lines.append(f"- disk C: free changed by {dd:+.1f} GB")
    ac = info.get("actions_count") or 0
    if ac:
        cats = info.get("actions_by_cat") or {}
        top = ", ".join(f"{c}:{n}" for c, n in sorted(cats.items(), key=lambda x: -x[1])[:3])
        lines.append(f"- actions logged: {ac} ({top})")
    if info.get("events_crits"):
        lines.append(f"- critical events: {info['events_crits']}")
    if info.get("events_errors"):
        lines.append(f"- total error events: {info['events_errors']}")
    if info.get("top_sources"):
        lines.append(f"- top event sources: {', '.join(info['top_sources'][:3])}")
    lines.append("")
    lines.append("Write a 2-3 sentence weekly summary. No invented numbers.")
    return "\n".join(lines)
