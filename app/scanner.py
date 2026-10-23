"""PC Health Agent — scanner module. stdlib only, no pip required."""
import os, sys, hashlib, winreg, shutil
import run_hidden
from pathlib import Path
from datetime import datetime

PROTECTED = [
    os.environ.get("OneDrive", ""),
    os.environ.get("OneDriveCommercial", ""),
    str(Path.home() / "OneDrive"),
    "C:\\Windows",
    "C:\\Program Files",
    "C:\\Program Files (x86)",
    os.path.join(os.environ.get("PROGRAMDATA",""), "ESET"),
    os.path.join(os.environ.get("LOCALAPPDATA",""), "ESET"),
    os.path.join(os.environ.get("PROGRAMDATA",""), "Optima"),
]
PROTECTED = [p for p in PROTECTED if p]

def _normalize_for_match(path: str) -> str:
    """Case-fold, resolve symlinks/junctions, and append a trailing separator
    so that `C:\\Windows2\\foo` does not match `C:\\Windows` by prefix.
    """
    if not path:
        return ""
    try:
        p = os.path.realpath(path)
    except (OSError, ValueError):
        p = path
    p = os.path.normcase(os.path.normpath(p))
    if not p.endswith(os.sep):
        p = p + os.sep
    return p


def is_protected(path: str) -> bool:
    if not path:
        return False
    pn = _normalize_for_match(path)
    if not pn:
        return False
    for root in PROTECTED:
        rn = _normalize_for_match(root)
        if rn and pn.startswith(rn):
            return True
    return False

def fmt_size(b: int) -> str:
    if b >= 1 << 30: return f"{b/(1<<30):.1f} GB"
    if b >= 1 << 20: return f"{b/(1<<20):.0f} MB"
    if b >= 1 << 10: return f"{b/(1<<10):.0f} KB"
    return f"{b} B"

def dir_size(path: str) -> int:
    total = 0
    try:
        for entry in os.scandir(path):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat().st_size
                elif entry.is_dir(follow_symlinks=False):
                    total += dir_size(entry.path)
            except (PermissionError, OSError):
                pass
    except (PermissionError, OSError):
        pass
    return total

# ── Disk ──────────────────────────────────────────────────────────────────────
def scan_disk():
    usage = shutil.disk_usage("C:\\")
    free_gb  = round(usage.free  / (1<<30), 1)
    used_gb  = round(usage.used  / (1<<30), 1)
    total_gb = round(usage.total / (1<<30), 1)
    pct_free = round(usage.free / usage.total * 100)
    return {
        "free_gb": free_gb, "used_gb": used_gb,
        "total_gb": total_gb, "pct_free": pct_free,
        "fmt_free": f"{free_gb} GB", "fmt_used": f"{used_gb} GB",
    }

# ── Temp / cache ──────────────────────────────────────────────────────────────
TEMP_TARGETS = [
    ("Temp files (%TEMP%)",   "一時ファイル (%TEMP%)",           os.environ.get("TEMP",""),           True,  "temp"),
    ("Windows Temp",          "Windows 一時ファイル",            "C:\\Windows\\Temp",                  True,  "temp"),
    ("npm cache",             "npm キャッシュ",                   os.path.join(os.environ.get("APPDATA",""), "npm-cache"), True, "cache"),
    ("pip cache",             "pip キャッシュ",                   os.path.join(os.environ.get("LOCALAPPDATA",""), "pip","Cache"), True, "cache"),
    ("Yarn cache",            "Yarn キャッシュ",                  os.path.join(os.environ.get("LOCALAPPDATA",""), "Yarn","Cache"), True, "cache"),
    ("VS Code logs",          "VS Code ログ",                    os.path.join(os.environ.get("APPDATA",""), "Code","logs"), True, "log"),
    ("Chrome cache",          "Chrome キャッシュ",               os.path.join(os.environ.get("LOCALAPPDATA",""), "Google","Chrome","User Data","Default","Cache"), True, "cache"),
    ("Edge cache",            "Edge キャッシュ",                 os.path.join(os.environ.get("LOCALAPPDATA",""), "Microsoft","Edge","User Data","Default","Cache"), True, "cache"),
    ("Windows Error Reports", "Windows エラーレポート",          os.path.join(os.environ.get("LOCALAPPDATA",""), "Microsoft","Windows","WER"), True, "log"),
    ("Thumbnail cache",       "サムネイル キャッシュ",            os.path.join(os.environ.get("LOCALAPPDATA",""), "Microsoft","Windows","Explorer"), True, "cache"),
]

def scan_temp():
    items = []
    for name_en, name_ja, path, safe, cat in TEMP_TARGETS:
        if not path or not os.path.exists(path): continue
        if is_protected(path): continue
        size = dir_size(path)
        if size > 0:
            items.append({
                "name_en": name_en, "name_ja": name_ja,
                "path": path, "size": size,
                "size_fmt": fmt_size(size), "safe": safe, "category": cat,
            })
    items.sort(key=lambda x: x["size"], reverse=True)
    return items

# ── Startup apps ──────────────────────────────────────────────────────────────
# IT_PATTERN is the shipping default list of apps/processes Helix treats as
# IT-managed (never close, never flag, never move). Users can extend this
# through Settings; see protected.get_protected_keywords(). Scanner-local
# code reads IT_PATTERN directly for backwards compatibility, but callers
# that need the live user-extended set should import from protected.
IT_PATTERN = {
    "eset", "ekrn", "egui",                   # ESET + its process names
    "optima", "acall",
    "onedrive", "teams",
    "microsoftedge", "msedge",                # Edge + its process name
    "securityhealth", "windowsdefender",
}

# Critical OS processes — hardcoded, never editable. Trying to close any of
# these from the UI must always be refused regardless of the user's own list.
CRITICAL_PROCESSES = frozenset({
    "system", "lsass", "csrss", "winlogon", "smss", "wininit",
    "services", "svchost", "msmpeng", "mssenses",
})

def scan_startup():
    apps = []
    reg_paths = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
    ]
    for hive, subkey in reg_paths:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                i = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(key, i)
                        is_it = any(kw in name.lower() for kw in IT_PATTERN)
                        apps.append({"name": name, "command": val, "it_managed": is_it})
                        i += 1
                    except OSError:
                        break
        except OSError:
            pass
    return apps

# ── Top processes ─────────────────────────────────────────────────────────────
def scan_processes():
    try:
        result = run_hidden.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Sort-Object WorkingSet64 -Desc | Select-Object -First 10 | "
             "ForEach-Object { $_.ProcessName + '|' + [math]::Round($_.WorkingSet64/1MB,0) }"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15
        )
        procs = []
        for line in result.stdout.strip().splitlines():
            if "|" in line:
                name, mb = line.strip().split("|", 1)
                procs.append({"name": name.strip(), "mem_mb": int(mb.strip())})
        return procs
    except Exception:
        return []

def scan_total_ram():
    try:
        result = run_hidden.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10
        )
        return round(int(result.stdout.strip()) / (1<<30))
    except Exception:
        return 0

# ── Duplicates ────────────────────────────────────────────────────────────────
SKIP_DIRS = {"node_modules", ".venv", ".git", "__pycache__", ".mypy_cache"}

def scan_duplicates(scan_path: str, max_files: int = 1500, min_size: int = 50_000,
                    progress_cb=None):
    if is_protected(scan_path): return []
    candidates = []
    try:
        for root, dirs, files in os.walk(scan_path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                fp = os.path.join(root, f)
                try:
                    sz = os.path.getsize(fp)
                    if sz >= min_size:
                        candidates.append((fp, sz))
                except OSError:
                    pass
            if len(candidates) >= max_files:
                break
    except (PermissionError, OSError):
        pass

    candidates = candidates[:max_files]
    hash_map: dict[str, list] = {}
    for i, (fp, sz) in enumerate(candidates):
        if progress_cb and i % 100 == 0:
            progress_cb(i, len(candidates))
        try:
            h = hashlib.md5(open(fp, "rb").read(), usedforsecurity=False).hexdigest()
            hash_map.setdefault(h, []).append({"path": fp, "size": sz, "name": os.path.basename(fp)})
        except (PermissionError, OSError):
            pass

    groups = []
    for files in hash_map.values():
        if len(files) > 1:
            total = sum(f["size"] for f in files)
            biggest = max(f["size"] for f in files)
            groups.append({
                "files": files, "count": len(files),
                "total_bytes": total, "saveable_bytes": total - biggest,
                "saveable_fmt": fmt_size(total - biggest),
                "total_fmt": fmt_size(total),
            })
    groups.sort(key=lambda g: g["saveable_bytes"], reverse=True)
    return groups

# ── Full scan ─────────────────────────────────────────────────────────────────
def run_full_scan(include_dupes=True, progress_cb=None):
    print("  [1/5] Disk...", flush=True)
    disk = scan_disk()

    print("  [2/5] Temp and cache...", flush=True)
    temp_items = scan_temp()
    total_temp = sum(t["size"] for t in temp_items)

    print("  [3/5] Startup apps...", flush=True)
    startup = scan_startup()

    print("  [4/5] RAM processes...", flush=True)
    processes = scan_processes()
    total_ram = scan_total_ram()

    dupes = []
    total_dupes = 0
    if include_dupes:
        dl = str(Path.home() / "Downloads")
        print(f"  [5/5] Duplicate scan ({dl})...", flush=True)
        dupes = scan_duplicates(dl, progress_cb=progress_cb)
        total_dupes = sum(g["saveable_bytes"] for g in dupes)
    else:
        print("  [5/5] Duplicate scan skipped.", flush=True)

    from thresholds import (
        DISK_PCT_WARN, TEMP_SIZE_WARN, DUPES_SIZE_HIGH,
        STARTUP_COUNT_WARN, PROCESS_RAM_HIGH_MB,
    )
    issues = sum([
        disk["pct_free"] < DISK_PCT_WARN,
        total_temp > TEMP_SIZE_WARN,
        total_dupes > DUPES_SIZE_HIGH,
        sum(1 for a in startup if not a["it_managed"]) > STARTUP_COUNT_WARN,
        bool(processes and processes[0]["mem_mb"] > PROCESS_RAM_HIGH_MB),
    ])

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "computer": os.environ.get("COMPUTERNAME", ""),
        "user": os.environ.get("USERNAME", ""),
        "disk": disk,
        "temp_items": temp_items,
        "total_temp": total_temp,
        "total_temp_fmt": fmt_size(total_temp),
        "startup": startup,
        "it_apps": [a for a in startup if a["it_managed"]],
        "personal_apps": [a for a in startup if not a["it_managed"]],
        "processes": processes,
        "total_ram_gb": total_ram,
        "dupes": dupes,
        "total_dupes": total_dupes,
        "total_dupes_fmt": fmt_size(total_dupes),
        "total_freeable": total_temp + total_dupes,
        "total_freeable_fmt": fmt_size(total_temp + total_dupes),
        "issues": issues,
        "dupe_groups_count": len(dupes),
    }


# ── System info ──────────────────────────────────────────────────────────────
def get_system_info() -> dict:
    info = {
        "computer": os.environ.get("COMPUTERNAME", "Unknown"),
        "user": os.environ.get("USERNAME", "Unknown"),
        "os": "", "cpu": "", "ram_gb": 0, "disk_total_gb": 0, "disk_free_gb": 0,
    }
    try:
        r = run_hidden.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_OperatingSystem).Caption"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=8)
        info["os"] = r.stdout.strip() or "Windows"
    except Exception:
        info["os"] = "Windows"
    try:
        r = run_hidden.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Processor).Name"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=8)
        info["cpu"] = r.stdout.strip()
    except Exception:
        pass
    info["ram_gb"] = scan_total_ram()
    usage = shutil.disk_usage("C:\\")
    info["disk_total_gb"] = round(usage.total / (1 << 30), 1)
    info["disk_free_gb"] = round(usage.free / (1 << 30), 1)
    return info


# ── Safe cleanup ─────────────────────────────────────────────────────────────
def cleanup_temp_files(paths: list[str]) -> list[dict]:
    results = []
    for p in paths:
        entry = {"path": p, "status": "skipped", "freed": 0, "error": ""}
        if is_protected(p):
            entry["error"] = "Protected path"
            results.append(entry)
            continue
        if not os.path.exists(p):
            entry["status"] = "missing"
            entry["error"] = "File or directory no longer exists"
            results.append(entry)
            continue
        try:
            if os.path.isfile(p):
                sz = os.path.getsize(p)
                os.remove(p)
                entry["status"] = "deleted"
                entry["freed"] = sz
            elif os.path.isdir(p):
                sz = dir_size(p)
                shutil.rmtree(p, ignore_errors=True)
                actual_freed = sz - (dir_size(p) if os.path.exists(p) else 0)
                entry["status"] = "deleted" if not os.path.exists(p) else "partial"
                entry["freed"] = actual_freed
        except PermissionError:
            entry["status"] = "failed"
            entry["error"] = "Permission denied"
        except OSError as e:
            entry["status"] = "failed"
            entry["error"] = str(e)
        results.append(entry)
    return results
