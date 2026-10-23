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
from scanner import run_full_scan, fmt_size, get_system_info, cleanup_temp_files, scan_disk
from report_gen import generate
from suggestions import run_all_checks, get_ai_summary
from organizer import scan_folder as org_scan, organize_category as org_move, get_organize_summary as org_summary
import monitor as mon
import event_analyzer as evtlog
import troubleshooter as tshoot
import db

db.migrate_from_txt(str(LOGS / "scan-history.txt"))

APP_VERSION = "1.0.0"

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
        m = mon.collect_metrics()
        mon.record_sample(m)
        alerts = mon.check_alerts(m)
        sparklines = {
            "cpu": mon.get_sparkline_data("cpu_pct", 30),
            "ram": mon.get_sparkline_data("ram_pct", 30),
            "disk": mon.get_sparkline_data("disk_pct", 30),
        }
        return {"metrics": m, "alerts": alerts, "sparklines": sparklines}

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
        issues = tshoot.run_diagnostics(
            scan_data=scan_data, metrics=metrics, events=all_events
        )
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


# ── APP HTML ─────────────────────────────────────────────────────────────────
APP_HTML = r"""<!DOCTYPE html>
<html lang="en" data-lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Helix</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+JP:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
/* ── Design tokens ────────────────────────────────────────────────────────── */
:root {
  --brand:#2DD4B0; --brand-h:#24B898; --brand-dim:rgba(45,212,176,.12);
  --brand-border:rgba(45,212,176,.25);
  --accent:#7DB3FF; --accent-dim:rgba(125,179,255,.1);
  --bg:#0B0F17; --bg2:#0E131D; --nav-bg:#0E131D; --surface:#121826; --surface2:#182133; --surface3:#1E2D44;
  --border:#1E2738; --border2:#2A3550; --border3:#354767;
  --shadow:0 2px 8px rgba(0,0,0,.4); --shadow-md:0 4px 20px rgba(0,0,0,.45);
  --shadow-lg:0 8px 40px rgba(0,0,0,.55);
  --fg:#E6EBF3; --fg2:#A3AEC2; --fg3:#8E9AAF; --fg4:#6F7B90;
  --success:#34D399; --success-bg:rgba(52,211,153,.08); --success-border:rgba(52,211,153,.2);
  --warn:#F5B544; --warn-bg:#3A2D12; --warn-border:rgba(245,181,68,.3);
  --error:#FF9C97; --error-bg:#3B1E22; --error-border:rgba(255,156,151,.3);
  --info:#7DB3FF; --info-bg:#172540; --info-border:rgba(125,179,255,.3);
  --cat-temp:#A78BFA; --cat-cache:#2DD4BF; --cat-log:#FB923C; --cat-proc:#F472B6;
  --nav-w:232px; --tb-h:40px; --sb-h:26px;
  --font:'IBM Plex Sans JP','Segoe UI Variable Text','Segoe UI',system-ui,sans-serif;
  --mono:'Cascadia Code','Consolas','Courier New',monospace;
  --r:8px; --r-sm:6px; --r-lg:14px; --r-xl:14px;
  --glass:#121826; --glass-border:#1E2738;
  color-scheme:dark;
}
[data-lang="en"] .ja{display:none!important}
[data-lang="ja"] .en{display:none!important}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden;background:var(--bg)}
body{font-family:var(--font);font-size:14px;color:var(--fg);display:flex;flex-direction:column;
  -webkit-font-smoothing:antialiased;background:var(--bg)}
button{font-family:var(--font)}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.18)}

/* ── Title bar ──────────────────────────────────────────────────────────── */
.titlebar{height:var(--tb-h);background:var(--bg2);
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;padding:0 16px 0 14px;flex-shrink:0;gap:10px;z-index:10}
.tb-brand{display:flex;align-items:center;gap:8px;flex-shrink:0}
.tb-logo{display:flex;align-items:center;justify-content:center;flex-shrink:0}
.tb-name{font-size:14px;font-weight:600;letter-spacing:-.01em;white-space:nowrap;color:var(--fg)}
.tb-sub{font-size:10px;color:var(--fg3);margin-top:-1px}
.tb-right{display:flex;align-items:center;gap:8px;margin-left:auto}

/* Buttons */
.btn{display:inline-flex;align-items:center;gap:5px;border-radius:8px;
  font-size:12px;font-weight:500;cursor:pointer;transition:all .15s ease;
  border:1px solid transparent;padding:6px 16px;line-height:1.4;white-space:nowrap;height:40px}
.btn:disabled{opacity:.35;cursor:not-allowed!important}
.btn:focus-visible{outline:2px solid var(--brand);outline-offset:1px}
.btn-primary{background:var(--brand);color:#062A22;font-weight:700;border-color:var(--brand)}
.btn-primary:hover:not(:disabled){background:var(--brand-h)}
.btn-secondary{background:var(--surface2);color:var(--fg);border-color:var(--border2)}
.btn-secondary:hover:not(:disabled){background:var(--surface3);border-color:var(--border3)}
.btn-ghost{background:transparent;color:var(--fg2);border-color:transparent}
.btn-ghost:hover:not(:disabled){background:var(--surface2);color:var(--fg)}
.btn-danger{background:var(--error-bg);color:var(--error);border-color:var(--error-border)}
.btn-danger:hover:not(:disabled){background:rgba(255,156,151,.15)}
.btn-accent{background:var(--info-bg);color:var(--accent);font-weight:700;border-color:var(--info-border)}
.btn-accent:hover:not(:disabled){background:rgba(125,179,255,.15)}
.btn-sm{padding:5px 12px;font-size:11px;height:32px}

/* Language toggle */
.lang-toggle{display:inline-flex;border:1px solid rgba(255,255,255,.08);border-radius:100px;
  overflow:hidden;background:rgba(255,255,255,.04);height:30px;padding:2px}
.lang-toggle button{border:none;background:transparent;padding:3px 14px;font-size:11px;
  font-weight:500;color:var(--fg3);cursor:pointer;transition:all .2s;border-radius:100px}
.lang-toggle button.active{background:var(--brand);color:#070C18;font-weight:700;
  box-shadow:0 0 10px rgba(0,212,170,.3)}
.lang-toggle button:hover:not(.active){background:rgba(255,255,255,.06);color:var(--fg)}

/* ── App body ────────────────────────────────────────────────────────────── */
.app-body{flex:1;display:flex;overflow:hidden}

/* ── Sidebar ─────────────────────────────────────────────────────────────── */
.sidebar{width:var(--nav-w);background:var(--nav-bg);
  border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;overflow:hidden}
.nav-section{padding:8px 8px;display:flex;flex-direction:column;gap:2px;overflow-y:auto}
.nav-item{display:flex;align-items:center;gap:10px;padding:0 12px;height:38px;width:100%;
  border:none;background:transparent;color:var(--fg3);cursor:pointer;
  border-radius:var(--r-lg);transition:background .12s,color .12s;text-align:left;
  font-size:13px;font-family:var(--font);position:relative}
.nav-item:hover{background:var(--surface);color:var(--fg2)}
.nav-item:focus-visible{outline:2px solid var(--brand);outline-offset:-2px}
.nav-item.active{background:var(--surface2);color:var(--fg);font-weight:500}
.nav-item svg{flex-shrink:0;width:16px;height:16px;stroke:currentColor;opacity:.55}
.nav-item.active svg{opacity:1;stroke:var(--brand)}
.nav-item span{display:flex;align-items:center;gap:6px;flex:1}
.nav-badge{display:inline-flex;align-items:center;justify-content:center;
  min-width:18px;height:18px;padding:0 5px;border-radius:9px;font-size:10px;font-weight:700;
  background:var(--error-bg);color:var(--error);border:1px solid var(--error-border);margin-left:auto}
.nav-sep{height:1px;background:var(--border);margin:4px 12px}
.nav-brand{padding:14px 12px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-shrink:0}
.nav-brand-name{font-size:13px;font-weight:700;letter-spacing:.01em;color:var(--fg);line-height:1.2}
.nav-search-btn{display:flex;align-items:center;gap:8px;margin:8px 8px 4px;padding:6px 12px;
  border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--fg3);
  cursor:pointer;font-size:12px;font-family:var(--font);transition:all .12s;text-align:left}
.nav-search-btn:hover{border-color:var(--border2);color:var(--fg2)}
.nav-search-btn kbd{margin-left:auto;font-size:10px;padding:1px 5px;border-radius:4px;
  background:var(--surface2);border:1px solid var(--border2);color:var(--fg4)}
/* Sidebar live widget */
.sb-widget{padding:12px 12px 8px;border-top:1px solid var(--border);flex-shrink:0}
.sb-widget-link{display:block;text-decoration:none;cursor:pointer;padding:8px;
  background:var(--surface);border:1px solid var(--border);border-radius:10px;transition:border-color .12s}
.sb-widget-link:hover{border-color:var(--border2)}
.sb-metric{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.sb-metric:last-child{margin-bottom:0}
.sb-metric-label{font-size:11px;color:var(--fg4);font-weight:600;width:32px;flex-shrink:0}
.sb-metric-bar-wrap{flex:1;height:4px;background:var(--border);border-radius:2px;overflow:hidden}
.sb-metric-bar{height:100%;border-radius:2px;width:0%;transition:width .8s ease}
.sb-metric-val{font-size:11px;font-variant-numeric:tabular-nums;color:var(--fg3);width:32px;text-align:right;flex-shrink:0}
.sb-assist-btn{display:flex;align-items:center;gap:8px;padding:8px 12px;border:none;
  background:transparent;color:var(--fg3);cursor:pointer;font-size:12px;font-family:var(--font);
  width:100%;border-radius:8px;transition:background .12s,color .12s;text-align:left}
.sb-assist-btn:hover{background:var(--surface);color:var(--brand)}
.sb-assist-btn svg{flex-shrink:0;width:16px;height:16px;opacity:.6}
.sb-assist-btn kbd{margin-left:auto;font-size:10px;padding:1px 5px;border-radius:4px;
  background:var(--surface2);border:1px solid var(--border2);color:var(--fg4)}
.nav-footer{padding:6px 8px 8px;border-top:1px solid var(--border);flex-shrink:0}
.nav-ver{font-size:9px;color:var(--fg4);padding:2px 6px}

/* ── Page area ───────────────────────────────────────────────────────────── */
.page-area{flex:1;overflow:hidden;display:flex;flex-direction:column;position:relative;background:var(--bg)}
.page{display:none;flex:1;overflow-y:auto;padding:22px 28px;flex-direction:column;gap:20px}
.page.active{display:flex}
.page-hdr{display:flex;align-items:flex-start;justify-content:space-between;flex-shrink:0;margin-bottom:8px}
.page-title{font-size:24px;font-weight:600;letter-spacing:-.01em;color:var(--fg)}
.page-sub{font-size:13px;color:var(--fg2);margin-top:4px}
.page-actions{display:flex;align-items:center;gap:8px}
/* Tabs */
.tab-bar{display:flex;align-items:flex-end;gap:0;border-bottom:1px solid var(--border);margin-bottom:16px;flex-shrink:0}
.tab-btn{padding:8px 16px;height:40px;border:none;background:transparent;color:var(--fg3);
  cursor:pointer;font-size:13px;font-family:var(--font);border-bottom:2px solid transparent;
  margin-bottom:-1px;transition:color .12s,border-color .12s}
.tab-btn:hover{color:var(--fg2)}
.tab-btn.active{color:var(--brand);border-bottom-color:var(--brand);font-weight:500}
.tab-panel{display:none}.tab-panel.active{display:block}

/* ── Progress overlay ────────────────────────────────────────────────────── */
/* Overlay lives INSIDE #page-overview so it hides automatically when the user
   navigates away — no JS display manipulation needed. position:absolute fills
   the page content area only; sidebar is always accessible. */
#page-overview{position:relative}
#progress-overlay{
  position:absolute;inset:0;
  background:rgba(7,12,24,.97);z-index:50;
  display:none;flex-direction:column;align-items:center;justify-content:center;gap:24px}
#progress-overlay.show{display:flex}
.scan-progress-ring{position:relative;width:140px;height:140px}
.scan-progress-ring svg{transform:rotate(-90deg)}
.spr-bg{fill:none;stroke:rgba(255,255,255,.06);stroke-width:6}
.spr-fill{fill:none;stroke:url(#scan-grad);stroke-width:6;stroke-linecap:round;
  stroke-dasharray:376.99;stroke-dashoffset:376.99;transition:stroke-dashoffset .35s ease;
  filter:drop-shadow(0 0 12px rgba(0,212,170,.5))}
.spr-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px}
.spr-pct{font-size:32px;font-weight:700;font-family:var(--mono);color:var(--brand);
  text-shadow:0 0 20px rgba(0,212,170,.4)}
.spr-lbl{font-size:12px;color:var(--fg2)}
.scan-step{font-size:12px;color:var(--fg2);text-align:center;max-width:300px}

/* ── Toast ───────────────────────────────────────────────────────────────── */
#toast{position:fixed;bottom:40px;left:50%;transform:translateX(-50%) translateY(20px);
  background:rgba(12,18,36,.9);border:1px solid rgba(255,255,255,.08);
  border-radius:100px;box-shadow:0 8px 32px rgba(0,0,0,.5);padding:10px 22px;font-size:12px;
  color:var(--fg);opacity:0;visibility:hidden;transition:opacity .25s,transform .25s,visibility 0s .25s;pointer-events:none;
  z-index:300;white-space:nowrap}
#toast.show{opacity:1;visibility:visible;transform:translateX(-50%) translateY(0);pointer-events:auto;
  backdrop-filter:blur(16px);transition:opacity .25s,transform .25s,visibility 0s}
#toast.toast-success{border-color:rgba(52,211,153,.3);box-shadow:0 0 20px rgba(52,211,153,.1),0 8px 32px rgba(0,0,0,.5)}
#toast.toast-error{border-color:rgba(248,113,113,.3);box-shadow:0 0 20px rgba(248,113,113,.1),0 8px 32px rgba(0,0,0,.5)}
#toast.toast-info{border-color:rgba(0,212,170,.3);box-shadow:0 0 20px rgba(0,212,170,.1),0 8px 32px rgba(0,0,0,.5)}

/* ── Cards ───────────────────────────────────────────────────────────────── */
.card{background:var(--glass);border:1px solid var(--glass-border);border-radius:var(--r-xl);
  box-shadow:0 2px 12px rgba(0,0,0,.25);padding:20px}
.card-sm{padding:18px}
.card-title{font-size:12px;font-weight:600;color:var(--fg2);text-transform:uppercase;
  letter-spacing:.06em;margin-bottom:14px;display:flex;align-items:center;gap:6px}
.card-title svg{opacity:.6;width:14px;height:14px}
.card-title-right{margin-left:auto;font-size:14px;font-weight:700;
  color:var(--fg);text-transform:none;letter-spacing:0}

/* ── Health score ────────────────────────────────────────────────────────── */
.health-wrap{display:grid;grid-template-columns:260px 1fr;gap:16px;align-items:stretch}
.health-score-card{display:flex;flex-direction:column;align-items:center;
  justify-content:center;padding:28px 16px;gap:8px;position:relative;overflow:hidden}
.hs-ring{position:relative;width:150px;height:150px;z-index:1}
.hs-ring svg{transform:rotate(-90deg)}
.hs-bg{fill:none;stroke:rgba(255,255,255,.05);stroke-width:8}
.hs-fill{fill:none;stroke-width:8;stroke-linecap:round;
  stroke-dasharray:376.99;stroke-dashoffset:376.99;transition:stroke-dashoffset 1.2s cubic-bezier(.4,0,.2,1);
  filter:drop-shadow(0 0 14px rgba(0,212,170,.45))}
.hs-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px}
.hs-number{font-size:44px;font-weight:700;font-family:var(--mono);line-height:1;
  text-shadow:0 0 24px rgba(0,212,170,.3)}
.hs-of{font-size:11px;color:var(--fg3)}
.hs-label{font-size:14px;font-weight:700;margin-top:4px;z-index:1}
.hs-desc{font-size:12px;color:var(--fg2);text-align:center;max-width:200px;line-height:1.6;z-index:1}

/* ── Issues ──────────────────────────────────────────────────────────────── */
.issues-grid{display:flex;flex-direction:column;gap:6px}
.issue-item{display:flex;align-items:flex-start;gap:10px;padding:10px 14px;
  border-radius:var(--r-lg);border:1px solid rgba(255,255,255,.05);cursor:pointer;
  transition:all .18s ease;background:rgba(255,255,255,.02)}
.issue-item:hover{background:rgba(255,255,255,.05);transform:translateX(2px)}
.issue-item.sev-error{background:rgba(248,113,113,.06);border-color:rgba(248,113,113,.15)}
.issue-item.sev-warning{background:rgba(251,191,36,.06);border-color:rgba(251,191,36,.15)}
.issue-item.sev-ok{background:rgba(52,211,153,.05);border-color:rgba(52,211,153,.12)}
.issue-dot{width:8px;height:8px;border-radius:50%;margin-top:4px;flex-shrink:0}
.issue-item.sev-error .issue-dot{background:var(--error);box-shadow:0 0 8px rgba(248,113,113,.4)}
.issue-item.sev-warning .issue-dot{background:var(--warn);box-shadow:0 0 8px rgba(251,191,36,.4)}
.issue-item.sev-ok .issue-dot{background:var(--success);box-shadow:0 0 8px rgba(52,211,153,.4)}
.issue-text{font-size:13px;line-height:1.5;flex:1}
.issue-arrow{color:var(--fg4);font-size:14px;margin-left:auto;flex-shrink:0}

/* ── KPI tiles ───────────────────────────────────────────────────────────── */
.kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.kpi{background:var(--glass);border:1px solid var(--glass-border);
  border-radius:var(--r-xl);padding:16px 18px;cursor:default;
  transition:border-color .15s ease;position:relative;overflow:hidden}
.kpi:hover{border-color:var(--border2)}
.kpi.clickable{cursor:pointer}
.kpi-label{font-size:11px;color:var(--fg2);font-weight:700;text-transform:uppercase;
  letter-spacing:.07em;margin-bottom:6px}
.kpi-value{font-size:1.5rem;font-weight:700;font-family:var(--mono);line-height:1.1;
  font-variant-numeric:tabular-nums}
.kpi-sub{font-size:11px;color:var(--fg3);margin-top:4px}
.kpi-chip{display:inline-flex;align-items:center;gap:3px;font-size:10px;font-weight:700;
  padding:2px 8px;border-radius:100px;margin-top:5px}
.chip-success{background:rgba(52,211,153,.08);color:var(--success);border:1px solid rgba(52,211,153,.2)}
.chip-warn{background:rgba(251,191,36,.08);color:var(--warn);border:1px solid rgba(251,191,36,.2)}
.chip-error{background:rgba(248,113,113,.08);color:var(--error);border:1px solid rgba(248,113,113,.2)}
.chip-info{background:rgba(96,165,250,.08);color:var(--info);border:1px solid rgba(96,165,250,.2)}

/* ── Disk ring ───────────────────────────────────────────────────────────── */
.disk-row{display:flex;align-items:center;gap:20px}
.disk-ring-wrap{position:relative;flex-shrink:0}
.disk-ring-wrap svg{transform:rotate(-90deg)}
.drw-bg{fill:none;stroke:rgba(255,255,255,.05);stroke-width:7}
.drw-fill{fill:none;stroke:url(#disk-grad);stroke-width:7;stroke-linecap:round;
  transition:stroke-dashoffset .8s cubic-bezier(.4,0,.2,1);filter:drop-shadow(0 0 10px rgba(0,212,170,.4))}
.drw-label{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.drw-pct{font-size:18px;font-weight:700;font-family:var(--mono);
  text-shadow:0 0 16px rgba(0,212,170,.3)}
.drw-sub{font-size:11px;color:var(--fg3);text-transform:uppercase;letter-spacing:.06em;margin-top:1px}
.disk-details{flex:1;min-width:0}
.disk-details .big{font-size:1.6rem;font-weight:700;font-family:var(--mono)}
.disk-lbl{font-size:11px;color:var(--fg3);text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.disk-seg-bar{height:4px;background:rgba(255,255,255,.06);border-radius:2px;margin-top:12px;overflow:hidden}
.disk-seg-used{height:100%;border-radius:2px;width:0%;transition:width .8s ease;
  background:linear-gradient(90deg,var(--brand),var(--accent))!important}
.disk-legend{display:flex;gap:16px;margin-top:8px}
.dl-item{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--fg2)}
.dl-swatch{width:8px;height:8px;border-radius:50%;flex-shrink:0}

/* ── Bar charts ──────────────────────────────────────────────────────────── */
.bar-list{display:flex;flex-direction:column;gap:8px}
.bar-row{display:grid;gap:10px;align-items:center;grid-template-columns:130px 1fr 60px}
.bar-name{font-size:12px;color:var(--fg);line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-name small{display:block;font-size:11px;color:var(--fg3);margin-top:1px}
.bar-track{height:6px;background:rgba(255,255,255,.05);border-radius:3px;overflow:hidden}
.bar-fill{height:100%;border-radius:3px;width:0%;transition:width .7s cubic-bezier(.4,0,.2,1)}
.bar-val{font-size:11px;color:var(--fg2);text-align:right;font-family:var(--mono);white-space:nowrap;font-weight:600}
.bc-temp{background:linear-gradient(90deg,#A78BFA,#C084FC);box-shadow:0 0 8px rgba(167,139,250,.3)}
.bc-cache{background:linear-gradient(90deg,#2DD4BF,#34D399);box-shadow:0 0 8px rgba(45,212,191,.3)}
.bc-log{background:linear-gradient(90deg,#FB923C,#F97316);box-shadow:0 0 8px rgba(251,146,60,.3)}
.bc-proc{background:linear-gradient(90deg,#F472B6,#EC4899);box-shadow:0 0 8px rgba(244,114,182,.3)}
.bc-brand{background:linear-gradient(90deg,#00D4AA,#00B894);box-shadow:0 0 8px rgba(0,212,170,.3)}

/* ── RAM gauge ───────────────────────────────────────────────────────────── */
.ram-gauge-wrap{margin-top:10px}
.ram-gauge-meta{display:flex;justify-content:space-between;font-size:11px;color:var(--fg2);margin-bottom:6px}
.ram-gauge-track{height:8px;background:rgba(255,255,255,.05);border-radius:4px;overflow:hidden}
.ram-gauge-fill{height:100%;border-radius:4px;width:0%;transition:width 1s ease;
  background:linear-gradient(90deg,var(--brand),#6366F1,var(--cat-proc));
  box-shadow:0 0 12px rgba(0,212,170,.3)}
.ram-gauge-labels{display:flex;justify-content:space-between;font-size:11px;color:var(--fg3);margin-top:4px}

/* ── Tables ──────────────────────────────────────────────────────────────── */
.table-wrap{overflow-x:auto}
.data-table{width:100%;border-collapse:separate;border-spacing:0 2px}
.data-table th{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
  color:var(--brand);text-align:left;padding:8px 12px;border-bottom:1px solid rgba(0,212,170,.1)}
.data-table td{padding:9px 12px;font-size:13px;vertical-align:middle}
.data-table tr:last-child td{border-bottom:none}
.data-table tbody tr{transition:all .15s}
.data-table tbody tr:hover td{background:rgba(255,255,255,.03)}
.data-table tbody tr:hover td:first-child{border-radius:var(--r) 0 0 var(--r)}
.data-table tbody tr:hover td:last-child{border-radius:0 var(--r) var(--r) 0}
.td-mono{font-family:var(--mono);font-size:11px;color:var(--fg2)}
.td-path{font-family:var(--mono);font-size:11px;color:var(--fg3);max-width:240px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ── Badges ──────────────────────────────────────────────────────────────── */
.badge{display:inline-flex;align-items:center;font-size:10px;font-weight:600;padding:2px 8px;border-radius:100px}
.badge-it{background:var(--info-bg);color:var(--info);border:1px solid var(--info-border)}
.badge-user{background:var(--warn-bg);color:var(--warn);border:1px solid var(--warn-border)}

/* ── History ─────────────────────────────────────────────────────────────── */
.hist-card{display:flex;align-items:center;gap:14px;padding:12px 16px;
  border-radius:var(--r-lg);border:1px solid rgba(255,255,255,.05);transition:all .2s ease;
  cursor:pointer;background:rgba(255,255,255,.02)}
.hist-card:hover{background:rgba(255,255,255,.04);border-color:rgba(255,255,255,.1);
  transform:translateX(3px)}
.hist-score{width:42px;height:42px;border-radius:50%;display:flex;align-items:center;
  justify-content:center;font-size:14px;font-weight:700;font-family:var(--mono);flex-shrink:0}
.hist-body{flex:1;min-width:0}
.hist-time{font-size:12px;font-weight:600;color:var(--fg)}
.hist-summary{font-size:12px;color:var(--fg2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:2px}
.hist-actions{display:flex;gap:4px;flex-shrink:0}
.hist-list{display:flex;flex-direction:column;gap:6px}

/* ── Settings ────────────────────────────────────────────────────────────── */
.setting-group{padding:16px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.setting-group:last-child{border-bottom:none}
.setting-row{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:8px 0}
.setting-label{font-size:13px;font-weight:600;color:var(--fg)}
.setting-desc{font-size:12px;color:var(--fg3);margin-top:2px}
.setting-control{flex-shrink:0}
select.setting-select{font-family:var(--font);font-size:12px;padding:5px 10px;
  border:1px solid rgba(255,255,255,.1);border-radius:var(--r-lg);
  background:rgba(255,255,255,.05);color:var(--fg);cursor:pointer;
  backdrop-filter:blur(8px)}
select.setting-select:focus{outline:2px solid var(--brand);outline-offset:1px}

/* ── Toggle ──────────────────────────────────────────────────────────────── */
.toggle{position:relative;width:38px;height:22px;display:inline-block}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;inset:0;background:rgba(255,255,255,.08);
  border:1px solid rgba(255,255,255,.1);border-radius:11px;cursor:pointer;transition:all .25s}
.toggle-slider::before{content:'';position:absolute;width:16px;height:16px;left:2px;bottom:2px;
  background:var(--fg3);border-radius:50%;transition:all .25s}
.toggle input:checked+.toggle-slider{background:var(--brand);border-color:rgba(0,212,170,.4);
  box-shadow:0 0 12px rgba(0,212,170,.3)}
.toggle input:checked+.toggle-slider::before{transform:translateX(16px);background:#070C18}

/* ── Idle state ──────────────────────────────────────────────────────────── */
.idle-screen{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:20px;padding:32px;text-align:center;position:relative}
.idle-icon{width:90px;height:90px;display:flex;align-items:center;justify-content:center;
  animation:idleFloat 4s ease-in-out infinite}
@keyframes idleFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}
.idle-title{font-size:24px;font-weight:600;letter-spacing:-.01em;color:var(--fg)}
.idle-sub{font-size:13px;color:var(--fg2);max-width:380px;line-height:1.65}
.idle-actions{display:flex;gap:10px;margin-top:6px}

/* ── System info ─────────────────────────────────────────────────────────── */
.sysinfo-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.sysinfo-item{padding:12px 14px;background:rgba(255,255,255,.03);backdrop-filter:blur(8px);
  border:1px solid rgba(255,255,255,.06);border-radius:var(--r-lg)}
.sysinfo-label{font-size:11px;color:var(--brand);font-weight:700;text-transform:uppercase;
  letter-spacing:.06em;margin-bottom:3px}
.sysinfo-value{font-size:13px;font-weight:600;color:var(--fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ── Cleanup dialog ──────────────────────────────────────────────────────── */
.dialog-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:200;
  display:none;align-items:center;justify-content:center;backdrop-filter:blur(12px)}
.dialog-backdrop.show{display:flex}
.dialog{background:rgba(12,18,36,.95);border-radius:var(--r-xl);
  box-shadow:0 24px 64px rgba(0,0,0,.6),0 0 1px rgba(255,255,255,.1);
  width:min(520px,90vw);max-height:80vh;display:flex;flex-direction:column;
  border:1px solid rgba(255,255,255,.08)}
.dialog-header{padding:16px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.dialog-title{font-size:15px;font-weight:600;color:var(--fg)}
.dialog-body{padding:16px 18px;overflow-y:auto;flex:1}
.dialog-footer{padding:12px 18px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px}
.cleanup-item{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)}
.cleanup-item:last-child{border-bottom:none}
.cleanup-item label{flex:1;font-size:12px;cursor:pointer;display:flex;align-items:center;gap:8px;color:var(--fg)}
.cleanup-size{font-size:11px;font-family:var(--mono);color:var(--fg2);flex-shrink:0}

/* ── Layouts ─────────────────────────────────────────────────────────────── */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.search-bar{display:flex;align-items:center;gap:8px;background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.08);border-radius:100px;padding:0 14px;height:34px;
  backdrop-filter:blur(8px);transition:all .2s}
.search-bar:focus-within{border-color:rgba(0,212,170,.3);box-shadow:0 0 16px rgba(0,212,170,.1)}
.search-bar input{border:none;background:transparent;font-family:var(--font);font-size:12px;color:var(--fg);flex:1;outline:none}
.search-bar svg{width:14px;height:14px;color:var(--fg3);flex-shrink:0}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px}
.legend-item{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--fg2)}
.legend-swatch{width:8px;height:8px;border-radius:50%;flex-shrink:0}

/* ── AI summary card ──────────────────────────────────────────────────────── */
.ai-card{background:radial-gradient(ellipse at 30% 0%,rgba(0,212,170,.08),rgba(7,12,24,.9) 60%);
  border:1px solid rgba(0,212,170,.15);border-radius:var(--r-xl);padding:20px 22px;
  margin-bottom:16px;position:relative;overflow:hidden;
  box-shadow:0 4px 24px rgba(0,212,170,.06)}
.ai-card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,var(--brand),var(--accent),var(--brand));
  animation:shimmer 3s ease infinite}
@keyframes shimmer{0%,100%{opacity:.6}50%{opacity:1}}
.ai-card-header{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.ai-card-badge{display:inline-flex;align-items:center;gap:6px;font-size:10px;font-weight:700;
  color:var(--brand);text-transform:uppercase;letter-spacing:.07em}
.ai-card-badge svg{width:14px;height:14px;filter:drop-shadow(0 0 4px rgba(0,212,170,.4))}
.ai-card-model{font-size:10px;color:var(--fg4);margin-left:auto;font-family:var(--mono)}
.ai-card-body{font-size:12px;color:var(--fg);line-height:1.7;white-space:pre-wrap}
.ai-card-loading{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--fg3)}
.ai-card-loading .spinner{width:14px;height:14px;border-width:1.5px}

/* ── Insight cards ────────────────────────────────────────────────────────── */
.insight-card{background:var(--glass);border:1px solid var(--glass-border);border-radius:var(--r-xl);padding:18px 20px;
  margin-bottom:12px;transition:all .2s ease;position:relative;overflow:hidden}
.insight-card:hover{border-color:rgba(255,255,255,.1);transform:translateY(-1px);
  box-shadow:0 8px 24px rgba(0,0,0,.3)}
.insight-card::before{content:'';position:absolute;top:0;left:0;bottom:0;width:3px;border-radius:3px 0 0 3px}
.insight-card.sev-critical::before{background:var(--error);box-shadow:0 0 10px rgba(248,113,113,.4)}
.insight-card.sev-error::before{background:var(--error);box-shadow:0 0 10px rgba(248,113,113,.4)}
.insight-card.sev-warning::before{background:var(--warn);box-shadow:0 0 10px rgba(251,191,36,.4)}
.insight-card.sev-info::before{background:var(--brand);box-shadow:0 0 10px rgba(0,212,170,.4)}
.insight-header{display:flex;align-items:center;gap:12px}
.insight-icon{width:36px;height:36px;border-radius:var(--r-lg);display:flex;align-items:center;
  justify-content:center;flex-shrink:0;font-size:16px}
.insight-card.sev-critical .insight-icon,.insight-card.sev-error .insight-icon{
  background:rgba(248,113,113,.1);color:var(--error)}
.insight-card.sev-warning .insight-icon{background:rgba(251,191,36,.1);color:var(--warn)}
.insight-card.sev-info .insight-icon{background:rgba(0,212,170,.1);color:var(--brand)}
.insight-body{flex:1;min-width:0}
.insight-title{font-size:12px;font-weight:600;color:var(--fg);line-height:1.4;
  display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.insight-desc{font-size:12px;color:var(--fg2);line-height:1.5;margin-top:4px}
.insight-actions{margin-top:10px;display:flex;gap:8px}
.insight-badge{display:inline-flex;align-items:center;font-size:9px;font-weight:700;
  padding:2px 8px;border-radius:100px;text-transform:uppercase;letter-spacing:.05em;flex-shrink:0}
.insight-card.sev-critical .insight-badge{background:rgba(248,113,113,.1);color:var(--error);border:1px solid rgba(248,113,113,.2)}
.insight-card.sev-error .insight-badge{background:rgba(248,113,113,.1);color:var(--error);border:1px solid rgba(248,113,113,.2)}
.insight-card.sev-warning .insight-badge{background:rgba(251,191,36,.1);color:var(--warn);border:1px solid rgba(251,191,36,.2)}
.insight-card.sev-info .insight-badge{background:rgba(0,212,170,.1);color:var(--brand);border:1px solid rgba(0,212,170,.2)}
.insight-items{margin-top:10px;max-height:180px;overflow-y:auto;border:1px solid rgba(255,255,255,.06);
  border-radius:var(--r-lg);background:rgba(0,0,0,.2)}
.insight-items table{width:100%;border-collapse:collapse;font-size:11px}
.insight-items td{padding:6px 12px;border-bottom:1px solid rgba(255,255,255,.04);color:var(--fg2)}
.insight-items tr:last-child td{border-bottom:none}
.insight-items .ii-name{color:var(--fg);max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.insight-items .ii-size{font-family:var(--mono);font-size:10px;text-align:right;white-space:nowrap}
.insight-items .ii-row{cursor:pointer;transition:background .15s}
.insight-items .ii-row:hover td{background:rgba(0,212,170,.06)}

/* ── Assistant page — design-system card look ─────────────────────────────── */
#page-assistant{padding:0}
.ai-panel{display:flex;flex-direction:column;flex:1;min-height:300px;
  background:#12161C;border:1px solid #262D37;border-radius:14px;overflow:hidden;
  font-family:'IBM Plex Sans','Segoe UI Variable Text','Segoe UI',system-ui,sans-serif}
.ai-header{height:48px;box-sizing:border-box;padding:0 6px 0 16px;
  display:flex;align-items:center;justify-content:space-between;gap:10px;
  border-bottom:1px solid #1E252F;background:#0F1318;flex-shrink:0}
.ai-header-left{display:flex;align-items:center;gap:10px}
.ai-icon{width:26px;height:26px;border-radius:7px;background:#1C232C;border:1px solid #2A3340;
  display:flex;align-items:center;justify-content:center;color:#4FD1C5;flex-shrink:0}
.ai-title-text{font-size:14px;font-weight:600;letter-spacing:.01em;color:#E7EBF0}
.ai-header-right{display:flex;align-items:center;gap:4px}
.ai-status-pill{display:flex;align-items:center;gap:6px;padding:4px 10px 4px 8px;
  border-radius:999px;background:rgba(79,209,197,.12);
  font-size:12px;font-weight:500;color:#4FD1C5;white-space:nowrap}
.ai-status-dot{width:7px;height:7px;border-radius:50%;background:#4FD1C5;flex-shrink:0;
  animation:aiPulse 2s ease-in-out infinite}
.ai-status-pill.offline{background:rgba(248,113,113,.1);color:#F87171}
.ai-status-pill.offline .ai-status-dot{background:#F87171;animation:none}
.ai-status-pill.checking{background:rgba(107,114,153,.12);color:var(--fg3)}
.ai-status-pill.checking .ai-status-dot{background:#6B7A99;animation:none}
@keyframes aiPulse{0%,100%{opacity:1}50%{opacity:.35}}
.ai-hdr-btn{width:34px;height:34px;border:0;border-radius:8px;background:transparent;
  color:#9AA4B2;display:flex;align-items:center;justify-content:center;
  cursor:pointer;transition:color .15s,background .15s}
.ai-hdr-btn:hover{color:#E7EBF0;background:#1C232C}
.ai-msgs{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;
  gap:14px;background:#12161C;min-height:0}
.ai-msgs::-webkit-scrollbar{width:4px}
.ai-msgs::-webkit-scrollbar-track{background:transparent}
.ai-msgs::-webkit-scrollbar-thumb{background:#2A3340;border-radius:2px}
/* Chat messages */
.chat-msg{display:flex;gap:10px;align-items:flex-start;animation:fadeUp .2s ease both}
.chat-msg.chat-user{flex-direction:row-reverse}
.chat-avatar{width:30px;height:30px;border-radius:8px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  background:#1C232C;border:1px solid #2A3340;color:#4FD1C5}
.chat-msg.chat-user .chat-avatar{
  background:rgba(108,168,255,.1);border-color:rgba(108,168,255,.2);color:#6CA8FF}
.chat-bubble{max-width:78%;padding:12px 16px;border-radius:12px;font-size:14px;
  line-height:1.65;color:#E7EBF0;background:#181D24;border:1px solid #232A33;
  font-family:'IBM Plex Sans','Segoe UI Variable Text','Segoe UI',system-ui,sans-serif}
.chat-msg.chat-user .chat-bubble{background:rgba(79,209,197,.08);border-color:rgba(79,209,197,.2)}
.chat-bubble strong,.chat-bubble b{color:#4FD1C5}
.chat-thinking{opacity:.55;font-style:italic;font-size:13px}
.ai-input-area{flex-shrink:0;padding:10px 12px;border-top:1px solid #1E252F;
  background:#0F1318;display:flex;gap:8px;align-items:flex-end}
.ai-textarea{flex:1;background:#1A2028;border:1px solid #262D37;border-radius:9px;
  padding:9px 12px;color:#E7EBF0;font-size:13px;resize:none;outline:none;
  font-family:'IBM Plex Sans','Segoe UI Variable Text','Segoe UI',system-ui,sans-serif;
  line-height:1.5;max-height:120px;overflow-y:auto;
  transition:border-color .15s,box-shadow .15s}
.ai-textarea::placeholder{color:#484F58}
.ai-textarea:focus{border-color:rgba(79,209,197,.4);box-shadow:0 0 0 3px rgba(79,209,197,.08)}
.ai-send-btn{width:38px;height:38px;flex-shrink:0;border:0;border-radius:9px;
  background:#4FD1C5;color:#0B0E12;display:flex;align-items:center;justify-content:center;
  cursor:pointer;transition:background .15s,transform .1s}
.ai-send-btn:hover{background:#5EDCD0}
.ai-send-btn:active{transform:scale(.95)}
.ai-send-btn:disabled{background:#1C232C;color:#484F58;cursor:default}
/* ── File results ─────────────────────────────────────── */
.fr-header{font-size:13px;opacity:1;margin-bottom:8px;color:var(--fg)}
.fr-list{display:flex;flex-direction:column;gap:4px;margin-top:4px}
.fr-btn{display:flex;align-items:center;gap:8px;width:100%;background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:7px 10px;cursor:pointer;
  text-align:left;transition:background .15s,border-color .15s;position:relative}
.fr-btn:hover{background:rgba(0,212,170,.1);border-color:rgba(0,212,170,.3)}
.fr-icon{font-size:16px;flex-shrink:0}
.fr-info{display:flex;flex-direction:column;flex:1;min-width:0}
.fr-name{font-size:13px;color:#e2e8f0;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fr-path{font-size:10px;color:rgba(150,170,200,.7);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fr-num{font-size:10px;color:rgba(100,130,160,.6);flex-shrink:0;margin-left:4px}
/* ── Plan step list ───────────────────────────────────── */
.plan-list{display:flex;flex-direction:column;gap:5px;padding:2px 0}
.plan-header{font-size:.95rem;color:var(--accent);margin-bottom:4px;font-weight:600}
.plan-step{display:flex;align-items:center;gap:8px;padding:8px 12px;background:rgba(255,255,255,.04);border-radius:8px;border:1px solid rgba(255,255,255,.06)}
.ps-num{font-size:.85rem;color:var(--fg2);min-width:20px;text-align:center}
.ps-text{flex:1;font-size:.95rem}
.ps-badge{font-size:.72rem;padding:3px 9px;border-radius:10px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;flex-shrink:0}
.ps-pending{background:rgba(255,255,255,.08);color:rgba(255,255,255,.4)}
.ps-running{background:rgba(0,212,170,.2);color:#00D4AA;animation:pulse-badge .9s infinite}
.ps-done{background:rgba(0,212,170,.15);color:#00D4AA}
.ps-failed{background:rgba(239,68,68,.2);color:#ef4444}
.ps-skipped{background:rgba(255,255,255,.06);color:rgba(255,255,255,.35)}
.ps-result{font-size:.85rem;color:var(--fg2);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@keyframes pulse-badge{0%,100%{opacity:1}50%{opacity:.4}}
/* ── Interactive enhancements ─────────────────────────── */
.card{transition:transform .18s ease,box-shadow .18s ease}
.card.hoverable:hover,.kpi.clickable:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(0,0,0,.3)}
.kpi.clickable{position:relative;overflow:hidden;cursor:pointer}
.ripple-wave{position:absolute;border-radius:50%;background:rgba(0,212,170,.18);transform:scale(0);
  animation:ripple-out .55s ease-out forwards;pointer-events:none;z-index:0}
@keyframes ripple-out{to{transform:scale(5);opacity:0}}
/* Live status pills */
.live-pills{display:flex;gap:5px;align-items:center}
.live-pill{display:flex;align-items:center;gap:4px;padding:3px 8px 3px 6px;border-radius:20px;
  font-size:10px;font-weight:700;font-family:var(--mono);letter-spacing:.02em;
  background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);
  color:var(--fg2);transition:color .4s,border-color .4s,background .4s}
.live-pill.warn{color:#FBBF24;border-color:rgba(251,191,36,.3);background:rgba(251,191,36,.06)}
.live-pill.crit{color:#F87171;border-color:rgba(248,113,113,.3);background:rgba(248,113,113,.06)}
.pill-dot{width:5px;height:5px;border-radius:50%;background:currentColor;opacity:.8;flex-shrink:0}
/* Status pulse dot next to logo */
.status-pulse{width:7px;height:7px;border-radius:50%;background:#00D4AA;flex-shrink:0;
  animation:spulse 2.5s ease-in-out infinite}
@keyframes spulse{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(0,212,170,.5)}
  50%{opacity:.5;box-shadow:0 0 0 4px rgba(0,212,170,0)}}
.status-pulse.warn{background:#FBBF24}
.status-pulse.crit{background:#F87171;animation:spulse-crit 1.2s ease-in-out infinite}
@keyframes spulse-crit{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(248,113,113,.6)}
  50%{opacity:.6;box-shadow:0 0 0 5px rgba(248,113,113,0)}}
.status-pulse.idle{background:rgba(255,255,255,.25);animation:none}
/* Dashboard idle live vitals */
.live-vitals{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;
  width:100%;max-width:520px;margin-top:16px}
.lv-card{padding:11px 12px;background:rgba(255,255,255,.03);
  border:1px solid rgba(255,255,255,.07);border-radius:10px;text-align:center;
  transition:border-color .4s}
.lv-card.warn{border-color:rgba(251,191,36,.3)}
.lv-card.crit{border-color:rgba(248,113,113,.3)}
.lv-label{font-size:11px;color:var(--fg2);font-weight:700;text-transform:uppercase;
  letter-spacing:.07em;margin-bottom:5px}
.lv-val{font-size:22px;font-weight:700;font-family:var(--mono);line-height:1}
.lv-unit{font-size:11px;color:var(--fg3)}
.lv-sub{font-size:11px;color:var(--fg3);margin-top:3px;min-height:14px}
.lv-bar{height:2px;border-radius:1px;background:rgba(255,255,255,.07);margin-top:7px;overflow:hidden}
.lv-bar-fill{height:100%;border-radius:1px;background:var(--brand);transition:width .8s ease}
/* ── Analysis panels ─────────────────────────────────────────────────────── */
.analysis-panel{background:rgba(0,212,170,.04);border:1px solid rgba(0,212,170,.14);border-radius:10px;padding:14px 16px;margin-bottom:12px}
.ap-header{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.ap-title{font-size:12px;font-weight:700;color:var(--fg);text-transform:uppercase;letter-spacing:.07em;flex:1}
.ap-badge{font-size:10px;font-weight:700;padding:3px 10px;border-radius:20px;text-transform:uppercase;letter-spacing:.06em}
.ap-good{background:rgba(52,211,153,.15);color:#34D399}
.ap-warn{background:rgba(251,191,36,.15);color:#FBBF24}
.ap-crit{background:rgba(248,113,113,.15);color:#F87171}
.ap-insights{list-style:none;padding:0;margin:0 0 8px}
.ap-item{display:flex;align-items:flex-start;gap:8px;padding:6px 0;font-size:13px;color:var(--fg);border-bottom:1px solid rgba(255,255,255,.06)}
.ap-item:last-child{border-bottom:none;padding-bottom:0}
.ap-dot{width:5px;height:5px;border-radius:50%;flex-shrink:0;margin-top:5px;background:currentColor}
.ap-rec{font-size:13px;color:var(--brand);border-top:1px solid rgba(0,212,170,.15);padding-top:8px;margin-top:4px;line-height:1.5}

/* ── Treemap ─────────────────────────────────────────────────────────────── */
.treemap-wrap{width:100%;height:220px;position:relative;overflow:hidden;border-radius:8px;margin-top:8px}
.treemap-cell{position:absolute;display:flex;flex-direction:column;justify-content:flex-end;
  padding:6px 8px;border-radius:4px;overflow:hidden;cursor:default;transition:filter .15s;box-sizing:border-box}
.treemap-cell:hover{filter:brightness(1.2)}
.treemap-name{font-size:11px;font-weight:700;color:#fff;text-shadow:0 1px 3px rgba(0,0,0,.6);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.treemap-size{font-size:9px;color:rgba(255,255,255,.75);margin-top:1px}
/* ── File type donut ─────────────────────────────────────────────────────── */
.donut-wrap{display:flex;align-items:center;gap:20px;flex-wrap:wrap}
.donut-canvas{flex-shrink:0}
.donut-legend{display:flex;flex-direction:column;gap:5px;flex:1;min-width:140px}
.donut-row{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--fg)}
.donut-swatch{width:10px;height:10px;border-radius:2px;flex-shrink:0}
.donut-pct{margin-left:auto;font-family:var(--mono);font-size:12px;color:var(--fg2)}
/* ── Score trend chart ───────────────────────────────────────────────────── */
.trend-wrap{width:100%;overflow-x:auto;padding-bottom:4px}
/* ── Age buckets ─────────────────────────────────────────────────────────── */
.age-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:8px}
.age-card{padding:10px 12px;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:8px;text-align:center}
.age-count{font-size:1.5rem;font-weight:700;font-family:var(--mono);color:var(--warn)}
.age-label{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--fg2);margin-top:3px;font-weight:600}
.age-size{font-size:11px;color:var(--fg3);margin-top:2px}
/* ── Task list / software list ───────────────────────────────────────────── */
.item-list{display:flex;flex-direction:column;gap:4px;margin-top:8px;max-height:260px;overflow-y:auto}
.item-row{display:flex;align-items:center;gap:10px;padding:8px 12px;background:rgba(255,255,255,.04);border:1px solid var(--border2);border-radius:8px;font-size:13px}
.item-name{font-weight:600;color:var(--fg);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item-meta{font-size:12px;color:var(--fg2);flex-shrink:0}
.item-badge{font-size:9px;padding:2px 7px;border-radius:10px;background:rgba(248,113,113,.15);color:#F87171;font-weight:700;flex-shrink:0}
/* ── Boot time bars ──────────────────────────────────────────────────────── */
.boot-row{display:flex;align-items:center;gap:10px;padding:6px 0;font-size:12px;border-bottom:1px solid rgba(255,255,255,.06)}
.boot-row:last-child{border-bottom:none}
.boot-date{color:var(--fg2);flex-shrink:0;width:128px;font-size:12px}
.boot-bar-track{flex:1;height:6px;background:rgba(255,255,255,.07);border-radius:3px;overflow:hidden}
.boot-bar-fill{height:100%;background:linear-gradient(90deg,#00D4AA,#3B82F6);border-radius:3px;transition:width .6s ease}
.boot-val{width:55px;text-align:right;font-family:var(--mono);color:var(--brand);font-size:11px}
/* ── Power plan chips ────────────────────────────────────────────────────── */
.plan-chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.plan-chip{padding:8px 16px;border-radius:20px;font-size:13px;font-weight:600;cursor:pointer;
  border:1px solid var(--border);background:rgba(255,255,255,.04);color:var(--fg2);transition:all .15s}
.plan-chip:hover{background:rgba(0,212,170,.1);border-color:rgba(0,212,170,.3)}
.plan-chip.active{background:rgba(0,212,170,.15);border-color:var(--brand);color:var(--brand)}
/* ── Update status ───────────────────────────────────────────────────────── */
.upd-status{display:flex;align-items:center;gap:10px;padding:12px 14px;border-radius:8px;margin-top:8px}
.upd-ok{background:rgba(52,211,153,.08);border:1px solid rgba(52,211,153,.2)}
.upd-warn{background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.2)}
.upd-crit{background:rgba(248,113,113,.08);border:1px solid rgba(248,113,113,.2)}
/* ── Light mode overrides ────────────────────────────────────────────────── */
:root[data-theme="light"]{
  --bg:#F0F4FF;--bg2:#E4EAF8;--bg3:#D8E0F2;
  --glass:rgba(255,255,255,.9);--glass-border:rgba(0,0,0,.09);
  --fg:#1A2035;--fg2:#2D3A55;--fg3:#4A5980;--fg4:#6D7B90;
  --border:rgba(0,0,0,.10);--border2:rgba(0,0,0,.14);--border3:rgba(0,0,0,.18);
  --brand:#00A882;--success:#15835D;--warn:#B45309;--error:#B91C1C;--info:#1D4ED8;
  --nav-bg:#E4EAF8;--surface:#fff;--surface2:rgba(0,168,130,.08);--surface3:rgba(0,168,130,.12);
}
:root[data-theme="light"] .titlebar{background:var(--bg2);border-bottom-color:var(--border)}
:root[data-theme="light"] .sidebar{background:var(--nav-bg);border-right-color:var(--border)}
:root[data-theme="light"] .card{background:rgba(255,255,255,.9);border-color:rgba(0,0,0,.09)}
:root[data-theme="light"] body{background:var(--bg)}
:root[data-theme="light"] .nav-item{color:var(--fg2)}
:root[data-theme="light"] .nav-item:hover{background:rgba(0,0,0,.05)}
:root[data-theme="light"] .nav-item.active{background:rgba(0,168,130,.12);color:var(--brand)}
:root[data-theme="light"] .btn-secondary{background:rgba(0,0,0,.06);border-color:rgba(0,0,0,.12);color:var(--fg)}
:root[data-theme="light"] .btn-secondary:hover{background:rgba(0,0,0,.1)}
:root[data-theme="light"] .statusbar{background:var(--sidebar-bg);border-top-color:var(--border)}
:root[data-theme="light"] input,
:root[data-theme="light"] textarea,
:root[data-theme="light"] select{background:rgba(0,0,0,.05);border-color:rgba(0,0,0,.12);color:var(--fg)}
/* Theme toggle button */
.btn-theme{display:flex;align-items:center;justify-content:center;width:30px;height:30px;
  border-radius:8px;border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.06);
  cursor:pointer;color:var(--fg2);font-size:14px;transition:background .15s,border-color .15s;flex-shrink:0}
.btn-theme:hover{background:rgba(255,255,255,.12);border-color:rgba(255,255,255,.2)}
/* ── Command palette ─────────────────────────────────────────────────────── */
#cmd-palette-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9000;
  display:none;align-items:flex-start;justify-content:center;padding-top:15vh}
#cmd-palette-backdrop.open{display:flex}
#cmd-palette{background:var(--surface);border:1px solid var(--border);
  border-radius:14px;width:min(560px,90vw);
  box-shadow:0 24px 64px rgba(0,0,0,.5);overflow:hidden}
#cmd-input{width:100%;padding:14px 16px;background:transparent;border:none;
  border-bottom:1px solid var(--border);color:var(--fg);font-size:14px;outline:none;
  font-family:inherit}
#cmd-results{max-height:320px;overflow-y:auto;padding:6px}
.cmd-item{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:8px;
  cursor:pointer;color:var(--fg2);font-size:13px;transition:background .1s}
.cmd-item:hover,.cmd-item.selected{background:rgba(0,212,170,.12);color:var(--fg)}
.cmd-item svg{opacity:.5;flex-shrink:0}
.cmd-hint{padding:8px 16px;font-size:11px;color:var(--fg3);border-top:1px solid var(--border);
  display:flex;gap:12px}
.cmd-hint kbd{font-family:var(--mono);background:rgba(255,255,255,.08);
  border:1px solid rgba(255,255,255,.14);border-radius:4px;padding:1px 5px;font-size:10px}
/* ── Keyboard shortcuts modal ────────────────────────────────────────────── */
#shortcuts-modal{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:8000;
  display:none;align-items:center;justify-content:center}
#shortcuts-modal.open{display:flex}
#shortcuts-box{background:var(--glass);backdrop-filter:blur(24px);border:1px solid var(--border);
  border-radius:16px;padding:24px;width:min(480px,90vw);max-height:80vh;overflow-y:auto;
  box-shadow:0 20px 60px rgba(0,0,0,.5)}
.sc-title{font-size:14px;font-weight:700;color:var(--fg);margin-bottom:16px;
  display:flex;align-items:center;justify-content:space-between}
.sc-group{margin-bottom:16px}
.sc-group-title{font-size:10px;font-weight:700;text-transform:uppercase;
  letter-spacing:.08em;color:var(--fg3);margin-bottom:8px}
.sc-row{display:flex;align-items:center;justify-content:space-between;padding:6px 0;
  font-size:12px;color:var(--fg2);border-bottom:1px solid rgba(255,255,255,.04)}
.sc-row:last-child{border-bottom:none}
.sc-key{display:flex;gap:4px}
.sc-key kbd{font-family:var(--mono);background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);
  border-radius:5px;padding:2px 7px;font-size:11px;color:var(--fg)}
/* ── Onboarding overlay ──────────────────────────────────────────────────── */
#onboard-overlay{position:fixed;inset:0;background:rgba(7,12,24,.92);z-index:7000;
  display:none;align-items:center;justify-content:center}
#onboard-overlay.open{display:flex}
#onboard-box{background:var(--glass);backdrop-filter:blur(24px);border:1px solid rgba(0,212,170,.2);
  border-radius:18px;padding:32px;width:min(480px,90vw);box-shadow:0 24px 80px rgba(0,0,0,.6)}
.ob-step{display:none}.ob-step.active{display:block}
.ob-icon{font-size:40px;margin-bottom:16px}
.ob-title{font-size:20px;font-weight:700;color:var(--fg);margin-bottom:10px}
.ob-body{font-size:14px;color:var(--fg2);line-height:1.7;margin-bottom:24px}
.ob-dots{display:flex;gap:6px;margin-bottom:20px}
.ob-dot{width:6px;height:6px;border-radius:50%;background:rgba(255,255,255,.2);transition:background .3s}
.ob-dot.active{background:var(--brand);width:18px;border-radius:3px}
.ob-actions{display:flex;gap:10px;justify-content:flex-end}
/* ── System Info page ────────────────────────────────────────────────────── */
.hw-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:12px}
.hw-card{background:var(--glass);border:1px solid var(--border);border-radius:12px;padding:16px;position:relative;overflow:hidden}
.hw-card-accent{position:absolute;top:0;left:0;right:0;height:2px}
.hw-label{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--fg3);margin-bottom:6px}
.hw-name{font-size:13px;font-weight:700;color:var(--fg);margin-bottom:8px;line-height:1.3}
.hw-spec{display:flex;justify-content:space-between;align-items:center;padding:4px 0;
  font-size:12px;color:var(--fg2);border-bottom:1px solid rgba(255,255,255,.04)}
.hw-spec:last-child{border-bottom:none}
.hw-spec-val{font-family:var(--mono);font-size:12px;color:var(--brand);font-weight:600}
.hw-badge{display:inline-block;font-size:9px;padding:2px 8px;border-radius:10px;
  background:rgba(0,212,170,.12);color:var(--brand);font-weight:700;margin-top:4px}
/* ── Animations ──────────────────────────────────────────────────────────── */
.fade-in{animation:fadeUp .4s cubic-bezier(.4,0,.2,1) both}
@keyframes fadeUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
@keyframes spin{to{transform:rotate(360deg)}}
.spinner{width:16px;height:16px;border:2px solid rgba(255,255,255,.08);
  border-top-color:var(--brand);border-radius:50%;animation:spin .6s linear infinite}

/* ── Status bar ──────────────────────────────────────────────────────────── */
.statusbar{height:var(--sb-h);background:rgba(8,12,26,.95);border-top:1px solid rgba(255,255,255,.04);
  display:flex;align-items:center;justify-content:space-between;padding:0 14px;flex-shrink:0}
.statusbar span{font-size:10px;color:var(--fg3)}
.s-dot{width:6px;height:6px;border-radius:50%;display:inline-block;margin-right:5px;
  background:var(--success);box-shadow:0 0 8px rgba(52,211,153,.4)}
.s-dot.scanning{background:var(--warn);box-shadow:0 0 8px rgba(251,191,36,.4);animation:pulse 1s ease-in-out infinite}
</style>
</head>
<body>

<!-- Shared SVG gradient defs (referenced by monitor rings) -->
<svg style="position:absolute;width:0;height:0;overflow:hidden" aria-hidden="true">
  <defs>
    <linearGradient id="lg-cpu" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#00D4AA"/><stop offset="100%" stop-color="#2DD4BF"/>
    </linearGradient>
    <linearGradient id="lg-ram" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#A78BFA"/><stop offset="100%" stop-color="#E879F9"/>
    </linearGradient>
    <linearGradient id="lg-disk-live" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#3B82F6"/><stop offset="100%" stop-color="#60A5FA"/>
    </linearGradient>
    <!-- ── Reusable logo symbol ── -->
    <symbol id="pcha-logo" viewBox="0 0 24 24">
      <defs>
        <linearGradient id="logo-grad" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stop-color="#00E0C0"/>
          <stop offset="100%" stop-color="#0098CC"/>
        </linearGradient>
      </defs>
      <path d="M5.9 17.1A8 8 0 1 1 18.1 17.1" fill="none"
            stroke="#2DD4C0" stroke-width="2.4" stroke-linecap="round"/>
      <path d="M6 12H9.3L10.8 8.8L12.8 15.2L14.2 12H16.7" fill="none"
            stroke="#8AF0FF" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="16.9" cy="12" r="1.35" fill="#ffffff"/>
    </symbol>
  </defs>
</svg>

<!-- Title bar -->
<div class="titlebar">
  <div style="display:flex;align-items:center;gap:8px;flex:1">
    <svg width="19" height="19" aria-hidden="true"><use href="#pcha-logo"/></svg>
    <span style="font-size:13px;font-weight:700;color:var(--fg);letter-spacing:.01em">Helix</span>
    <span class="status-pulse idle" id="status-pulse" title="System status" style="margin-left:2px"></span>
  </div>
  <div class="tb-right">
    <!-- hidden for JS compat -->
    <button id="btn-theme" onclick="toggleTheme()" style="display:none"></button>
    <div style="display:none"><button id="btn-en" onclick="setLang('en')">EN</button><button id="btn-ja" onclick="setLang('ja')">日本語</button></div>
    <button class="btn btn-ghost btn-sm" id="btn-guide" onclick="openGuide()" title="Feature guide / 機能ガイド" style="width:30px;padding:0;justify-content:center">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><path d="M8 11v-.2c0-.9.6-1.3 1.2-1.7.6-.4 1-.9 1-1.7a2.2 2.2 0 0 0-4.3-.6"/><circle cx="8" cy="12.6" r=".2" fill="currentColor"/></svg>
    </button>
    <button class="btn btn-secondary btn-sm" id="btn-fast" onclick="startScan(true)" title="Quick scan (no duplicate check)">
      ⚡ <span class="en">Quick Scan</span><span class="ja">クイック</span>
    </button>
    <button class="btn btn-primary btn-sm" id="btn-scan" onclick="startScan(false)" title="Full scan">
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="7" cy="7" r="5"/><line x1="10.5" y1="10.5" x2="14" y2="14"/></svg>
      <span class="en">Full Scan</span><span class="ja">フルスキャン</span>
    </button>
    <button class="btn btn-accent btn-sm" id="btn-report" style="display:none" onclick="openReport()">
      📄 <span class="en">Report</span><span class="ja">レポート</span>
    </button>
  </div>
</div>

<!-- App body -->
<div class="app-body">
  <nav class="sidebar">
    <!-- Search / Ctrl+K -->
    <button class="nav-search-btn" onclick="openCmdPalette()">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" style="width:14px;height:14px"><circle cx="7" cy="7" r="5"/><line x1="10.5" y1="10.5" x2="14" y2="14"/></svg>
      <span class="en">Search…</span><span class="ja">検索…</span>
      <kbd>Ctrl+K</kbd>
    </button>
    <div class="nav-section">
      <button class="nav-item active" data-page="overview" onclick="showPage('overview')">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="7" height="7" rx="1.5"/><rect x="11" y="2" width="7" height="7" rx="1.5"/><rect x="2" y="11" width="7" height="7" rx="1.5"/><rect x="11" y="11" width="7" height="7" rx="1.5"/></svg>
        <span><span class="en">Overview</span><span class="ja">概要</span></span>
      </button>
      <button class="nav-item" data-page="performance" onclick="showPage('performance')">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="2,14 6,9 9.5,12.5 13.5,7 18,11"/><line x1="2" y1="17" x2="18" y2="17"/></svg>
        <span><span class="en">Performance</span><span class="ja">パフォーマンス</span></span>
      </button>
      <button class="nav-item" data-page="storage" onclick="showPage('storage')">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><ellipse cx="10" cy="5.5" rx="7" ry="2.5"/><path d="M3 5.5v4c0 1.38 3.134 2.5 7 2.5s7-1.12 7-2.5v-4"/><path d="M3 9.5v4c0 1.38 3.134 2.5 7 2.5s7-1.12 7-2.5v-4"/></svg>
        <span><span class="en">Storage</span><span class="ja">ストレージ</span></span>
      </button>
      <button class="nav-item" data-page="privacy" onclick="showPage('privacy')">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2L3 5v5c0 4.4 3 8.2 7 9 4-0.8 7-4.6 7-9V5l-7-3z"/><path d="M7 10l2 2 4-4"/></svg>
        <span><span class="en">Security</span><span class="ja">セキュリティ</span></span>
      </button>
      <button class="nav-item" data-page="problems" onclick="showPage('problems')">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13.5 4a2.5 2.5 0 013.5 3.5l-1.5 1.5-3.5-3.5L13.5 4z"/><path d="M3 17l1-3 8.5-8.5 2.5 2.5L6.5 17H3z"/><path d="M10 6.5l3.5 3.5"/></svg>
        <span><span class="en">Problems</span><span class="ja">問題</span><span class="nav-badge" id="nav-problems-badge" style="display:none">0</span></span>
      </button>
      <button class="nav-item" data-page="history" onclick="showPage('history')">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="10" cy="10" r="7.5"/><polyline points="10,6 10,10 13,12.5"/></svg>
        <span><span class="en">History</span><span class="ja">履歴</span></span>
      </button>
    </div>
    <!-- Sidebar live widget -->
    <div class="sb-widget">
      <a class="sb-widget-link" onclick="showPage('performance')">
        <div class="sb-metric">
          <span class="sb-metric-label">CPU</span>
          <div class="sb-metric-bar-wrap"><div class="sb-metric-bar" id="lp-cpu-bar" style="background:var(--brand)"></div></div>
          <span class="sb-metric-val" id="lp-cpu-v">—%</span>
        </div>
        <div class="sb-metric">
          <span class="sb-metric-label">RAM</span>
          <div class="sb-metric-bar-wrap"><div class="sb-metric-bar" id="lp-ram-bar" style="background:#A78BFA"></div></div>
          <span class="sb-metric-val" id="lp-ram-v">—%</span>
        </div>
        <div class="sb-metric">
          <span class="sb-metric-label">Disk</span>
          <div class="sb-metric-bar-wrap"><div class="sb-metric-bar" id="lp-disk-bar" style="background:var(--accent)"></div></div>
          <span class="sb-metric-val" id="lp-disk-v">—%</span>
        </div>
      </a>
    </div>
    <!-- Assistant + Settings -->
    <div class="nav-footer">
      <div class="nav-sep" style="margin:0 0 4px"></div>
      <button class="sb-assist-btn" onclick="showPage('assistant')">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17 10c0 3.87-3.13 7-7 7H4l-1 1V10c0-3.87 3.13-7 7-7s7 3.13 7 7z"/><circle cx="8" cy="10" r=".8" fill="currentColor" stroke="none"/><circle cx="10" cy="10" r=".8" fill="currentColor" stroke="none"/><circle cx="12" cy="10" r=".8" fill="currentColor" stroke="none"/></svg>
        <span class="en">Ask assistant</span><span class="ja">アシスタント</span>
        <kbd>Ctrl+J</kbd>
      </button>
      <button class="nav-item" data-page="settings" onclick="showPage('settings')" style="margin-top:2px">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="10" r="2.5"/><path d="M10 2v2.5M10 15.5V18M2 10h2.5M15.5 10H18M4.2 4.2l1.8 1.8M14 14l1.8 1.8M4.2 15.8l1.8-1.8M14 6l1.8-1.8"/></svg>
        <span><span class="en">Settings</span><span class="ja">設定</span></span>
      </button>
      <div class="nav-ver" id="nav-ver" style="margin-top:6px">v1.0.0 · Local only</div>
    </div>
  </nav>

  <div class="page-area">

    <!-- ══ PAGE: Overview ══════════════════════════════════════════════════ -->
    <div class="page active" id="page-overview">
    <!-- Progress overlay is a direct child of page-overview so it hides
         automatically when any other page is shown (no JS needed) -->
    <div id="progress-overlay">
      <div class="scan-progress-ring">
        <svg width="140" height="140" viewBox="0 0 140 140">
          <defs><linearGradient id="scan-grad" x1="0" y1="0" x2="140" y2="140"><stop offset="0%" stop-color="#00D4AA"/><stop offset="100%" stop-color="#3B82F6"/></linearGradient></defs>
          <circle class="spr-bg" cx="70" cy="70" r="60"/>
          <circle class="spr-fill" id="spr-fill" cx="70" cy="70" r="60"/>
        </svg>
        <div class="spr-center">
          <span class="spr-pct"><span id="prog-pct">0</span>%</span>
          <span class="spr-lbl"><span class="en">scanning</span><span class="ja">スキャン中</span></span>
        </div>
      </div>
      <div class="scan-step" id="prog-step"><span class="en">Initializing scan…</span><span class="ja">スキャンを開始中…</span></div>
      <button class="btn btn-secondary btn-sm" onclick="cancelScan()" id="btn-cancel-scan"><span class="en">Cancel</span><span class="ja">キャンセル</span></button>
    </div>
      <div id="idle-state">
        <div class="idle-screen">
          <div class="idle-icon"><svg width="36" height="41" viewBox="0 0 24 28" fill="none"><defs><linearGradient id="sg2" x1="0" y1="0" x2="24" y2="28"><stop offset="0%" stop-color="#00D4AA"/><stop offset="100%" stop-color="#3B82F6"/></linearGradient></defs><path d="M12 1L2 5.5v8c0 7.2 4.3 12.2 10 13.5 5.7-1.3 10-6.3 10-13.5v-8L12 1z" fill="url(#sg2)" opacity=".85"/><path d="M10 14.5l2.5 2.5 4-5" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></div>
          <div class="idle-title"><span class="en">Your PC, Protected</span><span class="ja">あなたのPC、保護されています</span></div>
          <div class="idle-sub"><span class="en">Run a health scan to analyze disk, temp files, startup apps, memory, and duplicates. Everything stays local on this machine.</span><span class="ja">ディスク・一時ファイル・起動アプリ・メモリ・重複を分析。すべてこのPC上で処理されます。</span></div>
          <div class="idle-actions">
            <button class="btn btn-primary" style="padding:9px 24px;font-size:13px" onclick="startScan(false)">
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="7" cy="7" r="5"/><line x1="10.5" y1="10.5" x2="14" y2="14"/></svg>
              <span class="en">Full Scan</span><span class="ja">フルスキャン</span>
            </button>
            <button class="btn btn-secondary" onclick="startScan(true)">⚡ <span class="en">Quick Scan</span><span class="ja">クイックスキャン</span></button>
          </div>
          <div style="display:flex;gap:12px;margin-top:12px;width:100%;max-width:520px">
            <div style="flex:1;padding:12px 14px;background:var(--brand-dim);border:1px solid var(--brand-border);border-radius:var(--r-lg)">
              <div style="font-size:11px;font-weight:700;color:var(--brand);margin-bottom:6px"><span class="en">Full Scan</span><span class="ja">フルスキャン</span></div>
              <div style="font-size:10px;color:var(--fg2);line-height:1.6">
                <span class="en">✓ Disk usage<br>✓ Temp &amp; cache files<br>✓ Startup apps<br>✓ RAM processes<br>✓ Duplicate files in Downloads</span>
                <span class="ja">✓ ディスク使用量<br>✓ 一時・キャッシュ<br>✓ スタートアップ<br>✓ RAMプロセス<br>✓ ダウンロードの重複ファイル</span>
              </div>
            </div>
            <div style="flex:1;padding:12px 14px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:var(--r-lg)">
              <div style="font-size:11px;font-weight:700;color:var(--fg);margin-bottom:6px"><span class="en">Quick Scan</span><span class="ja">クイックスキャン</span></div>
              <div style="font-size:10px;color:var(--fg2);line-height:1.6">
                <span class="en">✓ Disk usage<br>✓ Temp &amp; cache files<br>✓ Startup apps<br>✓ RAM processes<br><span style="color:var(--fg3)">✗ Skips duplicate scan (faster)</span></span>
                <span class="ja">✓ ディスク使用量<br>✓ 一時・キャッシュ<br>✓ スタートアップ<br>✓ RAMプロセス<br><span style="color:var(--fg3)">✗ 重複スキャンなし（高速）</span></span>
              </div>
            </div>
          </div>
          <div class="live-vitals" id="idle-vitals">
            <div class="lv-card" id="lv-cpu-card">
              <div class="lv-label">CPU</div>
              <div><span class="lv-val" id="lv-cpu-v">—</span><span class="lv-unit">%</span></div>
              <div class="lv-sub" id="lv-cpu-sub"></div>
              <div class="lv-bar"><div class="lv-bar-fill" id="lv-cpu-bar" style="width:0"></div></div>
            </div>
            <div class="lv-card" id="lv-ram-card">
              <div class="lv-label">RAM</div>
              <div><span class="lv-val" id="lv-ram-v">—</span><span class="lv-unit">%</span></div>
              <div class="lv-sub" id="lv-ram-sub"></div>
              <div class="lv-bar"><div class="lv-bar-fill" id="lv-ram-bar" style="width:0;background:#A78BFA"></div></div>
            </div>
            <div class="lv-card" id="lv-disk-card">
              <div class="lv-label">Disk</div>
              <div><span class="lv-val" id="lv-disk-v">—</span><span class="lv-unit">%</span></div>
              <div class="lv-sub" id="lv-disk-sub"></div>
              <div class="lv-bar"><div class="lv-bar-fill" id="lv-disk-bar" style="width:0;background:#3B82F6"></div></div>
            </div>
          </div>
        </div>
      </div>
      <div id="ov-results" style="display:none" class="fade-in">
        <div id="analysis-overview"></div>
        <!-- Hero banner -->
        <div class="card" style="background:radial-gradient(ellipse at 0% 0%,rgba(0,212,170,.1),transparent 50%),radial-gradient(ellipse at 100% 100%,rgba(59,130,246,.08),transparent 50%),var(--glass);border:1px solid rgba(0,212,170,.1);padding:24px 26px;position:relative;overflow:hidden">
          <div style="position:absolute;top:-30px;right:-20px;opacity:.04"><svg width="140" height="160" viewBox="0 0 24 28" fill="none"><path d="M12 1L2 5.5v8c0 7.2 4.3 12.2 10 13.5 5.7-1.3 10-6.3 10-13.5v-8L12 1z" fill="#00D4AA"/></svg></div>
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
            <svg width="18" height="20" viewBox="0 0 24 28" fill="none"><defs><linearGradient id="hero-sg" x1="0" y1="0" x2="24" y2="28"><stop offset="0%" stop-color="#00D4AA"/><stop offset="100%" stop-color="#3B82F6"/></linearGradient></defs><path d="M12 1L2 5.5v8c0 7.2 4.3 12.2 10 13.5 5.7-1.3 10-6.3 10-13.5v-8L12 1z" fill="url(#hero-sg)" opacity=".9"/><path d="M10 14.5l2.5 2.5 4-5" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>
            <span style="font-size:10px;color:var(--brand);font-weight:700;text-transform:uppercase;letter-spacing:.08em"><span class="en">System Status</span><span class="ja">システムステータス</span></span>
          </div>
          <div style="font-size:20px;font-weight:700;margin-bottom:4px;letter-spacing:-.02em" id="ov-hero-title">
            <span class="en">Your PC is protected and performing well.</span>
            <span class="ja">PCは保護され、良好な状態です。</span>
          </div>
          <div style="font-size:11px;color:var(--fg3)" id="ov-hero-sub"></div>
        </div>

        <div class="health-wrap">
          <div class="card health-score-card hoverable">
            <div class="card-title" style="margin-bottom:8px"><span class="en">Health Score</span><span class="ja">ヘルススコア</span></div>
            <div class="hs-ring">
              <svg width="150" height="150" viewBox="0 0 150 150">
                <circle class="hs-bg" cx="75" cy="75" r="60"/>
                <circle class="hs-fill" id="hs-circle" cx="75" cy="75" r="60"/>
              </svg>
              <div class="hs-center">
                <span class="hs-number" id="hs-number">—</span>
                <span class="hs-of">/100</span>
              </div>
            </div>
            <div class="hs-label" id="hs-label">—</div>
            <div class="hs-desc" id="hs-desc"></div>
          </div>
          <div class="card" style="overflow:auto">
            <div class="card-title">
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="8" cy="8" r="6"/><line x1="8" y1="5" x2="8" y2="8.5"/><circle cx="8" cy="10.5" r=".5" fill="currentColor" stroke="none"/></svg>
              <span class="en">Findings</span><span class="ja">検出結果</span>
              <span class="card-title-right" id="ov-scan-time"></span>
            </div>
            <div class="issues-grid" id="issues-list">
              <div class="issue-item sev-ok"><div class="issue-dot"></div>
                <span class="issue-text en">Run a scan to see results</span><span class="issue-text ja">スキャンを実行して結果を確認</span></div>
            </div>
          </div>
        </div>
        <div class="kpi-grid">
          <div class="kpi clickable" onclick="showPage('storage')">
            <div class="kpi-label"><span class="en">Disk Free</span><span class="ja">空き容量</span></div>
            <div class="kpi-value" id="kpi-disk">—</div><div class="kpi-sub" id="kpi-disk-sub">—</div><div class="kpi-chip" id="kpi-disk-chip"></div>
          </div>
          <div class="kpi clickable" onclick="showPage('storage')">
            <div class="kpi-label"><span class="en">Reclaimable</span><span class="ja">解放可能</span></div>
            <div class="kpi-value" id="kpi-free">—</div><div class="kpi-sub" id="kpi-free-sub">—</div><div class="kpi-chip" id="kpi-free-chip"></div>
          </div>
          <div class="kpi clickable" onclick="showPage('startup')">
            <div class="kpi-label"><span class="en">Startup</span><span class="ja">起動アプリ</span></div>
            <div class="kpi-value" id="kpi-startup">—</div><div class="kpi-sub" id="kpi-startup-sub">—</div><div class="kpi-chip" id="kpi-startup-chip"></div>
          </div>
          <div class="kpi clickable" onclick="showPage('monitor')">
            <div class="kpi-label"><span class="en">Top Process</span><span class="ja">上位プロセス</span></div>
            <div class="kpi-value" id="kpi-ram">—</div><div class="kpi-sub" id="kpi-ram-sub">—</div><div class="kpi-chip" id="kpi-ram-chip"></div>
          </div>
        </div>
        <div class="two-col">
          <div class="card card-sm">
            <div class="card-title"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><ellipse cx="10" cy="5.5" rx="7" ry="2.5"/><path d="M3 5.5v4c0 1.38 3.134 2.5 7 2.5s7-1.12 7-2.5v-4"/></svg>
              <span class="en">Disk Usage</span><span class="ja">ディスク使用</span></div>
            <div class="disk-row">
              <div class="disk-ring-wrap"><svg width="80" height="80" viewBox="0 0 80 80"><defs><linearGradient id="disk-grad" x1="0" y1="0" x2="80" y2="80"><stop offset="0%" stop-color="#00D4AA"/><stop offset="100%" stop-color="#3B82F6"/></linearGradient></defs><circle class="drw-bg" cx="40" cy="40" r="35" style="stroke-width:6"/><circle class="drw-fill" id="ov-disk-ring" cx="40" cy="40" r="35" style="stroke-dasharray:219.91;stroke-dashoffset:219.91;stroke-width:6;stroke:url(#disk-grad)"/></svg>
                <div class="drw-label"><span class="drw-pct" id="ov-disk-pct">—</span><span class="drw-sub"><span class="en">used</span><span class="ja">使用</span></span></div></div>
              <div class="disk-details">
                <div class="disk-lbl"><span class="en">Free</span><span class="ja">空き</span></div>
                <div class="big" id="ov-disk-free">—</div>
                <div class="disk-seg-bar"><div class="disk-seg-used" id="ov-disk-bar"></div></div>
                <div class="disk-legend"><div class="dl-item"><div class="dl-swatch" style="background:var(--brand)"></div><span id="ov-disk-used-l">—</span></div><div class="dl-item"><div class="dl-swatch" style="background:var(--surface3)"></div><span id="ov-disk-free-l">—</span></div></div>
              </div>
            </div>
          </div>
          <div class="card card-sm">
            <div class="card-title"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="2,14 6,9 9.5,12.5 13.5,7 18,11"/><line x1="2" y1="17" x2="18" y2="17"/></svg>
              <span class="en">Top Processes</span><span class="ja">上位プロセス</span><span class="card-title-right" id="ov-ram-label"></span></div>
            <div class="bar-list" id="ov-proc-bars"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- ══ PAGE: Storage ═══════════════════════════════════════════════════ -->
    <div class="page" id="page-storage">
      <div class="page-hdr"><div><div class="page-title"><span class="en">Storage</span><span class="ja">ストレージ</span></div><div class="page-sub"><span class="en">Disk usage and reclaimable files</span><span class="ja">ディスク使用状況と解放可能なファイル</span></div></div>
        <div class="page-actions"><button class="btn btn-primary btn-sm" id="btn-cleanup" style="display:none" onclick="showCleanupDialog()">🧹 <span class="en">Clean Up</span><span class="ja">クリーンアップ</span></button></div></div>
      <div class="tab-bar">
        <button class="tab-btn active" onclick="switchTab('storage','files',this)"><span class="en">Files</span><span class="ja">ファイル</span></button>
        <button class="tab-btn" onclick="switchTab('storage','organize',this)"><span class="en">Organize</span><span class="ja">整理</span></button>
      </div>
      <div class="tab-panel active" id="storage-tab-files" style="flex:1;overflow-y:auto">
      <div id="storage-idle" class="idle-screen" style="padding:16px 0"><span style="font-size:12px;color:var(--fg3)"><span class="en">Run a scan to see storage details</span><span class="ja">スキャンでストレージ詳細を確認</span></span></div>
      <div id="storage-results" style="display:none" class="fade-in">
        <div id="analysis-storage"></div>
        <!-- Hero disk card — full width with centered large ring -->
        <div class="card" style="padding:28px;position:relative;overflow:hidden">
          <div style="position:absolute;inset:0;background:radial-gradient(ellipse at 30% 50%,rgba(0,212,170,.06),transparent 60%);pointer-events:none"></div>
          <div style="display:flex;align-items:center;gap:32px;position:relative">
            <div style="flex-shrink:0;text-align:center">
              <div class="disk-ring-wrap">
                <svg width="140" height="140" viewBox="0 0 140 140">
                  <defs><linearGradient id="disk-grad-st" x1="0" y1="0" x2="140" y2="140"><stop offset="0%" stop-color="#00D4AA"/><stop offset="100%" stop-color="#3B82F6"/></linearGradient></defs>
                  <circle class="drw-bg" cx="70" cy="70" r="60" style="stroke-width:8"/>
                  <circle class="drw-fill" id="st-disk-ring" cx="70" cy="70" r="60" style="stroke-dasharray:376.99;stroke-dashoffset:376.99;stroke-width:8;stroke:url(#disk-grad-st)"/>
                </svg>
                <div class="drw-label"><span class="drw-pct" id="st-disk-pct" style="font-size:28px">—</span><span class="drw-sub"><span class="en">used</span><span class="ja">使用</span></span></div>
              </div>
            </div>
            <div style="flex:1;min-width:0">
              <div style="font-size:10px;color:var(--brand);font-weight:700;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px"><span class="en">C: Drive</span><span class="ja">Cドライブ</span></div>
              <div style="font-size:2.2rem;font-weight:700;font-family:var(--mono);line-height:1" id="st-disk-free">—</div>
              <div style="font-size:11px;color:var(--fg3);margin-top:4px" id="st-disk-of">—</div>
              <div class="disk-seg-bar" style="margin-top:16px"><div class="disk-seg-used" id="st-disk-bar"></div></div>
              <div class="disk-legend" style="margin-top:8px">
                <div class="dl-item"><div class="dl-swatch" style="background:var(--brand)"></div><span id="st-used-l">—</span></div>
                <div class="dl-item"><div class="dl-swatch" style="background:rgba(255,255,255,.1)"></div><span id="st-free-l">—</span></div>
              </div>
            </div>
          </div>
        </div>
        <!-- Bento grid: reclaimable + temp breakdown side by side -->
        <div style="display:grid;grid-template-columns:minmax(220px,280px) 1fr;gap:16px">
          <div class="card"><div class="card-title"><span class="en">Reclaimable</span><span class="ja">解放可能</span><span class="card-title-right" id="st-free-total"></span></div><div id="st-reclaimable"></div></div>
          <div class="card"><div class="card-title"><span class="en">Temp &amp; Cache Breakdown</span><span class="ja">一時ファイル・キャッシュ内訳</span><span class="card-title-right" id="st-temp-total"></span></div><div class="bar-list" id="st-temp-bars"></div>
            <div class="legend"><div class="legend-item"><div class="legend-swatch" style="background:#A78BFA"></div><span class="en">Temp</span><span class="ja">一時</span></div><div class="legend-item"><div class="legend-swatch" style="background:#2DD4BF"></div><span class="en">Cache</span><span class="ja">キャッシュ</span></div><div class="legend-item"><div class="legend-swatch" style="background:#FB923C"></div><span class="en">Logs</span><span class="ja">ログ</span></div></div></div>
        </div>
        <div class="card" id="st-dupes-card" style="display:none"><div class="card-title"><span class="en">Duplicate Files — Downloads</span><span class="ja">重複ファイル — ダウンロード</span><span class="card-title-right" id="st-dupes-badge" style="color:var(--warn)"></span></div>
          <div class="table-wrap"><table class="data-table"><thead><tr><th><span class="en">File</span><span class="ja">ファイル</span></th><th><span class="en">Copies</span><span class="ja">コピー</span></th><th><span class="en">Saveable</span><span class="ja">節約</span></th><th><span class="en">Path</span><span class="ja">パス</span></th></tr></thead><tbody id="st-dupes-body"></tbody></table></div></div>

        <!-- File Type Breakdown -->
        <div class="card">
          <div class="card-title">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="10" r="8"/><path d="M10 2a8 8 0 010 16"/><path d="M2 10h16"/></svg>
            <span class="en">File Type Breakdown</span><span class="ja">ファイル種別内訳</span>
            <button class="btn btn-secondary btn-sm" onclick="loadFileTypes()" style="margin-left:auto"><span class="en">Analyse</span><span class="ja">分析</span></button>
          </div>
          <div id="st-ft-idle" style="font-size:13px;color:var(--fg2);padding:8px 0"><span class="en">Click Analyse to see what types of files fill your user space.</span><span class="ja">「分析」をクリックしてファイル種別の内訳を確認します。</span></div>
          <div id="st-ft-loading" style="display:none;text-align:center;padding:20px"><div class="spinner" style="margin:0 auto"></div></div>
          <div id="st-ft-results" style="display:none">
            <div class="donut-wrap"><canvas id="st-ft-canvas" class="donut-canvas" width="160" height="160"></canvas><div class="donut-legend" id="st-ft-legend"></div></div>
          </div>
        </div>

        <!-- Folder Size Treemap -->
        <div class="card">
          <div class="card-title">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="7" height="10" rx="1"/><rect x="11" y="2" width="7" height="5" rx="1"/><rect x="11" y="9" width="7" height="9" rx="1"/><rect x="2" y="14" width="7" height="4" rx="1"/></svg>
            <span class="en">Folder Size Map</span><span class="ja">フォルダサイズマップ</span>
            <button class="btn btn-secondary btn-sm" onclick="loadFolderMap()" style="margin-left:auto"><span class="en">Build Map</span><span class="ja">マップ作成</span></button>
          </div>
          <div id="st-tm-idle" style="font-size:13px;color:var(--fg2);padding:8px 0"><span class="en">Click Build Map to visualise folder sizes in your home directory.</span><span class="ja">「マップ作成」をクリックしてホームフォルダのサイズを視覚化します。</span></div>
          <div id="st-tm-loading" style="display:none;text-align:center;padding:20px"><div class="spinner" style="margin:0 auto"></div></div>
          <div id="st-tm-results" style="display:none"><div class="treemap-wrap" id="st-tm-canvas"></div></div>
        </div>

        <!-- Downloads Aging -->
        <div class="card">
          <div class="card-title">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="10" cy="10" r="7.5"/><polyline points="10,6 10,10 13,12.5"/></svg>
            <span class="en">Downloads Age Analysis</span><span class="ja">ダウンロードの経過日数</span>
            <button class="btn btn-secondary btn-sm" onclick="loadDownloadsAge()" style="margin-left:auto"><span class="en">Analyse</span><span class="ja">分析</span></button>
          </div>
          <div id="st-da-idle" style="font-size:13px;color:var(--fg2);padding:8px 0"><span class="en">See how old your Downloads folder files are.</span><span class="ja">ダウンロードフォルダのファイルの経過日数を確認します。</span></div>
          <div id="st-da-loading" style="display:none;text-align:center;padding:20px"><div class="spinner" style="margin:0 auto"></div></div>
          <div id="st-da-results" style="display:none">
            <div class="age-grid" id="st-da-grid"></div>
            <div style="margin-top:10px;font-size:12px;color:var(--fg3)" id="st-da-note"></div>
          </div>
        </div>

        <!-- Recycle Bin card -->
        <div class="card" id="st-recycle-card">
          <div class="card-title">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="4,6 16,6"/><path d="M7 6V4h6v2"/><rect x="5" y="6" width="10" height="11" rx="2"/><line x1="8" y1="10" x2="8" y2="14"/><line x1="12" y1="10" x2="12" y2="14"/></svg>
            <span class="en">Recycle Bin</span><span class="ja">ごみ箱</span>
            <span class="card-title-right" id="st-rb-size" style="color:var(--fg)">—</span>
          </div>
          <div style="display:flex;align-items:center;gap:20px">
            <div>
              <div style="font-size:2rem;font-weight:700;font-family:var(--mono);line-height:1" id="st-rb-items">—</div>
              <div style="font-size:13px;color:var(--fg2);margin-top:4px"><span class="en">items in bin</span><span class="ja">アイテム</span></div>
            </div>
            <div style="flex:1"></div>
            <button class="btn btn-danger" id="btn-empty-rb" onclick="emptyRecycleBin()" style="display:none">
              🗑 <span class="en">Empty Bin</span><span class="ja">空にする</span>
            </button>
          </div>
          <div id="st-rb-msg" style="margin-top:10px;font-size:13px;color:var(--success);display:none"></div>
        </div>

        <!-- Large Files -->
        <div class="card">
          <div class="card-title">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="2" width="14" height="16" rx="2"/><polyline points="8,8 10,6 12,8"/><line x1="10" y1="6" x2="10" y2="14"/></svg>
            <span class="en">Large Files</span><span class="ja">大きなファイル</span>
            <span class="card-title-right" id="st-lf-badge"></span>
            <button class="btn btn-secondary btn-sm" id="btn-find-large" onclick="findLargeFiles()" style="margin-left:auto">
              <span class="en">Find</span><span class="ja">検索</span>
            </button>
          </div>
          <div id="st-lf-idle" style="font-size:13px;color:var(--fg2);padding:8px 0"><span class="en">Click Find to scan for files larger than 10 MB.</span><span class="ja">「検索」をクリックして10 MB以上のファイルを探します。</span></div>
          <div id="st-lf-loading" style="display:none;text-align:center;padding:20px"><div class="spinner" style="margin:0 auto"></div><div style="margin-top:10px;font-size:13px;color:var(--fg2)"><span class="en">Scanning…</span><span class="ja">スキャン中…</span></div></div>
          <div id="st-lf-results" style="display:none">
            <div class="table-wrap"><table class="data-table">
              <thead><tr><th><span class="en">File</span><span class="ja">ファイル</span></th><th><span class="en">Size</span><span class="ja">サイズ</span></th><th><span class="en">Location</span><span class="ja">場所</span></th><th style="text-align:right"><span class="en">Actions</span><span class="ja">操作</span></th></tr></thead>
              <tbody id="st-lf-body"></tbody>
            </table></div>
          </div>
        </div>

        <!-- Old Files -->
        <div class="card">
          <div class="card-title">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="10" cy="10" r="7.5"/><polyline points="10,6 10,10 13,12.5"/></svg>
            <span class="en">Old Files (6+ months)</span><span class="ja">古いファイル（6ヶ月以上）</span>
            <span class="card-title-right" id="st-of-badge"></span>
            <button class="btn btn-secondary btn-sm" id="btn-find-old" onclick="findOldFiles()" style="margin-left:auto">
              <span class="en">Find</span><span class="ja">検索</span>
            </button>
          </div>
          <div id="st-of-idle" style="font-size:13px;color:var(--fg2);padding:8px 0"><span class="en">Click Find to locate files not modified in 6+ months.</span><span class="ja">「検索」をクリックして6ヶ月以上変更されていないファイルを探します。</span></div>
          <div id="st-of-loading" style="display:none;text-align:center;padding:20px"><div class="spinner" style="margin:0 auto"></div><div style="margin-top:10px;font-size:13px;color:var(--fg2)"><span class="en">Scanning…</span><span class="ja">スキャン中…</span></div></div>
          <div id="st-of-results" style="display:none">
            <div class="table-wrap"><table class="data-table">
              <thead><tr><th><span class="en">File</span><span class="ja">ファイル</span></th><th><span class="en">Size</span><span class="ja">サイズ</span></th><th><span class="en">Age</span><span class="ja">経過日数</span></th><th><span class="en">Location</span><span class="ja">場所</span></th><th style="text-align:right"><span class="en">Actions</span><span class="ja">操作</span></th></tr></thead>
              <tbody id="st-of-body"></tbody>
            </table></div>
          </div>
        </div>
      </div>
      </div>
      <div class="tab-panel" id="storage-tab-organize" style="flex:1;overflow-y:auto"></div>
    </div>



    <!-- ══ PAGE: Startup ═══════════════════════════════════════════════════ -->
    <div class="page" id="page-startup">
      <div class="page-hdr"><div><div class="page-title"><span class="en">Startup Apps</span><span class="ja">スタートアップ</span></div><div class="page-sub"><span class="en">Apps that run when Windows starts</span><span class="ja">Windows 起動時の自動実行アプリ</span></div></div>
        <div class="page-actions"><div class="search-bar" id="startup-search" style="display:none"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="7" cy="7" r="5"/><line x1="10.5" y1="10.5" x2="14" y2="14"/></svg><input type="text" placeholder="Search…" oninput="filterStartup(this.value)"></div></div></div>
      <div id="startup-idle" class="idle-screen" style="padding:16px 0"><span style="font-size:12px;color:var(--fg3)"><span class="en">Run a scan to see startup apps</span><span class="ja">スキャンで起動アプリを確認</span></span></div>
      <div id="startup-results" style="display:none" class="fade-in">
        <div id="analysis-startup"></div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:6px">
          <div class="card card-sm" style="text-align:center;position:relative;overflow:hidden"><div style="position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--warn),#FB923C)"></div><div style="font-size:1.8rem;font-weight:700;font-family:var(--mono);color:var(--warn);text-shadow:0 0 20px rgba(251,191,36,.3)" id="su-personal">—</div><div style="font-size:10px;color:var(--fg3);text-transform:uppercase;letter-spacing:.06em;margin-top:4px;font-weight:700"><span class="en">Personal</span><span class="ja">個人</span></div></div>
          <div class="card card-sm" style="text-align:center;position:relative;overflow:hidden"><div style="position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--info),#6366F1)"></div><div style="font-size:1.8rem;font-weight:700;font-family:var(--mono);color:var(--info);text-shadow:0 0 20px rgba(96,165,250,.3)" id="su-it">—</div><div style="font-size:10px;color:var(--fg3);text-transform:uppercase;letter-spacing:.06em;margin-top:4px;font-weight:700"><span class="en">IT Managed</span><span class="ja">IT 管理</span></div></div>
        </div>
        <div class="card"><div class="card-title"><span class="en">All Entries</span><span class="ja">すべてのエントリ</span><span class="card-title-right" id="su-total-label"></span></div>
          <div class="table-wrap"><table class="data-table"><thead><tr><th><span class="en">App</span><span class="ja">アプリ</span></th><th><span class="en">Type</span><span class="ja">種類</span></th><th><span class="en">Command</span><span class="ja">コマンド</span></th></tr></thead><tbody id="su-app-body"></tbody></table></div></div>
      </div>
    </div>

    <!-- ══ PAGE: Insights ══════════════════════════════════════════════════ -->
    <div class="page" id="page-insights">
      <div class="page-hdr"><div><div class="page-title"><span class="en">Insights</span><span class="ja">インサイト</span></div><div class="page-sub"><span class="en">Proactive suggestions to keep your PC healthy</span><span class="ja">PCの健康を維持するための提案</span></div></div>
        <div class="page-actions"><button class="btn btn-primary btn-sm" id="btn-refresh-insights" onclick="loadInsights()"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M1 8a7 7 0 0113.4-2.8M15 8A7 7 0 011.6 10.8"/><polyline points="1,3 1,8 6,8"/><polyline points="15,13 15,8 10,8"/></svg> <span class="en">Refresh</span><span class="ja">更新</span></button></div></div>
      <div id="insights-loading" style="display:none;text-align:center;padding:40px"><div class="spinner" style="margin:0 auto"></div><div style="margin-top:12px;font-size:12px;color:var(--fg3)"><span class="en">Analyzing your PC…</span><span class="ja">PCを分析中…</span></div></div>
      <div id="ai-summary-card" style="display:none"></div>
      <div id="insights-list"></div>
      <div id="insights-empty" class="idle-screen" style="padding:24px 0"><svg width="40" height="46" viewBox="0 0 24 28" fill="none" style="opacity:.3"><path d="M12 1L2 5.5v8c0 7.2 4.3 12.2 10 13.5 5.7-1.3 10-6.3 10-13.5v-8L12 1z" fill="#00D4AA"/><path d="M10 14.5l2.5 2.5 4-5" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg><div style="font-size:14px;font-weight:500;color:var(--fg2);margin-top:8px"><span class="en">No suggestions right now</span><span class="ja">現在、提案はありません</span></div><div style="font-size:11px;color:var(--fg3);margin-top:4px"><span class="en">Your PC looks healthy. Check back later.</span><span class="ja">PCは健康な状態です。後でまた確認してください。</span></div></div>
    </div>

    <!-- ══ PAGE: Organize ══════════════════════════════════════════════════ -->
    <div class="page" id="page-organize">
      <div class="page-hdr"><div><div class="page-title"><span class="en">File Organizer</span><span class="ja">ファイル整理</span></div><div class="page-sub"><span class="en">Sort files into category subfolders — nothing is deleted</span><span class="ja">ファイルをカテゴリ別フォルダに整理 — 何も削除されません</span></div></div>
        <div class="page-actions">
          <button class="btn btn-secondary btn-sm" id="btn-org-browse" onclick="orgBrowse()">📁 <span class="en">Browse…</span><span class="ja">参照…</span></button>
          <button class="btn btn-primary btn-sm" id="btn-org-scan" onclick="orgScan()"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="7" cy="7" r="5"/><line x1="10.5" y1="10.5" x2="14" y2="14"/></svg> <span class="en">Scan</span><span class="ja">スキャン</span></button>
        </div>
      </div>

      <!-- Quick folder shortcuts -->
      <div class="card" style="padding:10px 14px;margin-bottom:10px">
        <div style="font-size:10px;color:var(--fg3);text-transform:uppercase;letter-spacing:.08em;font-weight:700;margin-bottom:8px"><span class="en">Quick Folders</span><span class="ja">クイックフォルダ</span></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap" id="org-quick-btns">
          <button class="btn btn-secondary btn-sm" onclick="orgQuick('downloads')">📥 <span class="en">Downloads</span><span class="ja">ダウンロード</span></button>
          <button class="btn btn-secondary btn-sm" onclick="orgQuick('documents')">📄 <span class="en">Documents</span><span class="ja">ドキュメント</span></button>
          <button class="btn btn-secondary btn-sm" onclick="orgQuick('desktop')">🖥️ <span class="en">Desktop</span><span class="ja">デスクトップ</span></button>
          <button class="btn btn-secondary btn-sm" onclick="orgQuick('pictures')">🖼️ <span class="en">Pictures</span><span class="ja">ピクチャ</span></button>
        </div>
      </div>

      <!-- Selected folder display -->
      <div class="card" style="padding:10px 14px;margin-bottom:10px;display:flex;align-items:center;gap:10px">
        <span style="font-size:11px;color:var(--fg3);white-space:nowrap"><span class="en">Folder:</span><span class="ja">フォルダ:</span></span>
        <span id="org-folder-path" style="font-size:12px;font-family:var(--mono);color:var(--fg);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer" onclick="openPath(this.textContent)"><span style="color:var(--fg3);font-family:inherit"><span class="en">None selected</span><span class="ja">未選択</span></span></span>
      </div>

      <div id="org-ai-card" style="display:none"></div>
      <div id="org-loading" style="display:none;text-align:center;padding:40px"><div class="spinner" style="margin:0 auto"></div><div style="margin-top:12px;font-size:12px;color:var(--fg3)"><span class="en">Scanning folder…</span><span class="ja">フォルダをスキャン中…</span></div></div>
      <div id="org-summary" style="display:none"></div>
      <div id="org-categories"></div>

      <!-- Move history section -->
      <div id="org-history" style="display:none;margin-top:16px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;cursor:pointer" onclick="toggleOrgHistory()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--brand)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12,7 12,12 15,15"/></svg>
          <span style="font-size:12px;font-weight:600;color:var(--brand)"><span class="en">Move History</span><span class="ja">移動履歴</span></span>
          <span id="org-hist-count" style="font-size:10px;color:var(--fg3)"></span>
          <svg id="org-hist-chev" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="var(--fg3)" stroke-width="2" stroke-linecap="round" style="margin-left:auto;transition:transform .2s"><polyline points="4,6 8,10 12,6"/></svg>
        </div>
        <div id="org-history-body"></div>
      </div>

      <div id="org-empty" class="idle-screen" style="padding:24px 0">
        <svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="var(--brand)" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" style="opacity:.4"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
        <div style="font-size:14px;font-weight:500;color:var(--fg2);margin-top:8px"><span class="en">Select a folder to organize</span><span class="ja">整理するフォルダを選択</span></div>
        <div style="font-size:11px;color:var(--fg3);margin-top:4px"><span class="en">Choose from Quick Folders above or Browse to any folder.</span><span class="ja">クイックフォルダから選ぶか、参照で任意のフォルダを選択。</span></div>
      </div>
    </div>

    <!-- ══ PAGE: Monitor ══════════════════════════════════════════════════ -->
    <div class="page" id="page-monitor">
      <div class="page-hdr"><div><div class="page-title"><span class="en">System Monitor</span><span class="ja">システムモニター</span></div><div class="page-sub"><span class="en">Real-time CPU, RAM, disk, battery &amp; temperature</span><span class="ja">CPU・メモリ・ディスク・バッテリー・温度のリアルタイム監視</span></div></div></div>

      <!-- Live gauge row -->
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px" id="mon-gauges">
        <div class="card" style="text-align:center;padding:16px 10px;position:relative;overflow:hidden">
          <div style="position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#00D4AA,#2DD4BF)"></div>
          <div style="font-size:10px;color:var(--fg3);text-transform:uppercase;letter-spacing:.08em;font-weight:700;margin-bottom:8px">CPU</div>
          <div style="position:relative;width:80px;height:80px;margin:0 auto">
            <svg width="80" height="80" viewBox="0 0 80 80" style="transform:rotate(-90deg)">
              <circle cx="40" cy="40" r="34" fill="none" stroke="rgba(255,255,255,.06)" stroke-width="6"/>
              <circle id="mon-cpu-ring" cx="40" cy="40" r="34" fill="none" stroke="url(#lg-cpu)" stroke-width="6" stroke-dasharray="213.63" stroke-dashoffset="213.63" stroke-linecap="round" style="transition:stroke-dashoffset .8s ease"/>
            </svg>
            <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center">
              <span id="mon-cpu-pct" style="font-size:22px;font-weight:700;font-family:var(--mono)">0</span>
              <span style="font-size:8px;color:var(--fg3)">%</span>
            </div>
          </div>
        </div>
        <div class="card" style="text-align:center;padding:16px 10px;position:relative;overflow:hidden">
          <div style="position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#A78BFA,#E879F9)"></div>
          <div style="font-size:10px;color:var(--fg3);text-transform:uppercase;letter-spacing:.08em;font-weight:700;margin-bottom:8px">RAM</div>
          <div style="position:relative;width:80px;height:80px;margin:0 auto">
            <svg width="80" height="80" viewBox="0 0 80 80" style="transform:rotate(-90deg)">
              <circle cx="40" cy="40" r="34" fill="none" stroke="rgba(255,255,255,.06)" stroke-width="6"/>
              <circle id="mon-ram-ring" cx="40" cy="40" r="34" fill="none" stroke="url(#lg-ram)" stroke-width="6" stroke-dasharray="213.63" stroke-dashoffset="213.63" stroke-linecap="round" style="transition:stroke-dashoffset .8s ease"/>
            </svg>
            <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center">
              <span id="mon-ram-pct" style="font-size:22px;font-weight:700;font-family:var(--mono)">0</span>
              <span style="font-size:8px;color:var(--fg3)">%</span>
            </div>
          </div>
          <div style="font-size:9px;color:var(--fg3);margin-top:4px" id="mon-ram-label">—</div>
        </div>
        <div class="card" style="text-align:center;padding:16px 10px;position:relative;overflow:hidden">
          <div style="position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#3B82F6,#60A5FA)"></div>
          <div style="font-size:10px;color:var(--fg3);text-transform:uppercase;letter-spacing:.08em;font-weight:700;margin-bottom:8px">DISK</div>
          <div style="position:relative;width:80px;height:80px;margin:0 auto">
            <svg width="80" height="80" viewBox="0 0 80 80" style="transform:rotate(-90deg)">
              <circle cx="40" cy="40" r="34" fill="none" stroke="rgba(255,255,255,.06)" stroke-width="6"/>
              <circle id="mon-disk-ring" cx="40" cy="40" r="34" fill="none" stroke="url(#lg-disk-live)" stroke-width="6" stroke-dasharray="213.63" stroke-dashoffset="213.63" stroke-linecap="round" style="transition:stroke-dashoffset .8s ease"/>
            </svg>
            <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center">
              <span id="mon-disk-pct" style="font-size:22px;font-weight:700;font-family:var(--mono)">0</span>
              <span style="font-size:8px;color:var(--fg3)">%</span>
            </div>
          </div>
          <div style="font-size:9px;color:var(--fg3);margin-top:4px" id="mon-disk-label">—</div>
        </div>
        <div class="card" id="mon-battery-card" style="text-align:center;padding:16px 10px;position:relative;overflow:hidden;display:none">
          <div style="position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#FBBF24,#34D399)"></div>
          <div style="font-size:10px;color:var(--fg3);text-transform:uppercase;letter-spacing:.08em;font-weight:700;margin-bottom:8px"><span class="en">BATTERY</span><span class="ja">バッテリー</span></div>
          <div style="position:relative;width:80px;height:80px;margin:0 auto">
            <svg width="80" height="80" viewBox="0 0 80 80" style="transform:rotate(-90deg)">
              <circle cx="40" cy="40" r="34" fill="none" stroke="rgba(255,255,255,.06)" stroke-width="6"/>
              <circle id="mon-bat-ring" cx="40" cy="40" r="34" fill="none" stroke="#FBBF24" stroke-width="6" stroke-dasharray="213.63" stroke-dashoffset="213.63" stroke-linecap="round" style="transition:stroke-dashoffset .8s ease"/>
            </svg>
            <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center">
              <span id="mon-bat-pct" style="font-size:22px;font-weight:700;font-family:var(--mono)">—</span>
              <span style="font-size:8px;color:var(--fg3)">%</span>
            </div>
          </div>
          <div style="font-size:9px;color:var(--fg3);margin-top:4px" id="mon-bat-label">—</div>
        </div>
        <div class="card" id="mon-temp-card" style="text-align:center;padding:16px 10px;position:relative;overflow:hidden;display:none">
          <div style="position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#FB923C,#F87171)"></div>
          <div style="font-size:10px;color:var(--fg3);text-transform:uppercase;letter-spacing:.08em;font-weight:700;margin-bottom:8px">TEMP</div>
          <div style="font-size:28px;font-weight:700;font-family:var(--mono);margin-top:16px" id="mon-temp-val">—</div>
          <div style="font-size:9px;color:var(--fg3);margin-top:4px">°C</div>
        </div>
      </div>

      <!-- Sparkline charts -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
        <div class="card" style="padding:14px">
          <div style="font-size:10px;color:var(--fg3);text-transform:uppercase;letter-spacing:.06em;font-weight:700;margin-bottom:8px"><span class="en">CPU History</span><span class="ja">CPU履歴</span></div>
          <canvas id="mon-cpu-chart" width="400" height="80" style="width:100%;height:80px"></canvas>
        </div>
        <div class="card" style="padding:14px">
          <div style="font-size:10px;color:var(--fg3);text-transform:uppercase;letter-spacing:.06em;font-weight:700;margin-bottom:8px"><span class="en">RAM History</span><span class="ja">メモリ履歴</span></div>
          <canvas id="mon-ram-chart" width="400" height="80" style="width:100%;height:80px"></canvas>
        </div>
      </div>

      <!-- Alerts -->
      <div id="mon-alerts"></div>
      <div id="analysis-monitor"></div>
    </div>

    <!-- ══ PAGE: Privacy ══════════════════════════════════════════════════ -->
    <div class="page" id="page-privacy">
      <div class="page-hdr">
        <div>
          <div class="page-title"><span class="en">Privacy &amp; Security</span><span class="ja">プライバシー・セキュリティ</span></div>
          <div class="page-sub"><span class="en">Browser cache, active connections &amp; sensitive files</span><span class="ja">ブラウザキャッシュ、ネットワーク接続、機密ファイル</span></div>
        </div>
        <div class="page-actions">
          <button class="btn btn-primary btn-sm" id="btn-priv-scan" onclick="loadPrivacy()">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M8 1L2 3.5v5c0 3.6 2.5 6.6 6 7.5 3.5-.9 6-3.9 6-7.5v-5L8 1z"/></svg>
            <span class="en">Scan All</span><span class="ja">スキャン</span>
          </button>
        </div>
      </div>

      <div id="priv-idle" class="idle-screen" style="padding:16px 0">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--brand)" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" style="opacity:.4"><path d="M12 1L3 5v6c0 5.2 3.8 10.1 9 11.2 5.2-1.1 9-6 9-11.2V5l-9-4z"/><path d="M9 12l2 2 4-4"/></svg>
        <div style="font-size:15px;font-weight:600;color:var(--fg2);margin-top:10px"><span class="en">Run a Privacy Scan</span><span class="ja">プライバシースキャンを実行</span></div>
        <div style="font-size:13px;color:var(--fg3);margin-top:6px"><span class="en">Checks browser cache, network connections, and sensitive files.</span><span class="ja">ブラウザキャッシュ、ネットワーク接続、機密ファイルを確認します。</span></div>
      </div>

      <div id="priv-loading" style="display:none;text-align:center;padding:60px">
        <div class="spinner" style="margin:0 auto;width:24px;height:24px;border-width:3px"></div>
        <div style="margin-top:14px;font-size:14px;color:var(--fg2)"><span class="en">Scanning…</span><span class="ja">スキャン中…</span></div>
      </div>

      <div id="priv-results" style="display:none" class="fade-in">
        <div id="analysis-privacy"></div>

        <!-- Browser Cache -->
        <div class="card">
          <div class="card-title">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="10" r="7.5"/><path d="M6 8.5c.5-1.5 2-2.5 4-2.5 2.5 0 4 1.5 4 3.5 0 2-1.5 3.5-4 3.5H7"/><polyline points="5,11 7,13 5,15"/></svg>
            <span class="en">Browser Cache</span><span class="ja">ブラウザキャッシュ</span>
          </div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px" id="priv-cache-grid">
            <div style="font-size:13px;color:var(--fg3)"><span class="en">No browsers detected.</span><span class="ja">ブラウザが見つかりません。</span></div>
          </div>
        </div>

        <!-- Network Connections -->
        <div class="card">
          <div class="card-title">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="4" r="2"/><circle cx="4" cy="16" r="2"/><circle cx="16" cy="16" r="2"/><line x1="10" y1="6" x2="10" y2="10"/><line x1="10" y1="10" x2="4" y2="14"/><line x1="10" y1="10" x2="16" y2="14"/></svg>
            <span class="en">Active Network Connections</span><span class="ja">アクティブなネットワーク接続</span>
            <span class="card-title-right" id="priv-conn-count"></span>
          </div>
          <div id="priv-conn-empty" style="font-size:13px;color:var(--fg3);padding:8px 0;display:none"><span class="en">No established connections found.</span><span class="ja">確立された接続が見つかりません。</span></div>
          <div class="table-wrap" id="priv-conn-wrap" style="display:none">
            <table class="data-table">
              <thead><tr><th><span class="en">Process</span><span class="ja">プロセス</span></th><th><span class="en">State</span><span class="ja">状態</span></th><th><span class="en">Local</span><span class="ja">ローカル</span></th><th><span class="en">Remote</span><span class="ja">リモート</span></th></tr></thead>
              <tbody id="priv-conn-body"></tbody>
            </table>
          </div>
        </div>

        <!-- Sensitive Files -->
        <div class="card">
          <div class="card-title">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 1L2 5v5c0 4.4 3.5 8.4 8 9.8 4.5-1.4 8-5.4 8-9.8V5l-8-4z"/><line x1="10" y1="8" x2="10" y2="11"/><circle cx="10" cy="14" r=".8" fill="currentColor" stroke="none"/></svg>
            <span class="en">Sensitive File Radar</span><span class="ja">機密ファイル検出</span>
            <span class="card-title-right" id="priv-sens-count"></span>
          </div>
          <div style="font-size:13px;color:var(--fg3);padding:4px 0 12px;line-height:1.5">
            <span class="en">Files that may contain passwords, keys, or credentials. <strong style="color:var(--warn)">Review manually</strong> — nothing is read or uploaded.</span>
            <span class="ja">パスワード・鍵・認証情報を含む可能性のあるファイル。<strong style="color:var(--warn)">手動で確認</strong>してください。読み取りやアップロードはしません。</span>
          </div>
          <div id="priv-sens-empty" style="font-size:13px;color:var(--success);display:none">
            ✓ <span class="en">No sensitive files found in user space.</span><span class="ja">ユーザー領域に機密ファイルが見つかりませんでした。</span>
          </div>
          <div class="fr-list" id="priv-sens-list" style="display:none"></div>
        </div>

        <!-- Scheduled Tasks Audit -->
        <div class="card">
          <div class="card-title">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="2" width="14" height="16" rx="2"/><line x1="7" y1="7" x2="13" y2="7"/><line x1="7" y1="11" x2="13" y2="11"/><line x1="7" y1="15" x2="10" y2="15"/></svg>
            <span class="en">Scheduled Tasks</span><span class="ja">スケジュールされたタスク</span>
            <span class="card-title-right" id="priv-tasks-count"></span>
            <button class="btn btn-secondary btn-sm" onclick="loadScheduledTasks()" style="margin-left:auto"><span class="en">Scan</span><span class="ja">スキャン</span></button>
          </div>
          <div style="font-size:12px;color:var(--fg3);padding:2px 0 10px;line-height:1.5"><span class="en">Non-Microsoft scheduled tasks — review any you don't recognise.</span><span class="ja">Microsoft以外のスケジュールタスク。見覚えのないものがないか確認してください。</span></div>
          <div id="priv-tasks-idle" style="font-size:13px;color:var(--fg2);padding:4px 0"><span class="en">Click Scan to list third-party scheduled tasks.</span></div>
          <div id="priv-tasks-loading" style="display:none;padding:16px;text-align:center"><div class="spinner" style="margin:0 auto"></div></div>
          <div class="item-list" id="priv-tasks-list" style="display:none"></div>
        </div>

        <!-- Recently Installed Software -->
        <div class="card">
          <div class="card-title">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="16" height="13" rx="2"/><path d="M2 8h16"/><circle cx="6" cy="6" r="1" fill="currentColor" stroke="none"/><circle cx="9" cy="6" r="1" fill="currentColor" stroke="none"/></svg>
            <span class="en">Recently Installed Software</span><span class="ja">最近インストールされたソフトウェア</span>
            <span class="card-title-right" id="priv-soft-count"></span>
          </div>
          <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
            <select class="setting-select" id="priv-soft-days" style="font-size:11px;padding:4px 8px">
              <option value="30">Last 30 days</option>
              <option value="90" selected>Last 90 days</option>
              <option value="180">Last 180 days</option>
            </select>
            <button class="btn btn-secondary btn-sm" onclick="loadRecentSoftware()"><span class="en">Load</span><span class="ja">読み込み</span></button>
          </div>
          <div id="priv-soft-idle" style="font-size:13px;color:var(--fg2);padding:4px 0"><span class="en">Click Load to list recently installed programs.</span></div>
          <div id="priv-soft-loading" style="display:none;padding:16px;text-align:center"><div class="spinner" style="margin:0 auto"></div></div>
          <div class="item-list" id="priv-soft-list" style="display:none"></div>
        </div>

      </div>
    </div>

    <!-- ══ PAGE: Events ═══════════════════════════════════════════════════ -->
    <div class="page" id="page-events">
      <div class="page-hdr"><div><div class="page-title"><span class="en">Event Log</span><span class="ja">イベントログ</span></div><div class="page-sub"><span class="en">Windows errors, crashes &amp; warnings</span><span class="ja">Windowsのエラー・クラッシュ・警告</span></div></div>
        <div class="page-actions">
          <select class="setting-select" id="evt-hours" onchange="loadEvents()" style="font-size:13px;padding:5px 10px">
            <option value="24">Last 24 hours</option><option value="72">Last 3 days</option><option value="168" selected>Last 7 days</option><option value="720">Last 30 days</option>
          </select>
          <button class="btn btn-primary btn-sm" onclick="loadEvents()"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M1 8a7 7 0 0113.4-2.8M15 8A7 7 0 011.6 10.8"/><polyline points="1,3 1,8 6,8"/><polyline points="15,13 15,8 10,8"/></svg> <span class="en">Scan</span><span class="ja">スキャン</span></button>
        </div>
      </div>
      <div id="evt-loading" style="display:none;text-align:center;padding:40px"><div class="spinner" style="margin:0 auto"></div><div style="margin-top:12px;font-size:12px;color:var(--fg3)"><span class="en">Reading event logs…</span><span class="ja">イベントログを読み込み中…</span></div></div>
      <div id="evt-summary" style="display:none"></div>
      <div id="analysis-events"></div>
      <div id="evt-list"></div>
      <div id="evt-empty" class="idle-screen" style="padding:24px 0">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="var(--fg3)" stroke-width="1.2" stroke-linecap="round" style="opacity:.3"><rect x="4" y="3" width="16" height="18" rx="2"/><line x1="8" y1="7" x2="16" y2="7"/><line x1="8" y1="11" x2="16" y2="11"/><line x1="8" y1="15" x2="12" y2="15"/></svg>
        <div style="font-size:15px;font-weight:600;color:var(--fg);margin-top:10px"><span class="en">Click Scan to check Windows Event Logs</span><span class="ja">スキャンをクリックしてイベントログを読み込み</span></div>
        <div style="font-size:13px;color:var(--fg3);margin-top:6px;line-height:1.6"><span class="en">Scans Application &amp; System logs for errors and warnings.<br>Each event shows a plain-English explanation — no tech knowledge needed.</span><span class="ja">アプリケーション・システムログのエラー・警告を確認します。</span></div>
      </div>
    </div>

    <!-- ══ PAGE: Troubleshoot ══════════════════════════════════════════════ -->
    <div class="page" id="page-troubleshoot">
      <div class="page-hdr"><div><div class="page-title"><span class="en">Troubleshoot</span><span class="ja">トラブルシューティング</span></div><div class="page-sub"><span class="en">Diagnose issues and get guided fixes</span><span class="ja">問題を診断し、修復をガイド</span></div></div>
        <div class="page-actions"><button class="btn btn-primary btn-sm" id="btn-diagnose" onclick="runDiagnose()"><svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="7" cy="7" r="5"/><line x1="10.5" y1="10.5" x2="14" y2="14"/></svg> <span class="en">Diagnose</span><span class="ja">診断</span></button></div>
      </div>
      <div id="ts-loading" style="display:none;text-align:center;padding:40px"><div class="spinner" style="margin:0 auto"></div><div style="margin-top:12px;font-size:12px;color:var(--fg3)"><span class="en">Analyzing system…</span><span class="ja">システムを分析中…</span></div></div>
      <div id="ts-results" style="display:none"></div>
      <div id="ts-issues"></div>
      <div id="ts-empty" class="idle-screen" style="padding:24px 0">
        <svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="var(--brand)" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" style="opacity:.4"><path d="M14.5 5.5l-3 3M11.5 8.5l-5.5 5.5-1 3 3-1 5.5-5.5"/><path d="M12.5 3.5a2.5 2.5 0 013.5 3.5l-1 1"/></svg>
        <div style="font-size:14px;font-weight:500;color:var(--fg2);margin-top:8px"><span class="en">Click Diagnose to analyze your system</span><span class="ja">診断をクリックしてシステムを分析</span></div>
        <div style="font-size:11px;color:var(--fg3);margin-top:4px"><span class="en">Checks scan results, live metrics, and Windows events for issues</span><span class="ja">スキャン結果・リアルタイム指標・Windowsイベントを分析</span></div>
      </div>
    </div>

    <!-- ══ PAGE: Assistant ═══════════════════════════════════════════════════ -->
    <div class="page" id="page-assistant">
      <div class="ai-panel">
        <!-- Header -->
        <header class="ai-header">
          <div class="ai-header-left">
            <div class="ai-icon" aria-hidden="true">
              <svg width="16" height="16" aria-hidden="true"><use href="#pcha-logo"/></svg>
            </div>
            <div class="ai-title-text"><span class="en">Helix Assistant</span><span class="ja">Helixアシスタント</span></div>
          </div>
          <div class="ai-header-right">
            <div class="ai-status-pill checking" id="ai-status-pill">
              <span class="ai-status-dot"></span>
              <span class="en">Checking…</span><span class="ja">確認中…</span>
            </div>
            <button class="ai-hdr-btn" onclick="clearChat()" aria-label="Clear chat">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a1 1 0 011-1h4a1 1 0 011 1v2"/></svg>
            </button>
          </div>
        </header>
        <!-- Messages -->
        <div id="chat-messages" class="ai-msgs">
          <div class="chat-msg chat-assistant">
            <div class="chat-avatar">
              <svg width="15" height="15" aria-hidden="true"><use href="#pcha-logo"/></svg>
            </div>
            <div class="chat-bubble">
              <span class="en">Hi! I'm Helix, your PC health assistant. Try: "run a scan", "disk space", "cpu usage", or "help".</span>
              <span class="ja">Helixです。「スキャン」「ディスク」「CPU」「ヘルプ」などとどうぞ。</span>
            </div>
          </div>
        </div>
        <!-- Input -->
        <div class="ai-input-area">
          <textarea id="chat-input" rows="1" class="ai-textarea"
            placeholder="Ask about your PC health… / PCの状態を聞く…"
            onkeydown="chatKeydown(event)"
            oninput="this.style.height='auto';this.style.height=this.scrollHeight+'px'"></textarea>
          <button class="ai-send-btn" id="btn-chat-send" onclick="sendChat()" aria-label="Send message">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="14" y1="2" x2="6" y2="10"/><polyline points="14,2 9,14 6,10 2,7"/></svg>
          </button>
        </div>
      </div>
    </div>

    <!-- ══ PAGE: History ═══════════════════════════════════════════════════ -->
    <div class="page" id="page-history">
      <div class="page-hdr"><div><div class="page-title"><span class="en">Scan History</span><span class="ja">スキャン履歴</span></div><div class="page-sub"><span class="en">Previous scan results stored locally</span><span class="ja">ローカルに保存された過去のスキャン結果</span></div></div>
        <div class="page-actions"><button class="btn btn-secondary btn-sm" id="btn-hist-back" style="display:none" onclick="showHistList()">← <span class="en">Back</span><span class="ja">戻る</span></button></div></div>
      <div class="card" id="hist-trend-card" style="margin-bottom:12px;display:none">
        <div class="card-title"><span class="en">Health Score Trend</span><span class="ja">スコア推移</span></div>
        <div class="trend-wrap"><canvas id="hist-trend-canvas" height="90"></canvas></div>
      </div>
      <div class="card" id="hist-list-card"><div id="hist-entries"><div class="idle-screen" style="padding:12px 0"><span style="font-size:12px;color:var(--fg3)"><span class="en">No scan history yet</span><span class="ja">スキャン履歴はまだありません</span></span></div></div></div>
      <div id="hist-detail-card" style="display:none"></div>
    </div>

    <!-- ══ PAGE: Settings ══════════════════════════════════════════════════ -->
    <div class="page" id="page-settings">
      <div class="page-hdr"><div><div class="page-title"><span class="en">Settings</span><span class="ja">設定</span></div><div class="page-sub"><span class="en">Application preferences</span><span class="ja">アプリケーション設定</span></div></div></div>
      <div class="card">
        <div class="setting-group"><div class="setting-row"><div><div class="setting-label"><span class="en">Language</span><span class="ja">言語</span></div><div class="setting-desc"><span class="en">Display language</span><span class="ja">表示言語</span></div></div><div class="setting-control"><select class="setting-select" id="set-lang" onchange="saveSetting('language',this.value)"><option value="en">English</option><option value="ja">日本語</option></select></div></div></div>
        <div class="setting-group"><div class="setting-row"><div><div class="setting-label"><span class="en">Minimize to tray on close</span><span class="ja">閉じる時にトレイに最小化</span></div><div class="setting-desc"><span class="en">Keep running in the system tray</span><span class="ja">システムトレイで実行を継続</span></div></div><div class="setting-control"><label class="toggle"><input type="checkbox" id="set-tray" onchange="saveSetting('tray_on_close',this.checked?'true':'false')"><span class="toggle-slider"></span></label></div></div></div>
        <div class="setting-group"><div class="setting-row"><div><div class="setting-label"><span class="en">Notifications</span><span class="ja">通知</span></div><div class="setting-desc"><span class="en">Show notifications after scan</span><span class="ja">スキャン完了後に通知を表示</span></div></div><div class="setting-control"><label class="toggle"><input type="checkbox" id="set-notify" onchange="saveSetting('show_notifications',this.checked?'true':'false')"><span class="toggle-slider"></span></label></div></div></div>
        <div class="setting-group"><div class="setting-row"><div><div class="setting-label"><span class="en">Scan on startup</span><span class="ja">起動時にスキャン</span></div><div class="setting-desc"><span class="en">Run a quick scan automatically when the app opens</span><span class="ja">アプリ起動時に自動でクイックスキャンを実行</span></div></div><div class="setting-control"><label class="toggle"><input type="checkbox" id="set-scan-startup" onchange="saveSetting('scan_on_startup',this.checked?'true':'false')"><span class="toggle-slider"></span></label></div></div></div>
        <div class="setting-group"><div class="setting-row"><div><div class="setting-label"><span class="en">History retention</span><span class="ja">履歴保持期間</span></div><div class="setting-desc"><span class="en">Days to keep scan records</span><span class="ja">スキャン記録の保持日数</span></div></div><div class="setting-control"><select class="setting-select" id="set-retention" onchange="saveSetting('history_retention_days',this.value)"><option value="30">30</option><option value="60">60</option><option value="90">90</option><option value="180">180</option><option value="365">365</option></select></div></div></div>
        <div class="setting-group"><div class="setting-row"><div><div class="setting-label"><span class="en">About</span><span class="ja">アプリ情報</span></div></div></div>
          <div style="padding:8px 0 4px;display:flex;align-items:center;gap:14px">
            <svg width="36" height="42" aria-label="Helix logo"><use href="#pcha-logo"/></svg>
            <div>
              <div style="font-size:14px;font-weight:600;color:var(--fg)">Helix <span id="set-version" style="font-size:12px;font-weight:400;color:var(--fg3)">v1.0.0</span></div>
              <div style="font-size:12px;color:var(--fg3);margin-top:3px"><span class="en">Local-first PC diagnostic tool. No data is sent externally.</span><span class="ja">ローカル優先のPC診断ツール。データは外部に送信されません。</span></div>
            </div>
          </div></div>
      </div>
      <!-- Floating Launcher settings -->
      <div class="card" style="margin-top:12px">
        <div class="card-title"><span class="en">Floating Launcher</span><span class="ja">フローティングランチャー</span></div>
        <div class="setting-group"><div class="setting-row"><div><div class="setting-label"><span class="en">Enable floating icon</span><span class="ja">フローティングアイコンを有効化</span></div><div class="setting-desc"><span class="en">Show a draggable launcher on desktop</span><span class="ja">デスクトップにドラッグ可能なランチャーを表示</span></div></div><div class="setting-control"><label class="toggle"><input type="checkbox" id="set-launcher" onchange="toggleLauncher(this.checked)"><span class="toggle-slider"></span></label></div></div></div>
        <div class="setting-group"><div class="setting-row"><div><div class="setting-label"><span class="en">Show / Hide now</span><span class="ja">今すぐ表示/非表示</span></div><div class="setting-desc"><span class="en">Toggle visibility without disabling</span><span class="ja">無効化せずに表示切替</span></div></div><div class="setting-control"><button class="btn btn-secondary btn-sm" id="btn-launcher-vis" onclick="toggleLauncherVis()"><span class="en">Show</span><span class="ja">表示</span></button></div></div></div>
        <div class="setting-group"><div class="setting-row"><div><div class="setting-label"><span class="en">Start with Windows</span><span class="ja">Windows起動時に開始</span></div><div class="setting-desc"><span class="en">Launch automatically at login</span><span class="ja">ログイン時に自動起動</span></div></div><div class="setting-control"><label class="toggle"><input type="checkbox" id="set-startup" onchange="toggleStartup(this.checked)"><span class="toggle-slider"></span></label></div></div></div>
        <div class="setting-group"><div class="setting-row"><div><div class="setting-label"><span class="en">Reset position</span><span class="ja">位置をリセット</span></div><div class="setting-desc"><span class="en">Move icon back to default corner</span><span class="ja">アイコンをデフォルト位置に戻す</span></div></div><div class="setting-control"><button class="btn btn-secondary btn-sm" onclick="resetLauncherPos()"><span class="en">Reset</span><span class="ja">リセット</span></button></div></div></div>
      </div>
      <!-- Health Watch -->
      <div class="card" style="margin-top:12px">
        <div class="card-title"><span class="en">Background Health Watch</span><span class="ja">バックグラウンド監視</span></div>
        <div class="setting-group"><div class="setting-row"><div><div class="setting-label"><span class="en">Enable health alerts</span><span class="ja">健康アラートを有効化</span></div><div class="setting-desc"><span class="en">Get notified when disk or RAM cross thresholds (checks every 15 min)</span><span class="ja">ディスク・メモリが閾値を超えたら通知（15分ごとに確認）</span></div></div><div class="setting-control"><label class="toggle"><input type="checkbox" id="set-hwwatch" onchange="saveSetting('health_watch_enabled',this.checked?'true':'false')"><span class="toggle-slider"></span></label></div></div></div>
        <div class="setting-group"><div class="setting-row"><div><div class="setting-label"><span class="en">Disk alert threshold</span><span class="ja">ディスク警告閾値</span></div><div class="setting-desc"><span class="en">Alert when free space drops below this</span><span class="ja">空き容量がこの値を下回ったら警告</span></div></div><div class="setting-control"><select class="setting-select" id="set-hwdisk" onchange="saveSetting('health_watch_disk_gb',this.value)"><option value="3">3 GB</option><option value="5" selected>5 GB</option><option value="10">10 GB</option><option value="20">20 GB</option></select></div></div></div>
        <div class="setting-group"><div class="setting-row"><div><div class="setting-label"><span class="en">RAM alert threshold</span><span class="ja">メモリ警告閾値</span></div><div class="setting-desc"><span class="en">Alert when RAM usage exceeds this</span><span class="ja">メモリ使用率がこの値を超えたら警告</span></div></div><div class="setting-control"><select class="setting-select" id="set-hwram" onchange="saveSetting('health_watch_ram_pct',this.value)"><option value="85">85%</option><option value="90">90%</option><option value="92" selected>92%</option><option value="95">95%</option></select></div></div></div>
      </div>
      <!-- Quick Chat -->
      <div class="card" style="margin-top:12px">
        <div class="card-title"><span class="en">Quick Chat Assistant</span><span class="ja">クイックチャット</span></div>
        <div class="setting-group"><div class="setting-row"><div><div class="setting-label"><span class="en">Open chat popup</span><span class="ja">チャットウィンドウを開く</span></div><div class="setting-desc"><span class="en">Compact assistant window — also accessible via the floating launcher&#39;s Chat satellite</span><span class="ja">コンパクトなアシスタント。フローティングランチャーのチャットボタンからも開けます</span></div></div><div class="setting-control"><button class="btn btn-primary btn-sm" onclick="openChatPopup()"><span class="en">Open Chat</span><span class="ja">チャットを開く</span></button></div></div></div>
      </div>
    </div>
    <!-- ══ PAGE: System Info ════════════════════════════════════════════════ -->
    <div class="page" id="page-sysinfo">
      <div class="page-hdr">
        <div>
          <div class="page-title"><span class="en">System Information</span><span class="ja">システム情報</span></div>
          <div class="page-sub"><span class="en">Hardware, OS, and machine details</span><span class="ja">ハードウェア・OS・マシン詳細</span></div>
        </div>
        <div class="page-actions">
          <button class="btn btn-primary btn-sm" id="btn-sysinfo-refresh" onclick="loadSysInfo(true)">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M1 8a7 7 0 0113.4-2.8M15 8A7 7 0 011.6 10.8"/><polyline points="1,3 1,8 6,8"/><polyline points="15,13 15,8 10,8"/></svg>
            <span class="en">Refresh</span><span class="ja">更新</span>
          </button>
        </div>
      </div>
      <div id="sysinfo-loading" style="text-align:center;padding:60px">
        <div class="spinner" style="margin:0 auto;width:24px;height:24px;border-width:3px"></div>
        <div style="margin-top:14px;font-size:14px;color:var(--fg2)"><span class="en">Reading hardware info…</span><span class="ja">ハードウェア情報を読み込み中…</span></div>
      </div>
      <div id="sysinfo-results" style="display:none" class="fade-in">
        <div class="hw-grid" id="hw-cpu-wrap"></div>
        <div class="hw-grid" id="hw-ram-wrap"></div>
        <div class="hw-grid" id="hw-gpu-wrap"></div>
        <div class="hw-grid" id="hw-disk-wrap"></div>
        <div class="hw-grid" id="hw-os-wrap"></div>

        <!-- Windows Update Status -->
        <div class="card" style="margin-top:4px">
          <div class="card-title">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M10 2v4M10 14v4M2 10h4M14 10h4"/><circle cx="10" cy="10" r="4"/></svg>
            <span class="en">Windows Update</span><span class="ja">Windows Update</span>
            <button class="btn btn-secondary btn-sm" onclick="loadWinUpdate()" style="margin-left:auto"><span class="en">Check</span><span class="ja">確認</span></button>
          </div>
          <div id="si-upd-idle" style="font-size:13px;color:var(--fg2);padding:6px 0"><span class="en">Click Check to read update status.</span><span class="ja">「確認」をクリックして更新状況を確認します。</span></div>
          <div id="si-upd-loading" style="display:none;padding:16px;text-align:center"><div class="spinner" style="margin:0 auto"></div></div>
          <div id="si-upd-results" style="display:none"></div>
        </div>

        <!-- Boot Time History -->
        <div class="card" style="margin-top:4px">
          <div class="card-title">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><polyline points="2,15 6,9 9.5,12 14,5 18,8"/></svg>
            <span class="en">Boot Time History</span><span class="ja">起動時間履歴</span>
            <button class="btn btn-secondary btn-sm" onclick="loadBootTimes()" style="margin-left:auto"><span class="en">Load</span><span class="ja">読み込み</span></button>
          </div>
          <div id="si-boot-idle" style="font-size:13px;color:var(--fg2);padding:6px 0"><span class="en">Click Load to see your last 10 boot times.</span><span class="ja">「読み込み」をクリックして最近10回の起動時間を確認します。</span></div>
          <div id="si-boot-loading" style="display:none;padding:16px;text-align:center"><div class="spinner" style="margin:0 auto"></div></div>
          <div id="si-boot-results" style="display:none"></div>
        </div>

        <!-- Power Plan Switcher -->
        <div class="card" style="margin-top:4px">
          <div class="card-title">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M10 2v6l3 3-3 7"/><circle cx="10" cy="10" r="8"/></svg>
            <span class="en">Power Plan</span><span class="ja">電源プラン</span>
            <button class="btn btn-secondary btn-sm" onclick="loadPowerPlans()" style="margin-left:auto"><span class="en">Load</span><span class="ja">読み込み</span></button>
          </div>
          <div id="si-pwr-idle" style="font-size:13px;color:var(--fg2);padding:6px 0"><span class="en">Click Load to see and switch power plans.</span><span class="ja">「読み込み」をクリックして電源プランを確認・切替します。</span></div>
          <div id="si-pwr-loading" style="display:none;padding:16px;text-align:center"><div class="spinner" style="margin:0 auto"></div></div>
          <div id="si-pwr-results" style="display:none"></div>
        </div>

      </div>
    </div>

    <!-- ══ PAGE: Performance (merged: Monitor + Startup + System Info) ══════ -->
    <div class="page" id="page-performance">
      <div class="page-hdr">
        <div>
          <div class="page-title"><span class="en">Performance</span><span class="ja">パフォーマンス</span></div>
          <div class="page-sub"><span class="en">Live metrics, startup apps, and hardware info</span><span class="ja">ライブメトリクス・起動アプリ・ハードウェア情報</span></div>
        </div>
      </div>
      <div class="tab-bar">
        <button class="tab-btn active" onclick="switchTab('perf','live',this)"><span class="en">Live</span><span class="ja">ライブ</span></button>
        <button class="tab-btn" onclick="switchTab('perf','startup',this)"><span class="en">Startup apps</span><span class="ja">起動アプリ</span></button>
        <button class="tab-btn" onclick="switchTab('perf','sysinfo',this)"><span class="en">System info</span><span class="ja">システム情報</span></button>
      </div>
      <div class="tab-panel active" id="perf-tab-live" style="flex:1;overflow-y:auto"></div>
      <div class="tab-panel" id="perf-tab-startup" style="flex:1;overflow-y:auto"></div>
      <div class="tab-panel" id="perf-tab-sysinfo" style="flex:1;overflow-y:auto"></div>
    </div>

    <!-- ══ PAGE: Problems (merged: Fix + Events) ════════════════════════════ -->
    <div class="page" id="page-problems">
      <div class="page-hdr">
        <div>
          <div class="page-title"><span class="en">Problems</span><span class="ja">問題</span></div>
          <div class="page-sub"><span class="en">Diagnostics, fixes, and system event log</span><span class="ja">診断・修復・システムイベントログ</span></div>
        </div>
      </div>
      <div class="tab-bar">
        <button class="tab-btn active" onclick="switchTab('prob','fix',this)"><span class="en">Fix</span><span class="ja">修復</span></button>
        <button class="tab-btn" onclick="switchTab('prob','events',this)"><span class="en">Event log</span><span class="ja">イベントログ</span></button>
      </div>
      <div class="tab-panel active" id="prob-tab-fix" style="flex:1;overflow-y:auto"></div>
      <div class="tab-panel" id="prob-tab-events" style="flex:1;overflow-y:auto"></div>
    </div>

  </div>
</div>

<!-- Action confirm dialog -->
<div class="dialog-backdrop" id="action-dialog"><div class="dialog">
  <div class="dialog-header"><span class="dialog-title" id="action-dlg-title"></span><button class="btn btn-ghost" style="width:28px;height:28px;padding:0;justify-content:center;border-radius:var(--r)" onclick="closeActionDialog()">✕</button></div>
  <div class="dialog-body" id="action-dlg-body"></div>
  <div class="dialog-footer">
    <button class="btn btn-secondary btn-sm" onclick="closeActionDialog()"><span class="en">Cancel</span><span class="ja">キャンセル</span></button>
    <button class="btn btn-primary btn-sm" id="action-dlg-confirm" onclick="confirmAction()"><span class="en">Proceed</span><span class="ja">実行</span></button>
  </div>
</div></div>

<!-- Cleanup dialog -->
<div class="dialog-backdrop" id="cleanup-dialog"><div class="dialog">
  <div class="dialog-header"><span class="dialog-title"><span class="en">Clean Up Files</span><span class="ja">ファイルのクリーンアップ</span></span><button class="btn btn-ghost" style="width:28px;height:28px;padding:0;justify-content:center;border-radius:var(--r)" onclick="closeCleanupDialog()">✕</button></div>
  <div class="dialog-body" id="cleanup-body"></div>
  <div class="dialog-footer"><button class="btn btn-secondary btn-sm" onclick="closeCleanupDialog()"><span class="en">Cancel</span><span class="ja">キャンセル</span></button><button class="btn btn-danger btn-sm" id="btn-do-cleanup" onclick="doCleanup()">🗑 <span class="en">Delete Selected</span><span class="ja">選択を削除</span></button></div>
</div></div>

<!-- ── Command palette ──────────────────────────────────────────────────── -->
<div id="cmd-palette-backdrop" onclick="if(event.target===this)closeCmdPalette()">
  <div id="cmd-palette">
    <input id="cmd-input" placeholder="Search pages, run actions…" oninput="_cmdFilter(this.value)" onkeydown="_cmdKey(event)" autocomplete="off" spellcheck="false">
    <div id="cmd-results"></div>
    <div class="cmd-hint"><span><kbd>↑↓</kbd> <span class="en">navigate</span><span class="ja">移動</span></span><span><kbd>Enter</kbd> <span class="en">open</span><span class="ja">開く</span></span><span><kbd>Esc</kbd> <span class="en">close</span><span class="ja">閉じる</span></span></div>
  </div>
</div>

<!-- ── Keyboard shortcuts modal ─────────────────────────────────────────── -->
<div id="shortcuts-modal" onclick="if(event.target===this)closeShortcuts()">
  <div id="shortcuts-box">
    <div class="sc-title"><span><span class="en">Keyboard Shortcuts</span><span class="ja">キーボードショートカット</span></span><button class="btn btn-ghost" style="width:28px;height:28px;padding:0;justify-content:center" onclick="closeShortcuts()">✕</button></div>
    <div class="sc-group">
      <div class="sc-group-title"><span class="en">Navigation</span><span class="ja">ナビゲーション</span></div>
      <div class="sc-row"><span><span class="en">Dashboard</span><span class="ja">ダッシュボード</span></span><span class="sc-key"><kbd>Alt</kbd><kbd>1</kbd></span></div>
      <div class="sc-row"><span><span class="en">Storage</span><span class="ja">ストレージ</span></span><span class="sc-key"><kbd>Alt</kbd><kbd>2</kbd></span></div>
      <div class="sc-row"><span><span class="en">Monitor</span><span class="ja">モニター</span></span><span class="sc-key"><kbd>Alt</kbd><kbd>3</kbd></span></div>
      <div class="sc-row"><span><span class="en">Startup Apps</span><span class="ja">スタートアップ</span></span><span class="sc-key"><kbd>Alt</kbd><kbd>4</kbd></span></div>
      <div class="sc-row"><span><span class="en">Insights</span><span class="ja">インサイト</span></span><span class="sc-key"><kbd>Alt</kbd><kbd>5</kbd></span></div>
      <div class="sc-row"><span><span class="en">Organizer</span><span class="ja">オーガナイザー</span></span><span class="sc-key"><kbd>Alt</kbd><kbd>6</kbd></span></div>
      <div class="sc-row"><span><span class="en">Events</span><span class="ja">イベント</span></span><span class="sc-key"><kbd>Alt</kbd><kbd>7</kbd></span></div>
      <div class="sc-row"><span><span class="en">Fix &amp; Diagnose</span><span class="ja">修正・診断</span></span><span class="sc-key"><kbd>Alt</kbd><kbd>8</kbd></span></div>
      <div class="sc-row"><span><span class="en">Assistant</span><span class="ja">アシスタント</span></span><span class="sc-key"><kbd>Alt</kbd><kbd>9</kbd></span></div>
      <div class="sc-row"><span><span class="en">Settings</span><span class="ja">設定</span></span><span class="sc-key"><kbd>Alt</kbd><kbd>0</kbd></span></div>
    </div>
    <div class="sc-group">
      <div class="sc-group-title"><span class="en">Actions</span><span class="ja">アクション</span></div>
      <div class="sc-row"><span><span class="en">Command palette</span><span class="ja">コマンドパレット</span></span><span class="sc-key"><kbd>Ctrl</kbd><kbd>K</kbd></span></div>
      <div class="sc-row"><span><span class="en">Show shortcuts</span><span class="ja">ショートカット表示</span></span><span class="sc-key"><kbd>?</kbd></span></div>
      <div class="sc-row"><span><span class="en">Full Scan</span><span class="ja">フルスキャン</span></span><span class="sc-key"><kbd>Ctrl</kbd><kbd>Shift</kbd><kbd>S</kbd></span></div>
      <div class="sc-row"><span><span class="en">Quick Scan</span><span class="ja">クイックスキャン</span></span><span class="sc-key"><kbd>Ctrl</kbd><kbd>Shift</kbd><kbd>Q</kbd></span></div>
      <div class="sc-row"><span><span class="en">Toggle theme</span><span class="ja">テーマ切替</span></span><span class="sc-key"><kbd>Ctrl</kbd><kbd>Shift</kbd><kbd>T</kbd></span></div>
      <div class="sc-row"><span><span class="en">Close modal / overlay</span><span class="ja">モーダルを閉じる</span></span><span class="sc-key"><kbd>Esc</kbd></span></div>
    </div>
  </div>
</div>

<!-- ── Feature guide / tutorial modal ─────────────────────────────────────── -->
<div id="guide-modal" onclick="if(event.target===this)closeGuide()"
     style="position:fixed;inset:0;z-index:320;display:none;align-items:center;justify-content:center;background:rgba(5,9,18,.72);backdrop-filter:blur(6px);padding:24px">
  <div style="background:var(--bg2);border:1px solid var(--border2);border-radius:16px;box-shadow:0 20px 60px rgba(0,0,0,.6);width:min(680px,100%);max-height:86vh;display:flex;flex-direction:column;overflow:hidden">
    <div style="display:flex;align-items:center;gap:10px;padding:18px 22px;border-bottom:1px solid var(--border);flex-shrink:0">
      <svg width="24" height="24" aria-hidden="true"><use href="#pcha-logo"/></svg>
      <div style="flex:1">
        <div style="font-size:16px;font-weight:700;color:var(--fg)"><span class="en">Helix — Feature Guide</span><span class="ja">Helix — 機能ガイド</span></div>
        <div style="font-size:12px;color:var(--fg3)"><span class="en">What each part of the app does</span><span class="ja">各機能の使い方</span></div>
      </div>
      <button class="btn btn-ghost" style="width:30px;height:30px;padding:0;justify-content:center;border-radius:8px" onclick="closeGuide()">✕</button>
    </div>
    <div id="guide-body" style="overflow-y:auto;padding:12px 22px 22px"></div>
  </div>
</div>

<!-- ── Onboarding overlay ────────────────────────────────────────────────── -->
<div id="onboard-overlay">
  <div id="onboard-box">
    <div class="ob-dots" id="ob-dots"></div>
    <div id="ob-steps">
      <div class="ob-step active" id="ob-0">
        <div class="ob-icon" style="font-size:0;display:flex;align-items:center;justify-content:center;margin-bottom:16px">
          <svg width="56" height="65" aria-label="Helix logo"><use href="#pcha-logo"/></svg>
        </div>
        <div class="ob-title"><span class="en">Welcome to Helix</span><span class="ja">Helixへようこそ</span></div>
        <div class="ob-body"><span class="en">Your local-first PC health monitor. Everything runs on this machine — no data is sent anywhere.</span><span class="ja">ローカル優先のPC健康モニター。すべてこのPC上で動作し、データは外部に送信されません。</span></div>
      </div>
      <div class="ob-step" id="ob-1">
        <div class="ob-icon">🔍</div>
        <div class="ob-title"><span class="en">Run Your First Scan</span><span class="ja">最初のスキャンを実行</span></div>
        <div class="ob-body"><span class="en">Click <strong>Full Scan</strong> in the top-right to analyze disk space, temp files, startup apps, and duplicate files. Takes about 30–60 seconds.</span><span class="ja">右上の<strong>フルスキャン</strong>をクリックして、ディスク・一時ファイル・起動アプリ・重複ファイルを分析します。約30〜60秒かかります。</span></div>
      </div>
      <div class="ob-step" id="ob-2">
        <div class="ob-icon">💬</div>
        <div class="ob-title"><span class="en">Ask the Assistant</span><span class="ja">アシスタントに質問</span></div>
        <div class="ob-body"><span class="en">Type anything in the <strong>Assistant</strong> page — open apps, find files, check system info, or ask about your PC health. Powered by a local AI model.</span><span class="ja"><strong>アシスタント</strong>ページで何でも入力できます。アプリを開く、ファイルを検索、システム情報を確認など。ローカルAIモデルで動作します。</span></div>
      </div>
      <div class="ob-step" id="ob-3">
        <div class="ob-icon">⌨️</div>
        <div class="ob-title"><span class="en">Quick Tips</span><span class="ja">クイックヒント</span></div>
        <div class="ob-body"><span class="en">Press <kbd style="background:rgba(0,212,170,.15);border:1px solid rgba(0,212,170,.3);border-radius:4px;padding:1px 6px;font-size:13px">Ctrl+K</kbd> for the command palette. Press <kbd style="background:rgba(0,212,170,.15);border:1px solid rgba(0,212,170,.3);border-radius:4px;padding:1px 6px;font-size:13px">?</kbd> for keyboard shortcuts. Use <kbd style="background:rgba(0,212,170,.15);border:1px solid rgba(0,212,170,.3);border-radius:4px;padding:1px 6px;font-size:13px">Alt+1–9</kbd> to switch pages instantly.</span><span class="ja"><kbd style="background:rgba(0,212,170,.15);border:1px solid rgba(0,212,170,.3);border-radius:4px;padding:1px 6px;font-size:13px">Ctrl+K</kbd>でコマンドパレット。<kbd style="background:rgba(0,212,170,.15);border:1px solid rgba(0,212,170,.3);border-radius:4px;padding:1px 6px;font-size:13px">?</kbd>でショートカット一覧。<kbd style="background:rgba(0,212,170,.15);border:1px solid rgba(0,212,170,.3);border-radius:4px;padding:1px 6px;font-size:13px">Alt+1–9</kbd>でページ切替。</span></div>
      </div>
    </div>
    <div class="ob-actions">
      <button class="btn btn-secondary btn-sm" onclick="finishOnboarding()"><span class="en">Skip</span><span class="ja">スキップ</span></button>
      <button class="btn btn-primary" id="ob-next" onclick="nextOnboard()"><span class="en">Next →</span><span class="ja">次へ →</span></button>
    </div>
  </div>
</div>

<div id="toast"></div>

<div class="statusbar">
  <span><span class="s-dot" id="s-dot"></span><span id="s-text"><span class="en">Ready</span><span class="ja">準備完了</span></span></span>
  <span id="s-time"></span>
</div>

<script>
let scanData=null, lastUri=null, allStartupApps=[];
const CIRC_60=2*Math.PI*60;

function setLang(lang){
  document.documentElement.setAttribute('data-lang',lang);
  document.getElementById('btn-en').classList.toggle('active',lang==='en');
  document.getElementById('btn-ja').classList.toggle('active',lang==='ja');
  const ci=document.getElementById('cmd-input');
  if(ci) ci.placeholder=lang==='ja'?'ページ検索、アクション実行…':'Search pages, run actions…';
  if(window.pywebview) window.pywebview.api.save_settings({language:lang});
}

// Legacy sub-page names now live inside a tab of a container page (the pages
// are physically MOVED there by _mirrorIntoTab). Navigating straight to them
// used to blank the screen, so redirect each to its container page + tab.
const _SUBPAGE={
  monitor:{page:'performance',group:'perf',panel:'live'},
  startup:{page:'performance',group:'perf',panel:'startup'},
  sysinfo:{page:'performance',group:'perf',panel:'sysinfo'},
  organize:{page:'storage',group:'storage',panel:'organize'},
  events:{page:'problems',group:'prob',panel:'events'},
  troubleshoot:{page:'problems',group:'prob',panel:'fix'}
};
function _activateTab(group,panel){
  const prefix=group+'-tab-';
  document.querySelectorAll('[id^="'+prefix+'"]').forEach(p=>p.classList.remove('active'));
  const target=document.getElementById(prefix+panel); if(target) target.classList.add('active');
  const needle="switchTab('"+group+"','"+panel+"'";
  document.querySelectorAll('.tab-btn').forEach(b=>{
    if((b.getAttribute('onclick')||'').indexOf(needle)>-1){
      const bar=b.closest('.tab-bar'); if(bar) bar.querySelectorAll('.tab-btn').forEach(x=>x.classList.remove('active'));
      b.classList.add('active');
    }
  });
}
function showPage(name){
  const sub=_SUBPAGE[name];
  if(sub){
    showPage(sub.page);                // activate container (runs its mirror/init)
    _activateTab(sub.group,sub.panel); // then reveal the requested tab
    if(sub.group==='perf'&&sub.panel==='sysinfo') loadSysInfo();
    if(sub.group==='prob'&&sub.panel==='events') loadEvents();
    if(sub.group==='storage'&&sub.panel==='organize') orgLoadHistory();
    if(sub.group==='perf'&&sub.panel==='live') _globalPoll();
    return;
  }
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  const pg=document.getElementById('page-'+name); if(pg) pg.classList.add('active');
  const nav=document.querySelector(`[data-page="${name}"]`); if(nav) nav.classList.add('active');
  if(name==='history') loadHistory();
  if(name==='storage'){ loadRecycleBin(); _initStoragePage(); }
  if(name==='performance'){ _initPerfPage(); startGlobalPoll(); _globalPoll(); }
  if(name==='problems') _initProblemsPage();
}

function showToast(msg,type='info'){
  const t=document.getElementById('toast'); t.textContent=msg;
  t.className='show toast-'+type; setTimeout(()=>{t.className='';},3500);
}

function startScan(noDupes){ if(window.pywebview) window.pywebview.api.start_scan(noDupes); }
function cancelScan(){ if(window.pywebview) window.pywebview.api.cancel_scan(); document.getElementById('btn-cancel-scan').disabled=true; }
function openReport(){ if(lastUri && window.pywebview) window.pywebview.api.open_report(lastUri); }

let _scanRunning=false, _progressTimer=null;

function _startProgressPoll(){
  if(_progressTimer) return;
  _progressTimer = setInterval(()=>{
    if(!window.pywebview||!_scanRunning) return;
    window.pywebview.api.get_scan_progress().then(s=>{
      if(!s||!_scanRunning) return;
      const pct=s.pct||0, done=s.done||0, total=s.total||0;
      document.getElementById('prog-pct').textContent=pct;
      document.getElementById('spr-fill').style.strokeDashoffset=CIRC_60-(CIRC_60*pct/100);
      if(total>0){
        document.getElementById('prog-step').innerHTML=
          `<span class="en">Hashing ${done.toLocaleString()} / ${total.toLocaleString()} files…</span>`+
          `<span class="ja">${done.toLocaleString()} / ${total.toLocaleString()} ファイルをハッシュ中…</span>`;
      }
      document.getElementById('s-text').innerHTML=
        `<span class="en">Scanning… ${pct}%</span><span class="ja">スキャン中… ${pct}%</span>`;
    }).catch(()=>{});
  }, 300);
}

function _stopProgressPoll(){
  if(_progressTimer){ clearInterval(_progressTimer); _progressTimer=null; }
}

function onScanStart(){
  _scanRunning=true;
  ['btn-scan','btn-fast'].forEach(id=>document.getElementById(id).disabled=true);
  document.getElementById('btn-cancel-scan').disabled=false;
  // Show overlay — it lives inside #page-overview so only visible on that page
  const overlay=document.getElementById('progress-overlay');
  overlay.style.display=''; overlay.classList.add('show');
  document.getElementById('s-dot').classList.add('scanning');
  document.getElementById('prog-pct').textContent='0';
  document.getElementById('spr-fill').style.strokeDashoffset=CIRC_60;
  document.getElementById('s-text').innerHTML='<span class="en">Scanning… 0%</span><span class="ja">スキャン中… 0%</span>';
  _startProgressPoll();
}
function onScanDone(d){
  _stopProgressPoll();
  scanData=d; lastUri=d.reportUri; resetScanUI();
  document.getElementById('btn-report').style.display='inline-flex';
  document.getElementById('s-time').textContent=d.at;
  document.getElementById('s-text').innerHTML=
    `<span class="en">Scan complete · ${d.issues} issue(s) · ${d.freeable} reclaimable</span>`+
    `<span class="ja">完了 · ${d.issues}件 · ${d.freeable}解放可能</span>`;
  renderOverview(d); renderStorage(d); renderStartup(d);
  renderDashboardAnalysis(d); renderStorageAnalysis(d); renderStartupAnalysis(d);
  if(d.orgScan) renderOrgFromScan(d.orgScan);
  setTimeout(animateBars,100);
  showToast(d.issues===0?'No issues found!':'Scan complete — '+d.issues+' issue(s)', d.issues===0?'success':'info');
}
function onScanError(msg){ _stopProgressPoll(); resetScanUI(); document.getElementById('s-text').innerHTML=`<span style="color:var(--error)">Error: ${msg}</span>`; showToast('Scan failed: '+msg,'error'); }
function onScanCancelled(){ _stopProgressPoll(); resetScanUI(); document.getElementById('s-text').innerHTML=`<span class="en">Scan cancelled</span><span class="ja">キャンセル</span>`; showToast('Scan cancelled','info'); }
function resetScanUI(){ _scanRunning=false; ['btn-scan','btn-fast'].forEach(id=>document.getElementById(id).disabled=false); const o=document.getElementById('progress-overlay'); o.classList.remove('show'); o.style.display=''; document.getElementById('s-dot').classList.remove('scanning'); }

function renderOverview(d){
  document.getElementById('idle-state').style.display='none';
  document.getElementById('ov-results').style.display='block';
  document.getElementById('ov-scan-time').textContent=d.at;
  // Hero banner text
  const heroEN=d.score>=80?'Your PC is protected and performing well.':d.score>=60?'Your PC is in good shape.':d.score>=40?'Some areas need your attention.':'Your PC needs maintenance.';
  const heroJA=d.score>=80?'PCは保護され、良好な状態です。':d.score>=60?'PCは良好な状態です。':d.score>=40?'いくつかの箇所に注意が必要です。':'PCのメンテナンスが必要です。';
  document.getElementById('ov-hero-title').innerHTML=`<span class="en">${heroEN}</span><span class="ja">${heroJA}</span>`;
  document.getElementById('ov-hero-sub').innerHTML=`<span class="en">Last scan: ${d.at} · ${d.issues===0?'No critical issues found':d.issues+' issue(s) detected'}</span><span class="ja">最終スキャン: ${d.at} · ${d.issues===0?'重大な問題なし':d.issues+'件検出'}</span>`;

  const offset=CIRC_60*(1-d.score/100);
  const el=document.getElementById('hs-circle');
  const col=d.score>=80?'var(--success)':d.score>=60?'var(--brand)':d.score>=40?'var(--warn)':'var(--error)';
  el.style.stroke=col; el.style.strokeDasharray=CIRC_60; el.style.strokeDashoffset=CIRC_60;
  setTimeout(()=>{el.style.strokeDashoffset=offset;},80);
  const n=document.getElementById('hs-number'); animateNum(n,d.score,700); n.style.color=col;
  const lb=document.getElementById('hs-label'); lb.style.color=col;
  lb.innerHTML=`<span class="en">${d.scoreLabel}</span><span class="ja">${d.scoreLabelJa}</span>`;
  const descs={en:{100:'Excellent condition.',80:'Performing very well.',60:'In good shape.',40:'Needs attention.',20:'Maintenance needed.',0:'Action required.'},
    ja:{100:'優秀。',80:'とても良い。',60:'良好。',40:'注意が必要。',20:'メンテナンス必要。',0:'対処が必要。'}};
  const sk=Math.floor(d.score/20)*20;
  document.getElementById('hs-desc').innerHTML=`<span class="en">${descs.en[sk]||descs.en[60]}</span><span class="ja">${descs.ja[sk]||descs.ja[60]}</span>`;

  const il=document.getElementById('issues-list');
  if(d.issueDetails&&d.issueDetails.length>0){
    il.innerHTML=d.issueDetails.map(i=>`<div class="issue-item sev-${i.sev}" onclick="showPage('${i.page||'overview'}')"><div class="issue-dot"></div><span class="issue-text en">${i.en}</span><span class="issue-text ja">${i.ja}</span><span class="issue-arrow">›</span></div>`).join('');
  } else { il.innerHTML=`<div class="issue-item sev-ok"><div class="issue-dot"></div><span class="issue-text en">No issues — your PC is in great shape</span><span class="issue-text ja">問題なし — PCは素晴らしい状態です</span></div>`; }

  const dCol=d.diskPctFree<10?'var(--error)':d.diskPctFree<20?'var(--warn)':'inherit';
  setKpi('kpi-disk',d.diskFmtFree,dCol,`${d.diskFmtUsed} / ${d.diskTotalGb} GB`,d.diskPctFree<20?'Low':'OK',d.diskPctFree<20?'chip-warn':'chip-success');
  setKpi('kpi-free',d.freeable,d.freeableBytes>0?'var(--warn)':'inherit',`Temp: ${d.temp}`,d.freeableBytes>0?'Reclaimable':'Clean',d.freeableBytes>0?'chip-warn':'chip-success');
  setKpi('kpi-startup',d.startupTotal,d.startupPersonal>6?'var(--warn)':'inherit',`${d.startupPersonal} personal · ${d.startupIT} IT`,d.startupPersonal>6?'Many':'OK',d.startupPersonal>6?'chip-warn':'chip-success');
  const top=d.processes&&d.processes.length?d.processes[0]:null;
  setKpi('kpi-ram',top?fmtMB(top.mem_mb):'—',top&&top.mem_mb>1500?'var(--warn)':'inherit',top?top.name:'—',top&&top.mem_mb>1500?'High':'OK',top&&top.mem_mb>1500?'chip-warn':'chip-success');
  renderDiskRing('ov-disk-ring','ov-disk-pct','ov-disk-free','ov-disk-bar','ov-disk-used-l','ov-disk-free-l',d,219.91);
  document.getElementById('ov-ram-label').textContent=d.totalRamGb+' GB';
  if(d.processes&&d.processes.length){const max=d.processes[0].mem_mb;document.getElementById('ov-proc-bars').innerHTML=d.processes.slice(0,5).map(p=>{const pct=max>0?p.mem_mb/max*100:0;return`<div class="bar-row"><div class="bar-name">${esc(p.name)}</div><div class="bar-track"><div class="bar-fill bc-proc" data-pct="${pct.toFixed(1)}"></div></div><div class="bar-val">${fmtMB(p.mem_mb)}</div></div>`;}).join('');}
}

function renderStorage(d){
  document.getElementById('storage-idle').style.display='none'; document.getElementById('storage-results').style.display='block';
  if(d.tempItems&&d.tempItems.length) document.getElementById('btn-cleanup').style.display='inline-flex';
  renderDiskRing('st-disk-ring','st-disk-pct','st-disk-free','st-disk-bar','st-used-l','st-free-l',d,376.99);
  document.getElementById('st-disk-of').textContent=`of ${d.diskTotalGb} GB total`;
  document.getElementById('st-free-total').textContent=d.freeable;
  const fb=d.freeableBytes||1;
  document.getElementById('st-reclaimable').innerHTML=`<div class="bar-row" style="margin-bottom:8px"><div class="bar-name"><span class="en">Temp & Cache</span><span class="ja">一時・キャッシュ</span></div><div class="bar-track"><div class="bar-fill bc-temp" data-pct="${Math.min(d.tempBytes/fb*100,100).toFixed(1)}"></div></div><div class="bar-val">${d.temp}</div></div><div class="bar-row"><div class="bar-name"><span class="en">Duplicates</span><span class="ja">重複</span></div><div class="bar-track"><div class="bar-fill bc-cache" data-pct="${Math.min((d.dupesBytes||0)/fb*100,100).toFixed(1)}"></div></div><div class="bar-val">${d.dupes}</div></div>`;
  document.getElementById('st-temp-total').textContent=d.temp;
  if(d.tempItems&&d.tempItems.length){const max=d.tempItems[0].size;const cls={temp:'bc-temp',cache:'bc-cache',log:'bc-log'};document.getElementById('st-temp-bars').innerHTML=d.tempItems.map(t=>{const pct=max>0?t.size/max*100:0;const fp=esc(t.path).replace(/'/g,"\\'");return`<div class="bar-row" style="cursor:pointer" onclick="openPath('${fp}')" title="${esc(t.path)}"><div class="bar-name">${esc(t.name)}<small class="ja">${esc(t.nameJa)}</small></div><div class="bar-track"><div class="bar-fill ${cls[t.cat]||'bc-brand'}" data-pct="${pct.toFixed(1)}"></div></div><div class="bar-val">${t.fmt}</div></div>`;}).join('');}
  if(d.topDupes&&d.topDupes.length>0){document.getElementById('st-dupes-card').style.display='block';document.getElementById('st-dupes-badge').textContent=d.dupes+' saveable';document.getElementById('st-dupes-body').innerHTML=d.topDupes.map(g=>{const fp=esc(g.paths[0]||'').replace(/'/g,"\\'");return`<tr style="cursor:pointer" onclick="openPath('${fp}')" title="Open in Explorer"><td style="font-weight:500">${esc(g.name)}</td><td class="td-mono">${g.count}×</td><td class="td-mono" style="color:var(--warn);font-weight:600">${g.saveable}</td><td class="td-path">${esc(g.paths[0]||'—')}</td></tr>`;}).join('');}
}

function renderStartup(d){
  document.getElementById('startup-idle').style.display='none'; document.getElementById('startup-results').style.display='block';
  document.getElementById('startup-search').style.display='flex';
  document.getElementById('su-personal').textContent=d.startupPersonal; document.getElementById('su-it').textContent=d.startupIT;
  document.getElementById('su-total-label').textContent=d.startupTotal+' total';
  allStartupApps=d.startupApps||[]; renderStartupTable(allStartupApps);
}
function renderStartupTable(apps){document.getElementById('su-app-body').innerHTML=apps.filter(a=>a.name&&a.name.trim()).map(a=>`<tr><td style="font-weight:500">${esc(a.name)}</td><td><span class="${a.it?'badge badge-it':'badge badge-user'}">${a.it?'IT':'Personal'}</span></td><td class="td-path">${esc(a.cmd)}</td></tr>`).join('');}
function filterStartup(q){const lq=q.toLowerCase();renderStartupTable(lq?allStartupApps.filter(a=>a.name.toLowerCase().includes(lq)||a.cmd.toLowerCase().includes(lq)):allStartupApps);}

// ── Analysis panels ───────────────────────────────────────────────────────
function _apHTML(id,title,status,items,rec){
  const cls=status==='good'?'ap-good':status==='warn'?'ap-warn':'ap-crit';
  const label=status==='good'?'Good':status==='warn'?'Attention':'Action Needed';
  const dotColor=status==='good'?'#34D399':status==='warn'?'#FBBF24':'#F87171';
  const bullets=items.map(t=>`<li class="ap-item"><span class="ap-dot" style="background:${dotColor}"></span><span>${t}</span></li>`).join('');
  return `<div class="analysis-panel"><div class="ap-header"><span class="ap-title">${title}</span><span class="ap-badge ${cls}">${label}</span></div>`
    +`<ul class="ap-insights">${bullets}</ul>`
    +`<div class="ap-rec">💡 ${rec}</div></div>`;
}
function _setAnalysis(id,html){const el=document.getElementById(id);if(el)el.innerHTML=html;}

function renderDashboardAnalysis(d){
  const s=d.score||0;
  const status=s>=75?'good':s>=50?'warn':'crit';
  const freeGb=parseFloat(d.diskFmtFree)||0;
  const freePct=d.diskPctFree||0;
  const freeableLabel=d.freeable||'0 MB';
  const items=[
    `Health score is <strong>${s}/100</strong> — ${s>=75?'PC is performing well':s>=50?'some areas need attention':'significant maintenance required'}.`,
    `Disk has <strong>${d.diskFmtFree}</strong> free (${freePct}% available) out of ${d.diskTotalGb} GB total.`,
    `<strong>${d.issues} issue${d.issues===1?'':'s'}</strong> detected — ${d.issues===0?'no action needed':'review the list below'}.`,
    `<strong>${freeableLabel}</strong> reclaimable via temp files and duplicates${d.freeableBytes>0?' — consider cleaning up':''}.`,
    `<strong>${d.startupPersonal}</strong> personal startup app${d.startupPersonal===1?'':'s'} (${d.startupIT} IT-managed) run at boot.`,
  ];
  const rec=d.issues>0?`Run a cleanup to reclaim ${freeableLabel} and resolve ${d.issues} issue${d.issues===1?'':'s'}.`
    :freePct<20?`Disk space is low — clean up temp files or large files to stay healthy.`
    :`PC is in good shape. Re-scan weekly to stay on top of issues.`;
  _setAnalysis('analysis-overview',_apHTML('analysis-overview','Dashboard Summary',status,items,rec));
}

function renderStorageAnalysis(d){
  const freePct=d.diskPctFree||0;
  const status=freePct<10?'crit':freePct<25?'warn':'good';
  const tempMb=d.tempSizeMb||0;
  const dupCount=d.topDupes?d.topDupes.length:0;
  const items=[
    `<strong>${d.diskPctFree}%</strong> of disk is free — ${freePct<10?'critically low, action required':freePct<25?'getting tight':' healthy amount of space'}.`,
    `Temp & cache files occupy <strong>${d.temp}</strong>${tempMb>200?' — worth cleaning up':''}.`,
    d.topDupes&&d.topDupes.length?`Found <strong>${d.topDupes.length}</strong> duplicate group${d.topDupes.length===1?'':'s'} with <strong>${d.dupes}</strong> saveable space.`
      :`No duplicate files found — storage is clean.`,
    `Total reclaimable space: <strong>${d.freeable}</strong> from temp files and duplicates.`,
  ];
  const rec=freePct<10?`Disk is critically low. Run cleanup now to reclaim ${d.freeable} and free up space.`
    :d.freeableBytes>0?`Click Clean Up to reclaim ${d.freeable} of space from temp files.`
    :`Storage looks healthy. Scan periodically to catch temp file buildup.`;
  _setAnalysis('analysis-storage',_apHTML('analysis-storage','Storage Analysis',status,items,rec));
}

function renderStartupAnalysis(d){
  const personal=d.startupPersonal||0;
  const it=d.startupIT||0;
  const total=d.startupTotal||0;
  const status=personal>8?'crit':personal>5?'warn':'good';
  const apps=d.startupApps||[];
  const personalNames=apps.filter(a=>!a.it).slice(0,3).map(a=>a.name).join(', ');
  const items=[
    `<strong>${total}</strong> app${total===1?'':'s'} run at Windows startup — <strong>${personal}</strong> personal, <strong>${it}</strong> IT-managed.`,
    personal>0&&personalNames?`Personal startup apps include: <strong>${esc(personalNames)}${apps.filter(a=>!a.it).length>3?'…':''}</strong>.`
      :`All startup entries are IT-managed tools — no personal apps.`,
    `IT-managed apps (${it}) are required by your organisation and cannot be changed.`,
    personal>5?`${personal} personal startup apps may noticeably slow boot time.`:`${personal<=3?'Few':'Moderate number of'} personal apps — boot time should be unaffected.`,
  ];
  const rec=personal>8?`Many personal startup apps are slowing your boot. Disable unused ones via Task Manager > Startup.`
    :personal>5?`Consider disabling startup apps you don't need immediately at boot to speed up Windows start.`
    :`Startup looks lean. No action needed.`;
  _setAnalysis('analysis-startup',_apHTML('analysis-startup','Startup Analysis',status,items,rec));
}

function renderMonitorAnalysis(d){
  const m=d.metrics||{};
  const cpu=m.cpu_pct||0;
  const ram=m.ram_pct||0;
  const disk=m.disk_pct||0;
  const status=(cpu>85||ram>90||disk>90)?'crit':(cpu>70||ram>75||disk>80)?'warn':'good';
  const ramUsedGb=m.ram_used_mb?(m.ram_used_mb/1024).toFixed(1):'?';
  const ramTotalGb=m.ram_total_mb?(m.ram_total_mb/1024).toFixed(0):'?';
  const items=[
    `CPU usage is <strong>${cpu}%</strong> — ${cpu>85?'critically high, check for runaway processes':cpu>70?'elevated, monitor closely':'normal'.replace('normal',cpu<30?'very light load':'normal load')}.`,
    `RAM usage is <strong>${ram}%</strong> (${ramUsedGb} / ${ramTotalGb} GB) — ${ram>90?'nearly full, close unused apps':ram>75?'high but manageable':'healthy'.replace('healthy',ram<50?'plenty of headroom':'healthy')}.`,
    `Disk usage is <strong>${disk}%</strong> of C: drive — ${disk>90?'critically low on space':disk>80?'space is getting tight':'normal'.replace('normal',disk<60?'plenty of free space':'normal')}.`,
    m.has_battery?`Battery at <strong>${m.battery_pct}%</strong>${m.battery_charging?' and charging':m.battery_remaining_min>0?' — '+m.battery_remaining_min+' min remaining':''}.`
      :`No battery detected — running on AC power.`,
  ];
  const rec=(cpu>85)?`CPU is very high. Open Task Manager to identify and close resource-heavy processes.`
    :(ram>90)?`RAM is nearly full. Close unused applications or browser tabs to free memory.`
    :(disk>90)?`Disk is almost full. Run a storage cleanup to free space.`
    :`System resources look healthy. Monitor will alert you if anything spikes.`;
  _setAnalysis('analysis-monitor',_apHTML('analysis-monitor','Resource Analysis',status,items,rec));
}

function renderEventAnalysis(events,summary){
  const errors=(summary.critical||0)+(summary.errors||0);
  const warnings=summary.warnings||0;
  const total=summary.total||events.length;
  const status=errors>10?'crit':errors>3?'warn':errors>0?'warn':'good';
  const sources={};
  events.forEach(e=>{sources[e.source]=(sources[e.source]||0)+1;});
  const topSrc=Object.entries(sources).sort((a,b)=>b[1]-a[1]).slice(0,3).map(e=>`${e[0]} (${e[1]})`).join(', ');
  const items=[
    `Found <strong>${total}</strong> event${total===1?'':'s'} — <strong style="color:var(--error)">${errors}</strong> error${errors===1?'':'s'} and <strong style="color:var(--warn)">${warnings}</strong> warning${warnings===1?'':'s'}.`,
    errors===0?`No errors found — Windows event log is clean for this period.`:`<strong>${errors}</strong> error${errors===1?'':'s'} may indicate hardware issues, app crashes, or failed services.`,
    topSrc?`Most active sources: <strong>${topSrc}</strong>.`:`Event sources varied across the log.`,
    `Review the event list below for timestamps and messages to identify patterns.`,
  ];
  const rec=errors>10?`High error count — consider running the Fix & Diagnose tool to check for related issues.`
    :errors>0?`Expand error events below to read their details. Recurring sources may need investigation.`
    :warnings>10?`Many warnings logged — review if any relate to disk or memory issues.`
    :`Event log is clean. No significant errors in this period.`;
  _setAnalysis('analysis-events',_apHTML('analysis-events','Event Log Analysis',status,items,rec));
}

function renderPrivacyAnalysis(r){
  const caches=r.browserCache||{};
  const conns=r.connections||[];
  const sensitive=r.sensitiveFiles||[];
  const totalCacheMb=Object.values(caches).reduce((s,b)=>s+(b.cache_mb||0),0);
  const status=sensitive.length>0?'warn':totalCacheMb>500?'warn':conns.length>20?'warn':'good';
  const browserList=Object.keys(caches).length?Object.keys(caches).join(', '):'none detected';
  const items=[
    `Browser cache total: <strong>${totalCacheMb>1000?(totalCacheMb/1024).toFixed(1)+' GB':totalCacheMb+' MB'}</strong> across ${Object.keys(caches).length} browser${Object.keys(caches).length===1?'':'s'} (${browserList}).`,
    totalCacheMb>500?`Cache is large — clearing it can reclaim significant disk space.`:`Cache sizes are reasonable — no immediate action needed.`,
    `<strong>${conns.length}</strong> active network connection${conns.length===1?'':'s'} detected at scan time.`,
    sensitive.length?`<strong style="color:var(--warn)">${sensitive.length}</strong> file${sensitive.length===1?'':'s'} flagged as potentially sensitive (credentials or keys) — review manually.`
      :`No files flagged as sensitive in user space — privacy looks clean.`,
  ];
  const rec=sensitive.length?`Review the flagged sensitive files below. Move or rename any credentials you no longer need.`
    :totalCacheMb>500?`Consider clearing browser caches to reclaim ${totalCacheMb>1000?(totalCacheMb/1024).toFixed(1)+' GB':totalCacheMb+' MB'} of disk space.`
    :`No privacy concerns detected. Re-scan periodically to stay informed.`;
  _setAnalysis('analysis-privacy',_apHTML('analysis-privacy','Privacy Overview',status,items,rec));
}

// ── Theme toggle ─────────────────────────────────────────────────────────
function initTheme(){
  const t=localStorage.getItem('pcha_theme')||'dark';
  document.documentElement.setAttribute('data-theme',t);
  document.getElementById('btn-theme').textContent=t==='light'?'🌙':'☀️';
}
function toggleTheme(){
  const cur=document.documentElement.getAttribute('data-theme')||'dark';
  const next=cur==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',next);
  document.getElementById('btn-theme').textContent=next==='light'?'🌙':'☀️';
  localStorage.setItem('pcha_theme',next);
  if(window.pywebview) window.pywebview.api.save_settings({theme:next});
}

// ── Command palette ───────────────────────────────────────────────────────
const _CMD_ITEMS=[
  {label:'Dashboard',icon:'⊞',action:()=>showPage('overview')},
  {label:'Storage',icon:'🗄',action:()=>showPage('storage')},
  {label:'System Monitor',icon:'📈',action:()=>showPage('monitor')},
  {label:'Startup Apps',icon:'▶',action:()=>showPage('startup')},
  {label:'Insights',icon:'💡',action:()=>showPage('insights')},
  {label:'Organize Downloads',icon:'📁',action:()=>showPage('organize')},
  {label:'Event Log',icon:'📋',action:()=>showPage('events')},
  {label:'Privacy & Security',icon:'🔒',action:()=>showPage('privacy')},
  {label:'Fix & Diagnose',icon:'🔧',action:()=>showPage('troubleshoot')},
  {label:'AI Assistant',icon:'💬',action:()=>showPage('assistant')},
  {label:'System Information',icon:'🖥',action:()=>showPage('sysinfo')},
  {label:'Scan History',icon:'🕐',action:()=>showPage('history')},
  {label:'Settings',icon:'⚙',action:()=>showPage('settings')},
  {label:'Run Full Scan',icon:'🔍',action:()=>startScan(false)},
  {label:'Run Quick Scan',icon:'⚡',action:()=>startScan(true)},
  {label:'Toggle Light/Dark Mode',icon:'🌙',action:()=>toggleTheme()},
  {label:'Show Keyboard Shortcuts',icon:'⌨',action:()=>openShortcuts()},
  {label:'Open Chat Popup',icon:'💬',action:()=>openChatPopup()},
];
let _cmdIdx=0;
function openCmdPalette(){
  document.getElementById('cmd-palette-backdrop').classList.add('open');
  const inp=document.getElementById('cmd-input'); inp.value=''; inp.focus();
  _cmdFilter('');
}
function closeCmdPalette(){
  document.getElementById('cmd-palette-backdrop').classList.remove('open');
}
function _cmdFilter(q){
  const lq=q.toLowerCase().trim();
  const items=lq?_CMD_ITEMS.filter(c=>c.label.toLowerCase().includes(lq)):_CMD_ITEMS;
  _cmdIdx=0;
  document.getElementById('cmd-results').innerHTML=items.map((c,i)=>
    `<div class="cmd-item${i===0?' selected':''}" onmousedown="_cmdExec(${_CMD_ITEMS.indexOf(c)})" onmouseover="this.classList.add('selected');_cmdIdx=${i}">
      <span style="font-size:16px">${c.icon}</span><span>${c.label}</span>
    </div>`
  ).join('')||'<div style="padding:12px 16px;font-size:12px;color:var(--fg3)">No results</div>';
}
function _cmdKey(e){
  const rows=[...document.querySelectorAll('#cmd-results .cmd-item')];
  if(e.key==='ArrowDown'){e.preventDefault();_cmdIdx=Math.min(_cmdIdx+1,rows.length-1);}
  else if(e.key==='ArrowUp'){e.preventDefault();_cmdIdx=Math.max(_cmdIdx-1,0);}
  else if(e.key==='Enter'){
    if(rows[_cmdIdx]){rows[_cmdIdx].dispatchEvent(new MouseEvent('mousedown'));}
    return;
  } else if(e.key==='Escape'){closeCmdPalette();return;}
  rows.forEach((r,i)=>r.classList.toggle('selected',i===_cmdIdx));
}
function _cmdExec(idx){
  closeCmdPalette();
  if(_CMD_ITEMS[idx]) _CMD_ITEMS[idx].action();
}

// ── Keyboard shortcuts overlay ────────────────────────────────────────────
function openShortcuts(){document.getElementById('shortcuts-modal').classList.add('open');}
function closeShortcuts(){document.getElementById('shortcuts-modal').classList.remove('open');}

// ── Feature guide / tutorial ───────────────────────────────────────────────
const _GUIDE=[
  {t:['Getting started','はじめに'],items:[
    ['🔍',['Full Scan','フルスキャン'],['Analyses disk space, temp &amp; cache, startup apps and duplicate files (~30–60s). Top-right button or Ctrl+Shift+S.','ディスク・一時/キャッシュ・起動アプリ・重複ファイルを分析（約30〜60秒）。右上ボタンまたはCtrl+Shift+S。']],
    ['⚡',['Quick Scan','クイックスキャン'],['Same as Full Scan but skips duplicate hashing, so it finishes faster. Ctrl+Shift+Q.','フルスキャンと同じですが重複チェックを省くため高速です。Ctrl+Shift+Q。']],
    ['💯',['Health Score (Overview)','ヘルス スコア（概要）'],['A 0–100 score plus the issues that need attention — click any issue to jump straight to the page that fixes it.','0〜100のスコアと要対応の問題を表示。問題をクリックすると対処ページに移動します。']],
  ]},
  {t:['Performance','パフォーマンス'],items:[
    ['📈',['Live','ライブ'],['Real-time CPU, RAM, Disk, Battery and temperature gauges with CPU/RAM history and the top processes. Refreshes every 3 seconds.','CPU・メモリ・ディスク・バッテリー・温度のリアルタイム計測、CPU/メモリ履歴、上位プロセスを表示。3秒ごとに更新。']],
    ['🚀',['Startup apps','起動アプリ'],['Everything that launches when Windows starts, split into Personal and IT-managed entries.','Windows起動時に実行されるアプリを「個人」「IT管理」に分けて表示。']],
    ['🖥️',['System info','システム情報'],['Your CPU, RAM, GPU, disk and OS hardware details.','CPU・メモリ・GPU・ディスク・OSなどのハードウェア情報。']],
  ]},
  {t:['Storage','ストレージ'],items:[
    ['🗂️',['Files','ファイル'],['Disk usage, reclaimable space, temp/cache breakdown, duplicates, file-type breakdown, folder-size map and Downloads age.','ディスク使用量・解放可能容量・一時/キャッシュ内訳・重複・種別内訳・フォルダサイズマップ・ダウンロード経過日数。']],
    ['📄',['Large / Old files','大きい/古いファイル'],['Finds big or stale files. Use 📂 to reveal a file in Explorer, or 🗑 to send it to the Recycle Bin — always reversible.','大きい・古いファイルを検索。📂でエクスプローラー表示、🗑でごみ箱へ移動（復元可能）。']],
    ['🧹',['Organize','整理'],['Sorts a folder’s files into category subfolders (Images, Documents…). It never deletes — only moves — and logs every move. Use Quick Folders or Browse.','フォルダ内のファイルを種別サブフォルダ（画像・文書など）に整理。削除はせず移動のみで、全移動を記録。クイックフォルダまたは参照から。']],
  ]},
  {t:['Security &amp; Problems','セキュリティと問題'],items:[
    ['🛡️',['Security','セキュリティ'],['Browser cache sizes, active network connections and a sensitive-file radar. Read-only — nothing leaves your PC.','ブラウザキャッシュ・ネットワーク接続・機密ファイルの確認。読み取り専用で外部送信なし。']],
    ['🔧',['Problems ▸ Fix','問題 ▸ 修復'],['Runs diagnostics and offers safe, reversible fixes for common issues.','診断を実行し、一般的な問題に対して安全で元に戻せる修復を提案。']],
    ['📋',['Problems ▸ Event log','問題 ▸ イベントログ'],['Recent Windows errors and warnings, grouped and summarised.','最近のWindowsエラー・警告をまとめて表示。']],
  ]},
  {t:['Assistant, History &amp; extras','アシスタント・履歴・その他'],items:[
    ['💬',['Assistant','アシスタント'],['Type plain requests: “check my cpu”, “find resume.docx”, “open edge”, “go to storage”, “run a scan”. Commands work offline; free-form AI answers need Ollama running locally.','自然な言葉で入力：「CPUを確認」「resume.docxを探す」「edgeを開く」「ストレージへ」「スキャン実行」。コマンドはオフライン動作。自由な質問にはローカルのOllamaが必要。']],
    ['🕑',['History','履歴'],['Your past scans and health-score trend over time.','過去のスキャンとヘルススコアの推移。']],
    ['⌨️',['Command palette / shortcuts','コマンドパレット/ショートカット'],['Press Ctrl+K to jump to any page or action; press ? for the full keyboard-shortcut list.','Ctrl+Kで任意のページ/操作へ、?でショートカット一覧。']],
    ['⚙️',['Settings','設定'],['Language (EN/日本語), light/dark theme, tray behaviour, notifications, floating launcher and background health alerts.','言語・テーマ・トレイ動作・通知・フローティングランチャー・バックグラウンド警告。']],
  ]},
];
let _guideBuilt=false;
function _buildGuide(){
  const box=document.getElementById('guide-body'); if(!box) return;
  let h='<div style="display:flex;gap:8px;align-items:center;background:rgba(45,212,192,.08);border:1px solid rgba(45,212,192,.25);border-radius:10px;padding:10px 14px;margin:10px 0 16px;font-size:12.5px;color:var(--fg2)">🔒 <span><span class="en">Everything runs locally on this PC — no cloud, no telemetry, no uploads.</span><span class="ja">すべてこのPC上で動作します。クラウド・テレメトリ・アップロードは一切ありません。</span></span></div>';
  _GUIDE.forEach(function(sec){
    h+='<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--brand);margin:16px 0 8px"><span class="en">'+sec.t[0]+'</span><span class="ja">'+sec.t[1]+'</span></div>';
    sec.items.forEach(function(it){
      h+='<div style="display:flex;gap:12px;padding:10px 0;border-bottom:1px solid var(--border)">'
        +'<div style="font-size:18px;line-height:1.2;width:24px;text-align:center;flex-shrink:0">'+it[0]+'</div>'
        +'<div style="flex:1;min-width:0">'
        +'<div style="font-size:13.5px;font-weight:600;color:var(--fg);margin-bottom:2px"><span class="en">'+it[1][0]+'</span><span class="ja">'+it[1][1]+'</span></div>'
        +'<div style="font-size:12.5px;line-height:1.55;color:var(--fg2)"><span class="en">'+it[2][0]+'</span><span class="ja">'+it[2][1]+'</span></div>'
        +'</div></div>';
    });
  });
  box.innerHTML=h; _guideBuilt=true;
}
function openGuide(){ if(!_guideBuilt) _buildGuide(); document.getElementById('guide-modal').style.display='flex'; }
function closeGuide(){ document.getElementById('guide-modal').style.display='none'; }

// ── Onboarding ────────────────────────────────────────────────────────────
const _OB_STEPS=4;
let _obStep=0;
function _initOnboarding(){
  // Suppressed: the Feature Guide button (?) in the titlebar replaces this
  // flow. Hard-dismiss the overlay in case anything else opened it and set
  // the onboarded flag so nothing re-opens it.
  try{ localStorage.setItem('pcha_onboarded','1'); }catch(_){ }
  const ov=document.getElementById('onboard-overlay');
  if(ov) ov.classList.remove('open');
}
function _updateOnboard(){
  document.querySelectorAll('.ob-step').forEach((s,i)=>s.classList.toggle('active',i===_obStep));
  document.querySelectorAll('.ob-dot').forEach((d,i)=>d.classList.toggle('active',i===_obStep));
  const btn=document.getElementById('ob-next');
  if(btn) btn.innerHTML=_obStep===_OB_STEPS-1?'<span class="en">Get Started ✓</span><span class="ja">始める ✓</span>':'<span class="en">Next →</span><span class="ja">次へ →</span>';
}
function nextOnboard(){
  if(_obStep<_OB_STEPS-1){_obStep++;_updateOnboard();}
  else finishOnboarding();
}
function finishOnboarding(){
  localStorage.setItem('pcha_onboarded','1');
  document.getElementById('onboard-overlay').classList.remove('open');
}

// ── System Info page ──────────────────────────────────────────────────────
let _siLoaded=false;
function loadSysInfo(force){
  if(_siLoaded&&!force) return;
  document.getElementById('sysinfo-loading').style.display='block';
  document.getElementById('sysinfo-results').style.display='none';
  if(!window.pywebview) return;
  window.pywebview.api.get_hardware_info().then(function(d){
    document.getElementById('sysinfo-loading').style.display='none';
    document.getElementById('sysinfo-results').style.display='block';
    if(force) _siLoaded=false;  // allow next forced refresh to re-query
    _siLoaded=true;
    if(d.error){document.getElementById('sysinfo-loading').innerHTML='<div style="color:var(--error);font-size:13px">Error: '+esc(d.error)+'</div>';return;}
    _renderHW(d);
  }).catch(function(err){
    document.getElementById('sysinfo-loading').innerHTML='<div style="color:var(--error);font-size:13px">Could not load hardware info.</div>';
  });
}
function _hwCard(accentColor,label,name,specs,badge){
  const specRows=specs.map(s=>`<div class="hw-spec"><span>${s[0]}</span><span class="hw-spec-val">${s[1]}</span></div>`).join('');
  return `<div class="hw-card"><div class="hw-card-accent" style="background:linear-gradient(90deg,${accentColor},transparent)"></div>`
    +`<div class="hw-label">${label}</div><div class="hw-name">${name}</div>`
    +specRows+(badge?`<div class="hw-badge">${badge}</div>`:'')+`</div>`;
}
function _renderHW(d){
  const cpu=d.cpu||{};
  document.getElementById('hw-cpu-wrap').innerHTML=_hwCard('#00D4AA','CPU',cpu.name||'—',[
    ['Cores / Threads',`${cpu.cores||'?'} / ${cpu.threads||'?'}`],
    ['Base Clock',`${cpu.ghz||'?'} GHz`],
  ],'Processor');

  const ram=d.ram||{};
  document.getElementById('hw-ram-wrap').innerHTML=_hwCard('#A78BFA','Memory',`${ram.total_gb||'?'} GB ${ram.type||''}`,[
    ['Slots populated',ram.slots||'?'],
    ['Speed',ram.speed_mhz?(ram.speed_mhz+' MHz'):'?'],
    ['Type',ram.type||'?'],
  ],'RAM');

  const gpus=d.gpus||[];
  document.getElementById('hw-gpu-wrap').innerHTML=gpus.length?gpus.map(g=>_hwCard('#F59E0B','GPU',g.name||'—',[
    ['VRAM',g.vram_mb?(g.vram_mb>=1024?(g.vram_mb/1024).toFixed(1)+' GB':g.vram_mb+' MB'):'—'],
    ['Resolution',g.res||'—'],
  ],'Graphics')).join(''):_hwCard('#F59E0B','GPU','No GPU detected',[]);

  const disks=d.disks||[];
  document.getElementById('hw-disk-wrap').innerHTML=disks.length?disks.map(dk=>_hwCard('#34D399','Storage',dk.model||'Unknown',[
    ['Size',dk.size_gb?(dk.size_gb+' GB'):'?'],
    ['Type',dk.type||'Fixed'],
  ],'Drive')).join(''):_hwCard('#34D399','Storage','No drives detected',[]);

  const os=d.os||{}, mach=d.machine||{};
  document.getElementById('hw-os-wrap').innerHTML=
    _hwCard('#60A5FA','Operating System',os.caption||'Windows',[
      ['Build',os.build||'?'],
      ['Version',os.version||'?'],
      ['Uptime',os.uptime||'—'],
    ],'OS')
    +_hwCard('#EC4899','Machine',`${mach.maker||''} ${mach.model||''}`.trim()||'Unknown',[
      ['Domain',mach.domain||'—'],
      ['BIOS',mach.bios_mfr||'—'],
    ],'Hardware');
}

function loadHistory(){
  if(!window.pywebview) return;
  window.pywebview.api.get_history().then(rows=>{
    const el=document.getElementById('hist-entries');
    if(!rows||!rows.length){el.innerHTML=`<div class="idle-screen" style="padding:12px 0"><span style="font-size:12px;color:var(--fg3)"><span class="en">No scan history yet</span><span class="ja">スキャン履歴はまだありません</span></span></div>`;return;}
    el.innerHTML='<div class="hist-list">'+rows.map(r=>{
      const sc=r.score!=null?r.score:'—';
      const col=sc>=80?'var(--success)':sc>=60?'var(--brand)':sc>=40?'var(--warn)':'var(--error)';
      const bg=sc>=80?'rgba(52,211,153,.1)':sc>=60?'rgba(96,165,250,.1)':sc>=40?'rgba(251,191,36,.1)':'rgba(248,113,113,.1)';
      const dur=r.duration_ms?`${(r.duration_ms/1000).toFixed(1)}s`:'—';
      return`<div class="hist-card" onclick="showHistDetail('${r.id}')"><div class="hist-score" style="background:${bg};color:${col}">${sc}</div><div class="hist-body"><div class="hist-time">${esc(r.start_time||'')} <span style="color:var(--fg3);font-size:10px">${dur}</span></div><div class="hist-summary">${r.issues!=null?r.issues:'—'} issue(s) · ${esc(r.summary||r.status||'')}</div></div><div class="hist-actions"><button class="btn btn-secondary btn-sm" onclick="event.stopPropagation();showHistDetail('${r.id}')"><span class="en">Details</span><span class="ja">詳細</span></button>${r.report_path?`<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();openHistReport('${esc(r.report_path)}')">📄</button>`:''}<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();deleteHistory('${r.id}')" title="Delete">🗑</button></div></div>`;
    }).join('')+'</div>';
    renderScoreTrend(rows);
  });
}
// ── Health Score Trend chart ──────────────────────────────────────────────
function renderScoreTrend(rows){
  const valid=rows.filter(r=>r.score!=null).slice(0,20).reverse();
  if(valid.length<2){document.getElementById('hist-trend-card').style.display='none';return;}
  document.getElementById('hist-trend-card').style.display='block';
  const canvas=document.getElementById('hist-trend-canvas');
  const W=canvas.parentElement.offsetWidth||600; canvas.width=W;
  const H=90; canvas.height=H;
  const ctx=canvas.getContext('2d');
  ctx.clearRect(0,0,W,H);
  const scores=valid.map(r=>r.score);
  const labels=valid.map(r=>(r.start_time||'').slice(5,10));
  const minS=Math.max(0,Math.min(...scores)-10);
  const maxS=Math.min(100,Math.max(...scores)+5);
  const pad={l:8,r:8,t:10,b:20};
  const fx=i=>pad.l+(i/(scores.length-1))*(W-pad.l-pad.r);
  const fy=s=>pad.t+(1-(s-minS)/(maxS-minS||1))*(H-pad.t-pad.b);
  // Grid lines
  ctx.strokeStyle='rgba(255,255,255,.05)';ctx.lineWidth=1;
  [25,50,75,100].forEach(g=>{if(g>=minS&&g<=maxS){ctx.beginPath();ctx.moveTo(pad.l,fy(g));ctx.lineTo(W-pad.r,fy(g));ctx.stroke();}});
  // Gradient fill
  const grad=ctx.createLinearGradient(0,pad.t,0,H-pad.b);
  grad.addColorStop(0,'rgba(0,212,170,.3)');grad.addColorStop(1,'rgba(0,212,170,0)');
  ctx.beginPath();ctx.moveTo(fx(0),fy(scores[0]));
  scores.forEach((_,i)=>{if(i>0)ctx.lineTo(fx(i),fy(scores[i]));});
  ctx.lineTo(fx(scores.length-1),H-pad.b);ctx.lineTo(fx(0),H-pad.b);ctx.closePath();
  ctx.fillStyle=grad;ctx.fill();
  // Line
  ctx.strokeStyle='#00D4AA';ctx.lineWidth=2;ctx.lineJoin='round';ctx.beginPath();
  scores.forEach((s,i)=>{i===0?ctx.moveTo(fx(i),fy(s)):ctx.lineTo(fx(i),fy(s));});
  ctx.stroke();
  // Dots + labels
  scores.forEach((s,i)=>{
    const x=fx(i),y=fy(s);
    const col=s>=75?'#34D399':s>=50?'#FBBF24':'#F87171';
    ctx.beginPath();ctx.arc(x,y,3.5,0,Math.PI*2);ctx.fillStyle=col;ctx.fill();
    if(i===0||i===scores.length-1||i===Math.floor(scores.length/2)){
      ctx.fillStyle='rgba(255,255,255,.5)';ctx.font='9px sans-serif';ctx.textAlign='center';
      ctx.fillText(labels[i],x,H-5);
    }
  });
}

// ── Storage: File Type Breakdown ──────────────────────────────────────────
function loadFileTypes(){
  document.getElementById('st-ft-idle').style.display='none';
  document.getElementById('st-ft-loading').style.display='block';
  document.getElementById('st-ft-results').style.display='none';
  if(!window.pywebview)return;
  window.pywebview.api.get_file_type_breakdown().then(function(d){
    document.getElementById('st-ft-loading').style.display='none';
    document.getElementById('st-ft-results').style.display='block';
    _renderDonut(d.items||[]);
  });
}
function _renderDonut(items){
  const canvas=document.getElementById('st-ft-canvas');
  const ctx=canvas.getContext('2d');
  const W=160,H=160,cx=80,cy=80,R=60,r=34;
  ctx.clearRect(0,0,W,H);
  const total=items.reduce((s,x)=>s+x.size,0)||1;
  let angle=-Math.PI/2;
  items.forEach(it=>{
    const sweep=(it.size/total)*Math.PI*2;
    ctx.beginPath();ctx.moveTo(cx,cy);ctx.arc(cx,cy,R,angle,angle+sweep);ctx.closePath();
    ctx.fillStyle=it.color;ctx.fill();
    angle+=sweep;
  });
  ctx.beginPath();ctx.arc(cx,cy,r,0,Math.PI*2);
  ctx.fillStyle=getComputedStyle(document.documentElement).getPropertyValue('--bg2')||'#0F1628';
  ctx.fill();
  const leg=document.getElementById('st-ft-legend');
  leg.innerHTML=items.slice(0,7).map(it=>{
    const pct=total>0?Math.round(it.size/total*100):0;
    return`<div class="donut-row"><div class="donut-swatch" style="background:${it.color}"></div>`
      +`<span>${it.cat}</span><span class="donut-pct">${pct}% · ${it.size_str}</span></div>`;
  }).join('');
}

// ── Storage: Folder Size Treemap ──────────────────────────────────────────
function loadFolderMap(){
  document.getElementById('st-tm-idle').style.display='none';
  document.getElementById('st-tm-loading').style.display='block';
  document.getElementById('st-tm-results').style.display='none';
  if(!window.pywebview)return;
  window.pywebview.api.get_folder_sizes().then(function(d){
    document.getElementById('st-tm-loading').style.display='none';
    document.getElementById('st-tm-results').style.display='block';
    _renderTreemap(d.folders||[]);
  });
}
function _renderTreemap(folders){
  const wrap=document.getElementById('st-tm-canvas');
  if(!folders.length){wrap.innerHTML='<div style="padding:16px;font-size:13px;color:var(--fg3)">No folders found.</div>';return;}
  const W=wrap.offsetWidth||600,H=220;
  const total=folders.reduce((s,f)=>s+f.size,0)||1;
  const COLORS=['#00D4AA','#3B82F6','#A78BFA','#F59E0B','#34D399','#F87171','#60A5FA','#EC4899','#FBBF24','#6366F1'];
  // Simple squarified: split along longest axis
  function layout(items,x,y,w,h){
    if(!items.length)return[];
    const sz=items.reduce((s,i)=>s+i.size,0)||1;
    let cells=[],cx=x,cy=y;
    if(w>=h){
      items.forEach((it,i)=>{
        const cw=Math.round((it.size/sz)*w);
        cells.push({...it,x:cx,y,w:Math.max(cw,1),h});
        cx+=cw;
      });
    } else {
      items.forEach((it,i)=>{
        const ch=Math.round((it.size/sz)*h);
        cells.push({...it,x,y:cy,w,h:Math.max(ch,1)});
        cy+=ch;
      });
    }
    return cells;
  }
  const cells=layout(folders,0,0,W,H);
  wrap.style.height=H+'px';
  wrap.innerHTML=cells.map((c,i)=>{
    const col=COLORS[i%COLORS.length];
    const safePath=c.path.replace(/\\/g,'\\\\').replace(/'/g,"\\'");
    return`<div class="treemap-cell" style="left:${c.x}px;top:${c.y}px;width:${c.w}px;height:${c.h}px;background:${col};opacity:.85" onclick="openPath('${safePath}')" title="${esc(c.path)} — ${c.size_str}">
      ${c.w>50&&c.h>28?`<div class="treemap-name">${esc(c.name)}</div><div class="treemap-size">${c.size_str}</div>`:''}
    </div>`;
  }).join('');
}

// ── Storage: Downloads Aging ──────────────────────────────────────────────
function loadDownloadsAge(){
  document.getElementById('st-da-idle').style.display='none';
  document.getElementById('st-da-loading').style.display='block';
  document.getElementById('st-da-results').style.display='none';
  if(!window.pywebview)return;
  window.pywebview.api.get_downloads_aging().then(function(d){
    document.getElementById('st-da-loading').style.display='none';
    document.getElementById('st-da-results').style.display='block';
    const COLORS={'<30d':'#34D399','30–90d':'#60A5FA','90–180d':'#FBBF24','>180d':'#F87171'};
    document.getElementById('st-da-grid').innerHTML=(d.buckets||[]).map(b=>
      `<div class="age-card"><div class="age-count" style="color:${COLORS[b.label]||'var(--warn)'}">${b.count}</div>`+
      `<div class="age-label">${esc(b.label)}</div><div class="age-size">${b.size_str}</div></div>`
    ).join('');
    const note=document.getElementById('st-da-note');
    const old=(d.buckets||[]).find(b=>b.label==='>180d');
    if(old&&old.count>0)note.innerHTML=`<span style="color:var(--warn)">⚠ ${old.count} file${old.count===1?'':'s'} older than 6 months (${old.size_str}) — consider reviewing your Downloads folder.</span>`;
    else note.textContent=`${d.total||0} items in Downloads.`;
  });
}

// ── Privacy: Scheduled Tasks ──────────────────────────────────────────────
function loadScheduledTasks(){
  document.getElementById('priv-tasks-idle').style.display='none';
  document.getElementById('priv-tasks-loading').style.display='block';
  document.getElementById('priv-tasks-list').style.display='none';
  if(!window.pywebview)return;
  window.pywebview.api.get_scheduled_tasks().then(function(d){
    document.getElementById('priv-tasks-loading').style.display='none';
    const tasks=d.tasks||[];
    const cnt=document.getElementById('priv-tasks-count');
    if(cnt) cnt.textContent=tasks.length+' found';
    const list=document.getElementById('priv-tasks-list');
    if(!tasks.length){list.style.display='block';list.innerHTML='<div style="font-size:13px;color:var(--success);padding:8px 0">✓ No third-party scheduled tasks found.</div>';return;}
    list.style.display='block';
    list.innerHTML=tasks.map(t=>
      `<div class="item-row"><div class="item-name" title="${esc(t.execute||'')}">
        <span style="color:var(--brand)">${esc(t.path)}</span>${esc(t.name)}
      </div><span class="item-meta">${esc(t.state)}</span></div>`
    ).join('');
  });
}

// ── Privacy: Recently Installed Software ─────────────────────────────────
function loadRecentSoftware(){
  document.getElementById('priv-soft-idle').style.display='none';
  document.getElementById('priv-soft-loading').style.display='block';
  document.getElementById('priv-soft-list').style.display='none';
  if(!window.pywebview)return;
  const days=parseInt(document.getElementById('priv-soft-days').value)||90;
  window.pywebview.api.get_installed_software(days).then(function(d){
    document.getElementById('priv-soft-loading').style.display='none';
    const items=d.items||[];
    const cnt=document.getElementById('priv-soft-count');
    if(cnt) cnt.textContent=items.length+' found';
    const list=document.getElementById('priv-soft-list');
    if(!items.length){list.style.display='block';list.innerHTML='<div style="font-size:13px;color:var(--fg3);padding:8px 0">No software installed in this period.</div>';return;}
    list.style.display='block';
    list.innerHTML=items.map(x=>{
      const date=x.date?(x.date.slice(0,4)+'-'+x.date.slice(4,6)+'-'+x.date.slice(6,8)):'';
      const sz=x.size_kb>1024?Math.round(x.size_kb/1024)+' MB':x.size_kb?x.size_kb+' KB':'';
      return`<div class="item-row"><div class="item-name">${esc(x.name)}</div>`+
        `<span class="item-meta">${esc(x.publisher||'')}</span>`+
        `<span class="item-meta">${date}</span>`+
        (sz?`<span class="item-meta">${sz}</span>`:'')+
        `</div>`;
    }).join('');
  });
}

// ── System Info: Windows Update ───────────────────────────────────────────
function loadWinUpdate(){
  document.getElementById('si-upd-idle').style.display='none';
  document.getElementById('si-upd-loading').style.display='block';
  document.getElementById('si-upd-results').style.display='none';
  if(!window.pywebview)return;
  window.pywebview.api.get_windows_update_status().then(function(d){
    document.getElementById('si-upd-loading').style.display='none';
    document.getElementById('si-upd-results').style.display='block';
    const p=d.pending;
    const cls=p===0?'upd-ok':p>0?'upd-warn':'upd-crit';
    const icon=p===0?'✓':p>0?'⚠':'?';
    const msg=p===0?'System is up to date.':p>0?`${p} update${p===1?'':'s'} pending.`:'Could not check for updates.';
    document.getElementById('si-upd-results').innerHTML=
      `<div class="upd-status ${cls}"><span style="font-size:18px">${icon}</span>`+
      `<div><div style="font-size:13px;font-weight:600;color:var(--fg)">${msg}</div>`+
      `<div style="font-size:11px;color:var(--fg3);margin-top:3px">Last installed: ${esc(d.last_update||'Unknown')}${d.last_title?' — '+esc(d.last_title.slice(0,60)):''}</div></div></div>`;
  });
}

// ── System Info: Boot Times ───────────────────────────────────────────────
function loadBootTimes(){
  document.getElementById('si-boot-idle').style.display='none';
  document.getElementById('si-boot-loading').style.display='block';
  document.getElementById('si-boot-results').style.display='none';
  if(!window.pywebview)return;
  window.pywebview.api.get_boot_times().then(function(d){
    document.getElementById('si-boot-loading').style.display='none';
    document.getElementById('si-boot-results').style.display='block';
    const items=d.items||[];
    if(!items.length){document.getElementById('si-boot-results').innerHTML='<div style="font-size:13px;color:var(--fg3);padding:8px 0">No boot time data available (requires Diagnostics-Performance event log).</div>';return;}
    const maxMs=Math.max(...items.map(x=>x.boot_ms))||1;
    document.getElementById('si-boot-results').innerHTML=items.map(x=>{
      const pct=Math.round(x.boot_ms/maxMs*100);
      const sec=(x.boot_ms/1000).toFixed(1);
      const col=x.boot_ms<20000?'#34D399':x.boot_ms<40000?'#FBBF24':'#F87171';
      return`<div class="boot-row"><span class="boot-date">${esc(x.date)}</span>`+
        `<div class="boot-bar-track"><div class="boot-bar-fill" style="width:${pct}%;background:linear-gradient(90deg,${col},${col}88)"></div></div>`+
        `<span class="boot-val">${sec}s</span></div>`;
    }).join('');
  });
}

// ── System Info: Power Plan ───────────────────────────────────────────────
function loadPowerPlans(){
  document.getElementById('si-pwr-idle').style.display='none';
  document.getElementById('si-pwr-loading').style.display='block';
  document.getElementById('si-pwr-results').style.display='none';
  if(!window.pywebview)return;
  window.pywebview.api.get_power_plans().then(function(d){
    document.getElementById('si-pwr-loading').style.display='none';
    document.getElementById('si-pwr-results').style.display='block';
    const plans=d.plans||[];
    document.getElementById('si-pwr-results').innerHTML=
      '<div class="plan-chips" id="plan-chips-wrap">'+
      plans.map(p=>`<button class="plan-chip${p.active?' active':''}" id="pc-${p.guid}" onclick="switchPowerPlan('${p.guid}')">${esc(p.name)}</button>`).join('')+
      '</div><div id="plan-msg" style="font-size:12px;color:var(--fg3);margin-top:8px"></div>';
  });
}
function switchPowerPlan(guid){
  if(!window.pywebview)return;
  document.getElementById('plan-msg').textContent='Switching…';
  window.pywebview.api.set_power_plan(guid).then(function(r){
    document.getElementById('plan-msg').textContent=r.msg||'';
    if(r.ok){
      document.querySelectorAll('.plan-chip').forEach(c=>c.classList.remove('active'));
      const btn=document.getElementById('pc-'+guid);
      if(btn) btn.classList.add('active');
      showToast('Power plan switched','success');
    }
  });
}

function showHistList(){
  document.getElementById('hist-list-card').style.display='block';
  document.getElementById('hist-detail-card').style.display='none';
  document.getElementById('btn-hist-back').style.display='none';
}
function showHistDetail(id){
  if(!window.pywebview)return;
  document.getElementById('hist-list-card').style.display='none';
  document.getElementById('btn-hist-back').style.display='inline-flex';
  const dc=document.getElementById('hist-detail-card');
  dc.style.display='block';
  dc.innerHTML='<div class="card" style="text-align:center;padding:30px"><div class="spinner" style="margin:0 auto"></div><div style="margin-top:10px;font-size:12px;color:var(--fg3)">Loading…</div></div>';
  window.pywebview.api.get_scan_detail(id).then(r=>{
    if(!r){dc.innerHTML='<div class="card"><div style="font-size:12px;color:var(--fg3)">Scan not found.</div></div>';return;}
    const sc=r.score!=null?r.score:'—';
    const col=sc>=80?'var(--success)':sc>=60?'var(--brand)':sc>=40?'var(--warn)':'var(--error)';
    const dur=r.duration_ms?`${(r.duration_ms/1000).toFixed(1)}s`:'—';
    let data=null;
    try{data=r.data_json?JSON.parse(r.data_json):null;}catch(e){}

    let detailHtml=`<div class="card fade-in" style="margin-bottom:16px">
      <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px">
        <div style="width:60px;height:60px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:700;font-family:var(--mono);background:radial-gradient(circle,rgba(0,212,170,.12),transparent);border:2px solid ${col};color:${col}">${sc}</div>
        <div style="flex:1"><div style="font-size:16px;font-weight:700">${esc(r.start_time||'')}</div>
          <div style="font-size:12px;color:var(--fg3);margin-top:2px">${r.issues!=null?r.issues:0} issue(s) · ${dur} · ${esc(r.status||'')}</div></div>
        ${r.report_path?`<button class="btn btn-accent btn-sm" onclick="openHistReport('${esc(r.report_path)}')">📄 <span class="en">Full Report</span><span class="ja">レポート</span></button>`:''}
      </div>
      <div style="font-size:12px;color:var(--fg2);line-height:1.6;padding:10px 14px;background:rgba(255,255,255,.03);border-radius:var(--r-lg);border:1px solid rgba(255,255,255,.05)">${esc(r.summary||'No summary')}</div>
    </div>`;

    if(data){
      const dk=data.disk||{};
      detailHtml+=`<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">
        <div class="card card-sm" style="text-align:center"><div style="font-size:9px;color:var(--brand);font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px"><span class="en">Disk Free</span><span class="ja">空き</span></div><div style="font-size:1.3rem;font-weight:700;font-family:var(--mono)">${dk.fmt_free||'—'}</div><div style="font-size:10px;color:var(--fg3)">${dk.pct_free||0}% free</div></div>
        <div class="card card-sm" style="text-align:center"><div style="font-size:9px;color:var(--brand);font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px"><span class="en">Temp Files</span><span class="ja">一時ファイル</span></div><div style="font-size:1.3rem;font-weight:700;font-family:var(--mono)">${data.total_temp_fmt||'—'}</div></div>
        <div class="card card-sm" style="text-align:center"><div style="font-size:9px;color:var(--brand);font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px"><span class="en">Duplicates</span><span class="ja">重複</span></div><div style="font-size:1.3rem;font-weight:700;font-family:var(--mono)">${data.total_dupes_fmt||'—'}</div></div>
        <div class="card card-sm" style="text-align:center"><div style="font-size:9px;color:var(--brand);font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px"><span class="en">Startup</span><span class="ja">起動</span></div><div style="font-size:1.3rem;font-weight:700;font-family:var(--mono)">${data.startup?data.startup.length:'—'}</div></div>
      </div>`;

      if(data.processes&&data.processes.length){
        const pMax=data.processes[0].mem_mb;
        detailHtml+=`<div class="card" style="margin-bottom:16px"><div class="card-title"><span class="en">Top Processes</span><span class="ja">上位プロセス</span><span class="card-title-right">${data.total_ram_gb||0} GB RAM</span></div>
          <div class="bar-list">${data.processes.slice(0,8).map(p=>{const pw=pMax>0?p.mem_mb/pMax*100:0;return`<div class="bar-row"><div class="bar-name">${esc(p.name)}</div><div class="bar-track"><div class="bar-fill bc-proc" style="width:${pw.toFixed(1)}%"></div></div><div class="bar-val">${fmtMB(p.mem_mb)}</div></div>`;}).join('')}</div></div>`;
      }
      if(data.temp_items&&data.temp_items.length){
        const tMax=data.temp_items[0].size;
        const cls={temp:'bc-temp',cache:'bc-cache',log:'bc-log'};
        detailHtml+=`<div class="card"><div class="card-title"><span class="en">Temp &amp; Cache</span><span class="ja">一時・キャッシュ</span><span class="card-title-right">${data.total_temp_fmt||''}</span></div>
          <div class="bar-list">${data.temp_items.slice(0,10).map(t=>{const pw=tMax>0?t.size/tMax*100:0;return`<div class="bar-row"><div class="bar-name">${esc(t.name_en)}</div><div class="bar-track"><div class="bar-fill ${cls[t.category]||'bc-brand'}" style="width:${pw.toFixed(1)}%"></div></div><div class="bar-val">${t.size_fmt}</div></div>`;}).join('')}</div></div>`;
      }
    }
    dc.innerHTML=detailHtml;
  });
}
function openHistReport(p){if(window.pywebview){window.pywebview.api.open_report('file:///'+p.replace(/\\/g,'/'));}}
function deleteHistory(id){if(!window.pywebview)return;window.pywebview.api.delete_history_item(id).then(ok=>{if(ok){showToast('Deleted','info');loadHistory();}});}

function showCleanupDialog(){
  if(!scanData||!scanData.tempItems)return;
  document.getElementById('cleanup-body').innerHTML=`<div style="font-size:12px;color:var(--fg2);margin-bottom:10px"><span class="en">Select files to delete. Protected system files cannot be removed.</span><span class="ja">削除するファイルを選択。保護されたシステムファイルは削除できません。</span></div>`+scanData.tempItems.map((t,i)=>`<div class="cleanup-item"><label><input type="checkbox" checked data-path="${esc(t.path)}" data-idx="${i}"><span>${esc(t.name)}</span></label><span class="cleanup-size">${t.fmt}</span></div>`).join('');
  document.getElementById('cleanup-dialog').classList.add('show');
}
function closeCleanupDialog(){document.getElementById('cleanup-dialog').classList.remove('show');}
function doCleanup(){
  const paths=[...document.querySelectorAll('#cleanup-body input:checked')].map(c=>c.dataset.path);
  if(!paths.length){showToast('Nothing selected','info');return;}
  const btn=document.getElementById('btn-do-cleanup');
  const resetBtn=()=>{btn.disabled=false;btn.innerHTML='🗑 <span class="en">Delete Selected</span><span class="ja">選択を削除</span>';};
  const runWithToken=(token)=>{
    btn.disabled=true;btn.innerHTML='<div class="spinner"></div> Cleaning…';
    window.pywebview.api.cleanup_files(paths,token).then(res=>{
      closeCleanupDialog();resetBtn();
      const ok=(res.results||[]).filter(r=>r.status==='deleted').length;
      const fail=(res.results||[]).filter(r=>r.status==='failed').length;
      showToast(`Cleaned ${ok} item(s), freed ${res.totalFreedFmt||'0 B'}${fail?' · '+fail+' failed':''}`,fail?'error':'success');
    });
  };
  // First call returns a confirmation token; user must OK the delete count.
  window.pywebview.api.cleanup_files(paths).then(res=>{
    if(res && res.needs_confirm){
      if(!confirm(res.msg)) return;
      runWithToken(res.token);
      return;
    }
    // Fallback: backend ran inline (shouldn't normally happen for non-empty paths)
    closeCleanupDialog();resetBtn();
    const ok=(res.results||[]).filter(r=>r.status==='deleted').length;
    showToast(`Cleaned ${ok} item(s), freed ${res.totalFreedFmt||'0 B'}`,'success');
  });
}

function saveSetting(key,val){
  if(key==='language') setLang(val);
  if(window.pywebview){const s={};s[key]=val;window.pywebview.api.save_settings(s).then(()=>{if(key!=='language')showToast('Setting saved','success');});}
}
function loadSettings(){
  if(!window.pywebview)return;
  window.pywebview.api.get_settings().then(s=>{
    if(s.language){setLang(s.language);document.getElementById('set-lang').value=s.language;}
    document.getElementById('set-tray').checked=(s.tray_on_close==='true');
    document.getElementById('set-notify').checked=(s.show_notifications==='true');
    if(s.history_retention_days)document.getElementById('set-retention').value=s.history_retention_days;
    if(s.version)document.getElementById('set-version').textContent='v'+s.version;
    // Health watch settings
    const hwEl=document.getElementById('set-hwwatch');
    if(hwEl) hwEl.checked=(s.health_watch_enabled==='true');
    const hwDisk=document.getElementById('set-hwdisk');
    if(hwDisk&&s.health_watch_disk_gb) hwDisk.value=s.health_watch_disk_gb;
    const hwRam=document.getElementById('set-hwram');
    if(hwRam&&s.health_watch_ram_pct) hwRam.value=s.health_watch_ram_pct;
    const scanStartEl=document.getElementById('set-scan-startup');
    if(scanStartEl) scanStartEl.checked=(s.scan_on_startup==='true');
    if(s.scan_on_startup==='true'&&!_scanRunning) setTimeout(()=>startScan(true),2000);
  });
  window.pywebview.api.get_launcher_enabled().then(v=>{
    const el=document.getElementById('set-launcher'); if(el) el.checked=!!v;
    const visBtn=document.getElementById('btn-launcher-vis');
    if(visBtn) visBtn.innerHTML=v?'<span class="en">Hide</span><span class="ja">非表示</span>':'<span class="en">Show</span><span class="ja">表示</span>';
  });
  window.pywebview.api.get_launcher_startup().then(v=>{
    const el=document.getElementById('set-startup'); if(el) el.checked=!!v;
  });
}

function toggleLauncher(enabled){
  if(!window.pywebview) return;
  window.pywebview.api.set_launcher_enabled(enabled);
  const visBtn=document.getElementById('btn-launcher-vis');
  if(visBtn) visBtn.innerHTML=enabled?'<span class="en">Hide</span><span class="ja">非表示</span>':'<span class="en">Show</span><span class="ja">表示</span>';
}
function toggleLauncherVis(){
  if(!window.pywebview) return;
  window.pywebview.api.toggle_launcher_visibility().then(state=>{
    const visBtn=document.getElementById('btn-launcher-vis');
    if(visBtn) visBtn.innerHTML=state==='visible'?'<span class="en">Hide</span><span class="ja">非表示</span>':'<span class="en">Show</span><span class="ja">表示</span>';
  });
}
function toggleStartup(enabled){
  if(!window.pywebview) return;
  window.pywebview.api.set_launcher_startup(enabled);
}
function resetLauncherPos(){
  if(!window.pywebview) return;
  window.pywebview.api.reset_launcher_position().then(()=>showToast('Position reset','success'));
}
function toggleChatFloat(enabled){
  if(!window.pywebview) return;
  window.pywebview.api.set_chat_float_enabled(enabled);
}
function openChatPopup(){
  if(!window.pywebview) return;
  window.pywebview.api.open_chat_popup();
}

function renderDiskRing(ringId,pctId,freeId,barId,usedLId,freeLId,d,circ){
  const usedPct=Math.round(d.diskUsedGb/d.diskTotalGb*100);const offset=circ*(1-usedPct/100);
  const col=d.diskPctFree<10?'var(--error)':d.diskPctFree<20?'var(--warn)':'var(--brand)';
  const el=document.getElementById(ringId);el.style.stroke=col;el.style.strokeDasharray=circ;el.style.strokeDashoffset=circ;
  setTimeout(()=>{el.style.strokeDashoffset=offset;},100);
  document.getElementById(pctId).textContent=usedPct+'%';document.getElementById(pctId).style.color=col;
  document.getElementById(freeId).textContent=d.diskFmtFree;document.getElementById(freeId).style.color=col;
  document.getElementById(barId).style.background=col;document.getElementById(barId).setAttribute('data-pct',usedPct);
  document.getElementById(usedLId).textContent=d.diskFmtUsed+' used';document.getElementById(freeLId).textContent=d.diskFmtFree+' free';
}
function setKpi(id,val,col,sub,chip,cls){const v=document.getElementById(id);v.textContent=val;v.style.color=col||'inherit';const s=document.getElementById(id+'-sub');if(s)s.textContent=sub||'';const c=document.getElementById(id+'-chip');if(c&&chip){c.textContent=chip;c.className='kpi-chip '+cls;}}
function fmtMB(mb){return mb>=1024?(mb/1024).toFixed(1)+' GB':mb+' MB';}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function animateBars(){document.querySelectorAll('.bar-fill[data-pct],.disk-seg-used[data-pct],.ram-gauge-fill[data-pct]').forEach(el=>{el.style.width=el.dataset.pct+'%';});}
document.addEventListener('keydown',e=>{if(e.altKey){const map={'1':'overview','2':'storage','3':'monitor','4':'startup','5':'insights','6':'organize','7':'events','8':'troubleshoot','9':'assistant','0':'settings'};if(map[e.key]){e.preventDefault();showPage(map[e.key]);}}});
function loadSysInfo(){if(!window.pywebview)return;window.pywebview.api.get_system_info().then(info=>{const el=document.getElementById('sysinfo-area');if(!el)return;el.innerHTML=[{l:'OS',v:info.os||'Windows'},{l:'CPU',v:info.cpu||'—'},{l:'RAM',v:info.ram_gb?info.ram_gb+' GB':'—'}].map(i=>`<div class="sysinfo-item"><div class="sysinfo-label">${i.l}</div><div class="sysinfo-value">${esc(i.v)}</div></div>`).join('');});}

// ── Insights ─────────────────────────────────────────────────────────────
let _insightsData=[];
const _catIcons={storage:'💾',cleanup:'🧹',performance:'⚡',hardware:'🔧'};
const _sevLabels={critical:'Critical',error:'Error',warning:'Warning',info:'Info'};
const _sevLabelsJa={critical:'重大',error:'エラー',warning:'注意',info:'情報'};

function loadInsights(){
  if(!window.pywebview)return;
  document.getElementById('insights-loading').style.display='block';
  document.getElementById('insights-list').innerHTML='';
  document.getElementById('insights-empty').style.display='none';
  document.getElementById('ai-summary-card').style.display='none';
  document.getElementById('btn-refresh-insights').disabled=true;
  window.pywebview.api.get_suggestions().then(data=>{
    _insightsData=data||[];
    document.getElementById('insights-loading').style.display='none';
    document.getElementById('btn-refresh-insights').disabled=false;
    if(!_insightsData.length){document.getElementById('insights-empty').style.display='flex';return;}
    document.getElementById('insights-empty').style.display='none';
    renderInsights(_insightsData);
    loadAiSummary(_insightsData);
  });
}

function loadAiSummary(suggestions){
  if(!window.pywebview||!suggestions.length)return;
  const aiEl=document.getElementById('ai-summary-card');
  const lang=document.documentElement.getAttribute('data-lang')||'en';
  aiEl.style.display='block';
  aiEl.innerHTML=`<div class="ai-card"><div class="ai-card-header"><span class="ai-card-badge"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6"/><path d="M6 6.5C6 5.67 6.67 5 7.5 5h1c.83 0 1.5.67 1.5 1.5 0 .83-.67 1.5-1.5 1.5H8v1.5"/><circle cx="8" cy="11.5" r=".5" fill="currentColor"/></svg> AI Analysis</span><span class="ai-card-model"></span></div><div class="ai-card-loading"><div class="spinner"></div><span class="en">Generating AI analysis…</span><span class="ja">AI分析を生成中…</span></div></div>`;
  const stripped=suggestions.map(s=>({severity:s.severity,en:s.en,ja:s.ja,metric:s.metric}));
  window.pywebview.api.get_ai_summary(stripped,lang).then(res=>{
    if(!res||!res.summary){
      aiEl.innerHTML=`<div class="ai-card"><div class="ai-card-header"><span class="ai-card-badge"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6"/><path d="M6 6.5C6 5.67 6.67 5 7.5 5h1c.83 0 1.5.67 1.5 1.5 0 .83-.67 1.5-1.5 1.5H8v1.5"/><circle cx="8" cy="11.5" r=".5" fill="currentColor"/></svg> AI Analysis</span></div><div style="font-size:11px;color:var(--fg3)"><span class="en">AI analysis unavailable — make sure Ollama is running locally.</span><span class="ja">AI分析を利用できません — Ollamaがローカルで実行されているか確認してください。</span></div></div>`;
      return;
    }
    aiEl.innerHTML=`<div class="ai-card fade-in"><div class="ai-card-header"><span class="ai-card-badge"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6"/><path d="M6 6.5C6 5.67 6.67 5 7.5 5h1c.83 0 1.5.67 1.5 1.5 0 .83-.67 1.5-1.5 1.5H8v1.5"/><circle cx="8" cy="11.5" r=".5" fill="currentColor"/></svg> AI Analysis</span><span class="ai-card-model">${esc(res.model||'')}</span></div><div class="ai-card-body">${esc(res.summary)}</div></div>`;
  });
}

function shortPath(p){
  if(!p)return'';
  const parts=p.replace(/\\/g,'/').split('/');
  if(parts.length<=3)return parts.join('/');
  return'…/'+parts.slice(-2).join('/');
}

function openPath(p){if(window.pywebview)window.pywebview.api.open_in_explorer(p);}

function renderInsights(items){
  const el=document.getElementById('insights-list');
  el.innerHTML=items.map((s,idx)=>{
    const icon=_catIcons[s.category]||'💡';
    const sevEN=_sevLabels[s.severity]||'Info';
    const sevJA=_sevLabelsJa[s.severity]||'情報';
    const hasItems=s.items&&s.items.length>0;

    let itemsHtml='';
    if(hasItems&&(s.id==='large_files'||s.id==='old_downloads')){
      const rows=s.items.slice(0,10).map(f=>{
        const fp=esc(f.path||'');
        return`<tr class="ii-row" onclick="openPath('${fp.replace(/'/g,"\\'")}')">
          <td class="ii-name" title="${fp}"><span style="color:var(--fg)">${esc(f.name||'')}</span><br><span style="font-size:9px;color:var(--fg3);font-family:var(--mono)">${fp}</span></td>
          <td class="ii-size">${f.fmt||''}</td>
          <td style="width:30px;text-align:center"><span style="cursor:pointer;opacity:.5;font-size:12px" title="Open in Explorer">📂</span></td></tr>`;
      }).join('');
      itemsHtml=`<div class="insight-items" id="insight-items-${idx}" style="display:none"><table>${rows}</table></div>`;
    } else if(hasItems&&s.id==='folder_usage'){
      const rows=s.items.slice(0,8).map(f=>{
        const fp=esc(f.path||'');
        return`<tr class="ii-row" onclick="openPath('${fp.replace(/'/g,"\\'")}')">
          <td class="ii-name"><span style="color:var(--fg)">${esc(f.name)}</span><br><span style="font-size:9px;color:var(--fg3);font-family:var(--mono)">${fp}</span></td>
          <td class="ii-size">${f.fmt||''}</td>
          <td style="width:30px;text-align:center"><span style="cursor:pointer;opacity:.5;font-size:12px" title="Open in Explorer">📂</span></td></tr>`;
      }).join('');
      itemsHtml=`<div class="insight-items" id="insight-items-${idx}" style="display:none"><table>${rows}</table></div>`;
    } else if(hasItems&&s.id==='empty_folders'){
      const rows=s.items.slice(0,20).map(p=>{
        const fp=esc(p);
        return`<tr class="ii-row" onclick="openPath('${fp.replace(/'/g,"\\'")}')">
          <td class="ii-name" title="${fp}"><span style="font-family:var(--mono);font-size:10px;color:var(--fg)">${fp}</span></td>
          <td style="width:30px;text-align:center"><span style="cursor:pointer;opacity:.5;font-size:12px" title="Open in Explorer">📂</span></td></tr>`;
      }).join('');
      itemsHtml=`<div class="insight-items" id="insight-items-${idx}" style="display:none"><table>${rows}</table></div>`;
    }

    let viewBtn='';
    if(hasItems) viewBtn=`<button class="btn btn-sm btn-secondary" onclick="toggleInsightItems(${idx})"><span class="en" id="insight-view-en-${idx}">▶ View ${s.items.length} items</span><span class="ja" id="insight-view-ja-${idx}">▶ ${s.items.length}件 表示</span></button>`;

    let actionBtn='';
    if(s.action==='show_storage')actionBtn=`<button class="btn btn-sm btn-secondary" onclick="showPage('storage')"><span class="en">Go to Storage</span><span class="ja">ストレージへ</span></button>`;
    else if(s.action==='show_startup')actionBtn=`<button class="btn btn-sm btn-secondary" onclick="showPage('startup')"><span class="en">Go to Startup</span><span class="ja">スタートアップへ</span></button>`;
    else if(s.action==='run_scan')actionBtn=`<button class="btn btn-sm btn-primary" onclick="startScan(false)"><span class="en">Scan</span><span class="ja">スキャン</span></button>`;
    else if(s.action==='list_empty_folders')actionBtn=`<button class="btn btn-sm btn-danger" onclick="removeEmptyFolders(${idx})"><span class="en">Remove All</span><span class="ja">全て削除</span></button>`;

    return`<div class="insight-card sev-${s.severity} fade-in">
      <div class="insight-header">
        <div class="insight-icon">${icon}</div>
        <div class="insight-body">
          <div class="insight-title"><span class="insight-badge"><span class="en">${sevEN}</span><span class="ja">${sevJA}</span></span> <span class="en">${esc(s.en)}</span><span class="ja">${esc(s.ja)}</span></div>
          <div class="insight-desc"><span class="en">${esc(s.detail_en||'')}</span><span class="ja">${esc(s.detail_ja||'')}</span></div>
          ${itemsHtml}
          <div class="insight-actions">${viewBtn}${actionBtn}</div>
        </div>
      </div>
    </div>`;
  }).join('');
}

function toggleInsightItems(idx){
  const el=document.getElementById('insight-items-'+idx);
  if(!el)return;
  const showing=el.style.display!=='none';
  el.style.display=showing?'none':'block';
  const enLbl=document.getElementById('insight-view-en-'+idx);
  const jaLbl=document.getElementById('insight-view-ja-'+idx);
  if(enLbl)enLbl.textContent=showing?'View':'▼ Hide';
  if(jaLbl)jaLbl.textContent=showing?'表示':'▼ 隠す';
}

function removeEmptyFolders(idx){
  const s=_insightsData[idx];
  if(!s||!s.items)return;
  if(!window.pywebview)return;
  window.pywebview.api.remove_empty_folders(s.items).then(res=>{
    showToast(`Removed ${res.removed} empty folders${res.failed?' ('+res.failed+' failed)':''}`,res.failed?'error':'success');
    loadInsights();
  });
}

// ── File Organizer ──────────────────────────────────────────────────────
let _orgFolder='', _orgData=null;
(function(){
  const dl=localStorage.getItem('orgFolder');
  const home=navigator.userAgent.includes('Windows')?'':'';
  _orgFolder=dl||'';
})();

function orgSetFolder(f){
  _orgFolder=f; localStorage.setItem('orgFolder',f);
  document.getElementById('org-folder-path').textContent=f;
}

async function orgQuick(key){
  if(!window.pywebview) return;
  // Ask Python for the real path of a known folder
  const pathMap = await window.pywebview.api.get_known_folders();
  const f = pathMap[key];
  if(f){ orgSetFolder(f); orgScan(); }
}

async function orgLoadHistory(){
  if(!window.pywebview||!_orgFolder) return;
  const r = await window.pywebview.api.get_organize_history(_orgFolder);
  const el = document.getElementById('org-history');
  const body = document.getElementById('org-history-body');
  const countEl = document.getElementById('org-hist-count');
  if(!r.sessions||r.sessions.length===0){ el.style.display='none'; return; }
  el.style.display='block';
  const totalFiles = r.sessions.reduce((s,sess)=>s+(sess.moves||[]).length,0);
  const lang=document.documentElement.getAttribute('data-lang');
  countEl.textContent = lang==='ja'
    ? `(${r.sessions.length}セッション · ${totalFiles}ファイル)`
    : `(${r.sessions.length} session${r.sessions.length!==1?'s':''} · ${totalFiles} file${totalFiles!==1?'s':''})`;

  // Category color map from organizer
  const CAT_COLORS = {
    Documents:'#3B82F6',Spreadsheets:'#34D399',Presentations:'#F97316',
    Images:'#A78BFA',Videos:'#EC4899',Audio:'#F472B6',Archives:'#FBBF24',
    Installers:'#FB923C',Code:'#2DD4BF',Data:'#60A5FA',Fonts:'#8B5CF6',
    Design:'#E879F9',Other:'#5A6478'
  };
  const CAT_ICONS = {
    Documents:'📄',Spreadsheets:'📊',Presentations:'📽',Images:'🖼',
    Videos:'🎬',Audio:'🎵',Archives:'📦',Installers:'💿',
    Code:'💻',Data:'🗃',Fonts:'🔤',Design:'🎨',Other:'📁'
  };

  let html = '';
  r.sessions.forEach((sess,si)=>{
    const moves = sess.moves||[];
    const ts = sess.timestamp||'';
    const cat = sess.category||'';
    const color = CAT_COLORS[cat]||'#5A6478';
    const icon = CAT_ICONS[cat]||'📁';
    const catFolder = _orgFolder.replace(/\\/g,'\\\\') + '\\\\' + cat;
    html += `
    <div class="card" style="margin-bottom:8px;overflow:hidden;border-left:3px solid ${color}">
      <div style="display:flex;align-items:center;gap:10px;padding:10px 14px;cursor:pointer" onclick="toggleEl('org-hist-${si}','org-hist-chev2-${si}')">
        <span style="font-size:16px">${icon}</span>
        <div style="flex:1">
          <div style="font-size:12px;font-weight:600;color:var(--fg)">${cat}/</div>
          <div style="font-size:10px;color:var(--fg3)">${ts} &middot; ${moves.length} ${lang==='ja'?'ファイル':'file'}${moves.length!==1?'s':''}</div>
        </div>
        <button class="btn btn-secondary btn-sm" style="font-size:10px;padding:3px 8px" onclick="event.stopPropagation();openPath('${catFolder}')" title="${lang==='ja'?'フォルダを開く':'Open folder'}">
          <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M1 4h6l2 2h6v8H1z"/></svg>
        </button>
        <svg id="org-hist-chev2-${si}" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="var(--fg3)" stroke-width="2" stroke-linecap="round" style="transition:transform .2s"><polyline points="4,6 8,10 12,6"/></svg>
      </div>
      <div id="org-hist-${si}" style="display:none;border-top:1px solid rgba(255,255,255,.06);padding:0 8px 8px">
        ${moves.map(m=>{
          const fname = m.name||m.from.split('\\\\').pop()||m.from.split('/').pop()||'';
          const toPath = m.to.replace(/\\\\/g,'\\\\\\\\');
          return `<div style="display:flex;align-items:center;gap:6px;padding:5px 6px;border-radius:6px;font-size:11px;margin-top:4px;background:rgba(255,255,255,.03)">
            <span style="flex:1;font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--fg2)">${fname}</span>
            <svg width="14" height="10" viewBox="0 0 20 10" fill="none"><path d="M1 5h16m0 0l-4-4m4 4l-4 4" stroke="${color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
            <span style="font-size:10px;color:${color};font-weight:600;white-space:nowrap">${cat}/</span>
            <button style="background:none;border:none;cursor:pointer;padding:2px 4px;border-radius:4px;color:var(--fg3)" onclick="openPath('${toPath}')" title="${lang==='ja'?'ファイルを開く':'Open file'}">
              <svg width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 3H3a2 2 0 00-2 2v8a2 2 0 002 2h8a2 2 0 002-2v-2"/><polyline points="9,1 15,1 15,7"/><line x1="15" y1="1" x2="7" y2="9"/></svg>
            </button>
          </div>`;
        }).join('')}
      </div>
    </div>`;
  });
  body.innerHTML = html;
}

function toggleOrgHistory(){
  const body = document.getElementById('org-history-body');
  const chev = document.getElementById('org-hist-chev');
  const hidden = body.style.display==='none'||!body.style.display;
  body.style.display = hidden ? 'block' : 'none';
  chev.style.transform = hidden ? 'rotate(180deg)' : '';
}

function toggleEl(id, chevId){
  const el = document.getElementById(id);
  const chev = document.getElementById(chevId);
  if(!el) return;
  const hidden = el.style.display==='none'||!el.style.display;
  el.style.display = hidden ? 'block' : 'none';
  if(chev) chev.style.transform = hidden ? 'rotate(180deg)' : '';
}

async function orgBrowse(){
  if(!window.pywebview) return;
  const f=await window.pywebview.api.browse_folder();
  if(f) { orgSetFolder(f); orgScan(); }
}

async function orgScan(){
  if(!window.pywebview) return;
  if(!_orgFolder){
    const dl=await window.pywebview.api.browse_folder();
    if(!dl) return;
    orgSetFolder(dl);
  }
  document.getElementById('org-loading').style.display='block';
  document.getElementById('org-categories').innerHTML='';
  document.getElementById('org-summary').style.display='none';
  document.getElementById('org-ai-card').style.display='none';
  document.getElementById('org-empty').style.display='none';

  const r=await window.pywebview.api.scan_organize(_orgFolder);
  document.getElementById('org-loading').style.display='none';
  _orgData=r;

  if(r.error){
    showToast(r.error,'error'); document.getElementById('org-empty').style.display=''; return;
  }
  if(!r.categories||Object.keys(r.categories).length===0){
    document.getElementById('org-empty').style.display=''; return;
  }
  renderOrgSummary(r);
  renderOrgCategories(r.categories);
  orgLoadAI(r);
  orgLoadHistory();
}

function renderOrgFromScan(orgInfo){
  // Populate organize page with scan results (summary-only, not full file lists)
  orgSetFolder(orgInfo.folder);
  document.getElementById('org-empty').style.display='none';
  const lang=document.documentElement.getAttribute('data-lang');
  // Show summary tiles
  const sumEl=document.getElementById('org-summary');
  sumEl.style.display='flex';
  sumEl.innerHTML=`
    <div style="display:flex;gap:10px;margin-bottom:10px;flex-wrap:wrap">
      <div class="card" style="flex:1;min-width:120px;padding:12px 16px;text-align:center">
        <div style="font-size:22px;font-weight:700;color:var(--brand)">${orgInfo.total_files}</div>
        <div style="font-size:10px;color:var(--fg3)">${lang==='ja'?'ファイル':'Files'}</div>
      </div>
      <div class="card" style="flex:1;min-width:120px;padding:12px 16px;text-align:center">
        <div style="font-size:22px;font-weight:700;color:var(--brand)">${orgInfo.category_count}</div>
        <div style="font-size:10px;color:var(--fg3)">${lang==='ja'?'カテゴリ':'Categories'}</div>
      </div>
      <div class="card" style="flex:1;min-width:120px;padding:12px 16px;text-align:center">
        <div style="font-size:22px;font-weight:700;color:var(--brand)">${orgInfo.total_fmt}</div>
        <div style="font-size:10px;color:var(--fg3)">${lang==='ja'?'合計サイズ':'Total Size'}</div>
      </div>
    </div>`;
  // Show category overview cards (no file lists yet — user clicks Scan for full detail)
  const catEl=document.getElementById('org-categories');
  const entries=Object.entries(orgInfo.categories);
  catEl.innerHTML=entries.map(([name,cat])=>`
    <div class="card" style="margin-bottom:8px;padding:14px 16px;display:flex;align-items:center;gap:12px">
      <span style="font-size:18px">${cat.icon}</span>
      <div style="flex:1">
        <div style="font-size:13px;font-weight:600;color:var(--fg)">${name}</div>
        <div style="font-size:11px;color:var(--fg3)">${cat.count} ${lang==='ja'?'ファイル':'files'} · ${cat.total_fmt}</div>
      </div>
    </div>`).join('');
  // Add a hint to do full org scan for details
  catEl.innerHTML+=`<div style="text-align:center;padding:12px 0">
    <button class="btn btn-primary btn-sm" onclick="orgScan()" style="padding:8px 20px">
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="7" cy="7" r="5"/><line x1="10.5" y1="10.5" x2="14" y2="14"/></svg>
      <span class="en">Full Organize Scan</span><span class="ja">詳細スキャン</span>
    </button>
    <div style="font-size:10px;color:var(--fg3);margin-top:6px"><span class="en">Click to see all files and organize them into folders</span><span class="ja">クリックして全ファイルを表示し、フォルダに整理</span></div>
  </div>`;
}

function renderOrgSummary(r){
  const el=document.getElementById('org-summary');
  const lang=document.documentElement.getAttribute('data-lang');
  el.style.display='flex';
  el.innerHTML=`
    <div style="display:flex;gap:10px;margin-bottom:10px;flex-wrap:wrap">
      <div class="card" style="flex:1;min-width:120px;padding:12px 16px;text-align:center">
        <div style="font-size:22px;font-weight:700;color:var(--brand)">${r.total_files}</div>
        <div style="font-size:10px;color:var(--fg3)">${lang==='ja'?'ファイル':'Files'}</div>
      </div>
      <div class="card" style="flex:1;min-width:120px;padding:12px 16px;text-align:center">
        <div style="font-size:22px;font-weight:700;color:var(--brand)">${r.category_count}</div>
        <div style="font-size:10px;color:var(--fg3)">${lang==='ja'?'カテゴリ':'Categories'}</div>
      </div>
      <div class="card" style="flex:1;min-width:120px;padding:12px 16px;text-align:center">
        <div style="font-size:22px;font-weight:700;color:var(--brand)">${r.total_fmt}</div>
        <div style="font-size:10px;color:var(--fg3)">${lang==='ja'?'合計サイズ':'Total Size'}</div>
      </div>
    </div>`;
}

function renderOrgCategories(cats){
  const el=document.getElementById('org-categories');
  const lang=document.documentElement.getAttribute('data-lang');
  let html='';
  const entries=Object.entries(cats);
  entries.forEach(([name,cat],idx)=>{
    const fileRows=cat.files.map((f,fi)=>`
      <div class="ii-row" style="display:flex;align-items:center;gap:8px;padding:5px 8px;font-size:11px;border-bottom:1px solid rgba(255,255,255,.04);cursor:pointer" onclick="openPath('${f.path.replace(/\\/g,'\\\\')}')">
        <span style="flex:1;font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--fg2)">${f.name}</span>
        <span style="color:var(--fg3);white-space:nowrap">${f.fmt}</span>
        <span style="color:var(--fg3);white-space:nowrap;font-size:10px">${f.modified}</span>
      </div>`).join('');

    html+=`
    <div class="card" style="margin-bottom:8px;overflow:hidden">
      <div style="display:flex;align-items:center;gap:10px;padding:12px 14px;cursor:pointer" onclick="toggleOrgCat(${idx})">
        <span style="font-size:18px">${cat.icon}</span>
        <div style="flex:1">
          <div style="font-size:13px;font-weight:600;color:var(--fg)">${name}</div>
          <div style="font-size:11px;color:var(--fg3)">${cat.count} ${lang==='ja'?'ファイル':'files'} · ${cat.total_fmt}</div>
        </div>
        <button class="btn btn-primary btn-sm" style="font-size:10px;padding:4px 12px" onclick="event.stopPropagation();orgApply('${name}',${idx})">
          <span class="en">Organize</span><span class="ja">整理する</span>
        </button>
        <svg class="org-chevron" id="org-chev-${idx}" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="var(--fg3)" stroke-width="2" stroke-linecap="round" style="transition:transform .2s"><polyline points="4,6 8,10 12,6"/></svg>
      </div>
      <div id="org-files-${idx}" style="display:none;max-height:240px;overflow-y:auto;border-top:1px solid rgba(255,255,255,.06)">
        ${fileRows}
      </div>
    </div>`;
  });
  el.innerHTML=html;
}

function toggleOrgCat(idx){
  const el=document.getElementById('org-files-'+idx);
  const chev=document.getElementById('org-chev-'+idx);
  if(el.style.display==='none'){
    el.style.display='block'; chev.style.transform='rotate(180deg)';
  } else {
    el.style.display='none'; chev.style.transform='';
  }
}

async function orgApply(catName,idx){
  if(!window.pywebview||!_orgData) return;
  const cats=_orgData.categories;
  const cat=cats[catName];
  if(!cat||!cat.files.length) return;
  const paths=cat.files.map(f=>f.path);
  const lang=document.documentElement.getAttribute('data-lang');

  const btn=document.querySelectorAll('#org-categories .btn-primary')[idx];
  if(btn){ btn.disabled=true; btn.innerHTML=`<span class="spinner" style="width:12px;height:12px;border-width:2px"></span>`; }

  const r=await window.pywebview.api.apply_organize(_orgFolder,catName,paths);
  if(btn){ btn.disabled=false; btn.innerHTML=`<span class="en">Done</span><span class="ja">完了</span>`; }

  if(r.moved>0){
    const dest=r.target_dir||(_orgFolder+'\\'+catName);
    showToast(`${lang==='ja'?`${r.moved}件を移動 → ${dest}`:`Moved ${r.moved} file${r.moved!==1?'s':''} → ${dest}`}`,r.failed?'info':'success');
    setTimeout(()=>{ orgScan(); orgLoadHistory(); },500);
  } else {
    showToast(r.errors&&r.errors[0]||'No files moved','error');
  }
}

async function orgLoadAI(scanResult){
  const el=document.getElementById('org-ai-card');
  const lang=document.documentElement.getAttribute('data-lang');
  el.style.display='block';
  el.innerHTML=`<div class="card" style="padding:14px;position:relative;overflow:hidden">
    <div style="position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--brand),#3B82F6);animation:shimmer 2s infinite"></div>
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
      <span style="font-size:14px">🤖</span>
      <span style="font-size:12px;font-weight:600;color:var(--brand)">AI ${lang==='ja'?'アドバイス':'Recommendation'}</span>
      <span class="spinner" id="org-ai-spinner" style="width:12px;height:12px;border-width:2px"></span>
    </div>
    <div id="org-ai-text" style="font-size:12px;color:var(--fg2);line-height:1.6"><span class="en">Analyzing categories…</span><span class="ja">カテゴリを分析中…</span></div>
  </div>`;

  const r=await window.pywebview.api.get_organize_ai(scanResult,lang);
  const spinner=document.getElementById('org-ai-spinner');
  if(spinner) spinner.style.display='none';
  const txt=document.getElementById('org-ai-text');
  if(txt) txt.textContent=r.summary||'No recommendation available.';
}

// ── System Monitor ─────────────────────────────────────────────────────
const MON_CIRC=2*Math.PI*34;
let _monTimer=null;
let _globalMonTimer=null;

// ── Animated number counter ───────────────────────────────────────────────
function animateNum(el,to,duration){
  if(!el) return;
  duration=duration||500;
  const from=parseFloat(el.textContent)||0;
  if(from===to) return;
  const start=performance.now();
  function step(now){
    const t=Math.min((now-start)/duration,1);
    const ease=t<.5?2*t*t:1-Math.pow(-2*t+2,2)/2;
    el.textContent=Math.round(from+(to-from)*ease);
    if(t<1) requestAnimationFrame(step);
    else el.textContent=to;
  }
  requestAnimationFrame(step);
}

// ── Ripple on click ────────────────────────────────────────────────────────
function addRipple(e){
  const el=e.currentTarget;
  const r=document.createElement('span');
  r.className='ripple-wave';
  const rect=el.getBoundingClientRect();
  const sz=Math.max(rect.width,rect.height)*2;
  r.style.cssText=`width:${sz}px;height:${sz}px;left:${e.clientX-rect.left-sz/2}px;top:${e.clientY-rect.top-sz/2}px`;
  el.appendChild(r);
  r.addEventListener('animationend',()=>r.remove(),{once:true});
}
document.querySelectorAll('.kpi.clickable').forEach(el=>el.addEventListener('click',addRipple));

// ── Live pills + idle vitals update ──────────────────────────────────────
function _setPill(pillId,valId,val,warnAt,critAt){
  const v=document.getElementById(valId); if(v) v.textContent=val;
  const p=document.getElementById(pillId);
  if(p){ p.classList.toggle('warn',val>=warnAt&&val<critAt); p.classList.toggle('crit',val>=critAt); }
}
function _setLvCard(cardId,valId,barId,subId,val,sub,color,warnAt,critAt){
  const v=document.getElementById(valId); if(v) animateNum(v,val,400);
  const b=document.getElementById(barId); if(b) b.style.width=val+'%';
  if(color&&b) b.style.background=color;
  const s=document.getElementById(subId); if(s) s.textContent=sub||'';
  const c=document.getElementById(cardId);
  if(c){ c.classList.toggle('warn',val>=warnAt&&val<critAt); c.classList.toggle('crit',val>=critAt); }
}
function updateLivePills(m){
  const cpu=m.cpu_pct||0, ram=m.ram_pct||0, disk=m.disk_pct||0;
  // Update sidebar live widget
  const _sv=(id,v)=>{const el=document.getElementById(id);if(el)el.textContent=v+'%';};
  const _sb=(id,v)=>{const el=document.getElementById(id);if(el)el.style.width=Math.min(v,100)+'%';};
  _sv('lp-cpu-v',cpu); _sb('lp-cpu-bar',cpu);
  _sv('lp-ram-v',ram); _sb('lp-ram-bar',ram);
  _sv('lp-disk-v',disk); _sb('lp-disk-bar',disk);
  // Idle vitals on dashboard
  const ramUsed=m.ram_used_mb?`${(m.ram_used_mb/1024).toFixed(1)}/${(m.ram_total_mb/1024).toFixed(0)} GB`:'';
  const diskInfo=m.disk_used_gb?`${m.disk_used_gb}/${m.disk_total_gb} GB`:'';
  _setLvCard('lv-cpu-card','lv-cpu-v','lv-cpu-bar','lv-cpu-sub',cpu,'',null,70,90);
  _setLvCard('lv-ram-card','lv-ram-v','lv-ram-bar','lv-ram-sub',ram,ramUsed,'#A78BFA',80,95);
  _setLvCard('lv-disk-card','lv-disk-v','lv-disk-bar','lv-disk-sub',disk,diskInfo,'#3B82F6',80,92);
  // Status pulse
  const pulse=document.getElementById('status-pulse');
  if(pulse){
    if(cpu>=90||ram>=95) pulse.className='status-pulse crit';
    else if(cpu>=70||ram>=80||disk>=80) pulse.className='status-pulse warn';
    else pulse.className='status-pulse';
  }
}

// Single consolidated metrics poll: updates sidebar pills always, and the live
// gauges only when the Performance → Live tab is visible. One bridge call per tick.
function _globalPoll(){
  if(!window.pywebview||!window.pywebview.api||!window.pywebview.api.get_monitor_data) return;
  window.pywebview.api.get_monitor_data().then(d=>{
    if(!d) return;
    updateLivePills(d.metrics||{});
    const perf=document.getElementById('page-performance');
    const live=document.getElementById('perf-tab-live');
    if(perf&&perf.classList.contains('active')&&live&&live.classList.contains('active')) updateMonGauges(d);
  }).catch(()=>{});
}
function startGlobalPoll(){
  if(_globalMonTimer) return;
  _globalPoll();
  _globalMonTimer=setInterval(_globalPoll,3000);
}

function drawSparkline(canvasId,data,color){
  const c=document.getElementById(canvasId); if(!c) return;
  const ctx=c.getContext('2d');
  const w=c.width, h=c.height;
  ctx.clearRect(0,0,w,h);
  if(!data||data.length<2) return;
  const max=Math.max(100,...data), min=0;
  const step=w/(data.length-1);
  // fill
  ctx.beginPath();
  ctx.moveTo(0,h);
  data.forEach((v,i)=>{ ctx.lineTo(i*step,h-(v-min)/(max-min)*h); });
  ctx.lineTo(w,h); ctx.closePath();
  const grd=ctx.createLinearGradient(0,0,0,h);
  grd.addColorStop(0,color+'44'); grd.addColorStop(1,color+'05');
  ctx.fillStyle=grd; ctx.fill();
  // line
  ctx.beginPath();
  data.forEach((v,i)=>{ const y=h-(v-min)/(max-min)*h; i===0?ctx.moveTo(0,y):ctx.lineTo(i*step,y); });
  ctx.strokeStyle=color; ctx.lineWidth=1.5; ctx.stroke();
}

function updateMonGauges(d){
  const m=d.metrics||{};
  // CPU
  const cpuOff=MON_CIRC*(1-m.cpu_pct/100);
  const cpuRing=document.getElementById('mon-cpu-ring');
  if(cpuRing) cpuRing.style.strokeDashoffset=cpuOff;
  const cpuPct=document.getElementById('mon-cpu-pct');
  if(cpuPct) cpuPct.textContent=m.cpu_pct||0;
  // RAM
  const ramOff=MON_CIRC*(1-m.ram_pct/100);
  const ramRing=document.getElementById('mon-ram-ring');
  if(ramRing) ramRing.style.strokeDashoffset=ramOff;
  const ramPct=document.getElementById('mon-ram-pct');
  if(ramPct) ramPct.textContent=m.ram_pct||0;
  const ramLbl=document.getElementById('mon-ram-label');
  if(ramLbl) ramLbl.textContent=`${(m.ram_used_mb/1024).toFixed(1)} / ${(m.ram_total_mb/1024).toFixed(0)} GB`;
  // Disk
  const diskOff=MON_CIRC*(1-m.disk_pct/100);
  const diskRing=document.getElementById('mon-disk-ring');
  if(diskRing) diskRing.style.strokeDashoffset=diskOff;
  const diskPct=document.getElementById('mon-disk-pct');
  if(diskPct) diskPct.textContent=m.disk_pct||0;
  const diskLbl=document.getElementById('mon-disk-label');
  if(diskLbl) diskLbl.textContent=`${m.disk_used_gb} / ${m.disk_total_gb} GB`;
  // Battery
  const batCard=document.getElementById('mon-battery-card');
  if(m.has_battery){
    if(batCard) batCard.style.display='';
    const batOff=MON_CIRC*(1-(m.battery_pct||0)/100);
    const batRing=document.getElementById('mon-bat-ring');
    if(batRing){ batRing.style.strokeDashoffset=batOff; batRing.style.stroke=m.battery_charging?'#34D399':'#FBBF24'; }
    const batPct=document.getElementById('mon-bat-pct');
    if(batPct) batPct.textContent=m.battery_pct;
    const batLbl=document.getElementById('mon-bat-label');
    if(batLbl){
      if(m.battery_charging) batLbl.innerHTML='<span class="en">Charging</span><span class="ja">充電中</span>';
      else if(m.battery_remaining_min>0) batLbl.textContent=`${m.battery_remaining_min} min`;
      else batLbl.innerHTML='<span class="en">On battery</span><span class="ja">バッテリー</span>';
    }
  } else if(batCard){ batCard.style.display='none'; }
  // Temperature
  const tempCard=document.getElementById('mon-temp-card');
  if(m.has_temp){
    if(tempCard) tempCard.style.display='';
    const tempVal=document.getElementById('mon-temp-val');
    if(tempVal) tempVal.textContent=m.cpu_temp_c;
  } else if(tempCard){ tempCard.style.display='none'; }
  // Sparklines
  if(d.sparklines){
    drawSparkline('mon-cpu-chart',d.sparklines.cpu,'#00D4AA');
    drawSparkline('mon-ram-chart',d.sparklines.ram,'#A78BFA');
  }
  // Alerts
  renderMonAlerts(d.alerts||[]);
  renderMonitorAnalysis(d);
}

function renderMonAlerts(alerts){
  const el=document.getElementById('mon-alerts'); if(!el) return;
  if(!alerts.length){ el.innerHTML=''; return; }
  const lang=document.documentElement.getAttribute('data-lang')||'en';
  el.innerHTML=alerts.map(a=>{
    const sev=a.severity==='error'?'var(--error)':'var(--warn)';
    const msg=lang==='ja'?a.ja:a.en;
    return `<div class="card" style="padding:10px 14px;border-left:3px solid ${sev};display:flex;align-items:center;gap:8px;margin-top:8px">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="${sev}"><path d="M8 1a7 7 0 100 14A7 7 0 008 1zm0 10.5a.75.75 0 110-1.5.75.75 0 010 1.5zM8.75 8a.75.75 0 01-1.5 0V5a.75.75 0 011.5 0v3z"/></svg>
      <span style="font-size:12px">${msg}</span></div>`;
  }).join('');
}

// All metric updates now flow through the single _globalPoll timer.
// These remain as thin wrappers so existing callers keep working.
function pollMonitor(){ _globalPoll(); }
function startMonPolling(){ startGlobalPoll(); _globalPoll(); }
function _startSidebarPolling(){ startGlobalPoll(); }

// ── Tab switching ───────────────────────────────────────────────────────
function switchTab(group,panel,btn){
  const prefix=group+'-tab-';
  document.querySelectorAll(`[id^="${prefix}"]`).forEach(p=>p.classList.remove('active'));
  const target=document.getElementById(prefix+panel); if(target) target.classList.add('active');
  const bar=btn.closest('.tab-bar'); if(bar) bar.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  if(group==='perf'&&panel==='sysinfo') loadSysInfo();
  if(group==='perf'&&panel==='startup') _populatePerfStartup();
  if(group==='prob'&&panel==='events') loadEvents();
  if(group==='storage'&&panel==='organize') orgLoadHistory();
}

// ── Mirror helper: MOVE (not copy) a legacy page into a tab panel ────────
// Removes the .page class so showPage() cycling never hides/shows it again.
// Because we move (not clone), all original IDs remain unique in the DOM —
// getElementById/querySelector calls in loadEvents, loadSysInfo, etc. work
// on the live visible element without any changes to those functions.
function _mirrorIntoTab(srcId, tabId){
  const src=document.getElementById(srcId);
  const tab=document.getElementById(tabId);
  if(!src||!tab) return;
  // Already mirrored? (src is now a direct child of tab — nothing to do.)
  if(src.parentNode===tab) return;
  // Otherwise strip any whitespace nodes the tab panel may hold, then move.
  while(tab.firstChild) tab.removeChild(tab.firstChild);
  src.classList.remove('page');
  src.style.cssText='display:block;width:100%;height:auto;overflow:visible;padding:16px 0 0 0';
  tab.appendChild(src);
}

// ── Performance page init ─────────────────────────────────────────────────
// _mirrorIntoTab is idempotent (no-op once src is already inside tab), so
// these can be called more than once safely.
function _initPerfPage(){
  _mirrorIntoTab('page-monitor',  'perf-tab-live');
  _mirrorIntoTab('page-startup',  'perf-tab-startup');
  _mirrorIntoTab('page-sysinfo',  'perf-tab-sysinfo');
}
function _populatePerfStartup(){
  // no-op after _mirrorIntoTab handles it; kept for switchTab compatibility
}

// ── Problems page init ────────────────────────────────────────────────────
function _initProblemsPage(){
  _mirrorIntoTab('page-troubleshoot', 'prob-tab-fix');
  _mirrorIntoTab('page-events',       'prob-tab-events');
}

// ── Storage page init ─────────────────────────────────────────────────────
function _initStoragePage(){
  _mirrorIntoTab('page-organize', 'storage-tab-organize');
}

// ── Event Log ──────────────────────────────────────────────────────────
async function loadEvents(){
  if(!window.pywebview) return;
  const hours=parseInt(document.getElementById('evt-hours').value)||168;
  document.getElementById('evt-loading').style.display='block';
  document.getElementById('evt-empty').style.display='none';
  document.getElementById('evt-list').innerHTML='';
  document.getElementById('evt-summary').style.display='none';

  let r;
  try{ r=await window.pywebview.api.get_events(hours); }
  catch(e){ console.error('get_events error',e); r={events:[],summary:{total:0}}; }
  document.getElementById('evt-loading').style.display='none';

  const events=r.events||[];
  const summary=r.summary||{};

  if(!events.length){
    document.getElementById('evt-empty').style.display='';
    document.getElementById('evt-empty').querySelector('div').innerHTML=
      '<span class="en">No errors or warnings found in the selected period</span><span class="ja">選択期間にエラー・警告はありません</span>';
    return;
  }

  // Summary bar
  const sumEl=document.getElementById('evt-summary');
  sumEl.style.cssText='display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin-bottom:14px';
  sumEl.innerHTML=`
    <div class="card" style="padding:12px 16px;text-align:center">
      <div style="font-size:26px;font-weight:700;font-family:var(--mono)">${summary.total||events.length}</div>
      <div style="font-size:12px;color:var(--fg3);text-transform:uppercase;margin-top:2px;font-weight:600"><span class="en">Total Events</span><span class="ja">合計</span></div>
    </div>
    <div class="card" style="padding:12px 16px;text-align:center;border-top:3px solid var(--error)">
      <div style="font-size:26px;font-weight:700;font-family:var(--mono);color:var(--error)">${(summary.critical||0)+(summary.errors||0)}</div>
      <div style="font-size:12px;color:var(--fg3);text-transform:uppercase;margin-top:2px;font-weight:600"><span class="en">Errors</span><span class="ja">エラー</span></div>
    </div>
    <div class="card" style="padding:12px 16px;text-align:center;border-top:3px solid var(--warn)">
      <div style="font-size:26px;font-weight:700;font-family:var(--mono);color:var(--warn)">${summary.warnings||0}</div>
      <div style="font-size:12px;color:var(--fg3);text-transform:uppercase;margin-top:2px;font-weight:600"><span class="en">Warnings</span><span class="ja">警告</span></div>
    </div>`;

  // Analysis panel
  renderEventAnalysis(events,summary);

  // Event list
  const lang=document.documentElement.getAttribute('data-lang')||'en';
  const listEl=document.getElementById('evt-list');
  listEl.innerHTML=events.map(e=>{
    const isErr=e.level<=2;
    const sevColor=isErr?'var(--error)':'var(--warn)';
    const sevBg=isErr?'var(--error-bg)':'var(--warn-bg)';
    const sevIcon=isErr?'⛔':'⚠️';
    const sevLabel=lang==='ja'?e.level_label_ja:e.level_label_en;
    const explain=lang==='ja'?e.explanation_ja:e.explanation_en;
    const ts=e.timestamp||'';
    // Format timestamp: "2026-10-15T09:30:00" → "Oct 15, 09:30"
    let tsDisp=ts;
    try{const d=new Date(ts);tsDisp=d.toLocaleString('en-GB',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});}catch(_){}
    return `<div class="card" style="padding:14px 16px;margin-bottom:8px;border-left:4px solid ${sevColor}">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:${explain?'8px':'4px'}">
        <span style="font-size:12px;padding:3px 9px;border-radius:5px;background:${sevBg};color:${sevColor};font-weight:700;text-transform:uppercase;letter-spacing:.04em">${sevIcon} ${sevLabel}</span>
        <span style="font-size:13px;font-weight:700;color:var(--fg)">${esc(e.source||'')}</span>
        <span style="font-size:12px;color:var(--fg3);margin-left:auto;white-space:nowrap">${tsDisp}</span>
      </div>
      ${explain
        ? `<div style="font-size:14px;color:var(--fg);font-weight:500;line-height:1.6;margin-bottom:8px">${esc(explain)}</div>
           <details style="margin-top:2px">
             <summary style="font-size:12px;color:var(--fg3);cursor:pointer;user-select:none;list-style:none;display:flex;align-items:center;gap:4px">
               <span style="font-size:10px">▶</span> <span class="en">Technical log message</span><span class="ja">技術的な詳細</span>
             </summary>
             <div style="font-size:12px;color:var(--fg3);line-height:1.5;margin-top:6px;padding:8px 10px;background:rgba(0,0,0,.25);border-radius:6px;font-family:var(--mono);white-space:pre-wrap;word-break:break-word">${esc(e.message||'')}</div>
           </details>`
        : `<div style="font-size:13px;color:var(--fg2);line-height:1.5">${esc(e.message||'')}</div>`
      }
    </div>`;
  }).join('');
}

// ── Troubleshooter ─────────────────────────────────────────────────────
let _pendingActionId=null;

async function runDiagnose(){
  if(!window.pywebview) return;
  document.getElementById('ts-loading').style.display='block';
  document.getElementById('ts-empty').style.display='none';
  document.getElementById('ts-issues').innerHTML='';
  document.getElementById('ts-results').style.display='none';
  document.getElementById('btn-diagnose').disabled=true;

  const r=await window.pywebview.api.run_troubleshoot();
  document.getElementById('ts-loading').style.display='none';
  document.getElementById('btn-diagnose').disabled=false;

  const issues=r.issues||[];
  if(!issues.length){
    document.getElementById('ts-results').style.display='block';
    document.getElementById('ts-results').innerHTML=`
      <div class="card" style="padding:20px;text-align:center">
        <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="var(--brand)" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M9 12l2 2 4-4"/></svg>
        <div style="font-size:16px;font-weight:700;color:var(--brand);margin-top:10px"><span class="en">No issues detected</span><span class="ja">問題は検出されませんでした</span></div>
        <div style="font-size:13px;color:var(--fg3);margin-top:6px"><span class="en">Your system looks healthy — everything is within normal range.</span><span class="ja">システムは正常です。すべて正常範囲内です。</span></div>
      </div>`;
    return;
  }

  const lang=document.documentElement.getAttribute('data-lang')||'en';
  const listEl=document.getElementById('ts-issues');
  listEl.innerHTML=issues.map((iss,idx)=>{
    const isCrit=iss.severity==='critical';
    const isWarn=iss.severity==='warning';
    const sevColor=isCrit?'var(--error)':isWarn?'var(--warn)':'var(--fg3)';
    const sevBg=isCrit?'var(--error-bg)':isWarn?'var(--warn-bg)':'rgba(255,255,255,.05)';
    const sevIcon=isCrit?'🔴':isWarn?'🟡':'🔵';
    const sevWord=isCrit?'Critical':isWarn?'Warning':'Info';
    const title=lang==='ja'?(iss.title_ja||iss.title):iss.title;
    const desc=lang==='ja'?(iss.description_ja||iss.description):iss.description;
    const causes=(iss.causes||[]).map(c=>{
      const cText=lang==='ja'?(c.ja||c.en):c.en;
      return `<span style="font-size:12px;padding:3px 10px;border-radius:10px;background:rgba(255,255,255,.07);color:var(--fg2)">${esc(cText)}</span>`;
    }).join('');
    const actions=(iss.actions||[]).map(a=>{
      const aLabel=lang==='ja'?(a.label_ja||a.label):a.label;
      const aDesc=lang==='ja'?(a.description_ja||a.description||''):a.description||'';
      const safety=a.safety||'read_only';
      const btnClass=safety==='confirm'?'btn-secondary':'btn-primary';
      return `<button class="btn ${btnClass} btn-sm" onclick="requestAction('${a.id}',${safety==='confirm'})" title="${esc(aDesc)}" style="font-size:12px">${esc(aLabel)}</button>`;
    }).join('');
    return `<div class="card" style="padding:16px 18px;margin-bottom:10px;border-left:4px solid ${sevColor}">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
        <span style="font-size:12px;padding:4px 10px;border-radius:6px;background:${sevBg};color:${sevColor};font-weight:700">${sevIcon} ${sevWord}</span>
        <span style="font-size:15px;font-weight:700;color:var(--fg)">${esc(title)}</span>
      </div>
      <div style="font-size:14px;color:var(--fg);line-height:1.65;margin-bottom:${causes||actions?'12px':'0'}">${esc(desc)}</div>
      ${causes?`<div style="margin-bottom:10px"><div style="font-size:11px;font-weight:700;color:var(--fg3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">${lang==='ja'?'考えられる原因':'Likely causes'}</div><div style="display:flex;flex-wrap:wrap;gap:5px">${causes}</div></div>`:''}
      ${actions?`<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:4px">${actions}</div>`:''}
    </div>`;
  }).join('');
}

async function requestAction(actionId,needConfirm){
  if(!window.pywebview) return;
  if(needConfirm){
    _pendingActionId=actionId;
    const info=await window.pywebview.api.get_action_info(actionId);
    const lang=document.documentElement.getAttribute('data-lang')||'en';
    document.getElementById('action-dlg-title').textContent=lang==='ja'?(info.label_ja||info.label):info.label;
    document.getElementById('action-dlg-body').innerHTML=`
      <div style="font-size:14px;color:var(--fg);line-height:1.6;margin-bottom:12px">${lang==='ja'?(info.description_ja||info.description):info.description}</div>
      <div style="font-size:13px;color:var(--warn);background:var(--warn-bg);border:1px solid var(--warn-border);border-radius:6px;padding:8px 12px">⚠ ${lang==='ja'?(info.risk_ja||info.risk):info.risk}</div>`;
    document.getElementById('action-dialog').classList.add('show');
  } else {
    executeActionDirect(actionId);
  }
}

function closeActionDialog(){
  document.getElementById('action-dialog').classList.remove('show');
  _pendingActionId=null;
}

async function confirmAction(){
  if(_pendingActionId) await executeActionDirect(_pendingActionId);
  closeActionDialog();
}

async function executeActionDirect(actionId){
  if(!window.pywebview) return;
  const r=await window.pywebview.api.execute_action(actionId);
  if(r.ok){
    showToast(r.message||'Action executed','success');
  } else {
    showToast(r.error||'Action failed','error');
  }
}

window.addEventListener('pywebviewready',()=>{loadSettings();loadHistory();setTimeout(loadInsights,500);
  setTimeout(startGlobalPoll, 800); setTimeout(_startSidebarPolling, 1200);
  document.querySelectorAll('.kpi.clickable').forEach(el=>el.addEventListener('click',addRipple));
  const saved=localStorage.getItem('orgFolder');
  if(saved){ orgSetFolder(saved); }
  else { window.pywebview.api.get_default_org_folder().then(f=>{ if(f) orgSetFolder(f); }); }
  window.pywebview.api.start_monitoring();
  setTimeout(_initOnboarding, 1200);
  setTimeout(_pollAiStatus, 4000);
});

// Poll Ollama status forever. First few probes are quick so the pill flips
// to Ready/Offline right after launch; after that we check every 30 s so
// Helix recovers automatically when Ollama is started later.
const _AI_POLL_FAST_MS = 3000;
const _AI_POLL_SLOW_MS = 30000;
let _aiFastPolls = 0;
async function _pollAiStatus(){
  if(!window.pywebview){ setTimeout(_pollAiStatus, _AI_POLL_SLOW_MS); return; }
  let r;
  try { r = await window.pywebview.api.get_ai_status(); } catch(_){ r = {status:'checking'}; }
  const pill = document.getElementById('ai-status-pill');
  const lang = document.documentElement.getAttribute('data-lang');
  if(pill){
    const dot = pill.querySelector('.ai-status-dot');
    const lbl = pill.querySelector('span:not(.ai-status-dot)');
    if(r.status==='online'){
      pill.className='ai-status-pill';
      if(dot) dot.style.background='';
      if(lbl) lbl.innerHTML='<span class="en">AI Ready</span><span class="ja">AI準備完了</span>';
      pill.title='';
    } else if(r.status==='offline'){
      pill.className='ai-status-pill offline';
      if(dot) dot.style.background='';
      if(lbl) lbl.innerHTML='<span class="en">AI Offline</span><span class="ja">AIオフライン</span>';
      pill.title = lang==='ja'
        ? 'Ollamaが起動していません。起動すると自動で検出します。'
        : 'Ollama is not running. Helix will detect it automatically when it starts.';
    } else {
      pill.className='ai-status-pill checking';
      if(lbl) lbl.innerHTML='<span class="en">AI Checking…</span><span class="ja">AI確認中…</span>';
    }
  }
  // First 3 checks fast (so the pill resolves quickly), then slow forever.
  _aiFastPolls++;
  const nextDelay = (_aiFastPolls < 3) ? _AI_POLL_FAST_MS : _AI_POLL_SLOW_MS;
  setTimeout(_pollAiStatus, nextDelay);
}

// ── Global keyboard handler ───────────────────────────────────────────────
initTheme();
document.addEventListener('keydown',function(e){
  // Ctrl+K — command palette
  if(e.ctrlKey&&!e.shiftKey&&e.key==='k'){e.preventDefault();openCmdPalette();return;}
  // Ctrl+Shift+S — full scan
  if(e.ctrlKey&&e.shiftKey&&(e.key==='S'||e.key==='s')){e.preventDefault();startScan(false);return;}
  // Ctrl+Shift+Q — quick scan
  if(e.ctrlKey&&e.shiftKey&&(e.key==='Q'||e.key==='q')){e.preventDefault();startScan(true);return;}
  // Ctrl+Shift+T — toggle theme
  if(e.ctrlKey&&e.shiftKey&&(e.key==='T'||e.key==='t')){e.preventDefault();toggleTheme();return;}
  // ? — shortcuts modal (only when not typing in input/textarea)
  if(e.key==='?'&&document.activeElement.tagName!=='INPUT'&&document.activeElement.tagName!=='TEXTAREA'){
    e.preventDefault();openShortcuts();return;
  }
  // Escape — close any open overlay
  if(e.key==='Escape'){
    if(document.getElementById('cmd-palette-backdrop').classList.contains('open')){closeCmdPalette();return;}
    if(document.getElementById('guide-modal').style.display==='flex'){closeGuide();return;}
    if(document.getElementById('shortcuts-modal').classList.contains('open')){closeShortcuts();return;}
    if(document.getElementById('onboard-overlay').classList.contains('open')){finishOnboarding();return;}
  }
  // Alt+1–9,0 — page navigation
  if(e.altKey&&!e.ctrlKey){
    const pages=['overview','storage','monitor','startup','insights','organize','events','troubleshoot','assistant'];
    const idx=e.key==='0'?9:parseInt(e.key)-1;
    if(idx>=0&&idx<pages.length){e.preventDefault();showPage(pages[idx]);}
  }
});

// ── Assistant / Chat ──────────────────────────────────────────────────────
function sendChat(){
  const inp=document.getElementById('chat-input');
  const msg=inp.value.trim();
  if(!msg||!window.pywebview)return;
  inp.value=''; inp.style.height='auto';
  document.getElementById('btn-chat-send').disabled=true;
  appendChatMsg(msg,'user');
  const thinkId=appendChatMsg('…','assistant','chat-thinking');
  window.pywebview.api.chat(msg,document.documentElement.getAttribute('data-lang')||'en').then(r=>{
    removeChatMsg(thinkId);
    document.getElementById('btn-chat-send').disabled=false;
    handleChatResponse(r);
  }).catch(()=>{
    removeChatMsg(thinkId);
    document.getElementById('btn-chat-send').disabled=false;
    appendChatMsg('Error communicating with assistant.','assistant');
  });
}
function openPath(path){
  if(window.pywebview) window.pywebview.api.open_path(path).then(function(res){
    if(!res.ok) appendChatMsg('Could not open: '+res.msg,'assistant');
  });
}

/* ── Recycle Bin ─────────────────────────────────────────────────────────── */
function loadRecycleBin(){
  if(!window.pywebview) return;
  window.pywebview.api.get_recycle_bin().then(function(r){
    if(!r.ok) return;
    document.getElementById('st-rb-items').textContent = r.items;
    document.getElementById('st-rb-size').textContent  = r.size_str;
    const btn = document.getElementById('btn-empty-rb');
    if(btn) btn.style.display = r.items > 0 ? '' : 'none';
  });
}
function emptyRecycleBin(){
  if(!window.pywebview) return;
  // First call: backend returns a token + the item count/size for the prompt.
  window.pywebview.api.empty_recycle_bin().then(function(r){
    const msg = document.getElementById('st-rb-msg');
    if(r && r.needs_confirm){
      if(!confirm(r.msg)) return;
      window.pywebview.api.empty_recycle_bin(r.token).then(function(r2){
        if(msg){ msg.style.display=''; msg.textContent = r2.msg;
                 msg.style.color = r2.ok ? 'var(--success)' : 'var(--error)'; }
        if(r2.ok) loadRecycleBin();
      });
      return;
    }
    if(msg){ msg.style.display=''; msg.textContent = r.msg;
             msg.style.color = r.ok ? 'var(--success)' : 'var(--error)'; }
    if(r.ok) loadRecycleBin();
  });
}

/* ── Large Files ─────────────────────────────────────────────────────────── */
function findLargeFiles(){
  document.getElementById('st-lf-idle').style.display='none';
  document.getElementById('st-lf-loading').style.display='';
  document.getElementById('st-lf-results').style.display='none';
  document.getElementById('btn-find-large').disabled=true;
  if(!window.pywebview){showLargeResults({files:[],total_found:0});return;}
  window.pywebview.api.get_large_files(10).then(function(r){showLargeResults(r);});
}
function showLargeResults(r){
  document.getElementById('st-lf-loading').style.display='none';
  document.getElementById('btn-find-large').disabled=false;
  const files = r.files||[];
  document.getElementById('st-lf-badge').textContent = files.length ? files.length+' files' : '';
  if(!files.length){
    document.getElementById('st-lf-idle').style.display='';
    document.getElementById('st-lf-idle').innerHTML='<span class="en" style="color:var(--success)">✓ No files over 10 MB found.</span><span class="ja" style="color:var(--success)">10 MB以上のファイルはありません。</span>';
    return;
  }
  const tbody = document.getElementById('st-lf-body');
  tbody.innerHTML = files.map(function(f){
    const safePath = f.path.replace(/\\/g,'\\\\').replace(/'/g,"\\'");
    const dir = f.path.replace(/[^\\]+$/,'').replace(/\\$/,'');
    return '<tr><td><button class="fr-btn" style="width:100%;justify-content:flex-start;padding:4px 0;background:transparent;border:none;cursor:pointer;color:var(--fg);font-size:13px" onclick="openPath(\''+safePath+'\')">📄 '+esc(f.name)+'</button></td>'
      +'<td style="font-weight:700;font-family:var(--mono);color:var(--warn)">'+f.size_mb+' MB</td>'
      +'<td style="font-size:12px;color:var(--fg3);word-break:break-all">'+esc(dir)+'</td>'
      +'<td style="text-align:right;white-space:nowrap">'+_fileActionBtns(safePath)+'</td></tr>';
  }).join('');
  document.getElementById('st-lf-results').style.display='';
}

/* ── Old Files ───────────────────────────────────────────────────────────── */
function findOldFiles(){
  document.getElementById('st-of-idle').style.display='none';
  document.getElementById('st-of-loading').style.display='';
  document.getElementById('st-of-results').style.display='none';
  document.getElementById('btn-find-old').disabled=true;
  if(!window.pywebview){showOldResults({files:[],total_found:0});return;}
  window.pywebview.api.get_old_files(180).then(function(r){showOldResults(r);});
}
function showOldResults(r){
  document.getElementById('st-of-loading').style.display='none';
  document.getElementById('btn-find-old').disabled=false;
  const files = r.files||[];
  document.getElementById('st-of-badge').textContent = files.length ? files.length+' files' : '';
  if(!files.length){
    document.getElementById('st-of-idle').style.display='';
    document.getElementById('st-of-idle').innerHTML='<span class="en" style="color:var(--success)">✓ No files over 6 months old found.</span><span class="ja" style="color:var(--success)">6ヶ月以上古いファイルはありません。</span>';
    return;
  }
  const tbody = document.getElementById('st-of-body');
  tbody.innerHTML = files.map(function(f){
    const safePath = f.path.replace(/\\/g,'\\\\').replace(/'/g,"\\'");
    const dir = f.path.replace(/[^\\]+$/,'').replace(/\\$/,'');
    const ageColor = f.days_old > 365 ? 'var(--error)' : 'var(--warn)';
    return '<tr><td><button class="fr-btn" style="width:100%;justify-content:flex-start;padding:4px 0;background:transparent;border:none;cursor:pointer;color:var(--fg);font-size:13px" onclick="openPath(\''+safePath+'\')">📄 '+esc(f.name)+'</button></td>'
      +'<td style="font-weight:700;font-family:var(--mono)">'+f.size_mb+' MB</td>'
      +'<td style="font-weight:700;font-family:var(--mono);color:'+ageColor+'">'+f.days_old+'d</td>'
      +'<td style="font-size:12px;color:var(--fg3);word-break:break-all">'+esc(dir)+'</td>'
      +'<td style="text-align:right;white-space:nowrap">'+_fileActionBtns(safePath)+'</td></tr>';
  }).join('');
  document.getElementById('st-of-results').style.display='';
}

/* ── Per-file actions (Large / Old files) ────────────────────────────────── */
function _fileActionBtns(safePath){
  const rb="background:transparent;border:1px solid var(--border2);border-radius:6px;cursor:pointer;color:var(--fg2);padding:3px 7px;font-size:12px;margin-left:4px";
  return '<button title="Reveal in Explorer" style="'+rb+'" onclick="revealFile(\''+safePath+'\')">📂</button>'
       + '<button title="Delete to Recycle Bin" style="'+rb+';color:var(--error)" onclick="deleteFile(\''+safePath+'\',this)">🗑</button>';
}
function revealFile(path){
  if(!window.pywebview) return;
  window.pywebview.api.reveal_in_explorer(path).then(function(r){
    if(!r.ok) showToast(r.msg||'Could not reveal file','error');
  });
}
function deleteFile(path,btn){
  if(!window.pywebview) return;
  const name=path.replace(/\\\\/g,'\\').replace(/^.*\\/,'');
  if(!confirm('Move to Recycle Bin?\n\n'+name+'\n\n(You can restore it from the Recycle Bin.)')) return;
  if(btn) btn.disabled=true;
  window.pywebview.api.delete_to_recycle_bin(path).then(function(r){
    if(r.ok){
      const tr=btn&&btn.closest('tr'); if(tr) tr.remove();
      showToast(r.msg||'Moved to Recycle Bin','success');
      if(typeof loadRecycleBin==='function') loadRecycleBin();
    } else {
      if(btn) btn.disabled=false;
      showToast(r.msg||'Delete failed','error');
    }
  }).catch(function(){ if(btn) btn.disabled=false; showToast('Delete failed','error'); });
}

/* ── Privacy & Security ──────────────────────────────────────────────────── */
function loadPrivacy(){
  document.getElementById('priv-idle').style.display='none';
  document.getElementById('priv-loading').style.display='';
  document.getElementById('priv-results').style.display='none';
  document.getElementById('btn-priv-scan').disabled=true;
  if(!window.pywebview) return;
  window.pywebview.api.load_privacy().then(function(r){
    document.getElementById('priv-loading').style.display='none';
    document.getElementById('priv-results').style.display='';
    document.getElementById('btn-priv-scan').disabled=false;
    _renderBrowserCache(r.browserCache||{});
    _renderConnections(r.connections||[]);
    _renderSensitiveFiles(r.sensitiveFiles||[]);
    renderPrivacyAnalysis(r);
  }).catch(function(){
    document.getElementById('priv-loading').style.display='none';
    document.getElementById('priv-idle').style.display='';
    document.getElementById('btn-priv-scan').disabled=false;
  });
}
function _renderBrowserCache(browsers){
  const grid = document.getElementById('priv-cache-grid');
  if(!grid) return;
  const keys = Object.keys(browsers);
  if(!keys.length){ grid.innerHTML='<div style="font-size:13px;color:var(--fg3)"><span class="en">No browsers detected.</span></div>'; return; }
  grid.innerHTML = keys.map(function(name){
    const b = browsers[name];
    const color = b.cache_mb > 500 ? 'var(--error)' : b.cache_mb > 100 ? 'var(--warn)' : 'var(--success)';
    return '<div class="card card-sm" style="text-align:center">'
      +'<div style="font-size:12px;font-weight:700;color:var(--brand);margin-bottom:8px">'+esc(name)+'</div>'
      +'<div style="font-size:1.6rem;font-weight:700;font-family:var(--mono);color:'+color+'">'+b.cache_str+'</div>'
      +'<div style="font-size:12px;color:var(--fg3);margin-top:4px">cache</div>'
      +'</div>';
  }).join('');
}
function _renderConnections(conns){
  const count = document.getElementById('priv-conn-count');
  const tbody = document.getElementById('priv-conn-body');
  const wrap  = document.getElementById('priv-conn-wrap');
  const empty = document.getElementById('priv-conn-empty');
  if(!conns.length){
    if(empty) empty.style.display='';
    if(wrap)  wrap.style.display='none';
    if(count) count.textContent='';
    return;
  }
  if(count) count.textContent=conns.length+' connections';
  if(empty) empty.style.display='none';
  if(wrap)  wrap.style.display='';
  tbody.innerHTML = conns.map(function(c){
    const stateColor = c.state==='ESTABLISHED' ? 'var(--success)' : 'var(--fg3)';
    return '<tr><td style="font-weight:600">'+esc(c.process)+'<span style="font-size:11px;color:var(--fg3);margin-left:6px">PID '+c.pid+'</span></td>'
      +'<td><span style="font-size:12px;padding:2px 8px;border-radius:10px;background:rgba(0,0,0,.3);color:'+stateColor+'">'+c.state+'</span></td>'
      +'<td style="font-family:var(--mono);font-size:12px">'+esc(c.local)+'</td>'
      +'<td style="font-family:var(--mono);font-size:12px">'+esc(c.remote)+'</td></tr>';
  }).join('');
}
function _renderSensitiveFiles(files){
  const count  = document.getElementById('priv-sens-count');
  const list   = document.getElementById('priv-sens-list');
  const empty  = document.getElementById('priv-sens-empty');
  if(count) count.textContent = files.length ? files.length+' found' : '';
  if(!files.length){
    if(empty) empty.style.display='';
    if(list)  list.style.display='none';
    return;
  }
  if(empty) empty.style.display='none';
  if(list){
    list.style.display='';
    list.innerHTML = files.map(function(f,i){
      const safePath = f.path.replace(/\\/g,'\\\\').replace(/'/g,"\\'");
      return '<button class="fr-btn" onclick="openPath(\''+safePath+'\')" title="'+esc(f.path)+'">'
        +'<span class="fr-icon">⚠️</span>'
        +'<span class="fr-info"><span class="fr-name">'+esc(f.name)+'</span>'
        +'<span class="fr-path">'+esc(f.path)+'</span>'
        +'<span style="font-size:11px;color:var(--warn);margin-top:2px">'+esc(f.reason)+'</span>'
        +'</span>'
        +'<span class="fr-num">'+(i+1)+'</span>'
        +'</button>';
    }).join('');
  }
}

function handleChatResponse(r){
  if(r.type==='action'){
    appendChatMsg(r.content,'assistant');
    if(r.action==='navigate') showPage(r.page);
    if(r.extra==='load_insights') setTimeout(loadInsights,300);
    if(r.extra==='run_diagnose') setTimeout(runDiagnose,300);
  } else if(r.type==='thinking'){
    appendChatMsg(r.content,'assistant','chat-thinking','async-thinking');
  } else if(r.type==='help'){
    appendChatMsg(r.content.replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>'),'assistant','','',true);
  } else if(r.type==='file_results'){
    var html='<div class="fr-header">'+esc(r.content).replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>')+'</div>';
    html+='<div class="fr-list">';
    r.items.forEach(function(it,i){
      var icon=it.kind==='folder'?'📁':'📄';
      var safePath=it.path.replace(/\\/g,'\\\\').replace(/'/g,"\\'");
      var meta=it.size_kb?(' · '+it.size_kb+' KB'):'';
      html+='<button class="fr-btn" onclick="openPath(\''+safePath+'\')" title="'+esc(it.path)+'">'
           +'<span class="fr-icon">'+icon+'</span>'
           +'<span class="fr-info"><span class="fr-name">'+esc(it.name)+'</span>'
           +'<span class="fr-path">'+esc(it.path)+meta+'</span></span>'
           +'<span class="fr-num">'+(i+1)+'</span>'
           +'</button>';
    });
    html+='</div>';
    appendChatMsg(html,'assistant','','',true);
  } else if(r.type==='plan'){
    var html='<div class="plan-list">';
    html+='<div class="plan-header">Executing '+r.steps.length+' steps…</div>';
    r.steps.forEach(function(s){
      html+='<div class="plan-step" id="ps-'+r.plan_id+'-'+s.idx+'">'
           +'<span class="ps-num">'+(s.idx+1)+'</span>'
           +'<span class="ps-text">'+esc(s.text)+'</span>'
           +'<span class="ps-badge ps-pending">Pending</span>'
           +'<span class="ps-result"></span>'
           +'</div>';
    });
    html+='</div>';
    appendChatMsg(html,'assistant','','',true);
  } else {
    appendChatMsg(r.content,'assistant');
  }
}
function onChatResponse(r){
  const el=document.getElementById('async-thinking'); if(el) el.remove();
  handleChatResponse(r);
}
function onPlanStep(data){
  const step=document.getElementById('ps-'+data.plan_id+'-'+data.idx);
  if(!step) return;
  const badge=step.querySelector('.ps-badge');
  const res=step.querySelector('.ps-result');
  if(badge){badge.className='ps-badge ps-'+data.status;badge.textContent=data.status.charAt(0).toUpperCase()+data.status.slice(1)}
  if(res&&data.result) res.textContent=data.result;
  const box=document.getElementById('chat-messages');
  if(box) box.scrollTop=box.scrollHeight;
}
const _AI_AVATAR='<svg width="15" height="15" aria-hidden="true"><use href="#pcha-logo"/></svg>';
const _USR_AVATAR='<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>';
function appendChatMsg(text,role,extraClass,id,isHtml){
  extraClass=extraClass||''; id=id||('cm-'+Date.now()); isHtml=isHtml||false;
  const box=document.getElementById('chat-messages');
  if(!box) return id;
  const avatar=role==='user'?_USR_AVATAR:_AI_AVATAR;
  const div=document.createElement('div');
  div.className='chat-msg chat-'+role+(extraClass?' '+extraClass:'');
  div.id=id;
  div.innerHTML='<div class="chat-avatar">'+avatar+'</div><div class="chat-bubble">'+(isHtml?text:esc(text).replace(/\n/g,'<br>'))+'</div>';
  box.appendChild(div);
  box.scrollTop=box.scrollHeight;
  return id;
}
function removeChatMsg(id){const el=document.getElementById(id);if(el)el.remove();}
function clearChat(){
  const box=document.getElementById('chat-messages');
  if(box) box.innerHTML='<div class="chat-msg chat-assistant"><div class="chat-avatar">'+_AI_AVATAR+'</div><div class="chat-bubble"><span class="en">Chat cleared. How can I help?</span><span class="ja">クリアしました。何かお手伝いしますか？</span></div></div>';
}
function chatKeydown(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();}}
</script>
</body>
</html>
"""


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
        try: hud_window.evaluate_js("_hudPollStart()")
        except Exception: pass
    hud_window.events.shown += _hud_on_shown
    def _hud_on_hidden():
        try: hud_window.evaluate_js("_hudPollStop()")
        except Exception: pass
    hud_window.events.closing += _hud_on_hidden

    def on_shown():
        time.sleep(1.0)
        _set_window_icon(ico_path, "PC Health Agent")
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
