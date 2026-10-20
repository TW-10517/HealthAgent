"""PC Health Agent — proactive suggestions engine.

Scans the PC for actionable findings and returns plain-language suggestions.
All checks are local, read-only, and respect the protected-path list from scanner.
"""
import os
import json
import shutil
import subprocess
import winreg
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timedelta
from scanner import is_protected, fmt_size, dir_size, PROTECTED, IT_PATTERN

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:3b"

# ── Helpers ──────────────────────────────────────────────────────────────────

_HOME = Path.home()
_DOWNLOADS = _HOME / "Downloads"
_DOCUMENTS = _HOME / "Documents"
_DESKTOP = _HOME / "Desktop"

SCAN_ROOTS = [_DOWNLOADS, _DOCUMENTS, _DESKTOP]

_SKIP_DIRS = {"node_modules", ".venv", ".git", "__pycache__", ".mypy_cache",
              "$Recycle.Bin", "System Volume Information", "AppData"}


def _safe_scandir(path):
    try:
        yield from os.scandir(path)
    except (PermissionError, OSError):
        return


def _safe_stat(path):
    try:
        return os.stat(path)
    except (PermissionError, OSError):
        return None


# ── 1. Storage warning ──────────────────────────────────────────────────────

def check_storage() -> dict | None:
    usage = shutil.disk_usage("C:\\")
    pct_used = round(usage.used / usage.total * 100)
    pct_free = 100 - pct_used
    free_gb = round(usage.free / (1 << 30), 1)

    if pct_free >= 20:
        return None

    sev = "critical" if pct_free < 5 else "error" if pct_free < 10 else "warning"
    return {
        "id": "storage_full",
        "category": "storage",
        "severity": sev,
        "en": f"Your storage is {pct_used}% full — only {free_gb} GB free on C:",
        "ja": f"ストレージが{pct_used}%使用中 — Cドライブの空きは{free_gb} GBのみ",
        "detail_en": "Clean temp files or old downloads to reclaim space.",
        "detail_ja": "一時ファイルや古いダウンロードを削除して容量を確保。",
        "action": "show_storage",
        "metric": {"pct_used": pct_used, "free_gb": free_gb},
    }


# ── 2. Empty folders ────────────────────────────────────────────────────────

def check_empty_folders(roots=None, max_depth=5) -> dict | None:
    roots = roots or SCAN_ROOTS
    empty = []

    def _walk(path, depth):
        if depth > max_depth:
            return
        if is_protected(str(path)):
            return
        for entry in _safe_scandir(path):
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
                continue
            try:
                contents = list(os.scandir(entry.path))
                if not contents:
                    empty.append(entry.path)
                else:
                    _walk(entry.path, depth + 1)
            except (PermissionError, OSError):
                pass

    for root in roots:
        if root.exists():
            _walk(root, 0)

    if len(empty) < 5:
        return None

    return {
        "id": "empty_folders",
        "category": "cleanup",
        "severity": "info",
        "en": f"You have {len(empty)} empty folders that can be removed.",
        "ja": f"削除可能な空フォルダが{len(empty)}個あります。",
        "detail_en": f"In {', '.join(str(Path(p).parent.name) for p in empty[:3])} and others.",
        "detail_ja": f"{', '.join(str(Path(p).parent.name) for p in empty[:3])} 等にあります。",
        "action": "list_empty_folders",
        "items": empty[:200],
        "metric": {"count": len(empty)},
    }


# ── 3. Large files ──────────────────────────────────────────────────────────

def check_large_files(roots=None, top_n=10, min_size_mb=200) -> dict | None:
    roots = roots or SCAN_ROOTS
    min_bytes = min_size_mb * (1 << 20)
    large = []

    for root in roots:
        if not root.exists():
            continue
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS
                               and not is_protected(os.path.join(dirpath, d))]
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    st = _safe_stat(fp)
                    if st and st.st_size >= min_bytes:
                        large.append({"path": fp, "name": f, "size": st.st_size,
                                      "fmt": fmt_size(st.st_size)})
        except (PermissionError, OSError):
            pass

    large.sort(key=lambda x: x["size"], reverse=True)
    large = large[:top_n]

    if not large:
        return None

    total = sum(f["size"] for f in large)
    return {
        "id": "large_files",
        "category": "storage",
        "severity": "info",
        "en": f"These {len(large)} files use {fmt_size(total)} of space.",
        "ja": f"これらの{len(large)}ファイルが{fmt_size(total)}を使用しています。",
        "detail_en": f"Largest: {large[0]['name']} ({large[0]['fmt']})",
        "detail_ja": f"最大: {large[0]['name']} ({large[0]['fmt']})",
        "action": "list_large_files",
        "items": [{"path": f["path"], "name": f["name"], "size": f["size"],
                   "fmt": f["fmt"]} for f in large],
        "metric": {"count": len(large), "total_bytes": total, "total_fmt": fmt_size(total)},
    }


# ── 4. Old downloads ────────────────────────────────────────────────────────

def check_old_downloads(days=180) -> dict | None:
    dl = _DOWNLOADS
    if not dl.exists():
        return None

    cutoff = datetime.now().timestamp() - (days * 86400)
    old = []

    for entry in _safe_scandir(dl):
        if entry.name.startswith("."):
            continue
        st = _safe_stat(entry.path)
        if not st:
            continue
        last_access = max(st.st_atime, st.st_mtime)
        if last_access < cutoff:
            sz = st.st_size if entry.is_file(follow_symlinks=False) else dir_size(entry.path)
            old.append({"path": entry.path, "name": entry.name,
                        "size": sz, "fmt": fmt_size(sz), "is_dir": entry.is_dir()})

    if not old:
        return None

    old.sort(key=lambda x: x["size"], reverse=True)
    total = sum(f["size"] for f in old)

    if total < 50 * (1 << 20):
        return None

    return {
        "id": "old_downloads",
        "category": "cleanup",
        "severity": "warning" if total > 1 << 30 else "info",
        "en": f"Files in Downloads not opened for {days}+ days: {fmt_size(total)} ({len(old)} items)",
        "ja": f"ダウンロードの{days}日以上未使用ファイル: {fmt_size(total)} ({len(old)}件)",
        "detail_en": "Archive or delete to free space.",
        "detail_ja": "アーカイブまたは削除して容量を解放。",
        "action": "list_old_downloads",
        "items": [{"path": f["path"], "name": f["name"], "size": f["size"],
                   "fmt": f["fmt"]} for f in old[:50]],
        "metric": {"count": len(old), "total_bytes": total, "total_fmt": fmt_size(total),
                   "days": days},
    }


# ── 5. Startup apps ─────────────────────────────────────────────────────────

def check_startup_apps() -> dict | None:
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
                        if not is_it:
                            apps.append({"name": name, "command": val[:100]})
                        i += 1
                    except OSError:
                        break
        except OSError:
            pass

    if len(apps) <= 5:
        return None

    return {
        "id": "startup_apps",
        "category": "performance",
        "severity": "warning" if len(apps) > 8 else "info",
        "en": f"{len(apps)} personal apps start with Windows and may slow startup.",
        "ja": f"{len(apps)}個の個人アプリがWindows起動時に実行され、起動が遅くなる可能性があります。",
        "detail_en": f"e.g. {', '.join(a['name'] for a in apps[:3])}",
        "detail_ja": f"例: {', '.join(a['name'] for a in apps[:3])}",
        "action": "show_startup",
        "items": apps,
        "metric": {"count": len(apps)},
    }


# ── 6. SSD / Disk health ────────────────────────────────────────────────────

def check_disk_health() -> dict | None:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-PhysicalDisk | Select-Object MediaType,HealthStatus,OperationalStatus "
             "| ForEach-Object { $_.MediaType + '|' + $_.HealthStatus + '|' + $_.OperationalStatus }"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10
        )
        for line in r.stdout.strip().splitlines():
            parts = line.strip().split("|")
            if len(parts) >= 3:
                media, health, op = parts[0].strip(), parts[1].strip(), parts[2].strip()
                if health != "Healthy" or op != "OK":
                    return {
                        "id": "disk_health",
                        "category": "hardware",
                        "severity": "critical" if health == "Unhealthy" else "warning",
                        "en": f"Disk health issue: {health} ({op}). Back up your data.",
                        "ja": f"ディスクの健康状態に問題: {health} ({op})。データをバックアップしてください。",
                        "detail_en": f"{media} disk: {health} / {op}. Back up now.",
                        "detail_ja": f"{media}ディスク: {health} / {op}。今すぐバックアップを。",
                        "action": None,
                        "metric": {"media": media, "health": health, "status": op},
                    }
    except Exception:
        pass
    return None


# ── 7. Battery health ───────────────────────────────────────────────────────

def check_battery_health() -> dict | None:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Battery | ForEach-Object { "
             "$_.EstimatedChargeRemaining.ToString() + '|' + $_.Status }"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=8
        )
        output = r.stdout.strip()
        if not output:
            return None
        parts = output.split("|")
        if len(parts) >= 2:
            charge = int(parts[0])
            status = parts[1].strip()
            if status != "OK":
                return {
                    "id": "battery_health",
                    "category": "hardware",
                    "severity": "warning",
                    "en": f"Battery status: {status} (charge: {charge}%). Consider getting it checked.",
                    "ja": f"バッテリー状態: {status} (充電: {charge}%)。点検を検討してください。",
                    "detail_en": "Possible reduced capacity or unexpected shutdowns.",
                    "detail_ja": "容量低下や予期しないシャットダウンの可能性。",
                    "action": None,
                    "metric": {"charge": charge, "status": status},
                }
    except Exception:
        pass
    return None


# ── 8. Temp / cache size ────────────────────────────────────────────────────

def check_temp_cache() -> dict | None:
    temp_dir = os.environ.get("TEMP", "")
    local = os.environ.get("LOCALAPPDATA", "")
    targets = [
        temp_dir,
        "C:\\Windows\\Temp",
        os.path.join(local, "pip", "Cache"),
        os.path.join(os.environ.get("APPDATA", ""), "npm-cache"),
    ]
    total = 0
    for t in targets:
        if t and os.path.isdir(t) and not is_protected(t):
            total += dir_size(t)

    if total < 500 * (1 << 20):
        return None

    return {
        "id": "temp_cache",
        "category": "cleanup",
        "severity": "warning" if total > 2 * (1 << 30) else "info",
        "en": f"Temporary and cache files are using {fmt_size(total)}.",
        "ja": f"一時ファイルとキャッシュが{fmt_size(total)}を使用しています。",
        "detail_en": "Safe to clean. Run a scan to review and remove.",
        "detail_ja": "安全に削除可能。スキャンで確認・削除できます。",
        "action": "run_scan",
        "metric": {"total_bytes": total, "total_fmt": fmt_size(total)},
    }


# ── 9. Folder space usage ───────────────────────────────────────────────────

def check_folder_usage() -> dict | None:
    folders = []
    user_dirs = [_DOWNLOADS, _DOCUMENTS, _DESKTOP,
                 _HOME / "Pictures", _HOME / "Videos", _HOME / "Music"]
    for d in user_dirs:
        if d.exists() and not is_protected(str(d)):
            sz = dir_size(str(d))
            if sz > 100 * (1 << 20):
                folders.append({"path": str(d), "name": d.name,
                                "size": sz, "fmt": fmt_size(sz)})

    folders.sort(key=lambda x: x["size"], reverse=True)

    if not folders:
        return None

    biggest = folders[0]
    return {
        "id": "folder_usage",
        "category": "storage",
        "severity": "info",
        "en": f"Your largest user folder is {biggest['name']} at {biggest['fmt']}.",
        "ja": f"最大のユーザーフォルダは{biggest['name']}で{biggest['fmt']}です。",
        "detail_en": "Breakdown: " + ", ".join(f"{f['name']}: {f['fmt']}" for f in folders[:5]),
        "detail_ja": "内訳: " + ", ".join(f"{f['name']}: {f['fmt']}" for f in folders[:5]),
        "action": None,
        "items": folders,
        "metric": {"folders": len(folders)},
    }


# ── Run all checks ──────────────────────────────────────────────────────────

_SEVERITY_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3}

def run_all_checks() -> list[dict]:
    """Run all proactive checks and return sorted suggestions."""
    checks = [
        check_storage,
        check_disk_health,
        check_battery_health,
        check_temp_cache,
        check_empty_folders,
        check_large_files,
        check_old_downloads,
        check_startup_apps,
        check_folder_usage,
    ]
    results = []
    for fn in checks:
        try:
            result = fn()
            if result:
                results.append(result)
        except Exception:
            pass

    results.sort(key=lambda x: _SEVERITY_ORDER.get(x.get("severity", "info"), 9))
    return results


# ── AI-powered summary via local Ollama ─────────────────────────────────────

def _build_ai_prompt(suggestions: list[dict], lang: str = "en") -> str:
    lines = []
    for s in suggestions:
        sev = s.get("severity", "info")
        msg = s.get("en", "") if lang == "en" else s.get("ja", "")
        metric = s.get("metric", {})
        metric_str = ", ".join(f"{k}={v}" for k, v in metric.items()) if metric else ""
        lines.append(f"[{sev}] {msg}" + (f" ({metric_str})" if metric_str else ""))

    findings = "\n".join(lines) if lines else "No issues found."

    if lang == "ja":
        return (
            "あなたはPCヘルスアドバイザーです。以下のPC診断結果を分析し、"
            "最も重要な対処から順に、3〜4文の簡潔なアドバイスを日本語で書いてください。"
            "具体的な数値を使い、専門用語を避けてください。\n\n"
            f"診断結果:\n{findings}"
        )
    return (
        "You are a PC health advisor. Analyze these PC diagnostic findings and write "
        "3-4 sentences of advice, prioritized by importance. Use specific numbers, "
        "keep it conversational, no jargon.\n\n"
        f"Findings:\n{findings}"
    )


def get_ai_summary(suggestions: list[dict], lang: str = "en") -> dict | None:
    """Call local Ollama to generate a natural-language AI summary.
    Returns None if Ollama is unavailable or fails."""
    if not suggestions:
        return None

    prompt = _build_ai_prompt(suggestions, lang)

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 200},
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            text = body.get("response", "").strip()
            if not text:
                return None
            return {
                "summary": text,
                "model": OLLAMA_MODEL,
                "lang": lang,
            }
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
        return None
    except Exception:
        return None
