"""Helix — Error triage.

User pastes a crash message, stack trace, Blue Screen code, or any
system-looking error. Helix:
  1. detects the paste as an error (not a regular question)
  2. extracts a timestamp hint if there is one
  3. pulls Windows event-log entries from a ±5 min window around that time
     (or the last 2 h if no timestamp was found)
  4. hands the paste + event context to Ollama for a short, human
     explanation of what probably happened.

The module never executes anything — it only reads events and asks the
local model. All inference stays on-device per the project's rules.
"""
from __future__ import annotations
import re
import datetime as _dt
from typing import Any

# ── Detection ──────────────────────────────────────────────────────────────
# Keyword set tuned for Windows (BSODs, DLL faults) + common language stacks.
_ERROR_HINT = re.compile(
    r"(?:"
    r"traceback\b|exception\b|error:|errno|"
    r"blue\s*screen|bsod|stop\s*code|whea[-_ ]uncorrectable|"
    r"kernel[-_ ]?panic|dll\s*(?:fault|crash|not\s*found)|"
    r"access\s*violation|null\s*reference|segfault|segmentation\s*fault|"
    r"0x[0-9a-f]{6,}|"
    r"system\.\w+exception|fatal\s+error|unhandled\s+exception|"
    r"faulting\s+(?:application|module)|"
    r"page\s*fault|driver_power_state|critical_process_died"
    r")", re.IGNORECASE)


def looks_like_error(text: str) -> bool:
    """Heuristic: does this message look like an error paste rather than a
    normal question?

    Rules:
    - At least one of the keyword hints matches, OR
    - Message is multi-line AND contains a 0x hex code or 'at ' stack frame.
    """
    if not text:
        return False
    t = text.strip()
    if _ERROR_HINT.search(t):
        return True
    # Multi-line + looks like a trace (`at module.func` or `File "x", line N`)
    if "\n" in t and (re.search(r"\n\s*at\s+\w", t) or
                      re.search(r'File "[^"]+", line \d+', t)):
        return True
    return False


# ── Timestamp extraction ───────────────────────────────────────────────────

_TS_PATTERNS = [
    # 2026-10-27 23:45:12  (ISO-ish, log standard)
    (re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})\b"),
     "%Y-%m-%d %H:%M:%S"),
    # 10/27/2026 11:45 PM  (Windows event-viewer style, US)
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)",
                re.IGNORECASE), "us"),
    # 27/10/2026 23:45  (European)
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\b"),
     "eu"),
]

def extract_timestamp(text: str) -> _dt.datetime | None:
    """Pull the first plausible timestamp out of the paste. Returns None if
    nothing recognisable is in there (caller falls back to a recency window)."""
    if not text:
        return None
    for pat, fmt in _TS_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        try:
            if fmt == "us":
                month, day, year, hh, mm, ss, ampm = m.groups()
                hh = int(hh)
                if ampm.upper() == "PM" and hh < 12: hh += 12
                if ampm.upper() == "AM" and hh == 12: hh = 0
                return _dt.datetime(int(year), int(month), int(day), hh,
                                    int(mm), int(ss or 0))
            if fmt == "eu":
                day, month, year, hh, mm, ss = m.groups()
                return _dt.datetime(int(year), int(month), int(day), int(hh),
                                    int(mm), int(ss or 0))
            y, mo, d, hh, mm, ss = m.groups()[:6]
            return _dt.datetime(int(y), int(mo), int(d), int(hh),
                                int(mm), int(ss or 0))
        except Exception:
            continue
    return None


# ── Event correlation ──────────────────────────────────────────────────────

def _events_near(ts: _dt.datetime | None, *, window_min: int = 5,
                 fallback_hours: int = 2) -> list[dict]:
    """Return event-log entries near `ts`, or recent errors as fallback."""
    try:
        import event_analyzer as _ea
    except Exception:
        return []
    if ts is None:
        try:
            return _ea.get_events(hours=fallback_hours, max_level=2, max_results=20)
        except Exception:
            return []
    # Pull a slightly wide window and filter to ±window_min around ts.
    try:
        hours = max(1, int(window_min / 60) + 1)
        candidates = _ea.get_events(hours=hours, max_level=3, max_results=50)
    except Exception:
        return []
    hits = []
    low = ts - _dt.timedelta(minutes=window_min)
    high = ts + _dt.timedelta(minutes=window_min)
    for e in candidates:
        try:
            etime = _dt.datetime.strptime(e["timestamp"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if low <= etime <= high:
            hits.append(e)
    return hits[:15]


# ── Public API ─────────────────────────────────────────────────────────────

def triage(text: str, *, narrate: bool = True,
           force: bool = False) -> dict[str, Any]:
    """Return a structured triage result. Caller sends `narrated` to the UI
    on success, or `plain_text()` on Ollama failure.

    When `force=True`, we skip the "does it look like an error paste" check
    and run the correlation + narration anyway — useful for intents like
    "why did my pc crash" where the user is asking Helix to go look.
    """
    text = (text or "").strip()
    detected = looks_like_error(text) or force
    out: dict[str, Any] = {
        "detected":        detected,
        "timestamp":       None,
        "events":          [],
        "event_count":     0,
        "nearest_source":  None,
        "narrated":        None,
    }
    if not detected:
        return out
    ts = extract_timestamp(text)
    out["timestamp"] = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else None
    events = _events_near(ts)
    out["events"] = events[:5]  # keep payload small for UI
    out["event_count"] = len(events)
    if events:
        out["nearest_source"] = events[0].get("source")

    if narrate:
        try:
            import assistant as _asst
        except Exception:
            _asst = None
        if _asst is not None:
            # Compact context — keep well under the 3B model's prompt budget.
            evs_block = "\n".join(
                f"  - [{e.get('timestamp','?')}] {e.get('level_name','?')} "
                f"{e.get('source','?')} id={e.get('event_id','?')}: "
                f"{(e.get('message','') or '')[:160]}"
                for e in events[:6]
            ) or "  (no correlated events found)"
            paste_block = text if len(text) <= 800 else text[:800] + "…"
            ctx = (
                f"USER PASTED THIS ERROR:\n{paste_block}\n\n"
                f"TIMESTAMP HINT: {out['timestamp'] or 'none found'}\n"
                f"CORRELATED WINDOWS EVENTS (±5 min, same window):\n{evs_block}\n\n"
                f"INSTRUCTIONS for your reply (2-3 sentences):\n"
                f"- Say in plain English what probably caused this.\n"
                f"- If events point at a specific driver/app, name it.\n"
                f"- If events are empty, say the OS didn't log anything else and "
                f"treat the paste alone.\n"
                f"- Suggest at most ONE next check (never a fix command).\n"
                f"- Do NOT invent additional event IDs or driver names."
            )
            resp = _asst.ollama_chat("Help me understand this error",
                                     context=ctx, lang="en", history=None)
            if resp:
                out["narrated"] = resp
    return out


def plain_text(result: dict) -> str:
    """Offline fallback — short bullet rendering of the triage result."""
    if not result.get("detected"):
        return "That didn't look like an error paste. Try pasting the full message."
    lines = ["Here's what I can tell from the paste:"]
    if result.get("timestamp"):
        lines.append(f"• Timestamp in paste: {result['timestamp']}")
    n = result.get("event_count", 0)
    if n:
        lines.append(f"• Found {n} Windows event(s) in the same window.")
        if result.get("nearest_source"):
            lines.append(f"• Nearest source: {result['nearest_source']}")
    else:
        lines.append("• Windows didn't log any Critical/Error events in that window.")
    lines.append("• Open Problems ▸ Event log to see the raw entries.")
    return "\n".join(lines)
