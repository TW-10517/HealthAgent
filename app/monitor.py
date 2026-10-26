"""PC Health Agent — Real-time system monitor with ring-buffer history."""
import os
import shutil
import run_hidden
import threading
import time
from collections import deque
from datetime import datetime

import psutil

# Prime psutil's CPU meter so the first non-blocking read returns a real value.
psutil.cpu_percent(interval=None)
psutil.cpu_percent(interval=None, percpu=True)

_prev_cpu_stats = None  # for context-switch / interrupt deltas
_prev_cpu_stats_ts = 0.0
_freq_cache = {"value": [], "ts": 0.0}
_FREQ_TTL = 2.5  # cpu_freq(percpu=True) is slow on Windows; cache briefly
_cpu_cache = {"total": 0.0, "per": [], "ts": 0.0}
_CPU_TTL = 1.0  # coalesce rapid cpu_percent reads so non-blocking mode stays accurate

# Ring buffer: keep last 60 samples (at 3s interval = ~3 minutes of history)
MAX_HISTORY = 60

_history = deque(maxlen=MAX_HISTORY)
_lock = threading.Lock()
_running = False
_thread = None
from thresholds import monitor_defaults, CPU_TEMP_WARN, CPU_TEMP_CRIT
_thresholds = monitor_defaults()

# ── Metric collection ──────────────────────────────────────────────────────

def _read_cpu_pct(percpu: bool = False):
    """Non-blocking cpu_percent that coalesces rapid reads. psutil's
    interval=None returns % since last call — if two readers call close
    together the second gets ~0. We cache for _CPU_TTL seconds so UI and
    worker share the same value."""
    now = time.time()
    if now - _cpu_cache["ts"] < _CPU_TTL and _cpu_cache["per"]:
        return _cpu_cache["per"] if percpu else _cpu_cache["total"]
    try:
        total = psutil.cpu_percent(interval=None)
        per = psutil.cpu_percent(interval=None, percpu=True) or []
    except Exception:
        total, per = 0.0, []
    _cpu_cache["total"] = total
    _cpu_cache["per"] = per
    _cpu_cache["ts"] = now
    return per if percpu else total


def _collect_cpu_ram() -> dict:
    try:
        cpu = _read_cpu_pct()
        vm = psutil.virtual_memory()
        return {
            "cpu_pct": round(cpu),
            "ram_used_mb": round(vm.used / (1 << 20)),
            "ram_total_mb": round(vm.total / (1 << 20)),
            "ram_pct": round(vm.percent),
        }
    except Exception:
        return {"cpu_pct": 0, "ram_used_mb": 0, "ram_total_mb": 0, "ram_pct": 0}


def _collect_disk() -> dict:
    try:
        u = shutil.disk_usage("C:\\")
        return {
            "disk_used_gb": round(u.used / (1 << 30), 1),
            "disk_total_gb": round(u.total / (1 << 30), 1),
            "disk_pct": round(u.used / u.total * 100),
        }
    except Exception:
        return {"disk_used_gb": 0, "disk_total_gb": 0, "disk_pct": 0}


def _collect_battery() -> dict:
    try:
        b = psutil.sensors_battery()
        if b is None:
            return {"has_battery": False}
        remaining = 0
        if (not b.power_plugged and b.secsleft is not None
                and b.secsleft >= 0 and b.secsleft != psutil.POWER_TIME_UNLIMITED):
            remaining = round(b.secsleft / 60)
        return {
            "has_battery": True,
            "battery_pct": round(b.percent),
            "battery_status": "Charging" if b.power_plugged else "Discharging",
            "battery_charging": bool(b.power_plugged),
            "battery_remaining_min": remaining,
        }
    except Exception:
        return {"has_battery": False}


_temp_cache = {"value": {"has_temp": False}, "ts": 0.0}
_TEMP_TTL = 30.0  # thermal zone query is slow; temp changes slowly — cache it


def _collect_temperature() -> dict:
    """Try to read CPU temperature via WMI thermal zone. Not all hardware exposes this.
    Cached for _TEMP_TTL seconds because the CIM query spawns a slow PowerShell process."""
    now = time.time()
    if now - _temp_cache["ts"] < _TEMP_TTL:
        return _temp_cache["value"]
    val = {"has_temp": False}
    try:
        r = run_hidden.run(
            ["powershell", "-NoProfile", "-Command",
             "$t=Get-CimInstance -Namespace root/WMI -ClassName MSAcpi_ThermalZoneTemperature "
             "-ErrorAction SilentlyContinue;"
             "if($t){[math]::Round(($t[0].CurrentTemperature-2732)/10,1)}else{'na'}"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5
        )
        out = r.stdout.strip()
        if out and out != "na":
            temp_c = float(out)
            if 0 < temp_c < 120:
                val = {"cpu_temp_c": temp_c, "has_temp": True,
                       "temp_source": "ACPI thermal zone"}
    except Exception:
        pass
    _temp_cache["value"] = val
    _temp_cache["ts"] = now
    return val


def cpu_core_snapshot() -> dict:
    """Per-core detail: utilization, frequency, counts, context-switch rate,
    load imbalance (stddev across cores). Interpretation hints included so the
    UI can show plain-English guidance.
    """
    global _prev_cpu_stats, _prev_cpu_stats_ts
    out = {"ok": False}
    try:
        # Shared coalesced cache: avoids a double-sample race between UI + worker.
        per = _read_cpu_pct(percpu=True) or []
        # Cold-start fallback: non-blocking returns all-zeros until a delta exists.
        # On click, pay a one-time ~100ms blocking sample so values appear immediately.
        if not per or (max(per) == 0 and sum(per) == 0):
            try:
                per = psutil.cpu_percent(interval=0.1, percpu=True) or per
                _cpu_cache["per"] = per
                _cpu_cache["total"] = sum(per) / len(per) if per else 0.0
                _cpu_cache["ts"] = time.time()
            except Exception:
                pass
        logical = psutil.cpu_count(logical=True) or len(per)
        physical = psutil.cpu_count(logical=False) or logical

        # Frequencies — slow on Windows (calls into perf counters). Cache a few seconds.
        now0 = time.time()
        if now0 - _freq_cache["ts"] < _FREQ_TTL and _freq_cache["value"]:
            freqs = _freq_cache["value"]
        else:
            freqs = []
            try:
                fs = psutil.cpu_freq(percpu=True) or []
                freqs = [{
                    "current_mhz": round(f.current or 0),
                    "min_mhz": round(f.min or 0),
                    "max_mhz": round(f.max or 0),
                } for f in fs]
            except Exception:
                freqs = []
            if not freqs:
                try:
                    f = psutil.cpu_freq()
                    if f:
                        freqs = [{
                            "current_mhz": round(f.current or 0),
                            "min_mhz": round(f.min or 0),
                            "max_mhz": round(f.max or 0),
                        }] * logical
                except Exception:
                    pass
            _freq_cache["value"] = freqs
            _freq_cache["ts"] = now0

        # Context switches & interrupts, as rate per second.
        ctx_rate = 0
        int_rate = 0
        now = time.time()
        try:
            s = psutil.cpu_stats()
            if _prev_cpu_stats is not None and _prev_cpu_stats_ts > 0:
                dt = max(now - _prev_cpu_stats_ts, 0.001)
                ctx_rate = round(max(0, (s.ctx_switches - _prev_cpu_stats.ctx_switches) / dt))
                int_rate = round(max(0, (s.interrupts - _prev_cpu_stats.interrupts) / dt))
            _prev_cpu_stats = s
            _prev_cpu_stats_ts = now
        except Exception:
            pass

        # Imbalance: stddev across cores (0 = balanced, high = one core pinned).
        if per:
            avg = sum(per) / len(per)
            var = sum((p - avg) ** 2 for p in per) / len(per)
            stddev = var ** 0.5
            max_core = max(per)
            min_core = min(per)
        else:
            avg = stddev = max_core = min_core = 0

        # Interpretation hints.
        hints = []
        if max_core >= 95 and stddev >= 25 and len(per) > 1:
            hints.append({
                "en": "One core is pinned near 100% while others are idle — a single-threaded workload is the bottleneck. Closing other apps won't help; it's the app itself.",
                "ja": "1コアが100%近くで他はアイドル状態 — シングルスレッド処理がボトルネックです。他のアプリを閉じても改善しません。",
                "severity": "info",
            })
        if avg >= 80 and stddev < 15:
            hints.append({
                "en": "All cores are heavily loaded — the PC is compute-bound. Expect fan noise and heat; cleaning temp files won't change this.",
                "ja": "全コアが高負荷 — 計算能力が限界です。発熱・ファン音が増え、一時ファイル削除では改善しません。",
                "severity": "warn",
            })
        if ctx_rate >= 50000:
            hints.append({
                "en": f"Very high context-switch rate ({ctx_rate:,}/s). Often caused by driver churn, aggressive antivirus, or many small I/O requests.",
                "ja": f"コンテキストスイッチが非常に多い（{ctx_rate:,}/秒）。ドライバやアンチウイルス、I/O過多が原因のことが多いです。",
                "severity": "warn",
            })
        if freqs:
            curr = [f["current_mhz"] for f in freqs if f["current_mhz"]]
            maxes = [f["max_mhz"] for f in freqs if f["max_mhz"]]
            if curr and maxes:
                ratio = (sum(curr) / len(curr)) / (sum(maxes) / len(maxes))
                if ratio < 0.55 and avg >= 50:
                    hints.append({
                        "en": "CPU is running well below its max frequency under load — likely thermal throttling or a battery-saver power plan.",
                        "ja": "負荷があるのに最大クロックの半分以下で動作中 — 熱によるスロットリングか省電力プランの可能性があります。",
                        "severity": "warn",
                    })

        out = {
            "ok": True,
            "per_core_pct": [round(p) for p in per],
            "cores_physical": physical,
            "cores_logical": logical,
            "avg_pct": round(avg),
            "max_core_pct": round(max_core),
            "min_core_pct": round(min_core),
            "imbalance_stddev": round(stddev, 1),
            "freqs": freqs,
            "ctx_switches_per_sec": ctx_rate,
            "interrupts_per_sec": int_rate,
            "hints": hints,
        }
    except Exception as e:
        out = {"ok": False, "error": str(e)}
    return out


def collect_metrics() -> dict:
    """Collect all available metrics in one call."""
    m = {"ts": datetime.now().strftime("%H:%M:%S")}
    m.update(_collect_cpu_ram())
    m.update(_collect_disk())
    m.update(_collect_battery())
    m.update(_collect_temperature())
    return m


# ── History & alerts ────────────────────────────────────────────────────────

def record_sample(m: dict):
    with _lock:
        _history.append(m)


def get_history(count: int = MAX_HISTORY) -> list:
    with _lock:
        return list(_history)[-count:]


def get_sparkline_data(key: str, count: int = 30) -> list:
    with _lock:
        return [h.get(key, 0) for h in list(_history)[-count:]]


def check_alerts(m: dict) -> list:
    alerts = []
    if m.get("cpu_pct", 0) >= _thresholds["cpu_pct"]:
        alerts.append({
            "type": "cpu", "severity": "warning",
            "en": f"CPU usage is {m['cpu_pct']}%",
            "ja": f"CPU使用率が{m['cpu_pct']}%です",
        })
    if m.get("ram_pct", 0) >= _thresholds["ram_pct"]:
        alerts.append({
            "type": "ram", "severity": "warning",
            "en": f"RAM usage is {m['ram_pct']}%",
            "ja": f"メモリ使用率が{m['ram_pct']}%です",
        })
    if m.get("disk_pct", 0) >= _thresholds["disk_pct"]:
        alerts.append({
            "type": "disk", "severity": "warning",
            "en": f"Disk C: is {m['disk_pct']}% full",
            "ja": f"ディスクC:の使用率が{m['disk_pct']}%です",
        })
    if m.get("has_battery") and not m.get("battery_charging") and \
       m.get("battery_pct", 100) <= _thresholds["battery_pct"]:
        alerts.append({
            "type": "battery", "severity": "error",
            "en": f"Battery at {m['battery_pct']}% — plug in soon",
            "ja": f"バッテリー{m['battery_pct']}% — すぐに充電してください",
        })
    if m.get("has_temp") and m.get("cpu_temp_c", 0) >= CPU_TEMP_WARN:
        sev = "error" if m["cpu_temp_c"] >= CPU_TEMP_CRIT else "warning"
        alerts.append({
            "type": "temp", "severity": sev,
            "en": f"ACPI zone temperature is {m['cpu_temp_c']}°C",
            "ja": f"ACPIゾーン温度が{m['cpu_temp_c']}°Cです",
        })
    return alerts


def set_thresholds(t: dict):
    global _thresholds
    for k in ("cpu_pct", "ram_pct", "disk_pct", "battery_pct"):
        if k in t:
            _thresholds[k] = int(t[k])


def get_thresholds() -> dict:
    return dict(_thresholds)


# ── Background worker ───────────────────────────────────────────────────────
# Idle-aware: when nothing has called request_samples() in the last
# _IDLE_TIMEOUT seconds, the worker sleeps a long interval instead of
# polling. Pages that need live data call request_samples() each refresh;
# the sidebar pills do this on their 3s tick when the window is visible.

_IDLE_TIMEOUT = 15.0        # go dormant 15s after last UI request
_ACTIVE_INTERVAL = 3.0
_IDLE_INTERVAL = 30.0
_last_request_ts = 0.0


def request_samples():
    """UI calls this each time it displays a sample. Keeps worker in
    active-poll mode. Without recent calls, worker goes dormant."""
    global _last_request_ts
    _last_request_ts = time.time()


def _worker():
    global _running
    while _running:
        try:
            idle = (time.time() - _last_request_ts) > _IDLE_TIMEOUT
            if not idle:
                m = collect_metrics()
                record_sample(m)
                # Keep sampler warm so UI's non-blocking read is accurate.
                try: _read_cpu_pct(percpu=True)
                except Exception: pass
        except Exception:
            pass
        time.sleep(_IDLE_INTERVAL if (time.time() - _last_request_ts) > _IDLE_TIMEOUT
                   else _ACTIVE_INTERVAL)


def start_monitoring(interval: float = 3.0):
    global _running, _thread, _ACTIVE_INTERVAL
    _ACTIVE_INTERVAL = interval
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_worker, daemon=True)
    _thread.start()


def stop_monitoring():
    global _running
    _running = False
