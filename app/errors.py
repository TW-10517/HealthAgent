"""Helix — narrow failure logging helper.

Replaces the pattern:

    try:
        risky()
    except Exception:
        pass

with:

    try:
        risky()
    except Exception as e:
        quiet(e, "scanner.probe_disk")

Writes one line per swallowed error to `logs/silent.log` (bounded at
~1 MiB, rotated by a single rename). This is a pure logger — it never
re-raises, never returns a value, and is safe to call from any thread.

Keep bare `except Exception: pass` ONLY for tight hot loops where a log
write would dominate the cost (e.g. per-process iteration inside a
sampler). For every other case use `quiet()` so an invisible failure
leaves a trace for next time.
"""
from __future__ import annotations
import os
import sys
import threading
import datetime as _dt

_LOCK = threading.Lock()
_MAX_BYTES = 1 * 1024 * 1024    # 1 MiB cap before rotate
_PATH: str | None = None


def _log_path() -> str:
    global _PATH
    if _PATH is not None:
        return _PATH
    if getattr(sys, "frozen", False):
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "PCHealthAgent")
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(base, "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception:
        log_dir = os.environ.get("TEMP", ".")
    _PATH = os.path.join(log_dir, "silent.log")
    return _PATH


def _rotate_if_needed(path: str) -> None:
    try:
        if os.path.getsize(path) > _MAX_BYTES:
            prev = path + ".1"
            try:
                if os.path.exists(prev):
                    os.remove(prev)
            except Exception:
                return
            os.replace(path, prev)
    except Exception:
        pass


def quiet(err: BaseException, where: str = "") -> None:
    """Log one swallowed exception to logs/silent.log. Never raises."""
    try:
        path = _log_path()
        with _LOCK:
            _rotate_if_needed(path)
            with open(path, "a", encoding="utf-8") as f:
                ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                kind = type(err).__name__
                msg = str(err).replace("\n", " ")[:300]
                f.write(f"{ts} | {where or '?'} | {kind}: {msg}\n")
    except Exception:
        # Logger failure is itself swallowed — never crash the caller.
        pass
