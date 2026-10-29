"""Helix — Game / heavy-app launch narrator (Feature #6).

Narrate-only. Watches for new processes starting after Helix is up and fires
a single Windows toast when a known game launches or when a new process
becomes resource-heavy. **Never opens or closes anything** — this is a
hard rule for all new Helix features (per the user, 2026-10-29).

Narration priority: optional Ollama with a 3-second thread timeout; canned
single-line template on timeout, offline, or `ai_enabled=false`.
"""
from __future__ import annotations
import re
import time
import threading
from typing import Any, Callable

_HEAVY_MB_DEFAULT = 1024.0
_HEAVY_CPU_PCT = 25.0
_SETTLE_SECONDS = 10
_POLL_INTERVAL = 5
_OLLAMA_TIMEOUT_S = 3
_STARTUP_SETTLE = 30
_MAX_NOTIFIED_PER_SESSION = 50  # cap to prevent runaway toast storms


def _norm(s: str) -> str:
    """Lowercase, strip .exe and any non-alphanumeric char."""
    s = (s or "").lower().removesuffix(".exe")
    return re.sub(r"[^a-z0-9]", "", s)


def _build_game_tokens() -> dict[str, str]:
    """{normalized_token: canonical_title} for KNOWN_APPS + aliases.

    Only tokens of length >= 4 are kept, to avoid matching generic exe
    name fragments like 're' or 'ds'. Rebuilt lazily on first call.
    """
    out: dict[str, str] = {}
    try:
        import game_advisor as _ga
        for title in getattr(_ga, "KNOWN_APPS", {}).keys():
            tok = _norm(title)
            if len(tok) >= 4:
                out[tok] = title
        for alias, title in getattr(_ga, "_ALIASES", {}).items():
            tok = _norm(alias)
            if len(tok) >= 4:
                out[tok] = title
    except Exception:
        pass
    return out


_GAME_TOKENS: dict[str, str] | None = None


def match_game(exe_name: str) -> str | None:
    """Return canonical game title if the exe name contains a known game token."""
    global _GAME_TOKENS
    if _GAME_TOKENS is None:
        _GAME_TOKENS = _build_game_tokens()
    norm = _norm(exe_name)
    if len(norm) < 4:
        return None
    for tok, title in _GAME_TOKENS.items():
        if tok in norm:
            return title
    return None


def _snapshot_context() -> dict[str, Any]:
    """Current overall CPU/RAM + the top-RAM background process (via live_triage)."""
    out = {"cpu": 0.0, "ram": 0.0, "top_bg": None}
    try:
        import psutil
        out["cpu"] = float(psutil.cpu_percent(interval=None))
        out["ram"] = float(psutil.virtual_memory().percent)
    except Exception:
        pass
    try:
        import live_triage as _lt
        snap = _lt.snapshot(top_n=3)
        out["top_bg"] = snap["top_ram"][0] if snap.get("top_ram") else None
    except Exception:
        pass
    return out


def canned_message(kind: str, title: str, ctx: dict) -> str:
    bg = ctx.get("top_bg")
    bg_str = ""
    if bg:
        bg_str = f" — biggest background: {bg['name']} ({bg['mem_mb']:.0f} MB)"
    if kind == "game":
        return f"{title} launched. CPU {ctx['cpu']:.0f}%, RAM {ctx['ram']:.0f}%{bg_str}."
    return f"{title} is now using notable resources. CPU {ctx['cpu']:.0f}%, RAM {ctx['ram']:.0f}%{bg_str}."


def _build_ollama_prompt(kind: str, title: str, ctx: dict) -> tuple[str, str]:
    bg = ctx.get("top_bg")
    bg_line = (f"Biggest background RAM user: {bg['name']} ({bg['mem_mb']:.0f} MB)"
               if bg else "No big background RAM user.")
    system = (
        "LIVE FACTS (do NOT invent beyond these):\n"
        f"- App: {title} ({kind})\n"
        f"- Overall CPU: {ctx['cpu']:.0f}%\n"
        f"- Overall RAM: {ctx['ram']:.0f}%\n"
        f"- {bg_line}\n"
        "Write ONE short sentence (max 18 words) suitable for a Windows toast "
        "notification. State the facts only. Do NOT recommend closing anything. "
        "Do NOT suggest shell commands. Do NOT invent numbers."
    )
    user = (f"{title} just launched." if kind == "game"
            else f"{title} just became resource-heavy.")
    return user, system


def _ollama_narrate(kind: str, title: str, ctx: dict,
                     timeout_s: float = _OLLAMA_TIMEOUT_S) -> str | None:
    """Thread-wrapped Ollama call with hard timeout. None on fail or timeout."""
    user, system = _build_ollama_prompt(kind, title, ctx)
    result: dict[str, Any] = {"text": None}

    def _call():
        try:
            import assistant as _asst
            resp = _asst.ollama_chat(user, context=system, lang="en", history=None)
            text = (resp or "").strip()
            if text:
                result["text"] = text
        except Exception:
            pass

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    return result["text"]


def _truncate_toast(msg: str, limit: int = 240) -> str:
    if len(msg) <= limit:
        return msg
    return msg[: limit - 3] + "..."


def build_message(kind: str, title: str, ctx: dict,
                  try_ollama: bool = True) -> str:
    """Public helper: narrates via Ollama (if enabled & fast), else canned."""
    msg = _ollama_narrate(kind, title, ctx) if try_ollama else None
    if not msg:
        msg = canned_message(kind, title, ctx)
    return _truncate_toast(msg)


def start_watcher(notify: Callable[[str, str], None],
                  settings_fn: Callable[[], dict]) -> None:
    """Public entry.

    `notify(title, body)` sends a Windows toast (desktop_app._show_notification).
    `settings_fn()` returns the current db settings dict.

    Spawns a daemon thread that:
      1. baselines PIDs already running at Helix launch (they never fire),
      2. polls every 5 s for new PIDs,
      3. for a new PID whose name matches a known game → toasts immediately,
      4. otherwise waits SETTLE_SECONDS, then checks heavy-app thresholds,
      5. dedupes by lowercase exe name for the whole session.
    """
    _notified: set[str] = set()
    _new_pids: dict[int, float] = {}  # pid → first_seen_ts

    def _loop():
        try:
            import psutil
        except Exception:
            return
        try:
            baseline = {p.pid for p in psutil.process_iter(["pid"])}
        except Exception:
            return
        time.sleep(_STARTUP_SETTLE)

        try:
            import focus_detect as _fd
        except Exception:
            _fd = None

        while True:
            time.sleep(_POLL_INTERVAL)
            try:
                settings = settings_fn()
                if (settings.get("app_narrator_enabled") or "true").lower() != "true":
                    continue
                # Non-intrusive: skip toasts while the user is in fullscreen
                # (gaming, slideshow, video). We still refresh baseline so we
                # don't fire a toast storm when they alt-tab back.
                if _fd and _fd.is_fullscreen_foreground():
                    try:
                        baseline.update({p.pid for p in psutil.process_iter(["pid"])})
                    except Exception:
                        pass
                    continue
                try:
                    heavy_mb = float(settings.get("app_narrator_heavy_mb") or _HEAVY_MB_DEFAULT)
                except Exception:
                    heavy_mb = _HEAVY_MB_DEFAULT

                now = time.time()
                live_pids = set()
                for p in psutil.process_iter(["pid", "name"]):
                    try:
                        pid = p.pid
                        live_pids.add(pid)
                        if pid in baseline:
                            continue
                        name = p.info.get("name") or ""
                        if not name:
                            continue
                        key = name.lower()
                        if key in _notified:
                            baseline.add(pid)
                            continue
                        if len(_notified) >= _MAX_NOTIFIED_PER_SESSION:
                            continue

                        # Game-start → immediate narration.
                        game = match_game(name)
                        if game:
                            _notified.add(key)
                            baseline.add(pid)
                            _fire(notify, "game", game)
                            continue

                        # Heavy-app → wait SETTLE_SECONDS before measuring.
                        born = _new_pids.get(pid)
                        if born is None:
                            _new_pids[pid] = now
                            continue
                        if now - born < _SETTLE_SECONDS:
                            continue
                        # Skip the heavy-settle probe while the system is
                        # already under CPU load — we're not going to add
                        # a cpu_percent sweep on top of the user's workload.
                        if _fd and _fd.is_cpu_busy():
                            continue
                        try:
                            mem_mb = p.memory_info().rss / (1024 * 1024)
                        except Exception:
                            continue
                        try:
                            cpu_pct = p.cpu_percent(None)
                        except Exception:
                            cpu_pct = 0.0
                        if mem_mb >= heavy_mb or cpu_pct >= _HEAVY_CPU_PCT:
                            display = name.removesuffix(".exe").removesuffix(".EXE")
                            _notified.add(key)
                            baseline.add(pid)
                            _new_pids.pop(pid, None)
                            _fire(notify, "heavy", display)
                    except Exception:
                        pass

                # GC entries for dead pids.
                for dead in [pid for pid in _new_pids if pid not in live_pids]:
                    _new_pids.pop(dead, None)
            except Exception:
                pass

    def _fire(notify_fn: Callable[[str, str], None], kind: str, title: str):
        try:
            ctx = _snapshot_context()
            msg = build_message(kind, title, ctx, try_ollama=True)
            toast_title = "Helix — Game launched" if kind == "game" else "Helix — Heavy app"
            notify_fn(toast_title, msg)
        except Exception:
            pass

    threading.Thread(target=_loop, daemon=True).start()
