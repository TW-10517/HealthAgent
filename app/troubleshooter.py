"""PC Health Agent — Rule-based smart troubleshooting engine.

Analyzes scan results, monitoring data, and Windows events to detect issues,
show evidence, and guide users through safe fixes.

Safety levels:
  - read_only:  Inspect only, no changes
  - low_risk:   Reversible actions (refresh scan, generate report)
  - confirm:    Requires explicit user confirmation (delete temp, change startup)
  - prohibited: Never allowed (registry mods, disable security, delete system files)
"""
import os
import json
from datetime import datetime
from pathlib import Path
from scanner import is_protected, IT_PATTERN

# ── Action safety classification ────────────────────────────────────────────

ACTION_SAFETY = {
    "refresh_scan": "low_risk",
    "view_report": "read_only",
    "clean_temp": "confirm",
    "remove_empty_folders": "confirm",
    "organize_files": "confirm",
    "open_task_manager": "low_risk",
    "open_disk_cleanup": "low_risk",
    "open_event_viewer": "low_risk",
    "run_sfc": "confirm",
    "run_chkdsk": "confirm",
    "check_updates": "low_risk",
}

PROHIBITED_ACTIONS = {
    "modify_registry",
    "disable_firewall",
    "disable_antivirus",
    "delete_system_files",
    "alter_drivers",
    "terminate_critical_process",
    "reset_windows",
    "execute_arbitrary_command",
}

# ── Action definitions with descriptions ────────────────────────────────────

SAFE_ACTIONS = {
    "clean_temp": {
        "en": "Clean temporary and cache files",
        "ja": "一時ファイルとキャッシュを削除",
        "risk_en": "Removes cached data that apps may rebuild. Browser sessions stay intact.",
        "risk_ja": "キャッシュデータを削除。ブラウザセッションは保持されます。",
        "page": "storage",
    },
    "organize_files": {
        "en": "Organize Downloads folder into categories",
        "ja": "ダウンロードフォルダをカテゴリ別に整理",
        "risk_en": "Moves files into subfolders. Nothing is deleted. An undo log is saved.",
        "risk_ja": "ファイルをサブフォルダに移動。何も削除されません。元に戻すログが保存されます。",
        "page": "organize",
    },
    "remove_empty_folders": {
        "en": "Remove empty folders",
        "ja": "空のフォルダを削除",
        "risk_en": "Deletes only truly empty directories. Parent folders are not affected.",
        "risk_ja": "完全に空のディレクトリのみ削除。親フォルダには影響しません。",
        "page": "insights",
    },
    "open_task_manager": {
        "en": "Open Task Manager to manage processes",
        "ja": "タスクマネージャーを開いてプロセスを管理",
        "risk_en": "Opens the built-in Windows Task Manager. No changes made.",
        "risk_ja": "Windowsタスクマネージャーを開きます。変更は行いません。",
    },
    "open_disk_cleanup": {
        "en": "Open Windows Disk Cleanup",
        "ja": "Windowsディスク クリーンアップを開く",
        "risk_en": "Opens the built-in cleanup tool. You choose what to remove.",
        "risk_ja": "組み込みクリーンアップツールを開きます。削除対象は自分で選択。",
    },
    "open_event_viewer": {
        "en": "Open Windows Event Viewer",
        "ja": "Windowsイベントビューアーを開く",
        "risk_en": "Opens the built-in event log viewer. Read-only.",
        "risk_ja": "組み込みイベントログビューアーを開きます。読み取り専用。",
    },
    "check_updates": {
        "en": "Open Windows Update settings",
        "ja": "Windows Update設定を開く",
        "risk_en": "Opens Settings to check for updates. You decide what to install.",
        "risk_ja": "設定を開いて更新を確認。インストールするかは自分で決定。",
    },
    "run_sfc": {
        "en": "Run System File Checker (sfc /scannow)",
        "ja": "システムファイルチェッカーを実行 (sfc /scannow)",
        "risk_en": "Scans and repairs protected system files. Requires admin. Takes 5-15 minutes.",
        "risk_ja": "保護されたシステムファイルをスキャン・修復。管理者権限が必要。5-15分かかります。",
    },
}

# ── Diagnostic rules ────────────────────────────────────────────────────────

def run_diagnostics(scan_data: dict = None, metrics: dict = None,
                    events: list = None, suggestions: list = None) -> list:
    """Run all diagnostic rules and return prioritized issues."""
    issues = []

    # ── From scan data ──
    if scan_data:
        d = scan_data.get("disk", {})
        if d.get("pct_free", 100) < 10:
            issues.append(_issue(
                id="critical_disk_space",
                category="storage",
                confidence=95,
                severity="critical",
                en=f"Critical: Only {d.get('fmt_free', '?')} free on C: drive",
                ja=f"重大: Cドライブの空きが{d.get('fmt_free', '?')}のみ",
                evidence_en=f"Disk is {100 - d.get('pct_free', 0)}% full ({d.get('free_gb', '?')} GB free of {d.get('total_gb', '?')} GB).",
                evidence_ja=f"ディスクは{100 - d.get('pct_free', 0)}%使用中（{d.get('total_gb', '?')} GB中{d.get('free_gb', '?')} GB空き）。",
                causes=["Large temp/cache files", "Too many downloads", "Large applications"],
                causes_ja=["大量の一時/キャッシュファイル", "ダウンロードが多すぎる", "大きなアプリケーション"],
                actions=["clean_temp", "organize_files", "open_disk_cleanup"],
            ))
        elif d.get("pct_free", 100) < 20:
            issues.append(_issue(
                id="low_disk_space",
                category="storage",
                confidence=90,
                severity="warning",
                en=f"Low disk space — {d.get('fmt_free', '?')} free",
                ja=f"ディスク空き容量不足 — {d.get('fmt_free', '?')}",
                evidence_en=f"Disk is {100 - d.get('pct_free', 0)}% full.",
                evidence_ja=f"ディスクは{100 - d.get('pct_free', 0)}%使用中。",
                causes=["Accumulated temp files", "Large downloads"],
                causes_ja=["蓄積された一時ファイル", "大きなダウンロード"],
                actions=["clean_temp", "organize_files"],
            ))

        total_temp = scan_data.get("total_temp", 0)
        if total_temp > 1 << 30:  # >1GB
            from scanner import fmt_size
            issues.append(_issue(
                id="high_temp_cache",
                category="cleanup",
                confidence=85,
                severity="warning",
                en=f"Temp & cache files using {fmt_size(total_temp)}",
                ja=f"一時ファイルとキャッシュが{fmt_size(total_temp)}を使用",
                evidence_en=f"{fmt_size(total_temp)} in temporary and cache directories.",
                evidence_ja=f"一時ディレクトリとキャッシュに{fmt_size(total_temp)}。",
                causes=["Browser caches", "Build tool caches", "Windows temp"],
                causes_ja=["ブラウザキャッシュ", "ビルドツールキャッシュ", "Windows一時ファイル"],
                actions=["clean_temp"],
            ))

        startup = scan_data.get("personal_apps", [])
        if len(startup) > 6:
            issues.append(_issue(
                id="too_many_startup",
                category="performance",
                confidence=75,
                severity="warning",
                en=f"{len(startup)} personal apps launch at startup",
                ja=f"{len(startup)}個の個人アプリが起動時に実行",
                evidence_en=f"{len(startup)} non-IT-managed apps in startup registry.",
                evidence_ja=f"起動レジストリにIT管理外のアプリが{len(startup)}個。",
                causes=["Apps adding themselves to startup during install"],
                causes_ja=["インストール時にスタートアップに追加されたアプリ"],
                actions=["open_task_manager"],
            ))

        procs = scan_data.get("processes", [])
        if procs and procs[0].get("mem_mb", 0) > 2000:
            p = procs[0]
            issues.append(_issue(
                id="high_memory_process",
                category="performance",
                confidence=70,
                severity="warning",
                en=f"{p['name']} is using {p['mem_mb']} MB of RAM",
                ja=f"{p['name']}が{p['mem_mb']} MBのRAMを使用",
                evidence_en=f"Top process {p['name']} using {p['mem_mb']} MB.",
                evidence_ja=f"上位プロセス{p['name']}が{p['mem_mb']} MBを使用。",
                causes=["Memory leak", "Large dataset open", "Too many tabs/windows"],
                causes_ja=["メモリリーク", "大きなデータセットが開いている", "タブ/ウィンドウが多すぎる"],
                actions=["open_task_manager"],
            ))

    # ── From live metrics ──
    if metrics:
        if metrics.get("cpu_pct", 0) >= 95:
            issues.append(_issue(
                id="cpu_maxed",
                category="performance",
                confidence=80,
                severity="warning",
                en=f"CPU usage is at {metrics['cpu_pct']}%",
                ja=f"CPU使用率が{metrics['cpu_pct']}%です",
                evidence_en=f"Current CPU load: {metrics['cpu_pct']}%.",
                evidence_ja=f"現在のCPU負荷: {metrics['cpu_pct']}%。",
                causes=["Background updates", "Antivirus scan", "Runaway process"],
                causes_ja=["バックグラウンド更新", "ウイルススキャン", "暴走プロセス"],
                actions=["open_task_manager"],
            ))

        if metrics.get("ram_pct", 0) >= 90:
            used_gb = round(metrics.get("ram_used_mb", 0) / 1024, 1)
            total_gb = round(metrics.get("ram_total_mb", 0) / 1024)
            issues.append(_issue(
                id="ram_pressure",
                category="performance",
                confidence=85,
                severity="warning",
                en=f"RAM usage at {metrics['ram_pct']}% ({used_gb}/{total_gb} GB)",
                ja=f"メモリ使用率{metrics['ram_pct']}%（{used_gb}/{total_gb} GB）",
                evidence_en=f"System RAM is {metrics['ram_pct']}% utilized.",
                evidence_ja=f"システムRAMの{metrics['ram_pct']}%が使用中。",
                causes=["Too many apps open", "Memory-heavy application", "Memory leak"],
                causes_ja=["アプリが多すぎる", "メモリ大量消費アプリ", "メモリリーク"],
                actions=["open_task_manager"],
            ))

        if metrics.get("has_temp") and metrics.get("cpu_temp_c", 0) >= 85:
            issues.append(_issue(
                id="high_temperature",
                category="hardware",
                confidence=75,
                severity="error" if metrics["cpu_temp_c"] >= 95 else "warning",
                en=f"CPU temperature is {metrics['cpu_temp_c']}°C",
                ja=f"CPU温度が{metrics['cpu_temp_c']}°Cです",
                evidence_en=f"Thermal sensor reading: {metrics['cpu_temp_c']}°C.",
                evidence_ja=f"温度センサー: {metrics['cpu_temp_c']}°C。",
                causes=["Dust buildup in vents", "Heavy workload", "Thermal paste degradation"],
                causes_ja=["通気口のほこり詰まり", "高負荷", "サーマルペーストの劣化"],
                actions=[],
            ))

    # ── From Windows events ──
    if events:
        crash_sources = {}
        bsod_count = 0
        power_loss = 0
        for e in events:
            src = e.get("source", "")
            if "kernel-power" in src.lower() and e.get("event_id") == "41":
                power_loss += 1
            elif "bugcheck" in src.lower() or "systemerror" in src.lower():
                bsod_count += 1
            elif e.get("level", 4) <= 2:
                crash_sources[src] = crash_sources.get(src, 0) + 1

        if bsod_count > 0:
            issues.append(_issue(
                id="bsod_detected",
                category="stability",
                confidence=90,
                severity="critical",
                en=f"{bsod_count} blue screen (BSOD) event(s) detected",
                ja=f"ブルースクリーン（BSOD）が{bsod_count}件検出",
                evidence_en=f"Found {bsod_count} BugCheck/SystemError entries in System log.",
                evidence_ja=f"システムログにBugCheck/SystemErrorが{bsod_count}件。",
                causes=["Driver conflict", "Hardware failure", "Overheating", "Corrupt system files"],
                causes_ja=["ドライバーの競合", "ハードウェア障害", "過熱", "システムファイルの破損"],
                actions=["check_updates", "run_sfc", "open_event_viewer"],
            ))

        if power_loss > 0:
            issues.append(_issue(
                id="unexpected_shutdown",
                category="stability",
                confidence=85,
                severity="error",
                en=f"{power_loss} unexpected shutdown(s) detected",
                ja=f"予期しないシャットダウンが{power_loss}件検出",
                evidence_en=f"Kernel-Power event 41 found {power_loss} time(s).",
                evidence_ja=f"Kernel-Powerイベント41が{power_loss}件。",
                causes=["Power outage", "Overheating", "Hardware issue", "Driver crash"],
                causes_ja=["停電", "過熱", "ハードウェア問題", "ドライバークラッシュ"],
                actions=["open_event_viewer", "check_updates"],
            ))

        for src, count in sorted(crash_sources.items(), key=lambda x: -x[1])[:3]:
            if count >= 3:
                issues.append(_issue(
                    id=f"frequent_errors_{src.replace(' ', '_').lower()[:20]}",
                    category="stability",
                    confidence=65,
                    severity="warning",
                    en=f"{src} has {count} error(s) recently",
                    ja=f"{src}で最近{count}件のエラー",
                    evidence_en=f"Source '{src}' logged {count} errors.",
                    evidence_ja=f"ソース'{src}'が{count}件のエラーを記録。",
                    causes=["Software bug", "Compatibility issue", "Corrupt installation"],
                    causes_ja=["ソフトウェアのバグ", "互換性の問題", "インストールの破損"],
                    actions=["check_updates", "open_event_viewer"],
                ))

    # Sort by severity then confidence
    sev_order = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    issues.sort(key=lambda x: (sev_order.get(x["severity"], 9), -x["confidence"]))

    return issues


def _issue(id, category, confidence, severity, en, ja,
           evidence_en, evidence_ja, causes, causes_ja, actions) -> dict:
    return {
        "id": id,
        "category": category,
        "confidence": confidence,
        "severity": severity,
        "en": en,
        "ja": ja,
        "evidence_en": evidence_en,
        "evidence_ja": evidence_ja,
        "causes": causes,
        "causes_ja": causes_ja,
        "actions": [a for a in actions if a in SAFE_ACTIONS],
    }


# ── Execute safe actions ────────────────────────────────────────────────────

def get_action_info(action_id: str) -> dict | None:
    if action_id not in SAFE_ACTIONS:
        return None
    info = SAFE_ACTIONS[action_id]
    safety = ACTION_SAFETY.get(action_id, "confirm")
    return {
        "id": action_id,
        "safety": safety,
        "en": info["en"],
        "ja": info["ja"],
        "risk_en": info.get("risk_en", ""),
        "risk_ja": info.get("risk_ja", ""),
        "page": info.get("page", ""),
        "requires_confirmation": safety == "confirm",
    }


def execute_action(action_id: str) -> dict:
    """Execute a safe, predefined action. Returns result dict."""
    if action_id in PROHIBITED_ACTIONS:
        return {"ok": False, "error": "This action is prohibited for safety reasons."}

    safety = ACTION_SAFETY.get(action_id)
    if not safety:
        return {"ok": False, "error": "Unknown action."}

    import subprocess as sp

    try:
        if action_id == "open_task_manager":
            sp.Popen(["taskmgr"])
            return {"ok": True, "action": "opened_task_manager"}

        elif action_id == "open_disk_cleanup":
            sp.Popen(["cleanmgr"])
            return {"ok": True, "action": "opened_disk_cleanup"}

        elif action_id == "open_event_viewer":
            sp.Popen(["eventvwr"])
            return {"ok": True, "action": "opened_event_viewer"}

        elif action_id == "check_updates":
            sp.Popen(["powershell", "-NoProfile", "-Command",
                       "Start-Process ms-settings:windowsupdate"])
            return {"ok": True, "action": "opened_windows_update"}

        elif action_id == "run_sfc":
            sp.Popen(["powershell", "-NoProfile", "-Command",
                       "Start-Process cmd -ArgumentList '/k sfc /scannow' -Verb RunAs"])
            return {"ok": True, "action": "launched_sfc",
                    "note": "Admin prompt will appear. SFC takes 5-15 minutes."}

        else:
            return {"ok": False, "error": f"Action '{action_id}' must be handled by a specific page."}

    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Action log ──────────────────────────────────────────────────────────────

_LOG_PATH = Path(os.environ.get("LOCALAPPDATA", "")) / "PCHealthAgent" / "data" / "action_log.json"

def log_action(action_id: str, result: dict):
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log = []
        if _LOG_PATH.exists():
            log = json.loads(_LOG_PATH.read_text(encoding="utf-8"))
        log.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action": action_id,
            "result": result,
        })
        # Keep last 200 entries
        log = log[-200:]
        _LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def get_action_log(limit: int = 50) -> list:
    try:
        if _LOG_PATH.exists():
            log = json.loads(_LOG_PATH.read_text(encoding="utf-8"))
            return log[-limit:]
    except Exception:
        pass
    return []
