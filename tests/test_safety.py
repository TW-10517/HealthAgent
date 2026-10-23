"""Safety tests: protected paths, blocked extensions, injection strings."""
import os
import tempfile
import pytest


def test_protected_paths_match_windows_root():
    from scanner import is_protected
    assert is_protected(r"C:\Windows\System32\drivers\etc\hosts")
    assert is_protected(r"C:\Program Files\x\y.exe")
    assert is_protected(r"C:\Program Files (x86)\x\y.exe")


def test_protected_paths_do_not_match_prefix_lookalike():
    from scanner import is_protected
    # Regression for Step 4: realpath + normcase must prevent prefix bleed
    assert not is_protected(r"C:\Windows2\foo.txt")
    assert not is_protected(r"C:\Program FilesX\foo.txt")


def test_protected_paths_case_insensitive():
    from scanner import is_protected
    assert is_protected(r"c:\windows\system32\x")
    assert is_protected(r"C:\WINDOWS\system32\x")


def test_user_space_not_protected(tmp_path):
    from scanner import is_protected
    f = tmp_path / "foo.txt"
    f.write_text("hi")
    assert not is_protected(str(f))


def test_critical_processes_in_live_set():
    import protected
    live = protected.get_protected_keywords()
    for p in ("lsass", "csrss", "winlogon", "smss", "services", "svchost"):
        assert p in live, f"{p} must always be blocked"


def test_shipping_defaults_toggle_off(monkeypatch):
    import db, protected
    monkeypatch.setattr(db, "get_setting",
                        lambda k: "false" if k == "protected_apps_defaults" else "")
    live = protected.get_protected_keywords()
    assert "eset" not in live  # shipping default
    assert "lsass" in live     # critical floor stays


def test_user_extras_merged(monkeypatch):
    import db, protected
    def _get(k):
        if k == "protected_apps_extra": return "sophos,crowdstrike"
        if k == "protected_apps_defaults": return "true"
        return ""
    monkeypatch.setattr(db, "get_setting", _get)
    live = protected.get_protected_keywords()
    assert "sophos" in live
    assert "crowdstrike" in live


# ── Injection-string guard ──────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "notepad & calc",
    "notepad | del *.*",
    'notepad" & calc',
    "notepad; shutdown /s",
    "notepad\n\rcalc",
    "notepad`calc`",
])
def test_resolve_app_rejects_injection_in_whitelist_mismatch(bad):
    """resolve_app partial match requires >=4 chars AND whitelist membership.
    Injection metacharacters must not pass through to the launcher."""
    from assistant import resolve_app
    label, args = resolve_app(bad.lower())
    if args:
        # If resolved, each arg must be a plain path/name — no shell metachars
        for a in args:
            assert not any(c in a for c in '&|;`\n\r"'), f"leaked metachar: {a}"


# ── open_path extension gate (Step 2) ───────────────────────────────────────

@pytest.mark.parametrize("ext", [
    ".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".wsf",
    ".jar", ".msi", ".scr", ".pif", ".hta",
])
def test_blocked_extensions_are_outside_allowlist(ext):
    """These extensions must not be in the open_path allowlist."""
    # The _ALLOWED_EXTS is attached to AgentAPI; re-create it the same way.
    import webview, pystray  # noqa — may be stubbed in CI
    # Load the frozenset by reading desktop_app's AgentAPI class attr
    import importlib, desktop_app
    importlib.reload(desktop_app) if False else None
    allowed = desktop_app.AgentAPI._ALLOWED_EXTS
    assert ext not in allowed, f"{ext} should NOT be in allowlist"


# ── Blocked keyword in recycle-bin delete guard ─────────────────────────────

def test_delete_guard_blocks_it_keywords():
    import protected
    live = protected.get_protected_keywords()
    # Simulate a basename check
    for name in ("egui.exe", "ekrn.exe", "onedrive.exe", "msedge.exe"):
        low = name.lower()
        assert any(k in low for k in live), f"{name} should be blocked"
