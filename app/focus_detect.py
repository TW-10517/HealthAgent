"""Helix — foreground / load helpers for background daemons.

Keep Helix out of the user's way:
  * is_fullscreen_foreground() → True when a window is covering the whole
    primary monitor (gaming, presentation, full-screen video). Daemons that
    fire Windows toasts must skip when this is True, since toasts steal
    focus and interrupt fullscreen apps.
  * is_cpu_busy() → True when overall CPU has stayed high recently. Non-
    urgent daemon work (narration prompts, heavy-settle probes) backs off
    so Helix isn't competing with whatever the user is doing.

All helpers degrade gracefully — any failure returns the "safe to run" value
so Helix keeps working even if Win32 is weird.
"""
from __future__ import annotations
import time
import ctypes
from ctypes import wintypes

_user32 = None
try:
    _user32 = ctypes.windll.user32
except Exception:
    _user32 = None


def is_fullscreen_foreground() -> bool:
    """True when the foreground window fills the whole monitor it's on.

    Covers games, PowerPoint slide-show, Zoom share-fullscreen, and video
    players in true fullscreen. Returns False on any error so toasts still
    fire if detection fails.
    """
    if _user32 is None:
        return False
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return False
        rect = wintypes.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        screen_w = _user32.GetSystemMetrics(0)   # SM_CXSCREEN
        screen_h = _user32.GetSystemMetrics(1)   # SM_CYSCREEN
        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top
        # Allow a tiny slack for borderless-fullscreen windows that are 1-2 px
        # smaller than the monitor. Taskbar visibility doesn't matter — most
        # fullscreen games hide it anyway.
        return (win_w >= screen_w - 2 and win_h >= screen_h - 2
                and rect.left <= 0 and rect.top <= 0)
    except Exception:
        return False


_CPU_HIGH_PCT = 80.0
_CPU_HIGH_WINDOW_S = 10.0
_cpu_hits: list[float] = []


def is_cpu_busy() -> bool:
    """True when overall CPU has crossed 80% within the last ~10 s.

    Keeps a tiny rolling log of hits and treats "two hits in 10 s" as busy.
    Each call does one psutil.cpu_percent(interval=None) — cheap, non-
    blocking, uses the per-call delta.
    """
    try:
        import psutil
        pct = float(psutil.cpu_percent(interval=None))
    except Exception:
        return False
    now = time.time()
    _cpu_hits[:] = [t for t in _cpu_hits if now - t <= _CPU_HIGH_WINDOW_S]
    if pct >= _CPU_HIGH_PCT:
        _cpu_hits.append(now)
    return len(_cpu_hits) >= 2
