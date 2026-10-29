"""Integration test for the narrator daemon loop.

Mocks psutil.process_iter so we can inject a fake new game process and
assert the notify callback fires with the correct toast title + body.
No real subprocess spawn — stays safe and deterministic.
"""
import time
import threading
import pytest


class _FakeProc:
    def __init__(self, pid, name):
        self.pid = pid
        self.info = {"pid": pid, "name": name}

    def cpu_percent(self, _ignored=None):
        return 0.0

    def memory_info(self):
        class _MI:
            rss = 0
        return _MI()


def _patch_watcher_timings(aw, poll=0.05, settle=0, startup=0, timeout_s=0.0):
    """Shrink every sleep in the watcher so the test runs in a second."""
    aw._POLL_INTERVAL = poll
    aw._SETTLE_SECONDS = settle
    aw._STARTUP_SETTLE = startup
    aw._OLLAMA_TIMEOUT_S = timeout_s


def test_watcher_fires_toast_for_new_game(monkeypatch):
    import app_watch as aw
    _patch_watcher_timings(aw, poll=0.05, startup=0, timeout_s=0.0)
    monkeypatch.setattr(aw, "_ollama_narrate", lambda *a, **k: None)

    baseline_procs = [_FakeProc(1, "svchost.exe"), _FakeProc(2, "pwsh.exe")]
    game_proc = _FakeProc(1234, "EldenRing.exe")

    state = {"phase": "baseline"}

    import psutil

    def _fake_iter(attrs=None):
        if state["phase"] == "baseline":
            return iter(baseline_procs)
        return iter(baseline_procs + [game_proc])

    monkeypatch.setattr(psutil, "process_iter", _fake_iter)
    monkeypatch.setattr(psutil, "cpu_percent", lambda **_k: 11.0)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: type("VM", (), {"percent": 22.0})())

    captured: list[tuple[str, str]] = []
    notify_done = threading.Event()

    def notify(title, msg):
        captured.append((title, msg))
        notify_done.set()

    aw.start_watcher(notify=notify, settings_fn=lambda: {})

    time.sleep(0.1)
    state["phase"] = "launched"

    fired = notify_done.wait(timeout=3.0)
    assert fired, "notify callback never fired"
    assert len(captured) >= 1
    title, msg = captured[0]
    assert "Game" in title
    assert "launched" in msg.lower()
    assert "11%" in msg and "22%" in msg


def test_watcher_dedupes_within_session(monkeypatch):
    """Same exe launching twice must fire the toast only once."""
    import app_watch as aw
    _patch_watcher_timings(aw, poll=0.05, startup=0, timeout_s=0.0)
    monkeypatch.setattr(aw, "_ollama_narrate", lambda *a, **k: None)

    baseline_procs = [_FakeProc(1, "svchost.exe")]
    game_pids: list[int] = []  # none at baseline
    state = {"phase": "baseline"}

    import psutil

    def _fake_iter(attrs=None):
        if state["phase"] == "baseline":
            return iter(baseline_procs)
        extras = [_FakeProc(pid, "sekiro.exe") for pid in game_pids]
        return iter(baseline_procs + extras)

    monkeypatch.setattr(psutil, "process_iter", _fake_iter)
    monkeypatch.setattr(psutil, "cpu_percent", lambda **_k: 10.0)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: type("VM", (), {"percent": 10.0})())

    captured: list[tuple[str, str]] = []
    def notify(title, msg): captured.append((title, msg))

    aw.start_watcher(notify=notify, settings_fn=lambda: {})
    time.sleep(0.15)
    # First launch
    state["phase"] = "launched"
    game_pids.append(1234)
    time.sleep(0.3)
    # Second launch of same exe name under different pid
    game_pids.append(5678)
    time.sleep(0.3)

    assert len(captured) == 1, f"expected 1 toast, got {len(captured)}: {captured}"


def test_watcher_honours_disabled_setting(monkeypatch):
    """app_narrator_enabled=false must silence the daemon."""
    import app_watch as aw
    _patch_watcher_timings(aw, poll=0.05, startup=0, timeout_s=0.0)

    import psutil
    monkeypatch.setattr(psutil, "process_iter",
                        lambda attrs=None: iter([_FakeProc(1, "svchost.exe"),
                                                 _FakeProc(999, "eldenring.exe")]))
    monkeypatch.setattr(psutil, "cpu_percent", lambda **_k: 0.0)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: type("VM", (), {"percent": 0.0})())

    captured = []
    aw.start_watcher(notify=lambda t, m: captured.append((t, m)),
                     settings_fn=lambda: {"app_narrator_enabled": "false"})
    time.sleep(0.4)
    assert captured == [], f"expected no toast, got {captured}"
