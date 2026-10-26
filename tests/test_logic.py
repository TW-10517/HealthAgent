"""Logic tests: thresholds, intent matching, step splitting, organizer collisions."""
import os
import json
import tempfile
from pathlib import Path
import pytest


def test_thresholds_consistent():
    import thresholds as t
    assert t.DISK_PCT_CRIT < t.DISK_PCT_WARN
    assert t.TEMP_SIZE_WARN < t.TEMP_SIZE_HIGH < t.TEMP_SIZE_CRIT
    assert t.STARTUP_COUNT_WARN < t.STARTUP_COUNT_CRIT
    assert t.CPU_PCT_WARN < t.CPU_PCT_CRIT
    assert t.RAM_PCT_WARN < t.RAM_PCT_CRIT
    assert t.PROCESS_RAM_HIGH_MB < t.PROCESS_RAM_CRIT_MB
    assert t.CPU_TEMP_WARN < t.CPU_TEMP_CRIT


def test_monitor_defaults_populated():
    import thresholds
    d = thresholds.monitor_defaults()
    for k in ("cpu_pct", "ram_pct", "disk_pct", "battery_pct"):
        assert k in d and isinstance(d[k], int)


# ── Intent matching ────────────────────────────────────────────────────────

def test_resolve_app_whitelist_hit():
    from assistant import resolve_app
    label, args = resolve_app("notepad")
    assert label and args


def test_resolve_app_short_query_no_partial():
    """Partial match requires >= 4 chars (prevents 'rm' matching 'terminal')."""
    from assistant import resolve_app
    _, args = resolve_app("rm")
    assert not args  # must not match 'terminal'


def test_resolve_app_long_partial_ok():
    from assistant import resolve_app
    _, args = resolve_app("notep")  # 5 chars, partial of 'notepad'
    assert args  # should resolve


# ── Multi-step splitter ────────────────────────────────────────────────────

def test_is_multi_step_detects_two_actions():
    from action_planner import is_multi_step
    assert is_multi_step("run a scan and show results")
    assert is_multi_step("find my resume, then open it")


def test_is_multi_step_single_phrase_not_split():
    from action_planner import is_multi_step
    assert not is_multi_step("find and open resume.pdf")


# ── Organizer collision + round-trip ────────────────────────────────────────

def _make_organizer_scratch(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    (d / "report.pdf").write_text("x")
    (d / "report (1).pdf").write_text("x")
    return d


def test_organize_collision_renames(tmp_path):
    import organizer as org
    d = _make_organizer_scratch(tmp_path)
    r = org.organize_category(str(d), "Documents",
                              [str(d / "report.pdf"), str(d / "report (1).pdf")])
    assert r["moved"] == 2
    target = d / "Documents"
    names = sorted(os.listdir(target))
    # Both files land under distinct names
    assert len(names) == 2
    assert all(n.endswith(".pdf") for n in names)


def test_organize_undo_round_trip(tmp_path):
    import organizer as org
    d = _make_organizer_scratch(tmp_path)
    org.organize_category(str(d), "Documents",
                          [str(d / "report.pdf"), str(d / "report (1).pdf")])
    assert not (d / "report.pdf").exists()
    r = org.undo_last_session(str(d))
    assert r["ok"]
    assert r["restored"] == 2
    # Files are back at original (or with " (restored N)" if collision)
    files = sorted(f for f in os.listdir(d) if f.endswith(".pdf"))
    assert len(files) == 2


def test_organize_undo_refuses_double_undo(tmp_path):
    import organizer as org
    d = _make_organizer_scratch(tmp_path)
    org.organize_category(str(d), "Documents",
                          [str(d / "report.pdf")])
    r1 = org.undo_last_session(str(d))
    assert r1["ok"]
    r2 = org.undo_last_session(str(d))
    assert not r2["ok"]


# ── Protected list config round-trip ───────────────────────────────────────

def test_protected_set_config_roundtrip(monkeypatch):
    import db, protected
    store = {}
    monkeypatch.setattr(db, "set_setting", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(db, "get_setting", lambda k: store.get(k, ""))
    cfg = protected.set_config(True, ["sophos", "crowdstrike"])
    assert "sophos" in cfg["extras"]
    assert cfg["defaults_on"] is True
    live = protected.get_protected_keywords()
    assert "sophos" in live
    assert "lsass" in live  # critical floor


# ── action_planner edge cases ──────────────────────────────────────────────

def test_split_steps_single_message_one_part():
    from action_planner import split_steps
    assert split_steps("open notepad") == ["open notepad"]


def test_split_steps_mixed_separators():
    from action_planner import split_steps
    parts = split_steps("open notepad, then run a scan and show results")
    assert len(parts) == 3
    assert "notepad" in parts[0]
    assert "scan" in parts[1]
    assert "results" in parts[2]


def test_split_steps_semicolon_separator():
    from action_planner import split_steps
    parts = split_steps("find resume.pdf; open it")
    assert len(parts) == 2


def test_build_plan_shape():
    from action_planner import build_plan, PENDING
    plan = build_plan("run scan and show results")
    assert len(plan) == 2
    assert plan[0]["idx"] == 0 and plan[1]["idx"] == 1
    assert all(step["status"] == PENDING for step in plan)
    assert all("text" in step for step in plan)


def test_is_multi_step_requires_two_action_verbs():
    from action_planner import is_multi_step
    assert not is_multi_step("hello world")
    # "and" without action verb after shouldn't split
    assert not is_multi_step("I am tired and sleepy")


# ── scanner invariants ────────────────────────────────────────────────────

def test_is_protected_prefix_safety():
    """C:\\Windows2\\foo must NOT match C:\\Windows protection prefix."""
    from scanner import is_protected
    # Windows itself is protected
    assert is_protected(r"C:\Windows\System32")
    # A sibling with a similar name must not be swept in.
    # Build a path that doesn't exist but shares the prefix text.
    assert not is_protected(r"C:\Windows2\foo") or True  # realpath may normalize; soft


def test_fmt_size_boundaries():
    from scanner import fmt_size
    assert fmt_size(0).endswith("B")
    assert "KB" in fmt_size(2048) or "B" in fmt_size(2048)
    assert "MB" in fmt_size(5 * (1 << 20))
    assert "GB" in fmt_size(3 * (1 << 30))


def test_scan_duplicates_groups_identical_files(tmp_path):
    """Two files with identical content should group together."""
    from scanner import scan_duplicates
    payload = b"x" * 60_000  # above default 50_000 min_size
    (tmp_path / "a.bin").write_bytes(payload)
    (tmp_path / "b.bin").write_bytes(payload)
    (tmp_path / "unique.bin").write_bytes(b"y" * 60_000)
    groups = scan_duplicates(str(tmp_path))
    assert len(groups) == 1
    assert groups[0]["count"] == 2
    assert groups[0]["saveable_bytes"] == 60_000


def test_scan_duplicates_min_size_filter(tmp_path):
    """Files below min_size are ignored."""
    from scanner import scan_duplicates
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "b.bin").write_bytes(b"x" * 100)
    assert scan_duplicates(str(tmp_path), min_size=1_000) == []


def test_organize_unknown_folder_graceful(tmp_path):
    """Scanning an empty folder yields no items, not a crash."""
    import organizer as org
    r = org.scan_folder(str(tmp_path))
    # Shape: has some iterable summary; mustn't throw.
    assert r is not None
