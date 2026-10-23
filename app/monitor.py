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

# Ring buffer: keep last 60 samples (at 3s interval = ~3 minutes of history)
MAX_HISTORY = 60

_history = deque(maxlen=MAX_HISTORY)
_lock = threading.Lock()
_running = False
_thread = None
from thresholds import monitor_defaults, CPU_TEMP_WARN, CPU_TEMP_CRIT
_thresholds = monitor_defaults()

# ── Metric collection ──────────────────────────────────────────────────────

def _collect_cpu_ram() -> dict:
    try:
        cpu = psutil.cpu_percent(interval=None)
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


_gpu_cache = {"value": {"has_gpu": False}, "ts": 0.0}
_GPU_TTL = 5.0


def _collect_gpu() -> dict:
    """Aggregate GPU engine utilisation from Performance Counters.

    Reads \\GPU Engine(*)\\Utilization Percentage via typeperf. Values are
    per-engine; we sum them per adapter and clamp to 100%. Cached for
    _GPU_TTL seconds so the typeperf process does not run on every poll.
    """
    now = time.time()
    if now - _gpu_cache["ts"] < _GPU_TTL:
        return _gpu_cache["value"]
    val = {"has_gpu": False}
    try:
        r = run_hidden.run(
            ["typeperf", "-sc", "1", r"\GPU Engine(*engtype_3D)\Utilization Percentage"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=4,
            creationflags=0x08000000,
        )
        total = 0.0
        for line in (r.stdout or "").splitlines():
            parts = line.split(",")
            if len(parts) < 2:
                continue
            for raw in parts[1:]:
                try:
                    v = float(raw.strip().strip('"'))
                    if 0 <= v <= 100:
                        total += v
                except ValueError:
                    pass
            break
        if total > 0:
            val = {"has_gpu": True, "gpu_pct": round(min(total, 100))}
    except Exception:
        pass
    _gpu_cache["value"] = val
    _gpu_cache["ts"] = now
    return val


def collect_metrics() -> dict:
    """Collect all available metrics in one call."""
    m = {"ts": datetime.now().strftime("%H:%M:%S")}
    m.update(_collect_cpu_ram())
    m.update(_collect_disk())
    m.update(_collect_battery())
    m.update(_collect_temperature())
    m.update(_collect_gpu())
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

def _worker(interval: float):
    global _running
    while _running:
        try:
            m = collect_metrics()
            record_sample(m)
        except Exception:
            pass
        time.sleep(interval)


def start_monitoring(interval: float = 3.0):
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_worker, args=(interval,), daemon=True)
    _thread.start()


def stop_monitoring():
    global _running
    _running = False
