"""PC Health Agent — Persistent context cache for the Ollama AI agent.

Builds a compact knowledge file that gets injected into every Ollama prompt
so the 3B model knows the app's pages, commands, and current machine state
without needing to rediscover them each query.

Cache file: ../data/agent_context.txt  (plain text, ~400 tokens)
Updated on: app start, scan completion, and manual refresh.
"""
import json
import os
import platform
from pathlib import Path

_CACHE_PATH = Path(__file__).parent.parent / "data" / "agent_context.txt"
_mem_cache: str | None = None   # in-process copy; avoids disk read on every query


# ── Static knowledge ───────────────────────────────────────────────────────

_APP_KNOWLEDGE = """\
[APP] PC Health Agent — local Windows 11 desktop utility. No cloud, no telemetry.
[PAGES] Dashboard(overview), Storage, Monitor(live CPU/RAM/disk/battery/temp), \
Startup(IT-managed vs personal apps), Insights(AI suggestions), \
Organizer(Downloads cleanup), Events(Windows Event Log), \
Troubleshoot(rule-based diagnostics), Assistant(chat), History, Settings
[COMMANDS the assistant understands]
  navigate: "go to storage", "open monitor", "show events"
  scan: "run full scan", "quick scan", "cancel scan"
  health queries: "how is my pc", "how much disk space", "how much RAM", "show issues"
  apps: "open edge", "open vscode", "launch notepad" (whitelist + Start Menu search)
  files: "find resume", "list my desktop", "show downloads"
  system: "show running processes", "what is my ip", "system info", "recent files"
  AI: free-form PC health questions answered by local Ollama (qwen2.5:3b)
[LIMITS] read-only; never deletes/modifies files; no registry/firewall/antivirus changes; \
file search capped at 25 results / 8 s; Ollama must be running for AI answers\
"""


def _get_machine_info() -> str:
    """Collect basic machine facts. Cached after first call (fast path)."""
    lines = []
    try:
        import ctypes, ctypes.wintypes as wt
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.kernel32.GetComputerNameW(buf, ctypes.byref(ctypes.c_ulong(256)))
        lines.append(f"Host:{buf.value}")
    except Exception:
        pass

    lines.append(f"User:{os.environ.get('USERNAME','?')}")

    try:
        import platform
        lines.append(f"OS:{platform.version()[:20]}")
    except Exception:
        pass

    try:
        import psutil
        ram_gb = round(psutil.virtual_memory().total / 1e9)
        lines.append(f"RAM:{ram_gb}GB")
    except Exception:
        # Fallback: read from WMI via PowerShell isn't worth it here
        pass

    onedrive = os.environ.get("OneDrive", "")
    if onedrive:
        lines.append(f"OneDrive:{os.path.basename(onedrive)}")

    return "  ".join(lines)


def build_cache(scan_data: dict | None = None) -> str:
    """Build the full context string and write it to disk. Returns the string."""
    global _mem_cache

    parts = [_APP_KNOWLEDGE]

    machine = _get_machine_info()
    if machine:
        parts.append(f"[MACHINE] {machine}")

    if scan_data:
        try:
            score   = scan_data.get("health_score", "?")
            disk_gb = scan_data.get("disk_free_gb", "?")
            ram_pct = scan_data.get("ram_used_pct", "?")
            issues  = scan_data.get("issues", 0)
            tmp_mb  = scan_data.get("temp_size_mb", 0)
            parts.append(
                f"[LAST SCAN] score:{score}/100  disk_free:{disk_gb}GB  "
                f"RAM_used:{ram_pct}%  issues:{issues}  temp_files:{tmp_mb}MB"
            )
        except Exception:
            pass

    text = "\n".join(parts)

    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(text, encoding="utf-8")
    except Exception:
        pass

    _mem_cache = text
    return text


def load_cache() -> str:
    """Return the cached context string. Reads from disk once, then uses memory."""
    global _mem_cache
    if _mem_cache:
        return _mem_cache
    try:
        if _CACHE_PATH.exists():
            _mem_cache = _CACHE_PATH.read_text(encoding="utf-8")
            return _mem_cache
    except Exception:
        pass
    # No file yet — build a basic one now
    return build_cache()


def invalidate():
    """Force next load_cache() to re-read from disk (used after scan completes)."""
    global _mem_cache
    _mem_cache = None
