"""Tests for battery_coach: snapshot shape, no-battery fallback, context builder."""
import pytest


def _import():
    import battery_coach
    return battery_coach


def test_snapshot_shape():
    bc = _import()
    snap = bc.snapshot(top_n=3)
    assert snap["available"] is True
    for k in ("ts", "has_battery", "percent", "plugged", "time_left_min",
              "wear_pct", "top_drain", "highlights", "narrated"):
        assert k in snap, f"missing key {k}"
    assert isinstance(snap["has_battery"], bool)
    assert isinstance(snap["top_drain"], list)
    assert len(snap["top_drain"]) <= 3
    for row in snap["top_drain"]:
        assert "pid" in row and "name" in row and "cpu" in row
    assert snap["narrated"] is None  # snapshot() does not narrate
    assert len(snap["highlights"]) >= 1


def test_coach_no_narrate():
    bc = _import()
    out = bc.coach(narrate_=False)
    assert out["available"] is True
    assert out["narrated"] is None


def test_plain_text_renders():
    bc = _import()
    snap = bc.snapshot(top_n=2)
    txt = bc.plain_text(snap)
    assert isinstance(txt, str) and txt
    assert "Battery coach" in txt


def test_build_context_mentions_facts():
    bc = _import()
    snap = {
        "ts": "2026-10-29 10:00:00",
        "has_battery": True, "percent": 18.5, "plugged": False,
        "time_left_min": 92, "wear_pct": 24,
        "top_drain": [{"pid": 100, "name": "chrome.exe", "cpu": 35, "mem_mb": 1200}],
        "highlights": [], "narrated": None,
    }
    ctx = bc._build_context(snap)
    assert "chrome.exe" in ctx
    assert "18.5" in ctx
    assert "do NOT invent" in ctx.lower() or "do not invent" in ctx.lower()


def test_has_battery_returns_bool():
    bc = _import()
    v = bc.has_battery()
    assert isinstance(v, bool)


def test_no_battery_fallback_shape(monkeypatch):
    """If psutil reports no battery, snapshot should still return a valid shape
    with has_battery=False and a sensible highlight."""
    bc = _import()
    import psutil

    def _none(): return None
    monkeypatch.setattr(psutil, "sensors_battery", _none)
    snap = bc.snapshot(top_n=3)
    assert snap["has_battery"] is False
    assert snap["percent"] is None
    assert snap["top_drain"] == []
    assert any("desktop" in h.lower() or "no battery" in h.lower() for h in snap["highlights"])


def test_narrate_returns_none_when_no_battery():
    bc = _import()
    snap = {"has_battery": False, "percent": None, "plugged": None,
            "time_left_min": None, "wear_pct": None, "top_drain": [],
            "highlights": [], "narrated": None, "ts": "x"}
    assert bc.narrate(snap) is None
