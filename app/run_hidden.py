"""Explicit helper for launching console tools without a window flash.

Helix is built as a --windowed exe (no console of its own), so every
PowerShell / cmd / wmic / powercfg / … spawn would otherwise flash a black
window for ~1 second. This module replaces the previous global
subprocess.Popen monkey-patch with an explicit API that callers import.

Only adds CREATE_NO_WINDOW when the target exe is in _CONSOLE_EXES — never
for GUI apps (edge, chrome, taskmgr, …) and never for pywebview's WebView2
helper spawns.

Usage:
    from run_hidden import run, popen
    run(["powershell", "-NoProfile", "-Command", "..."], capture_output=True)
    popen(["cmd", "/c", "something"])

For launching a user-facing GUI app (edge, taskmgr, calc, …), keep using
subprocess directly — this helper is only for console tools.
"""
import os
import subprocess

_CREATE_NO_WINDOW = 0x08000000

_CONSOLE_EXES = frozenset((
    "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "cmd", "cmd.exe", "powercfg", "powercfg.exe",
    "wmic", "wmic.exe", "netsh", "netsh.exe",
    "tasklist", "tasklist.exe", "taskkill", "taskkill.exe",
    "reg", "reg.exe", "sc", "sc.exe",
    "ipconfig", "ipconfig.exe", "systeminfo", "systeminfo.exe",
))


def _exe_basename(args) -> str:
    if isinstance(args, (list, tuple)) and args:
        return os.path.basename(str(args[0])).lower()
    if isinstance(args, str) and args:
        first = args.split(None, 1)[0]
        return os.path.basename(first).lower()
    return ""


def _maybe_hide(args, kwargs):
    """Inject CREATE_NO_WINDOW into kwargs if the target is a console tool."""
    if os.name != "nt":
        return kwargs
    if kwargs.get("creationflags"):
        return kwargs  # caller already set flags; respect them
    if _exe_basename(args) in _CONSOLE_EXES:
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    return kwargs


def run(args, **kwargs):
    """subprocess.run with CREATE_NO_WINDOW auto-applied for console tools."""
    kwargs = _maybe_hide(args, dict(kwargs))
    return subprocess.run(args, **kwargs)


def popen(args, **kwargs):
    """subprocess.Popen with CREATE_NO_WINDOW auto-applied for console tools."""
    kwargs = _maybe_hide(args, dict(kwargs))
    return subprocess.Popen(args, **kwargs)
