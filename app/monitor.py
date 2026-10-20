"""PC Health Agent — Real-time system monitor with ring-buffer history."""
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

# Ring buffer: keep last 60 samples (at 3s interval = ~3 minutes of history)
MAX_HISTORY = 60

_history = deque(maxlen=MAX_HISTORY)
_lock = threading.Lock()
_running = False
_thread = None
_thresholds = {
    "cpu_pct": 90,
    "ram_pct": 85,
    "disk_pct": 90,
    "battery_pct": 15,
}

# ── Metric collection ──────────────────────────────────────────────────────

def _collect_cpu_ram() -> dict:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$c=[math]::Round((Get-CimInstance Win32_Processor).LoadPercentage);"
             "$os=Get-CimInstance Win32_OperatingSystem;"
             "$totalMB=[math]::Round($os.TotalVisibleMemorySize/1024);"
             "$freeMB=[math]::Round($os.FreePhysicalMemory/1024);"
             "$usedMB=$totalMB-$freeMB;"
             "\"$c|$usedMB|$totalMB\""],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=6
        )
        parts = r.stdout.strip().split("|")
        if len(parts) == 3:
            total = int(parts[2])
            used = int(parts[1])
            return {
                "cpu_pct": int(parts[0]) if parts[0] else 0,
                "ram_used_mb": used,
                "ram_total_mb": total,
                "ram_pct": round(used / max(total, 1) * 100),
            }
    except Exception:
        pass
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
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$b=Get-CimInstance Win32_Battery;"
             "if($b){"
             "$b.EstimatedChargeRemaining.ToString()+'|'+"
             "$b.Status+'|'+"
             "$b.BatteryStatus.ToString()+'|'+"
             "$(if($b.EstimatedRunTime -and $b.EstimatedRunTime -lt 71582788){$b.EstimatedRunTime}else{'0'})"
             "}else{'none'}"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5
        )
        out = r.stdout.strip()
        if not out or out == "none":
            return {"has_battery": False}
        parts = out.split("|")
        if len(parts) >= 4:
            charge = int(parts[0])
            # BatteryStatus: 1=discharging, 2=AC, 3-5=charging variants
            status_code = int(parts[2]) if parts[2].isdigit() else 0
            return {
                "has_battery": True,
                "battery_pct": charge,
                "battery_status": parts[1].strip(),
                "battery_charging": status_code >= 2,
                "battery_remaining_min": int(parts[3]) if parts[3].isdigit() else 0,
            }
    except Exception:
        pass
    return {"has_battery": False}


def _collect_temperature() -> dict:
    """Try to read CPU temperature via WMI thermal zone. Not all hardware exposes this."""
    try:
        r = subprocess.run(
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
                return {"cpu_temp_c": temp_c, "has_temp": True}
    except Exception:
        pass
    return {"has_temp": False}


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
    if m.get("has_temp") and m.get("cpu_temp_c", 0) >= 85:
        sev = "error" if m["cpu_temp_c"] >= 95 else "warning"
        alerts.append({
            "type": "temp", "severity": sev,
            "en": f"CPU temperature is {m['cpu_temp_c']}°C",
            "ja": f"CPU温度が{m['cpu_temp_c']}°Cです",
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
