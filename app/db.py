"""PC Health Agent — SQLite database for persistent scan history and settings."""
import sqlite3
import json
import os
import uuid
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager

DB_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PCHealthAgent" / "data"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "pchealthagent.db"

_SCHEMA_VERSION = 1

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

@contextmanager
def _db():
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scans (
                id          TEXT PRIMARY KEY,
                start_time  TEXT NOT NULL,
                end_time    TEXT,
                duration_ms INTEGER,
                status      TEXT NOT NULL DEFAULT 'running',
                score       INTEGER,
                issues      INTEGER DEFAULT 0,
                summary     TEXT,
                data_json   TEXT,
                report_path TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_scans_start ON scans(start_time DESC);
            CREATE INDEX IF NOT EXISTS idx_scans_status ON scans(status);
        """)
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if not row:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))


# ── Scan CRUD ────────────────────────────────────────────────────────────────

def create_scan(scan_id: str = None) -> str:
    sid = scan_id or uuid.uuid4().hex[:12]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute(
            "INSERT INTO scans (id, start_time, status) VALUES (?, ?, 'running')",
            (sid, now),
        )
    return sid

def complete_scan(scan_id: str, score: int, issues: int, summary: str,
                  data: dict, report_path: str, duration_ms: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute(
            """UPDATE scans SET end_time=?, duration_ms=?, status='completed',
               score=?, issues=?, summary=?, data_json=?, report_path=?
               WHERE id=?""",
            (now, duration_ms, score, issues, summary,
             json.dumps(data, ensure_ascii=False), report_path, scan_id),
        )

def fail_scan(scan_id: str, error_msg: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        conn.execute(
            "UPDATE scans SET end_time=?, status='failed', summary=? WHERE id=?",
            (now, error_msg, scan_id),
        )

def get_scan(scan_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        return dict(row) if row else None

def list_scans(limit: int = 50, offset: int = 0, status: str = None) -> list[dict]:
    with _db() as conn:
        if status:
            rows = conn.execute(
                "SELECT id, start_time, end_time, duration_ms, status, score, issues, summary, report_path "
                "FROM scans WHERE status=? ORDER BY start_time DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, start_time, end_time, duration_ms, status, score, issues, summary, report_path "
                "FROM scans ORDER BY start_time DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

def get_scan_data(scan_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT data_json FROM scans WHERE id=?", (scan_id,)).fetchone()
        if row and row["data_json"]:
            return json.loads(row["data_json"])
        return None

def delete_scan(scan_id: str) -> bool:
    with _db() as conn:
        cur = conn.execute("DELETE FROM scans WHERE id=?", (scan_id,))
        return cur.rowcount > 0

def delete_old_scans(retention_days: int):
    with _db() as conn:
        conn.execute(
            "DELETE FROM scans WHERE start_time < datetime('now', 'localtime', ?)",
            (f"-{retention_days} days",),
        )

def scan_count() -> int:
    with _db() as conn:
        row = conn.execute("SELECT COUNT(*) as c FROM scans").fetchone()
        return row["c"] if row else 0


# ── Settings ─────────────────────────────────────────────────────────────────

_DEFAULTS = {
    "language": "en",
    "theme": "system",
    "tray_on_close": "true",
    "show_notifications": "true",
    "history_retention_days": "90",
    "scan_on_startup": "false",
    "health_watch_enabled": "false",
    "health_watch_disk_gb": "5",
    "health_watch_ram_pct": "92",
    "protected_apps_defaults": "true",
    "protected_apps_extra": "",
    "version": "1.0.0",
}

def get_setting(key: str) -> str:
    with _db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row:
            return row["value"]
    return _DEFAULTS.get(key, "")

def set_setting(key: str, value: str):
    with _db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

def get_all_settings() -> dict:
    result = dict(_DEFAULTS)
    with _db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        for r in rows:
            result[r["key"]] = r["value"]
    return result

def set_settings(settings: dict):
    with _db() as conn:
        for k, v in settings.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, str(v)),
            )


# ── Migration from scan-history.txt ──────────────────────────────────────────

def migrate_from_txt(txt_path: str):
    p = Path(txt_path)
    if not p.exists():
        return
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    with _db() as conn:
        existing = conn.execute("SELECT COUNT(*) as c FROM scans WHERE status='completed'").fetchone()
        if existing and existing["c"] > 0:
            return
        for i, line in enumerate(lines):
            parts = line.split(" | ")
            ts = parts[0].strip() if parts else ""
            summary = " | ".join(parts[1:]) if len(parts) > 1 else line
            score_val = None
            issues_val = 0
            for part in parts:
                if "Issues=" in part:
                    try:
                        issues_val = int(part.split("Issues=")[1].strip())
                        score_val = max(0, 100 - issues_val * 20)
                    except (ValueError, IndexError):
                        pass
            sid = f"migrated-{i:04d}"
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO scans
                       (id, start_time, end_time, status, score, issues, summary)
                       VALUES (?, ?, ?, 'completed', ?, ?, ?)""",
                    (sid, ts, ts, score_val, issues_val, summary),
                )
            except Exception:
                pass


init_db()
