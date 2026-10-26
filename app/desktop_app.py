"""Helix — local-first PC health monitor, production desktop application with pywebview."""
import sys, os, threading, json, time, ctypes
from pathlib import Path
from datetime import datetime

# Console-tool spawns go through run_hidden.run / run_hidden.popen so this
# --windowed build does not flash a black console for every powershell call.
# GUI launches (taskmgr, calc, chrome, …) use plain subprocess.

if getattr(sys, "frozen", False):
    # Packaged .exe: keep writable data under %LOCALAPPDATA%\PCHealthAgent
    BASE = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PCHealthAgent"
else:
    BASE = Path(__file__).parent.parent
REPORTS = BASE / "reports"
LOGS    = BASE / "logs"
BASE.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).parent))
import run_hidden
from scanner import run_full_scan, fmt_size, get_system_info, cleanup_temp_files, scan_disk, scan_temp
from report_gen import generate
from suggestions import run_all_checks, get_ai_summary
from organizer import scan_folder as org_scan, organize_category as org_move, get_organize_summary as org_summary
import monitor as mon
import event_analyzer as evtlog
import troubleshooter as tshoot
import db

db.migrate_from_txt(str(LOGS / "scan-history.txt"))

from metadata import APP_VERSION, APP_NAME, APP_ID

# ── Error log ────────────────────────────────────────────────────────────────
# A single rolling error.log under LOGS captures uncaught exceptions on both
# the Python side (sys.excepthook) and background threads
# (threading.excepthook, 3.8+). Readers: the About page and bug reports.
import logging as _logging
_ERROR_LOG = LOGS / "error.log"
_logging.basicConfig(
    filename=str(_ERROR_LOG), level=_logging.ERROR, encoding="utf-8",
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
def _install_crash_handler():
    import traceback as _tb
    _log = _logging.getLogger("helix")
    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        _log.error("Unhandled: %s", "".join(_tb.format_exception(exc_type, exc, tb)))
    sys.excepthook = _hook
    def _thread_hook(args):
        _log.error("Unhandled in %s: %s", args.thread.name,
                   "".join(_tb.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass
_install_crash_handler()

def _create_shield_icon(size=64):
    # Helix "Orbit Pulse" mark: a glowing teal gauge arc with a heart-rate
    # pulse and end dot, on a dark rounded tile. Supersampled 4x + neon glow.
    from PIL import Image, ImageDraw, ImageFilter

    ss = 4
    S = size * ss
    cx = cy = S / 2.0

    # Dark rounded-square tile with a vertical navy gradient.
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    g = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(g)
    top, bot = (18, 22, 34), (10, 13, 20)
    for y in range(S):
        t = y / (S - 1)
        r = int(top[0] + (bot[0] - top[0]) * t)
        gg = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        gd.line([(0, y), (S, y)], fill=(r, gg, b, 255))
    tile = Image.new("L", (S, S), 0)
    ImageDraw.Draw(tile).rounded_rectangle([0, 0, S - 1, S - 1],
                                           radius=int(S * 0.23), fill=255)
    img.paste(g, (0, 0), tile)

    def draw_marks(d):
        d.arc([S * 0.22, S * 0.22, S * 0.78, S * 0.78], start=140, end=400,
              fill=(45, 220, 200, 255), width=max(3, int(S * 0.045)))
        pulse = [(S * 0.30, cy), (S * 0.42, cy), (S * 0.49, cy - S * 0.13),
                 (S * 0.57, cy + S * 0.15), (S * 0.64, cy), (S * 0.72, cy)]
        d.line(pulse, fill=(130, 240, 255, 255),
               width=max(3, int(S * 0.04)), joint="curve")
        rr = max(3, int(S * 0.028))
        d.ellipse([S * 0.72 - rr, cy - rr, S * 0.72 + rr, cy + rr],
                  fill=(255, 255, 255, 255))

    # Neon glow: draw marks, blur, composite twice, then crisp on top.
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw_marks(ImageDraw.Draw(glow))
    gb = glow.filter(ImageFilter.GaussianBlur(int(S * 0.05)))
    img.alpha_composite(gb)
    img.alpha_composite(gb)
    draw_marks(ImageDraw.Draw(img))

    # Clip any glow bleed back to the rounded tile.
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(img, (0, 0), tile)
    return out.resize((size, size), Image.LANCZOS)

def make_icon():
    return _create_shield_icon(64)

def make_ico_file():
    ico_path = str(BASE / "app_icon.ico")
    if not os.path.exists(ico_path):
        img = _create_shield_icon(256)
        sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        img.save(ico_path, format="ICO", sizes=sizes)
    return ico_path


def _show_notification(title: str, message: str):
    try:
        import subprocess
        safe_title = title.replace('"', '').replace("'", "")
        safe_msg   = message.replace('"', '').replace("'", "")
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Shield;"
            "$n.Visible=$true;"
            f'$n.ShowBalloonTip(4000,"{safe_title}","{safe_msg}",'
            "[System.Windows.Forms.ToolTipIcon]::Info);"
            "Start-Sleep 5;$n.Dispose()"
        )
        subprocess.Popen(
            ["powershell", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=0x08000000,
        )
    except Exception:
        pass


class AgentAPI:
    def __init__(self):
        self._window         = None
        self._scanning       = False
        self._cancel         = False
        self._scan_id        = None
        self._last_scan_data    = None
        self._scan_pct          = 0
        self._scan_done         = 0
        self._scan_total        = 0
        self._last_search_items      = []   # for "open the Nth result" follow-ups
        self._pending_confirmations  = {}   # token → callable, for close-app confirm
        self._chat_history: list[dict] = []  # rolling conversation for Ollama context
        self._hw_cache               = None  # cached hardware info (WMI is slow)

    def set_window(self, w):
        self._window = w
        self._start_health_watch()
        self._start_schedule_watch()
        threading.Thread(target=self._check_ollama_status, daemon=True).start()
        threading.Thread(target=self._preload_hw_cache, daemon=True).start()

    def _preload_hw_cache(self):
        import time as _t
        _t.sleep(8)  # let startup settle, then warm the WMI cache silently
        self.get_hardware_info()

    def _check_ollama_status(self):
        import urllib.request, time as _t
        _t.sleep(3)
        self._probe_ollama()

    def _probe_ollama(self) -> bool:
        import urllib.request, time as _t
        try:
            urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=4)
            self._ollama_online = True
        except Exception:
            self._ollama_online = False
        self._ollama_probed_at = _t.time()
        return self._ollama_online

    _OLLAMA_PROBE_CACHE_S = 20   # cache a probe result for 20 s to avoid spam

    def get_ai_status(self) -> dict:
        """Return current Ollama status, re-probing on a cadence so that
        starting Ollama after Helix is launched is detected automatically.
        """
        import time as _t
        if (db.get_setting("ai_enabled") or "true").lower() == "false":
            return {"status": "disabled"}
        online = getattr(self, '_ollama_online', None)
        probed_at = getattr(self, '_ollama_probed_at', 0.0)
        now = _t.time()
        # Re-probe when we have no result yet, when the last probe is stale,
        # OR when we last saw offline (so recovery is fast).
        if online is None or (now - probed_at) > self._OLLAMA_PROBE_CACHE_S or online is False:
            self._probe_ollama()
            online = self._ollama_online
        if online is None:
            return {"status": "checking"}
        return {"status": "online" if online else "offline"}

    _CADENCE_SECONDS = {"daily": 86400, "weekly": 7*86400, "monthly": 30*86400}

    def _start_schedule_watch(self):
        """Run user-selected maintenance actions on a cadence. Only starts a
        job if schedule_enabled='true' and schedule_last_run is older than the
        cadence. Jobs never require elevation or external services."""
        def _loop():
            import time as _t
            _t.sleep(90)
            while True:
                _t.sleep(600)   # recheck every 10 min
                try:
                    s = db.get_all_settings()
                    if (s.get("schedule_enabled") or "false").lower() != "true":
                        continue
                    cadence = self._CADENCE_SECONDS.get(s.get("schedule_cadence","weekly"))
                    if not cadence:
                        continue
                    last = s.get("schedule_last_run") or ""
                    due = True
                    if last:
                        try:
                            dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                            due = (datetime.now() - dt).total_seconds() >= cadence
                        except Exception:
                            due = True
                    if not due:
                        continue
                    actions = [a.strip() for a in
                               (s.get("schedule_actions","") or "").split(",") if a.strip()]
                    self._run_scheduled_actions(actions)
                    db.set_setting("schedule_last_run",
                                   datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                except Exception:
                    pass
        threading.Thread(target=_loop, daemon=True).start()

    def _run_scheduled_actions(self, actions: list):
        """Execute each named maintenance job. Safe, idempotent, non-destructive."""
        for a in actions:
            try:
                if a == "quick_scan":
                    self.start_scan(no_dupes=True)
                elif a == "temp_cleanup":
                    cleanup_temp_files(dry_run=False)
                elif a == "snapshot":
                    self.capture_metrics_snapshot("scheduled")
            except Exception:
                pass

    def run_schedule_now(self) -> dict:
        """Fire the configured schedule right now (manual kick)."""
        s = db.get_all_settings()
        actions = [a.strip() for a in
                   (s.get("schedule_actions","") or "").split(",") if a.strip()]
        self._run_scheduled_actions(actions)
        db.set_setting("schedule_last_run",
                       datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        return {"ok": True, "actions": actions}

    def _start_health_watch(self):
        def _watch():
            import time as _time
            _time.sleep(60)   # let app fully start before first check
            while True:
                _time.sleep(840)   # check every 14 min (+ 60s startup = ~15 min cycle)
                try:
                    settings = db.get_all_settings()
                    if settings.get("health_watch_enabled") != "true":
                        continue
                    import psutil
                    disk = psutil.disk_usage("C:\\")
                    ram  = psutil.virtual_memory()
                    disk_gb   = disk.free / (1024 ** 3)
                    ram_pct   = ram.percent
                    thr_disk  = float(settings.get("health_watch_disk_gb",  "5"))
                    thr_ram   = float(settings.get("health_watch_ram_pct",  "92"))
                    if disk_gb < thr_disk:
                        _show_notification(
                            "PC Health — Low Disk Space",
                            f"Only {disk_gb:.1f} GB free on C: — consider cleaning up temp files.")
                    elif ram_pct > thr_ram:
                        _show_notification(
                            "PC Health — High Memory Usage",
                            f"RAM at {ram_pct:.0f}% — close unused apps to free memory.")
                except Exception:
                    pass
        threading.Thread(target=_watch, daemon=True).start()

    # ── Scan ─────────────────────────────────────────────────────────────────
    def start_scan(self, no_dupes=False):
        if self._scanning:
            return {"ok": False, "reason": "already scanning"}
        self._scanning = True
        self._cancel   = False
        threading.Thread(target=self._do_scan, args=(bool(no_dupes),), daemon=True).start()
        return {"ok": True}

    def cancel_scan(self):
        self._cancel = True
        return {"ok": True}

    def get_scan_progress(self):
        """Polled by JS every 300 ms — no evaluate_js push, no UI-thread pressure."""
        return {
            "scanning": self._scanning,
            "pct":      self._scan_pct,
            "done":     self._scan_done,
            "total":    self._scan_total,
        }

    def _do_scan(self, no_dupes: bool):
        t0 = time.time()
        scan_id = db.create_scan()
        self._scan_id  = scan_id
        self._scan_pct = 0

        def progress(done, total):
            if self._cancel:
                raise InterruptedError("Scan cancelled")
            self._scan_done  = done
            self._scan_total = total
            self._scan_pct   = round(done / total * 100) if total else 0

        try:
            self._js("onScanStart()")
            data = run_full_scan(
                include_dupes=not no_dupes,
                progress_cb=progress if not no_dupes else None,
            )

            if self._cancel:
                db.fail_scan(scan_id, "Cancelled by user")
                self._js('onScanCancelled()')
                return

            html = generate(data)
            ts   = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = REPORTS / f"report-{ts}.html"
            path.write_text(html, encoding="utf-8")

            score = max(0, 100 - data["issues"] * 20)
            data["score"] = score
            duration_ms = int((time.time() - t0) * 1000)

            summary = (f"Free={data['disk']['fmt_free']} | "
                       f"Temp={data['total_temp_fmt']} | Dupes={data['total_dupes_fmt']} | "
                       f"Startup={len(data['startup'])} | Issues={data['issues']}")

            db.complete_scan(scan_id, score, data["issues"], summary,
                             data, str(path), duration_ms)

            score_labels = {100:"Excellent",80:"Very Good",60:"Good",
                            40:"Fair",20:"Poor",0:"Critical"}
            score_labels_ja = {100:"優秀",80:"とても良い",60:"良好",
                               40:"普通",20:"悪い",0:"危険"}
            score_key = (score // 20) * 20
            total_used_mb = sum(p["mem_mb"] for p in data["processes"])

            payload = {
                "scanId": scan_id,
                "at": data["generated_at"],
                "computer": data["computer"],
                "user": data["user"],
                "reportUri": path.as_uri(),
                "reportPath": str(path),
                "score": score,
                "scoreLabel": score_labels.get(score_key, "Good"),
                "scoreLabelJa": score_labels_ja.get(score_key, "良好"),
                "durationMs": duration_ms,
                "diskFreeGb": data["disk"]["free_gb"],
                "diskUsedGb": data["disk"]["used_gb"],
                "diskTotalGb": data["disk"]["total_gb"],
                "diskPctFree": data["disk"]["pct_free"],
                "diskFmtFree": data["disk"]["fmt_free"],
                "diskFmtUsed": data["disk"]["fmt_used"],
                "freeable": data["total_freeable_fmt"],
                "freeableBytes": data["total_freeable"],
                "temp": data["total_temp_fmt"],
                "tempBytes": data["total_temp"],
                "dupes": data["total_dupes_fmt"],
                "dupesBytes": data["total_dupes"],
                "dupeGroups": data["dupe_groups_count"],
                "startupTotal": len(data["startup"]),
                "startupPersonal": len(data["personal_apps"]),
                "startupIT": len(data["it_apps"]),
                "issues": data["issues"],
                "totalRamGb": data["total_ram_gb"],
                "totalUsedMb": total_used_mb,
                "tempItems": [
                    {"name": t["name_en"], "nameJa": t["name_ja"],
                     "size": t["size"], "fmt": t["size_fmt"],
                     "cat": t["category"], "path": t["path"]}
                    for t in data["temp_items"][:12]
                ],
                "processes": data["processes"][:10],
                "startupApps": [
                    {"name": a["name"], "cmd": a["command"][:80], "it": a["it_managed"]}
                    for a in data["startup"]
                ],
                "topDupes": [
                    {"name": g["files"][0]["name"], "count": g["count"],
                     "saveable": g["saveable_fmt"], "saveableBytes": g["saveable_bytes"],
                     "paths": [f["path"] for f in g["files"][:4]]}
                    for g in data["dupes"][:10]
                ],
                "issueDetails": _build_issue_details(data),
            }

            # Run organize scan on Downloads during full scan
            if not no_dupes:
                try:
                    dl_folder = str(Path.home() / "Downloads")
                    org_result = org_scan(dl_folder)
                    if not org_result.get("error") and org_result.get("categories"):
                        cat_summary = {}
                        for cname, cinfo in org_result["categories"].items():
                            cat_summary[cname] = {
                                "name": cname,
                                "icon": cinfo["icon"],
                                "color": cinfo["color"],
                                "count": cinfo["count"],
                                "total_fmt": cinfo["total_fmt"],
                            }
                        payload["orgScan"] = {
                            "folder": dl_folder,
                            "total_files": org_result["total_files"],
                            "total_fmt": org_result["total_fmt"],
                            "category_count": org_result["category_count"],
                            "categories": cat_summary,
                        }
                except Exception:
                    pass

            self._last_scan_data = data
            try:
                import agent_cache
                agent_cache.build_cache(data)
            except Exception:
                pass
            if db.get_setting("show_notifications") == "true":
                _show_notification(
                    "Scan Complete",
                    f"Score {score}/100 · {data['issues']} issue(s) · {data['total_freeable_fmt']} reclaimable"
                )
            self._js(f"onScanDone({json.dumps(payload, ensure_ascii=False)})")
        except InterruptedError:
            db.fail_scan(scan_id, "Cancelled by user")
            self._js('onScanCancelled()')
        except Exception as e:
            db.fail_scan(scan_id, str(e))
            self._js(f'onScanError({json.dumps(str(e))})')
        finally:
            self._scanning = False
            self._cancel   = False

    def open_report(self, uri: str):
        if self._window:
            def _load():
                time.sleep(0.05)
                self._window.load_url(uri)
            threading.Thread(target=_load, daemon=True).start()
        return True

    # ── History ──────────────────────────────────────────────────────────────
    def get_history(self):
        return db.list_scans(limit=50)

    def delete_history_item(self, scan_id: str):
        return db.delete_scan(scan_id)

    def get_scan_detail(self, scan_id: str):
        return db.get_scan(scan_id)

    # ── Settings ─────────────────────────────────────────────────────────────
    def get_settings(self):
        s = db.get_all_settings()
        s["version"] = APP_VERSION
        return s

    def report_js_error(self, info: dict):
        """JS global error handler calls here; written to logs/js-errors.log."""
        try:
            line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {info}\n"
            (LOGS / "js-errors.log").open("a", encoding="utf-8").write(line)
        except Exception:
            pass
        return {"ok": True}

    def get_about(self) -> dict:
        """Return app version + where data lives + error log size for About."""
        try:
            errsz = _ERROR_LOG.stat().st_size if _ERROR_LOG.exists() else 0
        except Exception:
            errsz = 0
        return {
            "version":    APP_VERSION,
            "base":       str(BASE),
            "logs":       str(LOGS),
            "reports":    str(REPORTS),
            "db":         str(BASE / "data" / "pchealthagent.db"),
            "error_log":  str(_ERROR_LOG),
            "error_log_bytes": errsz,
            "frozen":     bool(getattr(sys, "frozen", False)),
            "python":     sys.version.split()[0],
        }

    def open_error_log(self) -> dict:
        try:
            if not _ERROR_LOG.exists():
                _ERROR_LOG.touch()
            os.startfile(str(_ERROR_LOG))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def save_settings(self, settings: dict):
        allowed = {"language","theme","tray_on_close","show_notifications",
                    "history_retention_days","scan_on_startup",
                    "health_watch_enabled","health_watch_disk_gb","health_watch_ram_pct",
                    "protected_apps_defaults","protected_apps_extra","ai_enabled",
                    "onboarded_mode","schedule_enabled","schedule_cadence",
                    "schedule_actions","schedule_last_run"}
        filtered = {k: v for k, v in settings.items() if k in allowed}
        db.set_settings(filtered)
        return {"ok": True}

    def get_first_run_mode(self):
        return {"mode": db.get_setting("onboarded_mode") or ""}

    def apply_first_run_mode(self, mode: str):
        """One-shot: set defaults for 'work', 'personal' or 'minimal' PCs."""
        mode = (mode or "").strip().lower()
        presets = {
            "work":     {"ai_enabled": "true",  "protected_apps_defaults": "true"},
            "personal": {"ai_enabled": "true",  "protected_apps_defaults": "false"},
            "minimal":  {"ai_enabled": "false", "protected_apps_defaults": "false"},
        }
        if mode not in presets:
            return {"ok": False, "msg": "Unknown mode."}
        settings = dict(presets[mode])
        settings["onboarded_mode"] = mode
        db.set_settings(settings)
        return {"ok": True, "mode": mode, "applied": settings}

    def get_protected_apps(self):
        import protected
        return protected.get_config()

    def set_protected_apps(self, defaults_on: bool = True, extras: list = None):
        import protected
        return {"ok": True, "config": protected.set_config(bool(defaults_on), extras or [])}

    # ── System info ──────────────────────────────────────────────────────────
    def get_system_info(self):
        return get_system_info()

    def get_hardware_info(self):
        """Return detailed hardware/OS info via PowerShell WMI queries."""
        if self._hw_cache:
            return self._hw_cache
        import json as _json
        sp = run_hidden
        ps = r"""
$ErrorActionPreference='SilentlyContinue'
$cpu  = Get-CimInstance Win32_Processor | Select-Object -First 1 Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed
$ramM = Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Speed,SMBIOSMemoryType
$gpu  = Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,CurrentHorizontalResolution,CurrentVerticalResolution
$disk = Get-CimInstance Win32_DiskDrive | Select-Object Model,Size,MediaType
$os   = Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,LastBootUpTime,TotalVisibleMemorySize
$sys  = Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,Domain,PartOfDomain,TotalPhysicalMemory
$bios = Get-CimInstance Win32_BIOS | Select-Object SerialNumber,Manufacturer
@{cpu=$cpu;ramModules=$ramM;gpu=$gpu;disk=$disk;os=$os;sys=$sys;bios=$bios} | ConvertTo-Json -Depth 4
"""
        try:
            r = sp.run(
                ["powershell", "-WindowStyle", "Hidden", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20,
            )
            raw = _json.loads(r.stdout or "{}")

            def _obj(x):
                if isinstance(x, list): return x[0] if x else {}
                return x or {}

            def _arr(x):
                if isinstance(x, list): return x
                return [x] if x else []

            cpu  = _obj(raw.get("cpu", {}))
            os_  = _obj(raw.get("os", {}))
            sys_ = _obj(raw.get("sys", {}))
            bios = _obj(raw.get("bios", {}))

            ram_modules = _arr(raw.get("ramModules", []))
            total_ram_bytes = sys_.get("TotalPhysicalMemory", 0) or 0
            ram_speed = ram_modules[0].get("Speed", 0) if ram_modules else 0
            ram_type_map = {20:"DDR",21:"DDR",24:"DDR3",26:"DDR4",29:"LPDDR4",30:"DDR5",34:"DDR5"}
            smbios = ram_modules[0].get("SMBIOSMemoryType", 0) if ram_modules else 0
            ram_type = ram_type_map.get(smbios, "DDR4")

            gpus = _arr(raw.get("gpu", []))
            disks = _arr(raw.get("disk", []))

            # Boot time
            boot_raw = os_.get("LastBootUpTime", "") or ""
            uptime_str = ""
            try:
                from datetime import datetime as _dt
                import re as _re
                m = _re.search(r'(\d{14})', str(boot_raw))
                if m:
                    bt = _dt.strptime(m.group(1), "%Y%m%d%H%M%S")
                    delta = _dt.now() - bt
                    h, r = divmod(int(delta.total_seconds()), 3600)
                    uptime_str = f"{h//24}d {h%24}h" if h >= 24 else f"{h}h {r//60}m"
            except Exception:
                pass

            result = {
                "cpu": {
                    "name":    cpu.get("Name", "Unknown CPU").strip(),
                    "cores":   cpu.get("NumberOfCores", "?"),
                    "threads": cpu.get("NumberOfLogicalProcessors", "?"),
                    "mhz":     cpu.get("MaxClockSpeed", 0),
                    "ghz":     f"{cpu.get('MaxClockSpeed',0)/1000:.2f}" if cpu.get("MaxClockSpeed") else "?",
                },
                "ram": {
                    "total_gb": f"{total_ram_bytes/1024**3:.1f}" if total_ram_bytes else "?",
                    "slots":    len(ram_modules),
                    "speed_mhz": ram_speed,
                    "type":     ram_type,
                },
                "gpus": [
                    {
                        "name": g.get("Name", "Unknown GPU").strip(),
                        "vram_mb": round((g.get("AdapterRAM") or 0) / 1024**2),
                        "res": (f"{g.get('CurrentHorizontalResolution','?')}×"
                                f"{g.get('CurrentVerticalResolution','?')}")
                                if g.get("CurrentHorizontalResolution") else "—",
                    }
                    for g in gpus
                ],
                "disks": [
                    {
                        "model":    d.get("Model", "Unknown Disk").strip(),
                        "size_gb":  round((d.get("Size") or 0) / 1024**3),
                        "type":     d.get("MediaType") or "Fixed",
                    }
                    for d in disks
                ],
                "os": {
                    "caption":  os_.get("Caption", "Windows").strip(),
                    "version":  os_.get("Version", ""),
                    "build":    os_.get("BuildNumber", ""),
                    "uptime":   uptime_str,
                },
                "machine": {
                    "maker":    sys_.get("Manufacturer", "").strip(),
                    "model":    sys_.get("Model", "").strip(),
                    "domain":   sys_.get("Domain", ""),
                    "bios_mfr": bios.get("Manufacturer", "").strip(),
                },
            }
            self._hw_cache = result
            return result
        except Exception as e:
            return {"error": str(e)}

    # ── Cleanup ──────────────────────────────────────────────────────────────
    def quick_temp_scan(self) -> dict:
        """Scan temp/cache targets on demand (no full disk scan needed).
        Returns per-target size + total so the UI can show a Clean button."""
        try:
            items = scan_temp()
        except Exception as e:
            return {"ok": False, "msg": str(e), "items": [], "total_size": 0,
                    "total_fmt": "0 B"}
        total = sum(i.get("size", 0) for i in items)
        return {"ok": True, "items": items, "total_size": total,
                "total_fmt": fmt_size(total)}

    def cleanup_files(self, paths: list, confirm_token: str = ""):
        """Hard-delete temp/cache files.
        First call: queue + return a token with the count. Second call with
        the token: delete. This is irreversible, so confirmation is mandatory.
        """
        import uuid
        paths = [p for p in (paths or []) if isinstance(p, str) and p.strip()]
        if not paths:
            return {"results": [], "totalFreed": 0, "totalFreedFmt": "0 B"}

        def _do_cleanup():
            results = cleanup_temp_files(paths)
            total_freed = sum(r["freed"] for r in results)
            return {
                "results": results,
                "totalFreed": total_freed,
                "totalFreedFmt": fmt_size(total_freed),
            }

        if confirm_token:
            fn = self._pending_confirmations.pop(confirm_token, None)
            if not fn:
                return {"needs_confirm": False, "ok": False,
                        "msg": "Confirmation expired. Reselect the items and try again.",
                        "results": [], "totalFreed": 0, "totalFreedFmt": "0 B"}
            try:
                return fn()
            except Exception as e:
                return {"ok": False, "msg": str(e), "results": [],
                        "totalFreed": 0, "totalFreedFmt": "0 B"}

        token = uuid.uuid4().hex[:12]
        self._pending_confirmations[token] = _do_cleanup
        return {
            "needs_confirm": True,
            "token": token,
            "count": len(paths),
            "msg": f"Permanently delete {len(paths)} item(s)? "
                   f"These files cannot be recovered from the Recycle Bin.",
        }

    def get_disk_info(self):
        return scan_disk()

    # ── Suggestions ─────────────────────────────────────────────────────────
    def get_suggestions(self):
        try:
            return run_all_checks()
        except Exception:
            return []

    def get_ai_summary(self, suggestions: list, lang: str = "en"):
        try:
            return get_ai_summary(suggestions, lang)
        except Exception:
            return None

    def remove_empty_folders(self, paths: list):
        removed = 0
        failed = 0
        for p in paths:
            try:
                if os.path.isdir(p) and not os.listdir(p):
                    os.rmdir(p)
                    removed += 1
                else:
                    failed += 1
            except (PermissionError, OSError):
                failed += 1
        return {"removed": removed, "failed": failed}

    def open_in_explorer(self, path: str):
        import subprocess as sp
        p = os.path.normpath(path)
        try:
            if os.path.isdir(p):
                sp.Popen(["explorer", p])
            elif os.path.isfile(p):
                sp.Popen(["explorer", "/select,", p])
            else:
                parent = os.path.dirname(p)
                if os.path.isdir(parent):
                    sp.Popen(["explorer", parent])
                else:
                    return {"ok": False, "error": "Path not found"}
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── File Organizer ─────────────────────────────────────────────────────
    def scan_organize(self, folder: str):
        try:
            return org_scan(folder)
        except Exception as e:
            return {"error": str(e), "categories": {}}

    def apply_organize(self, folder: str, category: str, file_paths: list):
        try:
            return org_move(folder, category, file_paths)
        except Exception as e:
            return {"moved": 0, "failed": 0, "errors": [str(e)]}

    def get_organize_ai(self, scan_result: dict, lang: str = "en"):
        try:
            return org_summary(scan_result, lang)
        except Exception:
            return {"summary": "", "model": ""}

    def get_known_folders(self):
        home = Path.home()
        onedrive = Path(os.environ.get("OneDrive", str(home / "OneDrive")))
        def _try(*paths):
            for p in paths:
                if p.exists():
                    return str(p)
            return None
        return {
            "downloads": _try(home / "Downloads", onedrive / "Downloads"),
            "documents": _try(home / "Documents", onedrive / "Documents"),
            "desktop":   _try(home / "Desktop",
                              onedrive / "デスクトップ",
                              onedrive / "Desktop"),
            "pictures":  _try(home / "Pictures", onedrive / "Pictures"),
        }

    def undo_organize(self, folder: str):
        import organizer as _org
        try:
            return _org.undo_last_session(os.path.normpath(folder))
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_organize_history(self, folder: str):
        import json as _json
        import organizer as _org
        folder = os.path.normpath(folder)
        log_path = _org._log_path_for(folder)
        try:
            if not os.path.exists(log_path):
                return {"sessions": []}
            with open(log_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            # Newest first
            return {"sessions": list(reversed(data)) if isinstance(data, list) else []}
        except Exception as e:
            return {"sessions": [], "error": str(e)}

    def browse_folder(self):
        if not self._window:
            return None
        try:
            import webview
            result = self._window.create_file_dialog(
                dialog_type=getattr(webview, 'FOLDER_DIALOG', getattr(getattr(webview, 'FileDialog', None), 'FOLDER', 3)),
                directory=str(Path.home() / "Downloads"),
            )
            if result and len(result) > 0:
                return result[0]
        except Exception:
            pass
        return None

    def get_default_org_folder(self):
        return str(Path.home() / "Downloads")

    # ── Monitoring ──────────────────────────────────────────────────────────
    def start_monitoring(self):
        mon.start_monitoring(interval=3.0)
        return {"ok": True}

    def get_monitor_data(self):
        mon.request_samples()
        m = mon.collect_metrics()
        mon.record_sample(m)
        alerts = mon.check_alerts(m)
        sparklines = {
            "cpu": mon.get_sparkline_data("cpu_pct", 30),
            "ram": mon.get_sparkline_data("ram_pct", 30),
            "disk": mon.get_sparkline_data("disk_pct", 30),
        }
        return {"metrics": m, "alerts": alerts, "sparklines": sparklines}

    def get_cpu_core_detail(self):
        """Per-core utilization, frequencies, context switches + interpretation hints."""
        try:
            return mon.cpu_core_snapshot()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_monitor_thresholds(self):
        return mon.get_thresholds()

    def set_monitor_thresholds(self, t: dict):
        mon.set_thresholds(t)
        return {"ok": True}

    # ── Event Analyzer ──────────────────────────────────────────────────────
    def get_events(self, hours=168, max_level=3):
        try:
            hours = int(hours)
            max_level = int(max_level)
            events = evtlog.get_events(hours=hours, max_level=max_level)
            summary = evtlog.summarize_events(events)
            return {"events": events, "summary": summary}
        except Exception as e:
            import traceback; traceback.print_exc()
            return {"events": [], "summary": {"total": 0}, "error": str(e)}

    def get_crash_events(self, hours: int = 168):
        try:
            return evtlog.get_crash_events(hours=hours)
        except Exception:
            return []

    # ── Troubleshooter ──────────────────────────────────────────────────────
    def run_troubleshoot(self):
        scan_data = None
        if self._scan_id:
            scan_data = db.get_scan_data(self._scan_id)
        metrics = mon.collect_metrics()
        mon.record_sample(metrics)
        events = []
        try:
            events = evtlog.get_events(hours=72, max_level=2)
        except Exception:
            pass
        crashes = []
        try:
            crashes = evtlog.get_crash_events(hours=168)
        except Exception:
            pass
        all_events = events + crashes
        raw_issues = tshoot.run_diagnostics(
            scan_data=scan_data, metrics=metrics, events=all_events
        )
        # Map backend fields → UI shape. The troubleshooter emits en/ja/evidence_*
        # and action-id strings; the renderer expects title/description/actions[].
        issues = []
        for iss in raw_issues:
            acts = []
            for aid in iss.get("actions") or []:
                info = tshoot.get_action_info(aid)
                if not info:
                    continue
                acts.append({
                    "id": aid,
                    "label":        info.get("en", aid),
                    "label_ja":     info.get("ja", aid),
                    "description":  info.get("risk_en", ""),
                    "description_ja": info.get("risk_ja", ""),
                    "safety":       info.get("safety", "confirm"),
                })
            causes = []
            causes_en = iss.get("causes") or []
            causes_ja = iss.get("causes_ja") or []
            for i, c_en in enumerate(causes_en):
                causes.append({
                    "en": c_en,
                    "ja": causes_ja[i] if i < len(causes_ja) else c_en,
                })
            issues.append({
                "id":            iss.get("id"),
                "category":      iss.get("category"),
                "severity":      iss.get("severity"),
                "confidence":    iss.get("confidence"),
                "title":         iss.get("en", ""),
                "title_ja":      iss.get("ja", ""),
                "description":   iss.get("evidence_en", ""),
                "description_ja":iss.get("evidence_ja", ""),
                "causes":        causes,
                "actions":       acts,
            })
        return {"issues": issues, "metrics": metrics, "event_count": len(all_events)}

    def get_action_info(self, action_id: str):
        return tshoot.get_action_info(action_id)

    def execute_action(self, action_id: str):
        info = tshoot.get_action_info(action_id)
        if not info:
            return {"ok": False, "error": "Unknown action"}
        result = tshoot.execute_action(action_id)
        tshoot.log_action(action_id, result)
        return result

    def get_action_log(self):
        return tshoot.get_action_log(limit=30)

    def get_launcher_enabled(self):
        import launcher
        return launcher.is_enabled()

    def set_launcher_enabled(self, enabled):
        import launcher
        launcher.set_enabled(bool(enabled))
        fl = launcher.get_launcher()
        if enabled and (not fl or not fl._alive):
            def _open():
                try: self._window.show(); self._window.restore()
                except Exception: pass
            def _scan():
                try: self.start_scan(False); self._window.show(); self._window.restore()
                except Exception: pass
            launcher.start_launcher(_open, _scan, lambda: None)
        elif not enabled and fl:
            fl.stop()
        return True

    def toggle_launcher_visibility(self):
        import launcher
        fl = launcher.get_launcher()
        if fl and fl._alive:
            if fl._hwnd and not ctypes.windll.user32.IsWindowVisible(fl._hwnd):
                fl.show()
                return "visible"
            else:
                fl.hide()
                return "hidden"
        return "disabled"

    def get_launcher_startup(self):
        import launcher
        return launcher._get_startup_enabled()

    def set_launcher_startup(self, enabled):
        import launcher
        launcher._set_startup(bool(enabled))
        return True

    def reset_launcher_position(self):
        import launcher
        launcher._save_position(50, 50)
        fl = launcher.get_launcher()
        if fl and fl._hwnd:
            ctypes.windll.user32.SetWindowPos(fl._hwnd, None, 50 - 78, 50 - 78, 0, 0, 0x0001 | 0x0004)
        return True

    def open_chat_popup(self):
        # Chat lives inside the main window on the Assistant page — no separate
        # WebView2 renderer. Show the main window and navigate to that page.
        try:
            if self._window:
                self._window.show()
                try: self._window.restore()
                except Exception: pass
                self._window.evaluate_js("showPage('assistant')")
        except Exception:
            pass
        return True

    def get_chat_float_enabled(self):
        import chat_popup
        return chat_popup.is_float_enabled()

    def set_chat_float_enabled(self, enabled: bool):
        import chat_popup
        chat_popup.set_float_button_enabled(bool(enabled))
        return True

    # ── Safe file / folder opener (called from JS) ────────────────────────────
    # Allowlist: only data/document types can be opened via the chat. Anything
    # not listed here (including .lnk, .url, .reg, .msc, .vbe, .chm, .exe, .bat,
    # scripts, installers, control panel items) is treated as unsafe and shown
    # in Explorer instead of executed.
    _ALLOWED_EXTS = frozenset({
        # Documents
        '.pdf', '.txt', '.md', '.rtf', '.odt', '.epub', '.tex', '.log',
        '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx', '.csv', '.tsv',
        '.ods', '.odp', '.pages', '.numbers', '.key',
        # Images
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.svg', '.webp', '.ico',
        '.tif', '.tiff', '.heic', '.heif', '.raw',
        # Audio / video
        '.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.opus',
        '.mp4', '.avi', '.mkv', '.mov', '.wmv', '.webm', '.m4v',
        # Archives (opened in default viewer — not extracted/executed)
        '.zip', '.7z', '.rar', '.tar', '.gz',
        # Data / text source
        '.json', '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf',
        '.html', '.htm', '.css', '.sql',
    })

    def open_path(self, path: str) -> dict:
        """Open a file with its default app or a folder in Explorer. Read-only intent.
        Blocks executable extensions to prevent running programs via the chat.
        """
        import os, subprocess, sys_executor as sx
        path = os.path.normpath(path.strip())

        # Security: must be inside user space or OneDrive
        nc = os.path.normcase(path)
        home_nc = os.path.normcase(str(__import__('pathlib').Path.home()))
        od_nc   = os.path.normcase(os.environ.get("OneDrive", ""))
        in_scope = nc.startswith(home_nc) or (od_nc and nc.startswith(od_nc))
        if not in_scope or sx._protected(path):
            return {"ok": False, "msg": "Access to that path is restricted."}

        if os.path.isdir(path):
            subprocess.Popen(
                ["explorer.exe", path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return {"ok": True, "msg": f"Opened folder in Explorer."}

        if os.path.isfile(path):
            ext = os.path.splitext(path)[1].lower()
            if ext not in self._ALLOWED_EXTS:
                # Only known-safe document/media types open directly. Everything
                # else (scripts, installers, shortcuts, registry files, control
                # panel items, help files, raw binaries) is revealed in Explorer
                # so the user can decide what to do with it.
                subprocess.Popen(
                    ["explorer.exe", "/select,", path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return {"ok": True, "msg": f"That file type ({ext or 'unknown'}) can't be opened from chat — showed it in Explorer instead."}
            try:
                os.startfile(path)
                return {"ok": True, "msg": "Opened."}
            except Exception as e:
                return {"ok": False, "msg": str(e)}

        return {"ok": False, "msg": "Path not found."}

    @staticmethod
    def _file_results_response(header: str, items: list) -> dict:
        """Build a file_results response dict for the JS renderer."""
        return {
            "type":    "file_results",
            "content": header,
            "items":   items,
        }

    # ── Safe app closer (Win32 WM_CLOSE — never force-terminates) ────────────
    # Blocked set is now the LIVE set from protected.get_protected_keywords()
    # (critical floor + optional shipping defaults + user extras). Looked up
    # per-call so Settings edits take effect without a restart.
    _CLOSE_EXE_MAP = {
        "chrome": "chrome.exe",   "google chrome": "chrome.exe",
        "firefox": "firefox.exe",
        "teams": "teams.exe",     "microsoft teams": "teams.exe",
        "notepad": "notepad.exe", "notepad++": "notepad++.exe",
        "calc": "calc.exe",       "calculator": "calc.exe",
        "outlook": "outlook.exe",
        "word": "winword.exe",    "microsoft word": "winword.exe",
        "excel": "excel.exe",     "microsoft excel": "excel.exe",
        "powerpoint": "powerpnt.exe",
        "spotify": "spotify.exe",
        "slack": "slack.exe",
        "discord": "discord.exe",
        "zoom": "zoom.exe",
        "vscode": "code.exe",     "vs code": "code.exe",
        "visual studio code": "code.exe",
        "terminal": "wt.exe",     "windows terminal": "wt.exe",
        "paint": "mspaint.exe",   "snipping tool": "snippingtool.exe",
    }

    def close_app(self, name: str, confirm_token: str = "") -> dict:
        """Gracefully close an application by sending WM_CLOSE to its windows.
        Never force-terminates. Blocks security tools and critical system processes.
        Uses EXACT exe-name match. When multiple distinct processes match, the
        caller must confirm by passing a token returned from a prior call.
        """
        import ctypes, uuid
        from ctypes import wintypes

        name_lower = name.strip().lower()

        import protected
        _blocked = protected.get_protected_keywords()
        if any(b in name_lower for b in _blocked):
            return {"ok": False, "msg": f"**{name}** is a protected process — it cannot be closed from here."}

        target_exe = self._CLOSE_EXE_MAP.get(name_lower)
        if not target_exe:
            target_exe = (name_lower if name_lower.endswith(".exe")
                          else name_lower.replace(" ", "") + ".exe")
        target_exe = target_exe.lower()

        # Enumerate matching visible windows WITHOUT closing anything yet, so
        # we can confirm when more than one process matches.
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        matches: list[tuple[int, int, str]] = []  # (hwnd, pid, exe)
        EnumWinProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        @EnumWinProc
        def _collect(hwnd, _):
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD(0)
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            hp = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not hp:
                return True
            try:
                buf = ctypes.create_unicode_buffer(260)
                sz  = ctypes.c_ulong(260)
                ctypes.windll.kernel32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(sz))
                import os as _os
                exe = _os.path.basename(buf.value).lower()
                if exe == target_exe:
                    matches.append((hwnd, pid.value, exe))
            finally:
                ctypes.windll.kernel32.CloseHandle(hp)
            return True

        try:
            ctypes.windll.user32.EnumWindows(_collect, 0)
        except Exception as e:
            return {"ok": False, "msg": f"Error: {e}"}

        if not matches:
            return {"ok": False, "msg": f"No visible windows found for **{name}**. It may not be running."}

        distinct_pids = {pid for _, pid, _ in matches}

        # More than one DISTINCT process → require confirmation.
        if len(distinct_pids) > 1 and not confirm_token:
            token = uuid.uuid4().hex[:12]
            hwnds = [h for h, _, _ in matches]
            def _do_close():
                WM_CLOSE = 0x0010
                for h in hwnds:
                    ctypes.windll.user32.PostMessageW(h, WM_CLOSE, 0, 0)
                return {"ok": True, "msg": f"Closed {len(hwnds)} windows of **{name}**."}
            self._pending_confirmations[token] = _do_close
            return {
                "ok": False,
                "needs_confirm": True,
                "token": token,
                "msg": f"**{name}** has {len(distinct_pids)} separate processes "
                       f"with {len(hwnds)} visible windows. Close all of them?",
                "window_count": len(hwnds),
                "process_count": len(distinct_pids),
            }

        WM_CLOSE = 0x0010
        for hwnd, _, _ in matches:
            ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        n = len(matches)
        return {"ok": True, "msg": f"Sent close signal to {n} window{'s' if n > 1 else ''} of **{name}**."}

    def confirm_action(self, token: str) -> dict:
        """Execute a previously queued confirmation action (e.g. from JS confirm dialog)."""
        fn = self._pending_confirmations.pop(token, None)
        if not fn:
            return {"ok": False, "msg": "Action expired or not found."}
        try:
            return fn()
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ── Recycle Bin ───────────────────────────────────────────────────────────
    def get_recycle_bin(self) -> dict:
        """Return current Recycle Bin item count and total size."""
        try:
            import ctypes
            import ctypes.wintypes as wt

            class _RBINFO(ctypes.Structure):
                _fields_ = [("cbSize", wt.DWORD),
                             ("i64Size", ctypes.c_int64),
                             ("i64NumItems", ctypes.c_int64)]

            info = _RBINFO()
            info.cbSize = ctypes.sizeof(_RBINFO)
            ret = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
            if ret == 0:
                mb = info.i64Size / 1024 / 1024
                size_str = (f"{mb:.1f} MB" if mb < 1000
                            else f"{mb/1024:.2f} GB")
                return {"ok": True, "items": int(info.i64NumItems),
                        "size_mb": round(mb, 1), "size_str": size_str}
        except Exception:
            pass
        return {"ok": True, "items": 0, "size_mb": 0, "size_str": "0 MB"}

    def empty_recycle_bin(self, confirm_token: str = "") -> dict:
        """Empty the Windows Recycle Bin.
        First call returns a confirmation token with the current item count +
        size; second call with that token performs the delete. Prevents
        accidental one-click wipes.
        """
        import ctypes, uuid

        def _do_empty():
            SHERB_NOCONFIRMATION = 0x1
            SHERB_NOPROGRESSUI   = 0x2
            SHERB_NOSOUND        = 0x4
            ret = ctypes.windll.shell32.SHEmptyRecycleBinW(
                None, None,
                SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND,
            )
            if ret == 0:
                return {"ok": True, "msg": "Recycle Bin emptied."}
            if ret in (0x80070057, -2147418113):
                return {"ok": True, "msg": "Recycle Bin is already empty."}
            return {"ok": False, "msg": f"SHEmptyRecycleBin returned {ret:#x}."}

        if confirm_token:
            fn = self._pending_confirmations.pop(confirm_token, None)
            if not fn:
                return {"ok": False, "msg": "Confirmation expired. Try again."}
            try:
                return fn()
            except Exception as e:
                return {"ok": False, "msg": str(e)}

        info = self.get_recycle_bin()
        items = info.get("items", 0)
        size_str = info.get("size_str", "0 MB")
        if items == 0:
            return _do_empty()  # nothing to lose, run inline

        token = uuid.uuid4().hex[:12]
        self._pending_confirmations[token] = _do_empty
        return {
            "ok": False,
            "needs_confirm": True,
            "token": token,
            "items": items,
            "size_str": size_str,
            "msg": f"Permanently delete {items} item(s) ({size_str}) from the Recycle Bin? "
                   f"This cannot be undone.",
        }

    def delete_to_recycle_bin(self, path: str) -> dict:
        """Send a single file to the Recycle Bin (reversible). Refuses protected
        and IT-managed paths. Never deletes permanently — always undoable."""
        try:
            import scanner as _sc
            p = os.path.abspath(path)
            if not os.path.exists(p):
                return {"ok": False, "msg": "File no longer exists."}
            if os.path.isdir(p):
                return {"ok": False, "msg": "Folders are not deleted here — only files."}
            if _sc.is_protected(p):
                return {"ok": False, "msg": "Protected location — refused."}
            low = os.path.basename(p).lower()
            import protected
            if any(k in low for k in protected.get_protected_keywords()):
                return {"ok": False, "msg": "IT-managed file — refused."}

            # SHFileOperationW with FOF_ALLOWUNDO sends to the Recycle Bin.
            import ctypes
            from ctypes import wintypes
            FO_DELETE = 0x0003
            FOF_ALLOWUNDO = 0x0040
            FOF_NOCONFIRMATION = 0x0010
            FOF_SILENT = 0x0004
            FOF_NOERRORUI = 0x0400

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [
                    ("hwnd", wintypes.HWND),
                    ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_uint16),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", wintypes.LPVOID),
                    ("lpszProgressTitle", wintypes.LPCWSTR),
                ]

            op = SHFILEOPSTRUCTW()
            op.hwnd = None
            op.wFunc = FO_DELETE
            op.pFrom = p + "\x00\x00"            # must be double-null terminated
            op.pTo = None
            op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
            ret = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
            if ret == 0 and not op.fAnyOperationsAborted:
                return {"ok": True, "msg": f"Moved to Recycle Bin: {os.path.basename(p)}"}
            return {"ok": False, "msg": f"Delete failed (code {ret:#x})."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def reveal_in_explorer(self, path: str) -> dict:
        """Open Explorer with the file selected (reveal), not the file itself."""
        try:
            p = os.path.abspath(path)
            if not os.path.exists(p):
                return {"ok": False, "msg": "File no longer exists."}
            import subprocess as _sp
            _sp.Popen(["explorer", "/select,", p])
            return {"ok": True, "msg": "Opened in Explorer."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ── Extra storage queries (lazy-loaded on demand) ─────────────────────────
    def get_large_files(self, min_mb: int = 10) -> dict:
        import sys_executor as sx
        return sx.get_large_files(min_size_mb=int(min_mb))

    def get_old_files(self, days: int = 180) -> dict:
        import sys_executor as sx
        return sx.get_old_files(days=int(days))

    # ── Privacy & Security queries ────────────────────────────────────────────
    def load_privacy(self) -> dict:
        """Run all privacy/security scans in parallel and return combined results."""
        import sys_executor as sx
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            f_cache   = pool.submit(sx.get_browser_cache_sizes)
            f_net     = pool.submit(sx.get_network_connections)
            f_sens    = pool.submit(sx.get_sensitive_files)
            cache_res = f_cache.result(timeout=20)
            net_res   = f_net.result(timeout=15)
            sens_res  = f_sens.result(timeout=15)
        return {
            "ok":          True,
            "browserCache": cache_res.get("browsers", {}),
            "connections":  net_res.get("connections", []),
            "sensitiveFiles": sens_res.get("files", []),
            "sensitiveCount": len(sens_res.get("files", [])),
        }

    # ── Storage analysis ─────────────────────────────────────────────────────
    def get_folder_sizes(self, path: str = "") -> dict:
        """Return top-level subfolder sizes for treemap."""
        import os, sys_executor as sx
        base = path or os.path.expanduser("~")
        if not os.path.isdir(base):
            return {"ok": False, "folders": []}
        results = []
        try:
            for entry in os.scandir(base):
                if not entry.is_dir(follow_symlinks=False):
                    continue
                name = entry.name
                # Skip hidden, system, OneDrive sub-paths that are huge
                if name.startswith(".") or name.lower() in {"appdata", "ntuser.dat"}:
                    continue
                size = 0
                try:
                    for root, dirs, files in os.walk(entry.path):
                        dirs[:] = [d for d in dirs if not d.startswith(".")]
                        for f in files:
                            try:
                                size += os.path.getsize(os.path.join(root, f))
                            except OSError:
                                pass
                        if size > 50 * 1024 ** 3:  # cap at 50 GB per folder
                            break
                except (PermissionError, OSError):
                    pass
                if size > 0:
                    results.append({"name": name, "path": entry.path, "size": size,
                                    "size_str": fmt_size(size)})
        except (PermissionError, OSError):
            pass
        results.sort(key=lambda x: x["size"], reverse=True)
        return {"ok": True, "base": base, "folders": results[:40]}

    def get_file_type_breakdown(self) -> dict:
        """Return file count + total size grouped by category in user home."""
        import os
        EXT_MAP = {
            "Documents": {".doc",".docx",".xls",".xlsx",".ppt",".pptx",".pdf",".txt",".csv",".odt",".ods",".odp",".rtf",".md"},
            "Images":    {".jpg",".jpeg",".png",".gif",".bmp",".svg",".webp",".tiff",".ico",".heic",".heif",".raw"},
            "Videos":    {".mp4",".mkv",".avi",".mov",".wmv",".flv",".webm",".m4v",".mpg",".mpeg",".3gp"},
            "Audio":     {".mp3",".wav",".flac",".aac",".ogg",".wma",".m4a",".opus"},
            "Archives":  {".zip",".rar",".7z",".tar",".gz",".bz2",".xz",".cab",".iso"},
            "Code":      {".py",".js",".ts",".html",".css",".json",".xml",".yaml",".yml",".sh",".bat",".ps1",".java",".cpp",".c",".h",".go",".rs",".rb",".php"},
            "Executables":{".exe",".msi",".dll",".sys",".jar"},
        }
        counts = {k: 0 for k in EXT_MAP}
        sizes  = {k: 0 for k in EXT_MAP}
        counts["Other"] = 0; sizes["Other"] = 0
        base = os.path.expanduser("~")
        try:
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d.lower() not in {"appdata",".git","node_modules"}
                           and not d.startswith(".")]
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    try: sz = os.path.getsize(os.path.join(root, f))
                    except OSError: sz = 0
                    matched = False
                    for cat, exts in EXT_MAP.items():
                        if ext in exts:
                            counts[cat] += 1; sizes[cat] += sz; matched = True; break
                    if not matched:
                        counts["Other"] += 1; sizes["Other"] += sz
        except (PermissionError, OSError):
            pass
        COLORS = {"Documents":"#60A5FA","Images":"#34D399","Videos":"#A78BFA",
                  "Audio":"#F59E0B","Archives":"#F87171","Code":"#00D4AA",
                  "Executables":"#EC4899","Other":"#6B7A99"}
        items = [{"cat": k, "count": counts[k], "size": sizes[k],
                  "size_str": fmt_size(sizes[k]), "color": COLORS.get(k,"#6B7A99")}
                 for k in list(EXT_MAP.keys()) + ["Other"] if counts[k] > 0]
        items.sort(key=lambda x: x["size"], reverse=True)
        return {"ok": True, "items": items}

    def get_downloads_aging(self) -> dict:
        """Return Downloads folder files grouped by age bucket."""
        import os, time as _time
        dl = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.isdir(dl):
            return {"ok": False, "buckets": [], "total": 0}
        now = _time.time()
        buckets = {"<30d": 0, "30–90d": 0, "90–180d": 0, ">180d": 0}
        sizes   = {"<30d": 0, "30–90d": 0, "90–180d": 0, ">180d": 0}
        total   = 0
        for entry in os.scandir(dl):
            try:
                sz = entry.stat().st_size
                age_days = (now - entry.stat().st_mtime) / 86400
                total += 1
                if age_days < 30:   buckets["<30d"] += 1;   sizes["<30d"] += sz
                elif age_days < 90: buckets["30–90d"] += 1; sizes["30–90d"] += sz
                elif age_days < 180:buckets["90–180d"] +=1; sizes["90–180d"] += sz
                else:               buckets[">180d"] += 1;  sizes[">180d"] += sz
            except OSError:
                pass
        result = [{"label": k, "count": buckets[k], "size": sizes[k],
                   "size_str": fmt_size(sizes[k])} for k in buckets]
        return {"ok": True, "buckets": result, "total": total, "path": dl}

    # ── Security / Software queries ───────────────────────────────────────────
    def get_scheduled_tasks(self) -> dict:
        """Return non-Microsoft scheduled tasks via PowerShell."""
        import subprocess, json as _json
        ps = r"""
$ErrorActionPreference='SilentlyContinue'
Get-ScheduledTask | Where-Object {
    $_.TaskPath -notlike '\Microsoft\*' -and
    $_.TaskPath -notlike '\MicrosoftEdgeUpdate*' -and
    $_.State -ne 'Disabled'
} | ForEach-Object {
    $a = $_.Actions | Select-Object -First 1
    [PSCustomObject]@{
        Name    = $_.TaskName
        Path    = $_.TaskPath
        State   = $_.State.ToString()
        Execute = if($a.Execute){$a.Execute}else{''}
        Author  = $_.Author
    }
} | ConvertTo-Json -Depth 2
"""
        try:
            r = run_hidden.run(["powershell","-WindowStyle","Hidden","-NoProfile","-Command",ps],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
            raw = _json.loads(r.stdout or "[]")
            if isinstance(raw, dict): raw = [raw]
            tasks = [{"name": t.get("Name",""), "path": t.get("Path",""),
                      "state": t.get("State",""), "execute": t.get("Execute",""),
                      "author": t.get("Author","")} for t in (raw or [])]
            return {"ok": True, "tasks": tasks[:50]}
        except Exception as e:
            return {"ok": False, "tasks": [], "error": str(e)}

    def get_installed_software(self, days: int = 90) -> dict:
        """Return software installed in the last N days."""
        import subprocess, json as _json
        ps = f"""
$ErrorActionPreference='SilentlyContinue'
$cutoff=(Get-Date).AddDays(-{int(days)})
$paths=@(
  'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',
  'HKLM:\\Software\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',
  'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'
)
$paths | ForEach-Object {{
  Get-ItemProperty $_ -ErrorAction SilentlyContinue
}} | Where-Object {{
  $_.DisplayName -and $_.InstallDate -and
  [datetime]::ParseExact($_.InstallDate,'yyyyMMdd',$null) -ge $cutoff
}} | Select-Object DisplayName,InstallDate,Publisher,DisplayVersion,EstimatedSize |
Sort-Object InstallDate -Descending | Select-Object -First 50 | ConvertTo-Json -Depth 2
"""
        try:
            r = run_hidden.run(["powershell","-WindowStyle","Hidden","-NoProfile","-Command",ps],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
            raw = _json.loads(r.stdout or "[]")
            if isinstance(raw, dict): raw = [raw]
            items = [{"name": x.get("DisplayName",""), "date": x.get("InstallDate",""),
                      "publisher": x.get("Publisher",""), "version": x.get("DisplayVersion",""),
                      "size_kb": x.get("EstimatedSize",0) or 0}
                     for x in (raw or []) if x.get("DisplayName")]
            return {"ok": True, "items": items, "days": days}
        except Exception as e:
            return {"ok": False, "items": [], "error": str(e)}

    def get_windows_update_status(self) -> dict:
        """Return last update date and pending update count."""
        import subprocess, json as _json
        ps = r"""
$ErrorActionPreference='SilentlyContinue'
$sess=New-Object -ComObject Microsoft.Update.Session
$searcher=$sess.CreateUpdateSearcher()
$hist=$searcher.QueryHistory(0,$searcher.GetTotalHistoryCount())
$last=$hist | Where-Object{$_.ResultCode -eq 2} | Sort-Object Date -Descending | Select-Object -First 1
try{$pending=($searcher.Search("IsInstalled=0 and IsHidden=0")).Updates.Count}catch{$pending=-1}
@{
  LastUpdate=if($last){$last.Date.ToString("yyyy-MM-dd HH:mm")}else{'Unknown'}
  LastTitle=if($last){$last.Title}else{''}
  Pending=$pending
} | ConvertTo-Json
"""
        try:
            r = run_hidden.run(["powershell","-WindowStyle","Hidden","-NoProfile","-Command",ps],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=25)
            d = _json.loads(r.stdout or "{}")
            return {"ok": True, "last_update": d.get("LastUpdate","Unknown"),
                    "last_title": d.get("LastTitle",""), "pending": d.get("Pending",-1)}
        except Exception as e:
            return {"ok": False, "last_update": "Unknown", "pending": -1, "error": str(e)}

    # ── Performance queries ───────────────────────────────────────────────────
    def get_boot_times(self) -> dict:
        """Return last 10 boot times from Windows event log (Event ID 100)."""
        import subprocess, json as _json
        ps = r"""
$ErrorActionPreference='SilentlyContinue'
Get-WinEvent -ProviderName Microsoft-Windows-Diagnostics-Performance -ErrorAction SilentlyContinue |
  Where-Object{$_.Id -eq 100} | Select-Object -First 10 |
  ForEach-Object{
    $xml=[xml]$_.ToXml()
    $boot=$xml.Event.UserData.BootPerfInfo.BootTime
    [PSCustomObject]@{
      Date=$_.TimeCreated.ToString("yyyy-MM-dd HH:mm")
      BootMs=if($boot){[int]$boot}else{0}
    }
  } | ConvertTo-Json -Depth 2
"""
        try:
            r = run_hidden.run(["powershell","-WindowStyle","Hidden","-NoProfile","-Command",ps],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20)
            raw = _json.loads(r.stdout or "[]")
            if isinstance(raw, dict): raw = [raw]
            items = [{"date": x.get("Date",""), "boot_ms": x.get("BootMs",0)} for x in (raw or [])]
            return {"ok": True, "items": items}
        except Exception as e:
            return {"ok": False, "items": [], "error": str(e)}

    def get_power_plans(self) -> dict:
        """Return available power plans and the active one."""
        import subprocess, re as _re
        try:
            r = run_hidden.run(["powershell","-WindowStyle","Hidden","-NoProfile","-Command",
                                "powercfg /list"],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
            plans = []
            active = None
            for line in r.stdout.splitlines():
                m = _re.search(r'([0-9a-f-]{36})\s+\((.+?)\)\s*(\*)?', line, _re.IGNORECASE)
                if m:
                    guid, name, star = m.group(1), m.group(2).strip(), m.group(3)
                    plans.append({"guid": guid, "name": name, "active": bool(star)})
                    if star:
                        active = guid
            return {"ok": True, "plans": plans, "active": active}
        except Exception as e:
            return {"ok": False, "plans": [], "active": None, "error": str(e)}

    def set_power_plan(self, guid: str) -> dict:
        """Switch to a different power plan (low-risk action)."""
        import re as _re, subprocess
        if not _re.fullmatch(r'[0-9a-f-]{36}', guid.lower()):
            return {"ok": False, "msg": "Invalid GUID format."}
        try:
            run_hidden.run(["powercfg", "/setactive", guid],
                           capture_output=True, timeout=10, check=True)
            return {"ok": True, "msg": "Power plan changed."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def capture_metrics_snapshot(self, label: str = "") -> dict:
        """Freeze a lightweight snapshot of current CPU/RAM/disk free + process
        memory total, tagged with a user label. Used to show before/after
        after a Boost or cleanup. Snapshots stay in memory for this session.
        """
        import psutil
        import shutil as _shutil
        vm = psutil.virtual_memory()
        du = _shutil.disk_usage("C:\\")
        snap = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "label": label or "snapshot",
            "cpu_pct": round(psutil.cpu_percent(interval=None)),
            "ram_pct": round(vm.percent),
            "ram_used_mb": round(vm.used / (1 << 20)),
            "disk_free_gb": round(du.free / (1 << 30), 1),
            "proc_count": len(psutil.pids()),
        }
        if not hasattr(self, "_snapshots"):
            self._snapshots = []
        self._snapshots.append(snap)
        self._snapshots = self._snapshots[-20:]
        return {"ok": True, "snapshot": snap}

    def compare_snapshots(self, before_label: str = "", after_label: str = "") -> dict:
        """Return a delta of the two most recent snapshots matching the labels
        (or just the last two if labels are omitted)."""
        snaps = getattr(self, "_snapshots", [])
        if len(snaps) < 2:
            return {"ok": False, "msg": "Need at least two snapshots."}
        if before_label and after_label:
            before = next((s for s in reversed(snaps) if s["label"] == before_label), None)
            after  = next((s for s in reversed(snaps) if s["label"] == after_label),  None)
        else:
            before, after = snaps[-2], snaps[-1]
        if not before or not after:
            return {"ok": False, "msg": "Snapshot not found."}
        delta = {k: after[k] - before[k] for k in
                 ("cpu_pct","ram_pct","ram_used_mb","disk_free_gb","proc_count")}
        return {"ok": True, "before": before, "after": after, "delta": delta}

    # ── Per-app profiles (saved setting bundles) ─────────────────────────────
    _PROFILE_KEYS = ("ai_enabled", "protected_apps_defaults", "protected_apps_extra",
                     "schedule_enabled", "schedule_cadence", "schedule_actions",
                     "health_watch_enabled", "health_watch_disk_gb",
                     "health_watch_ram_pct", "theme", "scan_on_startup")

    def _load_profiles(self) -> dict:
        import json as _json
        try:
            return _json.loads(db.get_setting("profiles_json") or "{}") or {}
        except Exception:
            return {}

    def _save_profiles(self, data: dict):
        import json as _json
        db.set_setting("profiles_json",
                       _json.dumps(data, ensure_ascii=False))

    def list_profiles(self) -> dict:
        return {"profiles": sorted(self._load_profiles().keys()),
                "active": db.get_setting("active_profile") or ""}

    def save_profile(self, name: str) -> dict:
        name = (name or "").strip()
        if not name or len(name) > 40:
            return {"ok": False, "msg": "Name must be 1-40 chars."}
        s = db.get_all_settings()
        snap = {k: s.get(k, "") for k in self._PROFILE_KEYS}
        profiles = self._load_profiles()
        profiles[name] = snap
        self._save_profiles(profiles)
        db.set_setting("active_profile", name)
        return {"ok": True, "name": name, "snapshot": snap}

    def apply_profile(self, name: str) -> dict:
        profiles = self._load_profiles()
        snap = profiles.get(name)
        if not snap:
            return {"ok": False, "msg": "No such profile."}
        filtered = {k: v for k, v in snap.items() if k in self._PROFILE_KEYS}
        db.set_settings(filtered)
        db.set_setting("active_profile", name)
        return {"ok": True, "applied": filtered}

    def delete_profile(self, name: str) -> dict:
        profiles = self._load_profiles()
        if name not in profiles:
            return {"ok": False, "msg": "No such profile."}
        del profiles[name]
        self._save_profiles(profiles)
        if db.get_setting("active_profile") == name:
            db.set_setting("active_profile", "")
        return {"ok": True}

    _BROWSER_CACHES = {
        "edge":    [r"Microsoft\Edge\User Data\Default\Cache",
                    r"Microsoft\Edge\User Data\Default\Code Cache"],
        "chrome":  [r"Google\Chrome\User Data\Default\Cache",
                    r"Google\Chrome\User Data\Default\Code Cache"],
        "firefox": [r"Mozilla\Firefox\Profiles"],
        "brave":   [r"BraveSoftware\Brave-Browser\User Data\Default\Cache"],
    }

    def scan_browser_cache(self) -> dict:
        """Measure cache size per detected browser. No deletes."""
        local = os.environ.get("LOCALAPPDATA", "")
        roaming = os.environ.get("APPDATA", "")
        results = {}
        for browser, paths in self._BROWSER_CACHES.items():
            total = 0
            found = False
            for rel in paths:
                for root in (local, roaming):
                    if not root: continue
                    p = os.path.join(root, rel)
                    if not os.path.isdir(p): continue
                    found = True
                    for dirpath, _, files in os.walk(p):
                        for fn in files:
                            try:
                                total += os.path.getsize(os.path.join(dirpath, fn))
                            except OSError:
                                pass
            if found:
                results[browser] = {"size_bytes": total,
                                    "size_fmt": fmt_size(total)}
        return {"ok": True, "browsers": results}

    def clean_browser_cache(self, browser: str, confirm_token: str = "") -> dict:
        """Two-call confirm. Only deletes files inside the browser cache dirs,
        never the profile itself. Refuses if the browser process is running."""
        import uuid, psutil
        browser = (browser or "").strip().lower()
        if browser not in self._BROWSER_CACHES:
            return {"ok": False, "msg": "Unknown browser."}

        proc_map = {"edge": "msedge.exe", "chrome": "chrome.exe",
                    "firefox": "firefox.exe", "brave": "brave.exe"}
        target_proc = proc_map[browser]
        running = any(p.info.get("name","").lower() == target_proc
                      for p in psutil.process_iter(attrs=["name"]))
        if running:
            return {"ok": False, "msg": f"{browser.title()} is running — close it first."}

        local = os.environ.get("LOCALAPPDATA", "")
        roaming = os.environ.get("APPDATA", "")
        targets = []
        for rel in self._BROWSER_CACHES[browser]:
            for root in (local, roaming):
                if not root: continue
                p = os.path.join(root, rel)
                if os.path.isdir(p):
                    targets.append(p)
        if not targets:
            return {"ok": False, "msg": "No cache directory found."}

        if not confirm_token or confirm_token not in self._pending_confirmations:
            tok = str(uuid.uuid4())
            self._pending_confirmations[tok] = ("clean_cache", browser, targets)
            size = 0
            for t in targets:
                for dp, _, files in os.walk(t):
                    for fn in files:
                        try: size += os.path.getsize(os.path.join(dp, fn))
                        except OSError: pass
            return {"needs_confirm": True, "token": tok,
                    "msg": f"Delete {fmt_size(size)} of {browser} cache?",
                    "size_bytes": size, "paths": targets}

        _, bro, paths = self._pending_confirmations.pop(confirm_token)
        if bro != browser:
            return {"ok": False, "msg": "Token mismatch."}
        import shutil as _sh
        removed = 0
        for t in paths:
            for entry in os.listdir(t):
                full = os.path.join(t, entry)
                try:
                    if os.path.isdir(full):
                        _sh.rmtree(full, ignore_errors=True)
                    else:
                        os.remove(full)
                    removed += 1
                except Exception:
                    pass
        return {"ok": True, "msg": f"Cleared {removed} cache entries for {browser}."}

    def generate_battery_report(self) -> dict:
        """Run `powercfg /batteryreport` and return the output HTML path.
        No admin needed. On desktops without a battery, powercfg emits an
        error message and we surface it verbatim."""
        out_dir = LOGS / "battery"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = out_dir / f"battery-report-{stamp}.html"
        try:
            r = run_hidden.run(
                ["powercfg", "/batteryreport", "/output", str(out_path)],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30,
            )
            if out_path.exists():
                return {"ok": True, "path": str(out_path),
                        "msg": "Battery report saved."}
            msg = ((r.stdout or "") + " " + (r.stderr or "")).strip()
            return {"ok": False, "msg": msg or "powercfg produced no file."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_battery_capacity(self) -> dict:
        """Return design vs full-charge capacity + wear % from WMI."""
        ps = (
            "$s=Get-WmiObject -Class BatteryStaticData -Namespace root\\WMI "
            "-ErrorAction SilentlyContinue;"
            "$f=Get-WmiObject -Class BatteryFullChargedCapacity -Namespace root\\WMI "
            "-ErrorAction SilentlyContinue;"
            "if($s -and $f){"
            "$design=[int]($s | Select-Object -First 1).DesignedCapacity;"
            "$full=[int]($f | Select-Object -First 1).FullChargedCapacity;"
            "$wear=if($design -gt 0){[math]::Round((1-($full/$design))*100,1)}else{0};"
            "@{design=$design;full=$full;wear=$wear} | ConvertTo-Json"
            "}"
        )
        import json as _json
        try:
            r = run_hidden.run(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10,
            )
            d = _json.loads(r.stdout or "{}")
            if not d:
                return {"ok": False, "msg": "No battery data (desktop or WMI unavailable)."}
            return {"ok": True, "design_mwh": d.get("design"), "full_mwh": d.get("full"),
                    "wear_pct": d.get("wear")}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def boost_preview(self) -> dict:
        """List candidate memory-hog processes that Boost could offer to close.
        Does NOT close anything. Caller must confirm per-process via close_app.
        """
        import psutil
        import protected
        from thresholds import PROCESS_RAM_HIGH_MB
        blocked = protected.get_protected_keywords()
        self_pid = os.getpid()
        self_names = {"python.exe", "pythonw.exe", "helix.exe"}
        candidates = []
        for p in psutil.process_iter(attrs=["pid", "name", "memory_info"]):
            try:
                info = p.info
                name = (info.get("name") or "").lower()
                if not name or info["pid"] == self_pid or name in self_names:
                    continue
                if any(b in name for b in blocked):
                    continue
                mem_mb = info["memory_info"].rss // (1024 * 1024)
                if mem_mb >= PROCESS_RAM_HIGH_MB:
                    candidates.append({"pid": info["pid"], "name": info.get("name", ""),
                                       "mem_mb": int(mem_mb)})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        candidates.sort(key=lambda x: x["mem_mb"], reverse=True)
        return {"ok": True, "candidates": candidates[:10],
                "note": "Boost never closes anything automatically — review the list first."}

    # ── Startup entries: disable / re-enable with undo ───────────────────────
    _STARTUP_HIVES = {
        "HKCU": r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
    }
    _STARTUP_DISABLED_SUBKEY = r"SOFTWARE\PCHealthAgent\DisabledStartup"

    def _startup_backup_path(self):
        return LOGS / "startup-disabled.json"

    def _load_disabled_startup(self) -> dict:
        import json as _json
        p = self._startup_backup_path()
        try:
            if p.exists():
                return _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_disabled_startup(self, data: dict):
        import json as _json
        p = self._startup_backup_path()
        p.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def disable_startup(self, name: str) -> dict:
        """Remove an HKCU Run entry and remember its command for undo."""
        import winreg
        if not name or any(c in name for c in '\\/\x00'):
            return {"ok": False, "msg": "Invalid name."}
        subkey = self._STARTUP_HIVES["HKCU"]
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey, 0,
                                winreg.KEY_READ | winreg.KEY_WRITE) as k:
                try:
                    val, typ = winreg.QueryValueEx(k, name)
                except FileNotFoundError:
                    return {"ok": False, "msg": "Entry not found in HKCU Run."}
                data = self._load_disabled_startup()
                data[name] = {"hive": "HKCU", "subkey": subkey,
                              "value": val, "type": int(typ),
                              "disabled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                self._save_disabled_startup(data)
                winreg.DeleteValue(k, name)
            return {"ok": True, "msg": f"Disabled startup entry '{name}'."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def enable_startup(self, name: str) -> dict:
        """Restore a previously disabled HKCU Run entry from the backup log."""
        import winreg
        data = self._load_disabled_startup()
        entry = data.get(name)
        if not entry:
            return {"ok": False, "msg": "No undo record for that entry."}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, entry["subkey"], 0,
                                winreg.KEY_WRITE) as k:
                winreg.SetValueEx(k, name, 0, entry.get("type", winreg.REG_SZ),
                                  entry["value"])
            data.pop(name, None)
            self._save_disabled_startup(data)
            return {"ok": True, "msg": f"Restored startup entry '{name}'."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def list_disabled_startup(self) -> dict:
        data = self._load_disabled_startup()
        return {"items": [{"name": k, **v} for k, v in data.items()]}

    def create_restore_point(self, description: str = "Helix pre-maintenance") -> dict:
        """Request a Windows System Restore point. Requires System Protection
        enabled on C: and admin elevation; returns a readable error otherwise.
        Rate-limited by Windows to once every ~24h unless registry override.
        """
        desc = (description or "Helix").replace('"', '').replace("'", "")[:60]
        ps = (
            "try {"
            f"Checkpoint-Computer -Description \"{desc}\" "
            "-RestorePointType 'MODIFY_SETTINGS' -ErrorAction Stop;"
            "'ok'"
            "} catch { $_.Exception.Message }"
        )
        try:
            r = run_hidden.run(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60,
            )
            out = (r.stdout or "").strip()
            if out == "ok":
                return {"ok": True, "msg": f"Restore point '{desc}' created."}
            return {"ok": False, "msg": out or (r.stderr or "").strip() or "Unknown error."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def list_restore_points(self) -> dict:
        """Return existing restore points (ID, date, description)."""
        import json as _json
        ps = (
            "Get-ComputerRestorePoint -ErrorAction SilentlyContinue | "
            "Select-Object SequenceNumber,Description,"
            "@{N='Date';E={$_.ConvertToDateTime($_.CreationTime).ToString('yyyy-MM-dd HH:mm')}} | "
            "ConvertTo-Json -Depth 2"
        )
        try:
            r = run_hidden.run(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20,
            )
            raw = _json.loads(r.stdout or "[]")
            if isinstance(raw, dict): raw = [raw]
            items = [{"id": x.get("SequenceNumber"), "desc": x.get("Description",""),
                      "date": x.get("Date","")} for x in (raw or [])]
            return {"ok": True, "items": items}
        except Exception as e:
            return {"ok": False, "items": [], "error": str(e)}

    def get_smart_info(self) -> dict:
        """Return basic drive health via WMI SMART status."""
        import subprocess, json as _json
        ps = r"""
$ErrorActionPreference='SilentlyContinue'
Get-WmiObject -Namespace root\wmi -Class MSStorageDriver_FailurePredictStatus -ErrorAction SilentlyContinue |
ForEach-Object{
  $id=$_.InstanceName -replace '.*\\','' -replace '_\d+$',''
  [PSCustomObject]@{InstanceName=$_.InstanceName;PredictFailure=$_.PredictFailure;Reason=$_.Reason}
} | ConvertTo-Json -Depth 2
"""
        try:
            r = run_hidden.run(["powershell","-WindowStyle","Hidden","-NoProfile","-Command",ps],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
            raw = _json.loads(r.stdout or "[]")
            if isinstance(raw, dict): raw = [raw]
            items = [{"instance": x.get("InstanceName",""), "predict_failure": x.get("PredictFailure",False),
                      "reason": x.get("Reason",0)} for x in (raw or [])]
            return {"ok": True, "items": items}
        except Exception as e:
            return {"ok": False, "items": [], "error": str(e)}

    def chat(self, message: str, lang: str = "en", _popup_window=None) -> dict:
        """Handle a chat message. Returns {type, content, action?, page?, extra?}.

        _popup_window: if given, Ollama async callback goes to that window instead of self._window.
        """
        import assistant as asst
        import monitor as mon_mod

        if not message or not message.strip():
            return {"type": "error", "content": "Please type a message."}

        # ── Follow-up: "open the Nth result" ──────────────────────────────────
        import re as _re
        _ordinal_map = {
            "first": 0, "1st": 0, "second": 1, "2nd": 1,
            "third": 2, "3rd": 2, "fourth": 3, "4th": 3,
            "fifth": 4, "5th": 4, "sixth": 5, "6th": 5,
            "seventh": 6, "7th": 6, "eighth": 7, "8th": 7,
            "ninth": 8, "9th": 8, "tenth": 9, "10th": 9,
        }
        _m_ord = _re.search(
            r'open\s+(?:the\s+)?(' + '|'.join(_ordinal_map) + r'|\d+)(?:st|nd|rd|th)?\s*(?:result|one|file|folder)?',
            message.lower(),
        )
        if _m_ord and self._last_search_items:
            import os
            key = _m_ord.group(1)
            idx = int(key) - 1 if key.isdigit() else _ordinal_map.get(key, -1)
            if 0 <= idx < len(self._last_search_items):
                item = self._last_search_items[idx]
                res  = self.open_path(item["path"])
                verb = "folder" if item["kind"] == "folder" else "file"
                name = os.path.basename(item["path"])
                if res["ok"]:
                    return {"type": "info", "content": f"Opened {verb} **{name}**."}
                return {"type": "error", "content": res["msg"]}
            return {"type": "error",
                    "content": f"No result #{idx + 1} — last search returned {len(self._last_search_items)} item(s)."}

        # ── Follow-up: "show only *.pdf" / "filter to *.docx" ────────────────
        _m_ext = _re.search(r'(?:show only|filter(?: to)?|only show)\s+[*.]?(\w+)\s*(?:files?)?',
                            message.lower())
        if _m_ext and self._last_search_items:
            import os
            ext_want = '.' + _m_ext.group(1).lstrip('.').lower()
            filtered = [it for it in self._last_search_items
                        if os.path.splitext(it["name"])[1].lower() == ext_want]
            if filtered:
                self._last_search_items = filtered
                return self._file_results_response(
                    f"Filtered to {len(filtered)} **{ext_want}** file(s):", filtered
                )
            return {"type": "info", "content": f"No {ext_want} files in the last results."}

        # ── Multi-step plan detection ─────────────────────────────────────────
        import action_planner as _ap
        if _ap.is_multi_step(message):
            steps    = _ap.build_plan(message)
            plan_id  = _ap.new_plan_id()
            threading.Thread(
                target=self._run_plan_async,
                args=(plan_id, steps, _popup_window),
                daemon=True,
            ).start()
            return {"type": "plan", "plan_id": plan_id, "steps": steps}

        # ── Random file selection ("open a PDF", "find any Excel") ───────────
        _m_rand = _re.search(
            r'\b(open|find|show|get)\s+(?:a|an|any|one|random)\s+(\w+(?:\s+\w+)?)\s*(?:file|document|folder)?$',
            message.lower().strip(),
        )
        if _m_rand:
            import sys_executor as sx, random as _rand
            type_word = _m_rand.group(2).strip()
            res = sx.search_files(type_word)
            if "error" not in res and res["results"]:
                chosen = _rand.choice(res["results"])
                self._last_search_items = [chosen]
                r = self.open_path(chosen["path"])
                verb = "folder" if chosen["kind"] == "folder" else "file"
                if r["ok"]:
                    return {"type": "info",
                            "content": f"Opened {verb} **{chosen['name']}** (randomly selected from {len(res['results'])} match(es))."}
                return {"type": "error", "content": r["msg"]}

        action_id, params = asst.match_intent(message)

        if action_id == "nav":
            page = params["page"]
            names = {"overview": "Dashboard", "storage": "Storage", "monitor": "Monitor",
                     "startup": "Startup", "insights": "Insights", "organize": "Organizer",
                     "events": "Events", "privacy": "Privacy & Security",
                     "troubleshoot": "Troubleshoot",
                     "history": "History", "settings": "Settings", "assistant": "Assistant"}
            return {"type": "action", "action": "navigate", "page": page,
                    "content": f"Opening {names.get(page, page)}."}
        elif action_id == "scan":
            if self._scanning:
                return {"type": "info", "content": "A scan is already running."}
            self.start_scan(params["no_dupes"])
            kind = "quick" if params["no_dupes"] else "full"
            return {"type": "action", "action": "scan_started",
                    "content": f"Starting {kind} scan…"}
        elif action_id == "cancel_scan":
            if not self._scanning:
                return {"type": "info", "content": "No scan is currently running."}
            self.cancel_scan()
            return {"type": "info", "content": "Scan cancelled."}
        elif action_id == "query_health":
            if not self._last_scan_data:
                return {"type": "info", "content": "No scan data yet — run a scan first."}
            d = self._last_scan_data
            score = max(0, 100 - d.get("issues", 0) * 20)
            return {"type": "data", "content":
                    f"Health score: {score}/100 · {d.get('issues', 0)} issue(s) · "
                    f"{d.get('total_freeable_fmt', '—')} reclaimable."}
        elif action_id == "query_disk":
            if not self._last_scan_data:
                return {"type": "info", "content": "Run a scan to get disk info."}
            d = self._last_scan_data
            return {"type": "data", "content":
                    f"Disk: {d['disk']['fmt_free']} free of {d['disk']['total_gb']} GB "
                    f"({d['disk']['pct_free']}% free)."}
        elif action_id == "query_ram":
            try:
                import psutil as _pu
                vm = _pu.virtual_memory()
                used_gb = vm.used / (1 << 30)
                total_gb = vm.total / (1 << 30)
                return {"type": "data", "content":
                        f"RAM: {vm.percent:.0f}% used ({used_gb:.1f} / {total_gb:.0f} GB)."}
            except Exception:
                pass
            return {"type": "info", "content": "Monitor data not available yet — visit the Performance page."}
        elif action_id == "query_cpu":
            try:
                import psutil as _pu
                cpu = _pu.cpu_percent(interval=0.5)
                freq = _pu.cpu_freq()
                ghz = f" @ {freq.current/1000:.2f} GHz" if freq else ""
                return {"type": "data", "content": f"CPU: {cpu:.1f}% used{ghz}."}
            except Exception:
                pass
            return {"type": "info", "content": "CPU data unavailable — visit the Monitor page."}
        elif action_id == "query_uptime":
            try:
                import psutil as _pu, time as _t
                boot = _pu.boot_time()
                secs = int(_t.time() - boot)
                d, rem = divmod(secs, 86400)
                h, rem = divmod(rem, 3600)
                m = rem // 60
                uptime_str = (f"{d}d {h}h {m}m" if d else f"{h}h {m}m")
                from datetime import datetime as _dt
                boot_dt = _dt.fromtimestamp(boot).strftime("%Y-%m-%d %H:%M")
                return {"type": "data", "content":
                        f"Uptime: **{uptime_str}** (last boot: {boot_dt})."}
            except Exception:
                pass
            return {"type": "info", "content": "Uptime data unavailable."}
        elif action_id == "query_issues":
            if not self._last_scan_data:
                return {"type": "info", "content": "Run a scan to see issues."}
            n = self._last_scan_data.get("issues", 0)
            return {"type": "data", "content": f"{n} issue(s) found in the last scan."}
        elif action_id == "query_startup":
            if not self._last_scan_data:
                return {"type": "info", "content": "Run a scan to see startup apps."}
            d = self._last_scan_data
            return {"type": "data", "content":
                    f"{len(d['startup'])} startup apps: "
                    f"{len(d['personal_apps'])} personal, {len(d['it_apps'])} IT-managed."}
        elif action_id == "get_insights":
            return {"type": "action", "action": "navigate", "page": "insights",
                    "content": "Opening Insights and loading suggestions…",
                    "extra": "load_insights"}
        elif action_id == "run_diagnose":
            return {"type": "action", "action": "navigate", "page": "troubleshoot",
                    "content": "Opening Troubleshoot and running diagnostics…",
                    "extra": "run_diagnose"}
        elif action_id == "open_file":
            import sys_executor as sx, re as _re, os as _os
            m = _re.search(r'(?:find\s+and\s+open|open)\s+(.+)', message.lower().strip())
            query = m.group(1).strip() if m else message
            query = _re.sub(r'^(my|a|an|the)\s+', '', query).strip()
            res = sx.search_files(query)
            results = res.get("results", [])
            if not results:
                return {"type": "info",
                        "content": f"No file matching **{query}** found. Try a different name or extension."}
            if len(results) == 1:
                r = self.open_path(results[0]["path"])
                name = _os.path.basename(results[0]["path"])
                if r["ok"]:
                    self._last_search_items = results
                    return {"type": "info", "content": f"Opened **{name}**."}
                return {"type": "error", "content": r["msg"]}
            # Multiple results — auto-open best match (exact name match first)
            ql = query.lower()
            exact = [x for x in results if _os.path.splitext(x["name"].lower())[0] == ql or x["name"].lower() == ql]
            if len(exact) == 1:
                r = self.open_path(exact[0]["path"])
                name = _os.path.basename(exact[0]["path"])
                if r["ok"]:
                    self._last_search_items = exact
                    return {"type": "info", "content": f"Opened **{name}**."}
            self._last_search_items = results
            header = f"Found **{len(results)}** file(s) matching **{query}**. Say \"open the first\" to open one:"
            return self._file_results_response(header, results[:10])
        elif action_id == "open_folder":
            import os as _os, re as _re, subprocess as _sp
            m = _re.search(r'(documents?|downloads?|desktop|pictures?|videos?|music|onedrive)',
                           message.lower())
            name = m.group(1).rstrip("s") if m else "document"
            od = _os.environ.get("OneDrive", "")
            FOLDERS = {
                "document":  _os.path.expanduser("~\\Documents"),
                "download":  _os.path.expanduser("~\\Downloads"),
                "desktop":   _os.path.join(od, "デスクトップ") if (od and _os.path.isdir(_os.path.join(od, "デスクトップ"))) else
                             _os.path.join(od, "Desktop") if (od and _os.path.isdir(_os.path.join(od, "Desktop"))) else
                             _os.path.expanduser("~\\Desktop"),
                "picture":   _os.path.expanduser("~\\Pictures"),
                "video":     _os.path.expanduser("~\\Videos"),
                "music":     _os.path.expanduser("~\\Music"),
                "onedrive":  od if od else _os.path.expanduser("~\\OneDrive"),
            }
            folder = FOLDERS.get(name, FOLDERS.get("document"))
            if folder and _os.path.isdir(folder):
                _os.startfile(folder)
                display = name.replace("document","Documents").replace("download","Downloads") \
                              .replace("desktop","Desktop").replace("picture","Pictures") \
                              .replace("video","Videos").replace("music","Music") \
                              .replace("onedrive","OneDrive").title()
                return {"type": "info", "content": f"Opening **{display}** folder…"}
            return {"type": "error", "content": f"Could not find the **{name.title()}** folder on this PC."}
        elif action_id == "open_app":
            raw = asst.extract_app_name(message)
            msg, ok = asst.find_and_launch(raw)
            return {"type": "info" if ok else "error", "content": msg}
        elif action_id == "close_app":
            raw = asst.extract_close_target(message)
            res = self.close_app(raw)
            return {"type": "info" if res["ok"] else "error", "content": res["msg"]}
        elif action_id == "explain_issue":
            # Build issue context and send to Ollama for explanation
            if not self._last_scan_data:
                return {"type": "info", "content": "No scan data yet — run a scan first, then ask me to explain an issue."}
            issues = _build_issue_details(self._last_scan_data)
            if not issues:
                return {"type": "info", "content": "No issues found in the last scan — your PC looks healthy!"}
            # See if a specific issue number was mentioned
            _m_num = _re.search(r'(\d+)', message)
            idx    = int(_m_num.group(1)) - 1 if _m_num else None
            if idx is not None and 0 <= idx < len(issues):
                issue_ctx = f"Issue #{idx+1}: {issues[idx]['en']}"
            else:
                issue_ctx = "\n".join(f"#{i+1}: {iss['en']}" for i, iss in enumerate(issues[:5]))
            # Fall through to Ollama with enriched context
            context = f"Current issues found:\n{issue_ctx}\n\nUser health summary: {len(issues)} issue(s) total."
            _target = _popup_window

            def _explain():
                resp = asst.ollama_chat(message, context, lang, history=self._chat_history[-8:])
                if resp:
                    self._chat_history.append({"role": "assistant", "content": resp})
                    payload = json.dumps({"type": "text", "content": resp})
                else:
                    payload = json.dumps({"type": "error", "content": "Ollama is not available. Start it with: ollama run qwen2.5:3b"})
                code = f"onChatResponse({payload})"
                if _target:
                    try: _target.evaluate_js(code)
                    except Exception: pass
                else:
                    self._js(code)

            self._chat_history.append({"role": "user", "content": message})
            threading.Thread(target=_explain, daemon=True).start()
            return {"type": "thinking", "content": "Explaining…"}

        # ── System queries (read-only) ─────────────────────────────────────
        elif action_id == "sys_search":
            import sys_executor as sx, re as _re
            m = _re.search(r'(?:find|search\s+for|locate|where\s+is)\s+(.+?)(?:\s+in\s+(.+))?$',
                           message.lower().strip())
            query  = m.group(1).strip() if m else message
            folder = m.group(2).strip() if m and m.group(2) else None
            # Strip leading stop words: "my", "a", "an", "the", "for", "some", "any"
            query = _re.sub(r'^(my|a|an|the|for|some|any)\s+', '', query).strip()
            res = sx.search_files(query, folder)
            if "error" in res:
                return {"type": "error", "content": res["error"]}
            if not res["results"]:
                return {"type": "info",
                        "content": f"No files matching **{res['query']}** found in `{res['root']}`."}
            items = res["results"]   # already list of {path, name, kind, size_kb, mtime}
            self._last_search_items = items
            note = "  _(showing first 25 — results truncated)_" if res.get("truncated") else ""
            header = f"Found **{len(items)}** result(s) for **{res['query']}**:{note}"
            return self._file_results_response(header, items)

        elif action_id == "sys_list":
            import sys_executor as sx, re as _re, os as _os
            m = _re.search(r'(?:list|show|what.s\s+in)\s+(?:my\s+)?(?:files?\s+in\s+|contents?\s+of\s+)?(.+)',
                           message.lower().strip())
            path_hint = m.group(1).strip() if m else "home"
            # Strip leading stop words from path hint
            path_hint = _re.sub(r'^(the|a|an|my)\s+', '', path_hint).strip()
            res = sx.list_directory(path_hint)
            if "error" in res:
                return {"type": "error", "content": res["error"]}
            entries = res["entries"]
            if not entries:
                return {"type": "info", "content": f"`{res['path']}` is empty."}
            items = []
            for entry in entries:
                if entry.startswith("…"):
                    continue
                is_dir = entry.startswith("📁")
                # strip emoji + size suffix to get the name
                name = entry[2:].strip().split("  ")[0]
                full = _os.path.join(res["path"], name)
                items.append({"path": full, "name": name,
                              "kind": "folder" if is_dir else "file"})
            self._last_search_items = items
            header = f"**{res['path']}** — {len(items)} item(s)"
            return self._file_results_response(header, items)

        elif action_id == "sys_processes":
            import sys_executor as sx, re as _re
            m = _re.search(r'(?:named?|called?|like)\s+(\w+)', message.lower())
            filt = m.group(1) if m else ""
            res = sx.get_running_processes(filt)
            procs = res["processes"]
            if not procs:
                return {"type": "info", "content": "No matching processes found."}
            lines = [f"`{p['name']}` (PID {p['pid']}, {p['mem']})" for p in procs]
            return {"type": "data", "content":
                    f"**Running processes** ({len(lines)} shown):\n" + "\n".join(lines)}

        elif action_id == "sys_network":
            import sys_executor as sx
            res = sx.get_network_info()
            if res["ipv4"]:
                return {"type": "data", "content":
                        "**Network (IPv4)**:\n" + "\n".join(res["ipv4"])}
            return {"type": "data", "content": "```\n" + res["raw"][:800] + "\n```"}

        elif action_id == "sys_sysinfo":
            import sys_executor as sx
            res = sx.get_system_info()
            info = res.get("info", {})
            if not info:
                return {"type": "error", "content": "Could not retrieve system info."}
            lines = [f"**{k}**: {v}" for k, v in info.items()]
            return {"type": "data", "content": "\n".join(lines)}

        elif action_id == "sys_recent":
            import sys_executor as sx, re as _re, os as _os
            m = _re.search(r'(\d+)\s+days?', message.lower())
            days = int(m.group(1)) if m else 7
            res = sx.get_recent_files(days=days)
            if "error" in res:
                return {"type": "error", "content": res["error"]}
            files = res["results"]
            if not files:
                return {"type": "info", "content": f"No files modified in the last {days} day(s)."}
            items = [{"path": p, "name": _os.path.basename(p), "kind": "file"} for p in files]
            self._last_search_items = items
            return self._file_results_response(
                f"**{len(items)}** file(s) modified in the last {days} day(s):", items
            )

        elif action_id == "sys_env":
            import sys_executor as sx
            env = sx.get_env_info()
            lines = [f"**{k}**: `{v}`" for k, v in env.items() if v]
            return {"type": "data", "content": "**Environment**:\n" + "\n".join(lines)}
        elif action_id == "cleanup_confirm":
            return {"type": "info", "action": "cleanup",
                    "content": "Navigate to Storage → Clean Up to review and confirm file deletion."}
        elif action_id == "minimize_window":
            try:
                if self._window:
                    self._window.minimize()
            except Exception:
                pass
            return {"type": "info", "content": "Window minimized."}
        elif action_id == "greet":
            greetings = {
                "en": "Hey! I'm Helix — your PC health assistant. Type **help** to see what I can do, or try: \"disk space\", \"cpu usage\", \"run a scan\".",
                "ja": "こんにちは！Helixです。**help**と入力するとできることが確認できます。「スキャン」「ディスク」「CPU」などとどうぞ。",
            }
            return {"type": "text", "content": greetings.get(lang, greetings["en"])}

        elif action_id == "help":
            return {"type": "help", "content": (
                "I can help with:\n"
                "• **Navigate** — \"go to storage\", \"open monitor\"\n"
                "• **Scan** — \"run full scan\", \"quick scan\", \"cancel scan\"\n"
                "• **Health** — \"how is my PC?\", \"disk space?\", \"check RAM\", \"check CPU\", \"uptime\"\n"
                "• **Actions** — \"show insights\", \"run diagnostics\"\n"
                "• **Open apps** — \"open Edge\", \"open VS Code\", \"open Notepad\"\n"
                "• **Close apps** — \"close Chrome\", \"quit Notepad\"\n"
                "• **Find files** — \"find resume.docx\", \"find all PDFs\", \"search for budget\"\n"
                "• **Open a file** — \"open a PDF\", \"find any Excel file\"\n"
                "• **List folder** — \"list my Desktop\", \"show files in Documents\"\n"
                "• **System** — \"show system info\", \"list running processes\", \"show my IP\"\n"
                "• **Recent** — \"show recently modified files\"\n"
                "• **Explain issues** — \"explain issue 1\", \"why is my disk space low?\"\n"
                "• **Multi-step** — \"close Notepad, then open Calculator, then find my resume\"\n"
                "• **General** — ask anything about your PC health"
            )}

        # Fall back to Ollama
        context = ""
        if self._last_scan_data:
            d = self._last_scan_data
            score = max(0, 100 - d.get("issues", 0) * 20)
            context = (f"Health score {score}/100, disk {d['disk']['fmt_free']} free "
                       f"of {d['disk']['total_gb']} GB, {d.get('issues', 0)} issues found.")

        self._chat_history.append({"role": "user", "content": message})
        if len(self._chat_history) > 20:
            self._chat_history = self._chat_history[-20:]

        _target  = _popup_window
        _history = list(self._chat_history)

        def _ask():
            resp = asst.ollama_chat(message, context, lang, history=_history[:-1])
            if resp:
                self._chat_history.append({"role": "assistant", "content": resp})
                payload = json.dumps({"type": "text", "content": resp})
            else:
                msg_en = ("I didn't quite understand that. Try: \"disk space\", \"cpu usage\", \"run a scan\", "
                          "\"show insights\", or type **help** for the full command list.\n\n"
                          "_(Ollama AI is offline — free-form questions need `ollama run qwen2.5:3b`)_")
                msg_ja = ("うまく理解できませんでした。「ディスク」「CPU」「スキャン」「ヘルプ」などとどうぞ。\n\n"
                          "_(Ollama AIがオフラインです。自由な質問には `ollama run qwen2.5:3b` が必要です)_")
                payload = json.dumps({"type": "info", "content": msg_ja if lang == "ja" else msg_en})
            code = f"onChatResponse({payload})"
            if _target:
                try: _target.evaluate_js(code)
                except Exception: pass
            else:
                self._js(code)

        threading.Thread(target=_ask, daemon=True).start()
        return {"type": "thinking", "content": "Thinking…"}

    def _run_plan_async(self, plan_id: str, steps: list, popup_window=None):
        """Execute a multi-step plan sequentially, pushing JS status updates."""
        import time
        import action_planner as _ap

        def _push(idx: int, status: str, result: str):
            payload = json.dumps({"plan_id": plan_id, "idx": idx,
                                  "status": status, "result": result})
            code = f"onPlanStep({payload})"
            if popup_window:
                try: popup_window.evaluate_js(code)
                except Exception: pass
            else:
                self._js(code)

        for step in steps:
            _push(step["idx"], _ap.RUNNING, "")
            time.sleep(0.08)   # let the browser render the running badge

            # Execute step synchronously (chat() is sync for all non-Ollama intents)
            result = self.chat(step["text"], _popup_window=popup_window)

            rtype = result.get("type", "")
            if rtype == "error":
                status  = _ap.FAILED
                msg     = result.get("content", "Failed")
                _push(step["idx"], status, msg)
                # Mark remaining steps as skipped
                for s in steps[step["idx"] + 1:]:
                    _push(s["idx"], "skipped", "Skipped after failure")
                break
            elif rtype == "thinking":
                status = _ap.DONE
                msg    = "Sent to AI — reply incoming"
            else:
                status = _ap.DONE
                msg    = result.get("content", "Done")
                # Fire any navigation side-effects
                if result.get("action") == "navigate":
                    nav_code = f"showPage('{result.get('page', '')}')"
                    if popup_window:
                        try: popup_window.evaluate_js(nav_code)
                        except Exception: pass
                    else:
                        self._js(nav_code)
                # Surface file results in chat separately
                if rtype == "file_results":
                    fr = json.dumps(result)
                    cb = f"onChatResponse({fr})"
                    if popup_window:
                        try: popup_window.evaluate_js(cb)
                        except Exception: pass
                    else:
                        self._js(cb)

            _push(step["idx"], status, msg)
            time.sleep(0.05)

    def _js(self, code: str):
        if self._window:
            try: self._window.evaluate_js(code)
            except Exception: pass


def _check_downloads_organization():
    """Quick check if Downloads folder needs organization."""
    dl = str(Path.home() / "Downloads")
    if not os.path.isdir(dl):
        return None
    try:
        files = [e for e in os.scandir(dl)
                 if e.is_file(follow_symlinks=False) and not e.name.startswith(".")]
    except (PermissionError, OSError):
        return None
    count = len(files)
    if count < 15:
        return None
    exts = set()
    for f in files[:200]:
        name = f.name
        if "." in name:
            exts.add(name.rsplit(".", 1)[-1].lower())
    return {"file_count": count, "ext_count": len(exts), "folder": dl}

def _build_issue_details(data):
    items = []
    d = data["disk"]
    if d["pct_free"] < 10:
        items.append({"en": f"Critical: Only {d['fmt_free']} free on C: ({d['pct_free']}%)",
                      "ja": f"重大: Cドライブの空き {d['fmt_free']} のみ ({d['pct_free']}%)",
                      "sev": "error", "page": "storage"})
    elif d["pct_free"] < 20:
        items.append({"en": f"Low disk space — {d['fmt_free']} free ({d['pct_free']}%)",
                      "ja": f"ディスク空き容量不足 — {d['fmt_free']} のみ ({d['pct_free']}%)",
                      "sev": "warning", "page": "storage"})
    if data["total_temp"] > 500 * (1 << 20):
        items.append({"en": f"Temp & cache files — {data['total_temp_fmt']} reclaimable",
                      "ja": f"一時ファイル — {data['total_temp_fmt']} 解放可能",
                      "sev": "warning", "page": "storage"})
    if data["total_dupes"] > 100 * (1 << 20):
        items.append({"en": f"Duplicate files — {data['total_dupes_fmt']} saveable in Downloads",
                      "ja": f"重複ファイル — ダウンロードで {data['total_dupes_fmt']} 節約可能",
                      "sev": "warning", "page": "storage"})
    personal = len(data["personal_apps"])
    if personal > 5:
        items.append({"en": f"{personal} personal startup apps may slow boot time",
                      "ja": f"個人スタートアップアプリ {personal} 個が起動を遅くする可能性",
                      "sev": "warning", "page": "startup"})
    if data["processes"] and data["processes"][0]["mem_mb"] > 1500:
        p = data["processes"][0]
        items.append({"en": f"High memory — {p['name']} using {p['mem_mb']} MB",
                      "ja": f"メモリ使用量大 — {p['name']} が {p['mem_mb']} MB 使用",
                      "sev": "warning", "page": "monitor"})
    org = _check_downloads_organization()
    if org:
        items.append({
            "en": f"Downloads has {org['file_count']} loose files ({org['ext_count']} types) — consider organizing",
            "ja": f"ダウンロードに {org['file_count']} 個のファイル（{org['ext_count']}種類）— 整理を推奨",
            "sev": "info", "page": "organize",
        })
    return items


# ── APP HTML (loaded from app/web/index.html) ───────────────────────────────────────────
def _load_app_html():
    # When frozen, PyInstaller unpacks data files into sys._MEIPASS.
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    candidate = base / 'web' / 'index.html'
    if not candidate.exists():
        candidate = Path(__file__).parent / 'web' / 'index.html'
    return candidate.read_text(encoding='utf-8')

APP_HTML = _load_app_html()


def _set_window_icon(ico_path, title="Helix"):
    try:
        import ctypes
        user32 = ctypes.windll.user32
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        ICON_BIG = 1
        ICON_SMALL = 0
        WM_SETICON = 0x0080

        hicon_big = user32.LoadImageW(0, ico_path, IMAGE_ICON, 48, 48, LR_LOADFROMFILE)
        hicon_small = user32.LoadImageW(0, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)

        # Find the window by title — more reliable than GetForegroundWindow
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            # Fallback: enumerate windows to find one containing the title
            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            found = [0]
            def callback(h, _):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(h, buf, 256)
                if title in buf.value:
                    found[0] = h
                    return False
                return True
            user32.EnumWindows(EnumWindowsProc(callback), 0)
            hwnd = found[0]

        if hwnd and hicon_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
        if hwnd and hicon_small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
    except Exception:
        pass

def run():
    import webview
    import launcher
    import chat_popup
    import monitor_hud

    # Give this process its own taskbar identity (prevents grouping under python.exe)
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PCHealthAgent.App")
    except Exception:
        pass

    if not launcher.acquire_single_instance():
        launcher.bring_existing_to_front()
        return

    # Build agent knowledge cache at startup (no scan data yet)
    try:
        import agent_cache
        agent_cache.build_cache()
    except Exception:
        pass

    ico_path = make_ico_file()
    api    = AgentAPI()
    window = webview.create_window(
        title            = "Helix",
        html             = APP_HTML,
        js_api           = api,
        width            = 1100,
        height           = 720,
        min_size          = (900, 580),
        background_color = "#0B1120",
    )
    api.set_window(window)

    # Chat popup was collapsed into the main window's Assistant page — one
    # fewer WebView2 renderer. See AgentAPI.open_chat_popup.

    # Create the gaming-style monitor HUD window (hidden at startup)
    hud_api = monitor_hud.MonitorHudAPI(api)
    hud_window = webview.create_window(
        title            = monitor_hud.HUD_TITLE,
        html             = monitor_hud.MONITOR_HUD_HTML,
        js_api           = hud_api,
        width            = monitor_hud.HUD_W,
        height           = monitor_hud.HUD_H,
        resizable        = False,
        background_color = "#0D1117",
        hidden           = True,
    )
    hud_api.set_window(hud_window)

    # Belt-and-suspenders hide for HUD (same pywebview hidden=True issue)
    def _force_hide_hud():
        time.sleep(1.5)
        try: hud_window.hide()
        except Exception: pass
    threading.Thread(target=_force_hide_hud, daemon=True).start()

    def _hud_on_shown():
        time.sleep(0.3)
        monitor_hud._set_topmost(monitor_hud.HUD_TITLE)
        monitor_hud._position_top_right(monitor_hud.HUD_TITLE,
                                         monitor_hud.HUD_W, monitor_hud.HUD_H)
        try: _set_window_icon(ico_path, monitor_hud.HUD_TITLE)
        except Exception: pass
        try: hud_window.evaluate_js("_hudPollStart()")
        except Exception: pass
    hud_window.events.shown += _hud_on_shown
    def _hud_on_hidden():
        try: hud_window.evaluate_js("_hudPollStop()")
        except Exception: pass
    hud_window.events.closing += _hud_on_hidden

    def on_shown():
        time.sleep(1.0)
        _set_window_icon(ico_path, APP_NAME)
    window.events.shown += on_shown

    _tray_icon = [None]

    def _quit_all():
        fl = launcher.get_launcher()
        if fl:
            fl.stop()
        ti = _tray_icon[0]
        if ti:
            try: ti.stop()
            except Exception: pass
        try: window.destroy()
        except Exception: pass

    def on_closing():
        if db.get_setting("tray_on_close") == "true":
            try: window.hide()
            except Exception: pass
            fl = launcher.get_launcher()
            if fl: fl.hide()
            return False
        _quit_all()
        return True
    window.events.closing += on_closing

    def setup_tray():
        try:
            import pystray
            icon_img = make_icon()
            def on_open(_i, _):
                window.show()
                fl = launcher.get_launcher()
                if fl: fl.show()
            def on_scan(_i, _): api.start_scan(False); window.show()
            def on_assistant(_i, _):
                window.show()
                fl = launcher.get_launcher()
                if fl: fl.show()
                window.evaluate_js("showPage('assistant')")
            def on_quick_chat(_i, _):
                window.show()
                fl = launcher.get_launcher()
                if fl: fl.show()
                try: window.evaluate_js("showPage('assistant')")
                except Exception: pass
            def on_quit(_i, _): _quit_all()
            menu = pystray.Menu(
                pystray.MenuItem("Open PC Health Agent", on_open, default=True),
                pystray.MenuItem("Open Assistant",       on_assistant),
                pystray.MenuItem("Quick Chat",           on_quick_chat),
                pystray.MenuItem("Run Full Scan",        on_scan),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", on_quit),
            )
            icon = pystray.Icon("PCHealth", icon_img, "PC Health Agent", menu)
            _tray_icon[0] = icon
            icon.run_detached()
        except Exception as e:
            print(f"Tray unavailable: {e}")

    threading.Thread(target=setup_tray, daemon=True).start()

    def on_open_from_launcher():
        try:
            window.show()
            window.restore()
        except Exception:
            pass

    def on_scan_from_launcher():
        try:
            api.start_scan(False)
            window.show()
            window.restore()
        except Exception:
            pass

    def on_quit_from_launcher():
        _quit_all()

    def on_chat_from_launcher():
        try:
            window.show()
            window.restore()
            window.evaluate_js("showPage('assistant')")
        except Exception:
            pass

    def on_monitor_from_launcher():
        monitor_hud.toggle_hud()

    launcher.start_launcher(on_open_from_launcher, on_scan_from_launcher, on_quit_from_launcher,
                            on_chat_cb=on_chat_from_launcher,
                            on_monitor_cb=on_monitor_from_launcher)

    # Chat popup system removed — assistant lives on page-assistant in the main window.

    # Start the monitor HUD system (accessed via launcher HUD segment)
    monitor_hud.start_hud_system(api, hud_window)

    webview.start(debug=False)

if __name__ == "__main__":
    run()
