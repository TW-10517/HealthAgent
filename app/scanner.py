"""PC Health Agent — scanner module. stdlib only, no pip required."""
import os, hashlib, winreg, shutil, platform
from pathlib import Path
from datetime import datetime

try:
    import psutil
except ImportError:
    psutil = None

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
    "eye247", "eye\"247\"", "fuva",           # Fuva Brain Eye"247" work agent
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

_APPROVED_BASE = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"

# Every place Windows looks for logon entries, paired with the
# StartupApproved key that records whether the user disabled it.
#   (hive, run_subkey, approved_hive, approved_subkey, source_label)
_RUN_KEYS = [
    (winreg.HKEY_CURRENT_USER,
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
     winreg.HKEY_CURRENT_USER, _APPROVED_BASE + r"\Run", "HKCU\\Run"),
    (winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
     winreg.HKEY_LOCAL_MACHINE, _APPROVED_BASE + r"\Run", "HKLM\\Run"),
    (winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
     winreg.HKEY_LOCAL_MACHINE, _APPROVED_BASE + r"\Run32", "HKLM\\Run (32-bit)"),
]


def _read_approved(hive, subkey) -> dict:
    """Map lower-cased entry name -> enabled bool from a StartupApproved key.

    Task Manager stores its enable/disable decision here as a binary blob;
    bit 0 of byte 0 is set when the entry is disabled. An entry with no
    record has never been touched and therefore runs.
    """
    out = {}
    try:
        with winreg.OpenKey(hive, subkey) as key:
            i = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(key, i)
                    i += 1
                except OSError:
                    break
                if not name:
                    continue
                try:
                    disabled = bool(val[0] & 1)
                except (TypeError, IndexError, KeyError):
                    disabled = False
                out[name.lower()] = not disabled
    except OSError:
        pass
    return out


def _classify_startup(name: str, command: str) -> str:
    """Return 'it' | 'system' | 'personal' for one startup entry.

    Keywords are matched against the command as well as the entry name:
    the name alone is unreliable (HKCU's "Microsoft.Lists" actually launches
    OneDrive.Sync.Service.exe). Anything launched out of the Windows
    directory is an OS or driver component, not a personal app.
    """
    blob = (str(name) + " " + str(command)).lower()
    for kw in IT_PATTERN:
        if kw in blob:
            return "it"
    cmd = str(command).strip().strip('"').lower()
    if cmd.startswith(("%windir%", "%systemroot%")):
        return "system"
    sysroot = (os.environ.get("SystemRoot") or "C:\\Windows").lower()
    if cmd.startswith(sysroot):
        return "system"
    return "personal"


def scan_startup():
    """Every entry Windows launches at logon, with its real enabled state.

    Reads the 64-bit and 32-bit HKLM Run keys, the HKCU Run key, and both
    Startup folders. Disabled entries are returned with enabled=False
    rather than dropped, so the UI can show them without callers counting
    them as things that actually run.
    """
    apps = []
    seen = set()

    for hive, subkey, ahive, asubkey, label in _RUN_KEYS:
        approved = _read_approved(ahive, asubkey)
        try:
            with winreg.OpenKey(hive, subkey) as key:
                i = 0
                while True:
                    try:
                        name, val, _ = winreg.EnumValue(key, i)
                        i += 1
                    except OSError:
                        break
                    # The key's unnamed default value is not a startup entry.
                    if not name or not str(name).strip():
                        continue
                    command = "" if val is None else str(val)
                    ident = (label, str(name).lower())
                    if ident in seen:
                        continue
                    seen.add(ident)
                    cat = _classify_startup(name, command)
                    apps.append({
                        "name": name,
                        "command": command,
                        "it_managed": cat in ("it", "system"),
                        "category": cat,
                        "enabled": approved.get(str(name).lower(), True),
                        "source": label,
                    })
        except OSError:
            pass

    folders = [
        (os.path.join(os.environ.get("APPDATA", ""),
                      r"Microsoft\Windows\Start Menu\Programs\Startup"),
         winreg.HKEY_CURRENT_USER, "Startup folder"),
        (os.path.join(os.environ.get("PROGRAMDATA", ""),
                      r"Microsoft\Windows\Start Menu\Programs\Startup"),
         winreg.HKEY_LOCAL_MACHINE, "Startup folder (all users)"),
    ]
    for folder, ahive, label in folders:
        if not folder or not os.path.isdir(folder):
            continue
        approved = _read_approved(ahive, _APPROVED_BASE + r"\StartupFolder")
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
        except OSError:
            continue
        for e in entries:
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if e.name.lower() == "desktop.ini":
                continue
            display = os.path.splitext(e.name)[0]
            cat = _classify_startup(display, e.path)
            apps.append({
                "name": display,
                "command": e.path,
                "it_managed": cat in ("it", "system"),
                "category": cat,
                # Approval records for shortcuts keep the file extension.
                "enabled": approved.get(e.name.lower(), True),
                "source": label,
            })
    return apps

# ── Top processes ─────────────────────────────────────────────────────────────
# Kernel bookkeeping entries that are not applications. Memory
# Compression's working set is the OS's own compressed page store — it is
# already counted in RAM usage and the user cannot act on it, so listing it
# as the top memory consumer is actively misleading.
_PSEUDO_PROCESSES = {"memcompression", "memory compression",
                     "system idle process"}


def scan_processes():
    """Top-10 apps by memory, summed per executable.

    Chromium-based apps (Edge, WebView2, Electron) spread their memory over
    dozens of processes with the same name, so reporting one process' RSS
    understates the app several-fold — Edge measured 1001 MB for its largest
    process against 2625 MB across all 23. Instances are summed and counted.
    psutil is ~100x faster than PowerShell here.
    """
    if psutil is None:
        return []
    agg: dict[str, dict] = {}
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            mi = p.info.get("memory_info")
            if not mi:
                continue
            name = p.info["name"] or ""
            if not name or name.lower() in _PSEUDO_PROCESSES:
                continue
            e = agg.get(name)
            if e is None:
                agg[name] = {"bytes": mi.rss, "count": 1}
            else:
                e["bytes"] += mi.rss
                e["count"] += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs = [{"name": n,
              "mem_mb": round(v["bytes"] / (1 << 20)),
              "count": v["count"]}
             for n, v in agg.items()]
    procs.sort(key=lambda x: x["mem_mb"], reverse=True)
    return procs[:10]

def scan_total_ram():
    if psutil is not None:
        return round(psutil.virtual_memory().total / (1 << 30))
    return 0


def startup_counts(startup) -> dict:
    """The logon-entry counts every surface quotes.

    run_full_scan bakes these into a scan and AgentAPI.get_startup_entries()
    recomputes them after the user switches an entry on or off. Both call this
    one function, so a number on the Startup page cannot drift from the number
    inside the scan that produced the page.
    """
    enabled = [a for a in startup if a.get("enabled", True)]
    return {
        "total": len(startup),
        "enabled": len(enabled),
        "disabled": len(startup) - len(enabled),
        "personalEnabled": sum(1 for a in enabled if not a["it_managed"]),
        "itEnabled": sum(1 for a in enabled if a["it_managed"]),
        "personal": sum(1 for a in startup if not a["it_managed"]),
        "it": sum(1 for a in startup if a["it_managed"]),
    }


def scan_total_ram_mb() -> int:
    """Unrounded total RAM in MB, for share-of-memory comparisons."""
    if psutil is not None:
        return round(psutil.virtual_memory().total / (1 << 20))
    return 0


def top_process_is_hogging(processes, total_ram_mb) -> bool:
    """True when the heaviest app exceeds both the MB floor and the share
    of installed RAM. See thresholds.PROCESS_RAM_PCT_HIGH for why a share
    is used rather than a fixed ceiling."""
    from thresholds import PROCESS_RAM_HIGH_MB, PROCESS_RAM_PCT_HIGH
    if not processes:
        return False
    mb = processes[0].get("mem_mb", 0)
    if mb <= PROCESS_RAM_HIGH_MB:
        return False
    if not total_ram_mb:
        return False
    return (mb / total_ram_mb * 100) > PROCESS_RAM_PCT_HIGH

# ── Duplicates ────────────────────────────────────────────────────────────────
SKIP_DIRS = {"node_modules", ".venv", ".git", "__pycache__", ".mypy_cache"}

def _file_digest(path: str, chunk: int = 1 << 20):
    """MD5 of a file's contents, read in fixed-size chunks.

    Only ever used to compare files that already share an exact byte size,
    so this is a duplicate check and not a security boundary. Chunked
    reading keeps a multi-gigabyte download from being pulled into memory
    in one go, which the previous open(fp,'rb').read() did.
    """
    h = hashlib.md5(usedforsecurity=False)
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
    except (PermissionError, OSError):
        return None
    return h.hexdigest()


def scan_duplicates(scan_path: str, max_files: int = 1500, min_size: int = 50_000,
                    progress_cb=None):
    if is_protected(scan_path): return []
    candidates = []
    try:
        # Sorted walk so the max_files cap always truncates at the same
        # place; unsorted order made repeat scans of one folder disagree.
        for root, dirs, files in os.walk(scan_path):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for f in sorted(files):
                fp = os.path.join(root, f)
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    continue
                if sz >= min_size:
                    candidates.append((fp, sz))
            if len(candidates) >= max_files:
                break
    except (PermissionError, OSError):
        pass

    candidates = candidates[:max_files]

    # Two files of different sizes cannot be duplicates, so hash only
    # within size groups. The previous version hashed every candidate,
    # reading far more data than necessary.
    by_size: dict[int, list] = {}
    for fp, sz in candidates:
        by_size.setdefault(sz, []).append(fp)
    to_hash = [(fp, sz) for sz, fps in by_size.items() if len(fps) > 1 for fp in fps]

    # Keyed on (size, digest) so a digest collision across different sizes
    # can never merge two unrelated files into one "duplicate" group.
    hash_map: dict[tuple, list] = {}
    for i, (fp, sz) in enumerate(to_hash):
        if progress_cb and i % 25 == 0:
            progress_cb(i, len(to_hash))
        h = _file_digest(fp)
        if h is None:
            continue
        hash_map.setdefault((sz, h), []).append(
            {"path": fp, "size": sz, "name": os.path.basename(fp)})

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
    total_ram_mb = scan_total_ram_mb()

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
        STARTUP_COUNT_WARN,
    )
    it_apps = [a for a in startup if a["it_managed"]]
    personal_apps = [a for a in startup if not a["it_managed"]]
    # Only entries that are actually enabled can slow boot, so the warning
    # counts those. A disabled entry still appears in the list for review.
    personal_enabled = [a for a in personal_apps if a.get("enabled", True)]
    enabled_apps = [a for a in startup if a.get("enabled", True)]
    counts = startup_counts(startup)

    issues = sum([
        disk["pct_free"] < DISK_PCT_WARN,
        total_temp > TEMP_SIZE_WARN,
        total_dupes > DUPES_SIZE_HIGH,
        len(personal_enabled) > STARTUP_COUNT_WARN,
        top_process_is_hogging(processes, total_ram_mb),
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
        "it_apps": it_apps,
        "personal_apps": personal_apps,
        "personal_enabled": personal_enabled,
        "startup_enabled": enabled_apps,
        "startup_disabled_count": len(startup) - len(enabled_apps),
        "startup_counts": counts,
        "processes": processes,
        "total_ram_gb": total_ram,
        "total_ram_mb": total_ram_mb,
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
    # OS caption: registry read is instant vs ~400ms for Get-CimInstance.
    # Win11 still writes "Windows 10 …" to ProductName — detect via build number.
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
            name = winreg.QueryValueEx(k, "ProductName")[0] or "Windows"
            try:
                build = int(winreg.QueryValueEx(k, "CurrentBuildNumber")[0])
            except (OSError, ValueError):
                build = 0
            if build >= 22000 and "10" in name:
                name = name.replace("Windows 10", "Windows 11")
            info["os"] = name
    except OSError:
        info["os"] = platform.system() or "Windows"
    # CPU name: registry read, same reasoning.
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            info["cpu"] = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
    except OSError:
        info["cpu"] = platform.processor() or ""
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
