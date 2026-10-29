"""Tests for live_triage: snapshot shape, offline-fallback behavior, no-ollama path."""
import pytest


def _import():
    import live_triage
    return live_triage


def test_snapshot_shape():
    lt = _import()
    snap = lt.snapshot(top_n=3)
    assert snap["available"] is True
    for k in ("ts", "cpu_overall", "ram_overall", "disk_busy_pct",
             "net_mbps", "top_cpu", "top_ram", "highlights", "narrated"):
        assert k in snap, f"missing key {k}"
    assert isinstance(snap["cpu_overall"], float)
    assert isinstance(snap["ram_overall"], float)
    assert 0.0 <= snap["ram_overall"] <= 100.0
    assert isinstance(snap["top_cpu"], list)
    assert isinstance(snap["top_ram"], list)
    assert len(snap["top_cpu"]) <= 3
    assert len(snap["top_ram"]) <= 3
    for row in snap["top_cpu"] + snap["top_ram"]:
        assert "pid" in row and "name" in row and "cpu" in row and "mem_mb" in row
        assert isinstance(row["name"], str)
    assert snap["narrated"] is None  # snapshot() does not narrate
    assert len(snap["highlights"]) >= 1


def test_top_lists_sorted():
    lt = _import()
    snap = lt.snapshot(top_n=5)
    cpu_vals = [r["cpu"] for r in snap["top_cpu"]]
    ram_vals = [r["mem_mb"] for r in snap["top_ram"]]
    assert cpu_vals == sorted(cpu_vals, reverse=True)
    assert ram_vals == sorted(ram_vals, reverse=True)


def test_triage_live_no_narrate():
    lt = _import()
    out = lt.triage_live(narrate_=False)
    assert out["available"] is True
    assert out["narrated"] is None


def test_plain_text_renders():
    lt = _import()
    snap = lt.snapshot(top_n=2)
    txt = lt.plain_text(snap)
    assert isinstance(txt, str) and txt
    assert "Live triage" in txt


def test_self_pid_excluded():
    import os
    lt = _import()
    snap = lt.snapshot(top_n=10)
    pids = {r["pid"] for r in snap["top_cpu"]} | {r["pid"] for r in snap["top_ram"]}
    assert os.getpid() not in pids


def test_build_context_mentions_facts():
    lt = _import()
    snap = {
        "ts": "2026-10-29 10:00:00",
        "cpu_overall": 72.4, "ram_overall": 55.1,
        "disk_busy_pct": 42.0, "net_mbps": 3.2,
        "top_cpu": [{"pid": 100, "name": "chrome.exe", "cpu": 50, "mem_mb": 1200}],
        "top_ram": [{"pid": 100, "name": "chrome.exe", "cpu": 50, "mem_mb": 1200}],
        "highlights": [], "narrated": None,
    }
    ctx = lt._build_context(snap)
    assert "chrome.exe" in ctx
    assert "72" in ctx
    assert "do NOT invent" in ctx.lower() or "do not invent" in ctx.lower()
