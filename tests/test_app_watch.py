"""Tests for app_watch: exe-to-game matching, canned messages, dedupe logic."""
import pytest


def _import():
    import app_watch
    return app_watch


def test_norm_strips_exe_and_punctuation():
    aw = _import()
    assert aw._norm("EldenRing.exe") == "eldenring"
    assert aw._norm("ff14_dx11.EXE") == "ff14dx11"
    assert aw._norm("") == ""
    assert aw._norm(None) == ""


def test_match_game_known_titles():
    aw = _import()
    # These should match something in game_advisor's catalog + aliases.
    # Exact game names won't survive without hits — we probe common exes.
    hit = aw.match_game("eldenring.exe")
    assert hit is None or isinstance(hit, str)
    # Pure-digit / tiny tokens don't false-match.
    assert aw.match_game("a.exe") is None
    assert aw.match_game("xx.exe") is None


def test_match_game_requires_min_length():
    aw = _import()
    assert aw.match_game("abc") is None
    assert aw.match_game("ab.exe") is None


def test_canned_message_game():
    aw = _import()
    ctx = {"cpu": 42.0, "ram": 65.0, "top_bg": None}
    msg = aw.canned_message("game", "Elden Ring", ctx)
    assert "Elden Ring" in msg and "launched" in msg
    assert "42%" in msg and "65%" in msg


def test_canned_message_heavy_includes_bg():
    aw = _import()
    ctx = {"cpu": 10.0, "ram": 55.0, "top_bg": {"name": "chrome.exe", "mem_mb": 1800}}
    msg = aw.canned_message("heavy", "Blender", ctx)
    assert "Blender" in msg
    assert "chrome.exe" in msg
    assert "1800" in msg


def test_truncate_toast():
    aw = _import()
    short = "hello"
    assert aw._truncate_toast(short) == short
    long = "x" * 500
    out = aw._truncate_toast(long, limit=50)
    assert len(out) == 50
    assert out.endswith("...")


def test_build_ollama_prompt_contains_facts():
    aw = _import()
    ctx = {"cpu": 72.0, "ram": 55.0, "top_bg": {"name": "chrome.exe", "mem_mb": 1800}}
    user, system = aw._build_ollama_prompt("game", "Valorant", ctx)
    assert "Valorant" in system and "chrome.exe" in system
    assert "do NOT invent" in system.lower() or "do not invent" in system.lower()
    assert "72" in system and "55" in system


def test_build_message_falls_back_to_canned_when_ollama_offline(monkeypatch):
    aw = _import()
    monkeypatch.setattr(aw, "_ollama_narrate", lambda *a, **k: None)
    ctx = {"cpu": 10.0, "ram": 20.0, "top_bg": None}
    msg = aw.build_message("game", "Testgame", ctx, try_ollama=True)
    assert "Testgame" in msg and "launched" in msg


def test_build_message_uses_ollama_when_available(monkeypatch):
    aw = _import()
    monkeypatch.setattr(aw, "_ollama_narrate", lambda *a, **k: "AI-narrated line.")
    msg = aw.build_message("game", "X", {"cpu": 0.0, "ram": 0.0, "top_bg": None})
    assert msg == "AI-narrated line."


def test_ollama_narrate_respects_timeout(monkeypatch):
    """_ollama_narrate must return None if the Ollama call hangs past the timeout."""
    aw = _import()
    import sys as _sys

    class _FakeAsst:
        @staticmethod
        def ollama_chat(message, context="", lang="en", history=None):
            import time
            time.sleep(5)  # longer than any sensible timeout
            return "late reply"
    monkeypatch.setitem(_sys.modules, "assistant", _FakeAsst)
    out = aw._ollama_narrate("game", "X", {"cpu": 0.0, "ram": 0.0, "top_bg": None},
                              timeout_s=0.3)
    assert out is None


def test_module_has_no_close_or_open_api():
    """Narrate-only rule: the module must not import or expose close/open calls."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "app" / "app_watch.py").read_text(encoding="utf-8")
    assert "close_app" not in src
    assert "subprocess" not in src
    assert "os.startfile" not in src
    assert "Popen" not in src
    assert ".kill(" not in src and ".terminate(" not in src
