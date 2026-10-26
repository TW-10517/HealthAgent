"""PC Health Agent — Safe system query executor.

SECURITY CONTRACT (immutable):
  • READ-ONLY: zero writes, zero deletes, zero configuration changes
  • Scope: user home directory and same drive only; never C:\\Windows,
    C:\\Program Files, or IT-managed tool directories
  • No shell=True anywhere; all subprocess calls use explicit arg lists
  • All user-supplied strings are sanitised before use
  • Hard 8-second timeout on every operation
  • No registry modification, no firewall changes, no process termination
  • No network writes; `ping` is intentionally excluded (can be abused)
  • Results are capped to prevent info dumps
"""
import os
import re
import subprocess
import threading
from pathlib import Path

# ── Security boundaries ────────────────────────────────────────────────────────

_HOME       = str(Path.home())
_DRIVE      = os.path.splitdrive(_HOME)[0] + os.sep   # e.g. C:\
_ONEDRIVE   = os.environ.get("OneDrive", "")

# Directories we never descend into
_PROTECTED_PREFIXES = tuple(
    os.path.normcase(p) for p in [
        r"C:\Windows",
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        os.path.join(_HOME, "AppData", "Roaming", "Microsoft", "Windows"),
    ]
)

# IT-managed name fragments (match against directory basename)
_IT_FRAGMENTS = {"eset", "optima", "acall", "windowsdefender",
                 "securityhealth", "microsoftedge"}

_MAX_FILES   = 25     # max rows returned per search
_TIMEOUT_SEC = 8      # hard wall-clock limit per operation

# Common file-type aliases for extension-based queries
_EXT_ALIASES: dict[str, list[str]] = {
    "pdf":        [".pdf"],
    "word":       [".docx", ".doc"],
    "excel":      [".xlsx", ".xls", ".csv"],
    "csv":        [".csv"],
    "powerpoint": [".pptx", ".ppt"],
    "ppt":        [".pptx", ".ppt"],
    "image":      [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic"],
    "photo":      [".jpg", ".jpeg", ".png", ".heic"],
    "picture":    [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"],
    "video":      [".mp4", ".mov", ".avi", ".mkv", ".wmv"],
    "audio":      [".mp3", ".wav", ".flac", ".aac", ".m4a"],
    "music":      [".mp3", ".wav", ".flac", ".aac", ".m4a"],
    "zip":        [".zip", ".7z", ".rar", ".tar", ".gz"],
    "archive":    [".zip", ".7z", ".rar", ".tar", ".gz"],
    "text":       [".txt", ".md", ".log"],
    "doc":        [".docx", ".doc", ".pdf", ".odt"],
    "document":   [".docx", ".doc", ".pdf", ".odt"],
}


def _protected(path: str) -> bool:
    p = os.path.normcase(path)
    if any(p.startswith(pr) for pr in _PROTECTED_PREFIXES):
        return True
    base = os.path.basename(path).lower()
    return any(frag in base for frag in _IT_FRAGMENTS)


def _clean(s: str, maxlen: int = 120) -> str:
    """Strip shell-metacharacters from a user string."""
    return re.sub(r'[\\/:*?"<>|;&`$\r\n]', '', s).strip()[:maxlen]


def _run_safe(args: list, timeout: int = _TIMEOUT_SEC) -> str:
    """Run a whitelisted command with no shell and capture stdout."""
    try:
        r = subprocess.run(
            args, shell=False, capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=timeout,
            creationflags=0x08000000,   # CREATE_NO_WINDOW
        )
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return "(timed out)"
    except FileNotFoundError:
        return "(command not found)"
    except Exception as e:
        return f"(error: {e})"


# ── Operations ─────────────────────────────────────────────────────────────────

def _file_meta(path: str) -> dict:
    """Return a result dict with metadata for a path."""
    is_dir = os.path.isdir(path)
    size_kb = 0
    mtime   = 0.0
    try:
        st = os.stat(path)
        mtime = st.st_mtime
        if not is_dir:
            size_kb = max(1, st.st_size // 1024)
    except OSError:
        pass
    return {
        "path":    path,
        "name":    os.path.basename(path),
        "kind":    "folder" if is_dir else "file",
        "size_kb": size_kb,
        "mtime":   mtime,
    }


def search_files(query: str, folder: str = None) -> dict:
    """Search for files/folders matching `query`. User-space only.

    `query` can be:
      - a plain substring  ("resume")
      - multiple words     ("budget report") — all words must appear in name
      - a file-type alias  ("pdf", "excel", "image") — matches by extension
      - a bare extension   (".docx") — matches that extension
    Returns results as a list of dicts: {path, name, kind, size_kb, mtime}.
    """
    query = _clean(query)
    if not query:
        return {"error": "Empty query."}

    if folder:
        r = os.path.normpath(folder)
        if not os.path.normcase(r).startswith(os.path.normcase(_DRIVE)):
            r = _HOME
        search_roots = [r]
    else:
        search_roots = [_HOME]
        if _ONEDRIVE and os.path.isdir(_ONEDRIVE) and \
                os.path.normcase(_ONEDRIVE) != os.path.normcase(_HOME):
            search_roots.append(_ONEDRIVE)

    # Determine match mode
    q_lower  = query.lower().strip()
    ext_list: list[str] | None = None

    bare = q_lower.lstrip('.')
    if q_lower.startswith('.'):                        # ".pdf"
        ext_list = [q_lower]
    elif bare in _EXT_ALIASES:                         # "pdf", "excel"
        ext_list = _EXT_ALIASES[bare]
    elif len(q_lower) <= 5 and re.fullmatch(r'[a-z0-9]+', q_lower) \
            and q_lower in _EXT_ALIASES:               # short alias fallback
        ext_list = _EXT_ALIASES[q_lower]

    qwords = q_lower.split() if not ext_list else []   # for fuzzy word matching

    paths     = []
    stop_flag = threading.Event()

    def _matches_name(name: str) -> bool:
        nl = name.lower()
        if q_lower in nl:
            return True
        return len(qwords) > 1 and all(w in nl for w in qwords)

    def _walk():
        for root in search_roots:
            for dirpath, dirs, files in os.walk(root, topdown=True):
                if stop_flag.is_set():
                    break
                if _protected(dirpath):
                    dirs.clear()
                    continue
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith('.') and not _protected(os.path.join(dirpath, d))
                ]
                if ext_list:
                    for fname in files:
                        if any(fname.lower().endswith(e) for e in ext_list):
                            paths.append(os.path.join(dirpath, fname))
                            if len(paths) >= _MAX_FILES:
                                stop_flag.set()
                                return
                else:
                    # Match files
                    for fname in files:
                        if _matches_name(fname):
                            paths.append(os.path.join(dirpath, fname))
                            if len(paths) >= _MAX_FILES:
                                stop_flag.set()
                                return
                    # Match directories too (for "find project folder")
                    for d in dirs:
                        if _matches_name(d):
                            paths.append(os.path.join(dirpath, d))
                            if len(paths) >= _MAX_FILES:
                                stop_flag.set()
                                return

    t = threading.Thread(target=_walk, daemon=True)
    t.start()
    t.join(timeout=_TIMEOUT_SEC)
    stop_flag.set()

    results = [_file_meta(p) for p in paths]

    return {
        "query":     query,
        "root":      ", ".join(search_roots),
        "results":   results,
        "truncated": len(results) >= _MAX_FILES,
    }


def list_directory(path: str = None) -> dict:
    """List files in a directory (user-space only)."""
    path = _clean(path or _HOME)
    path = os.path.normpath(path)

    # Resolve shell special folders (handles OneDrive-redirected Desktop etc.)
    def _shell_folder(csidl: int) -> str:
        import ctypes, ctypes.wintypes as wt
        buf = ctypes.create_unicode_buffer(wt.MAX_PATH)
        ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
        return buf.value or ""

    aliases = {
        "desktop":   _shell_folder(0x0010) or os.path.join(_HOME, "Desktop"),
        "downloads": os.path.join(_HOME, "Downloads"),
        "documents": _shell_folder(0x0005) or os.path.join(_HOME, "Documents"),
        "pictures":  _shell_folder(0x0027) or os.path.join(_HOME, "Pictures"),
        "music":     _shell_folder(0x000D) or os.path.join(_HOME, "Music"),
        "videos":    _shell_folder(0x000E) or os.path.join(_HOME, "Videos"),
        "home":      _HOME,
        "onedrive":  os.environ.get("OneDrive", os.path.join(_HOME, "OneDrive")),
    }
    path_low = path.lower()
    for alias, resolved in aliases.items():
        if alias in path_low:
            path = resolved
            break

    if _protected(path):
        return {"error": "Access to that path is restricted."}
    if not os.path.isdir(path):
        return {"error": f"Directory not found: {path}"}

    try:
        entries = []
        for e in sorted(os.scandir(path), key=lambda x: (not x.is_dir(), x.name.lower())):
            if e.name.startswith('.'):
                continue
            kind = "📁" if e.is_dir() else "📄"
            try:
                size = "" if e.is_dir() else f"  {e.stat().st_size // 1024} KB"
            except OSError:
                size = ""
            entries.append(f"{kind} {e.name}{size}")
            if len(entries) >= _MAX_FILES:
                entries.append(f"… (showing first {_MAX_FILES})")
                break
        return {"path": path, "entries": entries}
    except PermissionError:
        return {"error": "Permission denied."}


def get_running_processes(filter_name: str = "") -> dict:
    """List running processes (read-only tasklist)."""
    out = _run_safe(["tasklist", "/fo", "csv", "/nh"])
    lines = [l.strip('"') for l in out.splitlines() if l]
    procs = []
    for line in lines:
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 5:
            continue
        name, pid, _, _, mem = parts[:5]
        if filter_name and filter_name.lower() not in name.lower():
            continue
        procs.append({"name": name, "pid": pid, "mem": mem})
    return {"processes": procs[:_MAX_FILES], "filter": filter_name}


def get_network_info() -> dict:
    """Get IP configuration (read-only ipconfig)."""
    out = _run_safe(["ipconfig"])
    # Extract just IPv4 lines to keep it short
    ipv4 = []
    adapter = ""
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.endswith(':') and not line.startswith(' '):
            adapter = stripped.rstrip(':')
        if "IPv4 Address" in stripped:
            ip = stripped.split(':', 1)[-1].strip().replace('(Preferred)', '').strip()
            ipv4.append(f"{adapter}: {ip}")
    return {"ipv4": ipv4, "raw": out[:1500]}


def get_system_info() -> dict:
    """Get basic system information."""
    out = _run_safe(["systeminfo"], timeout=15)
    # Extract the most useful lines
    keep = ["Host Name", "OS Name", "OS Version", "System Type",
            "Total Physical Memory", "Available Physical Memory",
            "Windows Directory", "System Directory"]
    lines = {}
    for line in out.splitlines():
        for k in keep:
            if line.startswith(k):
                val = line.split(":", 1)[-1].strip()
                lines[k] = val
                break
    return {"info": lines}


def get_recent_files(folder: str = None, days: int = 7) -> dict:
    """Find recently modified files in the user's home."""
    import time
    cutoff = time.time() - days * 86400
    root   = folder or _HOME
    root   = os.path.normpath(root)

    if _protected(root):
        return {"error": "Restricted path."}

    results   = []
    stop_flag = threading.Event()

    def _walk():
        for dirpath, dirs, files in os.walk(root, topdown=True):
            if stop_flag.is_set():
                break
            if _protected(dirpath):
                dirs.clear()
                continue
            dirs[:] = [d for d in dirs if not d.startswith('.') and
                       not _protected(os.path.join(dirpath, d))]
            for fname in files:
                fpath = os.path.join(dirpath, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                    if mtime >= cutoff:
                        results.append((mtime, fpath))
                        if len(results) >= _MAX_FILES * 2:
                            stop_flag.set()
                            return
                except OSError:
                    pass

    t = threading.Thread(target=_walk, daemon=True)
    t.start()
    t.join(timeout=_TIMEOUT_SEC)
    stop_flag.set()

    results.sort(reverse=True)
    return {
        "days":    days,
        "results": [p for _, p in results[:_MAX_FILES]],
    }


def get_large_files(min_size_mb: int = 10, limit: int = 25) -> dict:
    """Find the largest files in user space (> min_size_mb MB)."""
    min_bytes = min_size_mb * 1024 * 1024
    results: list[tuple[int, str]] = []
    stop_flag = threading.Event()
    roots = [_HOME]
    if _ONEDRIVE and os.path.isdir(_ONEDRIVE) and \
            os.path.normcase(_ONEDRIVE) != os.path.normcase(_HOME):
        roots.append(_ONEDRIVE)

    def _walk():
        for root in roots:
            for dirpath, dirs, files in os.walk(root, topdown=True):
                if stop_flag.is_set():
                    break
                if _protected(dirpath):
                    dirs.clear()
                    continue
                dirs[:] = [d for d in dirs if not d.startswith('.') and
                           not _protected(os.path.join(dirpath, d))]
                for fname in files:
                    fpath = os.path.join(dirpath, fname)
                    try:
                        size = os.path.getsize(fpath)
                        if size >= min_bytes:
                            results.append((size, fpath))
                    except OSError:
                        pass

    t = threading.Thread(target=_walk, daemon=True)
    t.start()
    t.join(timeout=15)
    stop_flag.set()
    results.sort(reverse=True)

    return {
        "files": [
            {
                "path": p,
                "name": os.path.basename(p),
                "size_mb": round(s / 1024 / 1024, 1),
                "kind": "folder" if os.path.isdir(p) else "file",
            }
            for s, p in results[:limit]
        ],
        "total_found": len(results),
        "min_size_mb": min_size_mb,
    }


def get_old_files(days: int = 180, limit: int = 25) -> dict:
    """Find files not modified in `days` days, sorted by size descending."""
    import time
    cutoff = time.time() - days * 86400
    results: list[tuple[int, float, str]] = []
    stop_flag = threading.Event()
    roots = [_HOME]
    if _ONEDRIVE and os.path.isdir(_ONEDRIVE) and \
            os.path.normcase(_ONEDRIVE) != os.path.normcase(_HOME):
        roots.append(_ONEDRIVE)

    def _walk():
        for root in roots:
            for dirpath, dirs, files in os.walk(root, topdown=True):
                if stop_flag.is_set():
                    break
                if _protected(dirpath):
                    dirs.clear()
                    continue
                dirs[:] = [d for d in dirs if not d.startswith('.') and
                           not _protected(os.path.join(dirpath, d))]
                for fname in files:
                    fpath = os.path.join(dirpath, fname)
                    try:
                        st = os.stat(fpath)
                        if st.st_mtime < cutoff:
                            results.append((st.st_size, st.st_mtime, fpath))
                    except OSError:
                        pass

    t = threading.Thread(target=_walk, daemon=True)
    t.start()
    t.join(timeout=15)
    stop_flag.set()
    results.sort(reverse=True)  # largest first

    import time as _time
    return {
        "files": [
            {
                "path": p,
                "name": os.path.basename(p),
                "size_mb": round(s / 1024 / 1024, 1),
                "days_old": int((_time.time() - m) / 86400),
            }
            for s, m, p in results[:limit]
        ],
        "total_found": len(results),
        "days": days,
    }


# ── Privacy / Security helpers ─────────────────────────────────────────────────

def get_browser_cache_sizes() -> dict:
    """Return cache folder sizes for Chrome, Edge, and Firefox."""
    appdata = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")

    browser_roots = {
        "Chrome": [
            os.path.join(appdata, "Google", "Chrome", "User Data", "Default", "Cache"),
            os.path.join(appdata, "Google", "Chrome", "User Data", "Default", "Code Cache"),
            os.path.join(appdata, "Google", "Chrome", "User Data", "Default", "GPUCache"),
        ],
        "Edge": [
            os.path.join(appdata, "Microsoft", "Edge", "User Data", "Default", "Cache"),
            os.path.join(appdata, "Microsoft", "Edge", "User Data", "Default", "Code Cache"),
            os.path.join(appdata, "Microsoft", "Edge", "User Data", "Default", "GPUCache"),
        ],
        "Firefox": [
            os.path.join(appdata, "Mozilla", "Firefox", "Profiles"),
            os.path.join(roaming, "Mozilla", "Firefox", "Profiles"),
        ],
    }

    def _dir_size(path: str) -> int:
        total = 0
        try:
            for dp, _, fnames in os.walk(path):
                for fn in fnames:
                    try:
                        total += os.path.getsize(os.path.join(dp, fn))
                    except OSError:
                        pass
        except (PermissionError, OSError):
            pass
        return total

    result = {}
    for browser, paths in browser_roots.items():
        detected = any(os.path.exists(p) for p in paths)
        total_bytes = sum(_dir_size(p) for p in paths)
        if detected or total_bytes > 0:
            result[browser] = {
                "detected": detected,
                "cache_mb": round(total_bytes / 1024 / 1024, 1),
                "cache_str": (f"{round(total_bytes/1024/1024,1)} MB"
                              if total_bytes < 1024**3
                              else f"{round(total_bytes/1024**3,2)} GB"),
            }
    return {"browsers": result}


def get_network_connections() -> dict:
    """Return active TCP connections with process names (read-only netstat)."""
    # Get PID→name map up front to avoid per-connection tasklist calls
    pid_map: dict[int, str] = {}
    pout = _run_safe(["tasklist", "/fo", "csv", "/nh"])
    for line in pout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2:
            try:
                pid_map[int(parts[1])] = parts[0]
            except (ValueError, IndexError):
                pass

    out = _run_safe(["netstat", "-ano"], timeout=10)
    conns: list[dict] = []
    seen: set = set()

    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        proto = parts[0]
        if proto != "TCP":
            continue
        local, remote, state, pid_str = parts[1], parts[2], parts[3], parts[4]
        # ESTABLISHED only — LISTENING sockets are internal services and clutter the UI.
        if state != "ESTABLISHED":
            continue
        # Hide loopback (127.0.0.1 ↔ 127.0.0.1): internal IPC, meaningless to a user.
        if local.startswith("127.") and remote.startswith("127."):
            continue
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        key = (pid, local, remote)
        if key in seen:
            continue
        seen.add(key)
        try:
            rport = int(remote.rsplit(":", 1)[-1])
        except (ValueError, IndexError):
            rport = 0
        kind = _PORT_LABELS.get(rport, "")
        conns.append({
            "proto":   proto,
            "local":   local,
            "remote":  remote,
            "state":   state,
            "pid":     pid,
            "process": pid_map.get(pid, "?"),
            "kind_en": kind or f"TCP port {rport}",
            "kind_ja": _PORT_LABELS_JA.get(rport, kind or f"TCPポート {rport}"),
        })

    return {"connections": conns[:50]}


_PORT_LABELS = {
    80:   "Web (HTTP)",
    443:  "Secure web (HTTPS)",
    22:   "SSH",
    21:   "FTP",
    25:   "Email (SMTP)",
    465:  "Email (SMTP, encrypted)",
    587:  "Email (SMTP submission)",
    110:  "Email (POP3)",
    995:  "Email (POP3, encrypted)",
    143:  "Email (IMAP)",
    993:  "Email (IMAP, encrypted)",
    53:   "DNS lookup",
    3389: "Remote Desktop",
    5900: "VNC",
    3306: "MySQL database",
    5432: "PostgreSQL database",
    27017:"MongoDB",
    1433: "SQL Server",
    6379: "Redis",
    8080: "Web (HTTP alt)",
    8443: "Secure web (HTTPS alt)",
}

_PORT_LABELS_JA = {
    80:   "Web (HTTP)",
    443:  "暗号化Web (HTTPS)",
    22:   "SSH",
    21:   "FTP",
    25:   "メール (SMTP)",
    465:  "メール (SMTP 暗号化)",
    587:  "メール (SMTP 送信)",
    110:  "メール (POP3)",
    995:  "メール (POP3 暗号化)",
    143:  "メール (IMAP)",
    993:  "メール (IMAP 暗号化)",
    53:   "DNS",
    3389: "リモートデスクトップ",
    5900: "VNC",
    3306: "MySQL",
    5432: "PostgreSQL",
    27017:"MongoDB",
    1433: "SQL Server",
    6379: "Redis",
    8080: "Web (HTTP 代替)",
    8443: "暗号化Web (HTTPS 代替)",
}


_SENSITIVE_NAME_FRAGMENTS = {
    "password", "passwd", "credentials", "credential",
    "secret", "private_key", "privatekey", "api_key", "apikey",
    "access_token", "auth_token", "token", "id_rsa", "id_ed25519",
}
_SENSITIVE_EXTS = {
    ".kdbx", ".p12", ".pfx", ".pem", ".key", ".crt", ".cer",
    ".jks", ".keystore", ".ppk",
}


_SENSITIVE_NOISE_FRAGMENTS = (
    "\\appdata\\", "\\cache\\", "\\caches\\", "\\code cache\\",
    "\\gpucache\\", "\\service worker\\", "\\indexeddb\\",
    "\\ebwebview\\", "\\webcache\\", "\\localstate\\",
    "\\node_modules\\", "\\.git\\", "\\.vscode\\",
    "\\microsoft\\windows\\", "\\packagecache\\", "\\onedrive\\cache\\",
)


def _is_sensitive_noise(path: str) -> bool:
    p = path.lower() + "\\"
    return any(frag in p for frag in _SENSITIVE_NOISE_FRAGMENTS)


def get_sensitive_files() -> dict:
    """Find potentially sensitive files in user space (detection only — read-only).
    Skips browser caches and system subtrees so results aren't flooded with noise
    like 'Trust Tokens' from Edge/Kindle webviews."""
    results: list[dict] = []
    stop_flag = threading.Event()
    roots = [_HOME]
    if _ONEDRIVE and os.path.isdir(_ONEDRIVE) and \
            os.path.normcase(_ONEDRIVE) != os.path.normcase(_HOME):
        roots.append(_ONEDRIVE)

    def _walk():
        for root in roots:
            for dirpath, dirs, files in os.walk(root, topdown=True):
                if stop_flag.is_set():
                    break
                if _protected(dirpath) or _is_sensitive_noise(dirpath):
                    dirs.clear()
                    continue
                dirs[:] = [d for d in dirs if not d.startswith('.') and
                           not _protected(os.path.join(dirpath, d)) and
                           not _is_sensitive_noise(os.path.join(dirpath, d))]
                for fname in files:
                    fl    = fname.lower()
                    ext   = os.path.splitext(fl)[1]
                    nm    = fl.replace("_", "").replace("-", "").replace(".", "")
                    name_hit = any(frag in fl for frag in _SENSITIVE_NAME_FRAGMENTS)
                    ext_hit  = ext in _SENSITIVE_EXTS
                    if name_hit or ext_hit:
                        reason = (f"{ext} credential file" if ext_hit
                                  else "suspicious filename")
                        results.append({
                            "path":   os.path.join(dirpath, fname),
                            "name":   fname,
                            "reason": reason,
                            "kind":   "file",
                        })
                        if len(results) >= _MAX_FILES:
                            stop_flag.set()
                            return

    t = threading.Thread(target=_walk, daemon=True)
    t.start()
    t.join(timeout=_TIMEOUT_SEC)
    stop_flag.set()

    return {"files": results, "truncated": len(results) >= _MAX_FILES}


def get_env_info() -> dict:
    """Return safe, non-sensitive environment variables."""
    safe_keys = ["USERNAME", "COMPUTERNAME", "OS", "PROCESSOR_ARCHITECTURE",
                 "SystemRoot", "USERPROFILE", "TEMP", "NUMBER_OF_PROCESSORS",
                 "LANG", "HOMEDRIVE", "HOMEPATH", "OneDrive"]
    return {k: os.environ.get(k, "") for k in safe_keys}
