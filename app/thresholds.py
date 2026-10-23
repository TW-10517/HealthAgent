"""Helix — shared thresholds.

Single source of truth for every numeric limit used by scanner.py,
suggestions.py, troubleshooter.py and monitor.py. Previously each module
hard-coded its own values, so one would say "warning" while another said
"critical" for the same reading.
"""

MB = 1 << 20
GB = 1 << 30

# ── Disk free percentage ────────────────────────────────────────────────────
DISK_PCT_WARN = 20    # low disk warning
DISK_PCT_CRIT = 10    # critical: free space is dangerous

# ── Temp / cache total bytes ────────────────────────────────────────────────
TEMP_SIZE_WARN = 500 * MB    # worth surfacing
TEMP_SIZE_HIGH = 1 * GB      # troubleshooter warning
TEMP_SIZE_CRIT = 2 * GB      # error-level

# ── Duplicate waste bytes ───────────────────────────────────────────────────
DUPES_SIZE_HIGH = 100 * MB

# ── Startup app count (personal / non-IT-managed) ──────────────────────────
STARTUP_COUNT_WARN = 5
STARTUP_COUNT_CRIT = 8

# ── Process RAM MB (top process) ────────────────────────────────────────────
PROCESS_RAM_HIGH_MB = 1500
PROCESS_RAM_CRIT_MB = 2000

# ── Live CPU % ──────────────────────────────────────────────────────────────
CPU_PCT_WARN = 90
CPU_PCT_CRIT = 95

# ── Live RAM % ──────────────────────────────────────────────────────────────
RAM_PCT_WARN = 85
RAM_PCT_CRIT = 90

# ── Battery % ───────────────────────────────────────────────────────────────
BATTERY_PCT_LOW = 15

# ── CPU temperature (°C) ────────────────────────────────────────────────────
CPU_TEMP_WARN = 85
CPU_TEMP_CRIT = 95

# ── Downloads age ───────────────────────────────────────────────────────────
OLD_DOWNLOADS_DAYS = 180
OLD_DOWNLOADS_SIZE_MIN = 50 * MB     # ignore anything smaller
OLD_DOWNLOADS_SIZE_WARN = 1 * GB     # bump severity above this

# ── Large files ─────────────────────────────────────────────────────────────
LARGE_FILE_MIN_MB = 200
LARGE_FILE_TOP_N = 10

# ── Empty folders ───────────────────────────────────────────────────────────
EMPTY_FOLDER_COUNT_MIN = 5

# ── Per-folder usage min (home subfolders) ──────────────────────────────────
FOLDER_USAGE_MIN = 100 * MB


def monitor_defaults() -> dict:
    """Default thresholds for the live monitor alert bands."""
    return {
        "cpu_pct":     CPU_PCT_WARN,
        "ram_pct":     RAM_PCT_WARN,
        "disk_pct":    100 - DISK_PCT_WARN,   # usage % that triggers alert
        "battery_pct": BATTERY_PCT_LOW,
    }
