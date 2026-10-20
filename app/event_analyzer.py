"""PC Health Agent — Windows Event Log analyzer."""
import subprocess
import json
from datetime import datetime, timedelta

# Event severity mapping
LEVEL_MAP = {1: "critical", 2: "error", 3: "warning", 4: "info", 5: "verbose"}
LEVEL_LABEL = {
    1: ("Critical", "重大"), 2: ("Error", "エラー"),
    3: ("Warning", "警告"), 4: ("Information", "情報"), 5: ("Verbose", "詳細"),
}

# Logs to scan
TARGET_LOGS = ["Application", "System"]

# Known error patterns with explanations
KNOWN_PATTERNS = {
    "Application Error": {
        "en": "An application crashed unexpectedly. Check for updates or reinstall.",
        "ja": "アプリケーションが予期せずクラッシュしました。更新や再インストールを確認してください。",
    },
    "Application Hang": {
        "en": "An application stopped responding. May indicate insufficient RAM or a software bug.",
        "ja": "アプリケーションが応答しなくなりました。メモリ不足またはソフトウェアのバグの可能性。",
    },
    ".NET Runtime": {
        "en": "A .NET application encountered an unhandled exception.",
        "ja": ".NETアプリケーションで未処理の例外が発生しました。",
    },
    "Windows Error Reporting": {
        "en": "Windows captured a crash dump for analysis.",
        "ja": "Windowsがクラッシュダンプを記録しました。",
    },
    "Kernel-Power": {
        "en": "Unexpected shutdown or power loss. May indicate hardware, driver, or overheating issue.",
        "ja": "予期しないシャットダウンまたは電源喪失。ハードウェア、ドライバー、過熱の可能性。",
    },
    "BugCheck": {
        "en": "Blue screen (BSOD) occurred. Check drivers and hardware.",
        "ja": "ブルースクリーン(BSOD)が発生しました。ドライバーとハードウェアを確認。",
    },
    "Disk": {
        "en": "A disk error was detected. Back up data and check disk health.",
        "ja": "ディスクエラーが検出されました。データをバックアップしディスクの状態を確認。",
    },
    "Ntfs": {
        "en": "NTFS file system error. Run chkdsk to check for corruption.",
        "ja": "NTFSファイルシステムエラー。chkdskで破損を確認してください。",
    },
    "WHEA-Logger": {
        "en": "Hardware error logged. Possible RAM, CPU, or storage issue.",
        "ja": "ハードウェアエラーが記録されました。RAM、CPU、ストレージの問題の可能性。",
    },
    "Service Control Manager": {
        "en": "A Windows service failed to start or crashed.",
        "ja": "Windowsサービスの起動失敗またはクラッシュ。",
    },
    "EventLog": {
        "en": "The event log service itself encountered an issue.",
        "ja": "イベントログサービス自体に問題が発生しました。",
    },
    "volmgr": {
        "en": "Volume manager error — possible crash dump failure.",
        "ja": "ボリュームマネージャーエラー — クラッシュダンプの失敗の可能性。",
    },
}


def _explain_source(source: str) -> dict | None:
    for pattern, explanation in KNOWN_PATTERNS.items():
        if pattern.lower() in source.lower():
            return explanation
    return None


# ── Query events ────────────────────────────────────────────────────────────

def get_events(hours: int = 24, max_level: int = 3, max_results: int = 100) -> list:
    """Fetch recent Windows events at or below max_level (1=critical..3=warning)."""
    start_time = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")

    ps_cmd = (
        f"$start='{start_time}';"
        f"$logs=@('Application','System');"
        f"$events=@();"
        f"foreach($log in $logs){{"
        f"try{{$events+=Get-WinEvent -FilterHashtable @{{"
        f"LogName=$log;Level=@(1,2,3);StartTime=$start"
        f"}} -MaxEvents 50 -ErrorAction SilentlyContinue}}catch{{}}"
        f"}};"
        f"$events|Sort-Object TimeCreated -Descending|Select-Object -First {max_results}"
        f"|ForEach-Object{{"
        f"$_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss')+'|||'+"
        f"$_.LevelDisplayName+'|||'+"
        f"$_.Level.ToString()+'|||'+"
        f"$_.ProviderName+'|||'+"
        f"$_.Id.ToString()+'|||'+"
        f"$_.LogName+'|||'+"
        f"($_.Message -replace '\\r?\\n',' ').Substring(0,[Math]::Min(300,$_.Message.Length))"
        f"}}"
    )

    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15
        )
        events = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("|||")
            if len(parts) < 7:
                continue
            ts, level_name, level_num, source, event_id, log_name, message = parts
            level = int(level_num) if level_num.isdigit() else 4
            if level > max_level:
                continue
            explanation = _explain_source(source)
            events.append({
                "timestamp": ts.strip(),
                "level": level,
                "level_name": level_name.strip(),
                "level_label_en": LEVEL_LABEL.get(level, ("Unknown", "不明"))[0],
                "level_label_ja": LEVEL_LABEL.get(level, ("Unknown", "不明"))[1],
                "source": source.strip(),
                "event_id": event_id.strip(),
                "log": log_name.strip(),
                "message": message.strip()[:300],
                "explanation_en": explanation["en"] if explanation else "",
                "explanation_ja": explanation["ja"] if explanation else "",
            })
        return events
    except Exception:
        return []


def get_crash_events(hours: int = 168) -> list:
    """Get application crashes and BSODs from the last N hours (default 7 days)."""
    ps_cmd = (
        f"$start=(Get-Date).AddHours(-{hours});"
        "$logs=@("
        "@{LogName='Application';ProviderName='Application Error'},"
        "@{LogName='Application';ProviderName='Application Hang'},"
        "@{LogName='Application';ProviderName='Windows Error Reporting'},"
        "@{LogName='System';ProviderName='Microsoft-Windows-Kernel-Power';Id=41},"
        "@{LogName='System';ProviderName='Microsoft-Windows-WER-SystemErrorReporting'}"
        ");"
        "$events=@();"
        "foreach($f in $logs){"
        "$f['StartTime']=$start;"
        "try{$events+=Get-WinEvent -FilterHashtable $f -MaxEvents 20 "
        "-ErrorAction SilentlyContinue}catch{}}"
        "$events|Sort-Object TimeCreated -Descending|Select-Object -First 30"
        "|ForEach-Object{"
        "$_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss')+'|||'+"
        "$_.ProviderName+'|||'+"
        "$_.Level.ToString()+'|||'+"
        "$_.Id.ToString()+'|||'+"
        "($_.Message -replace '\\r?\\n',' ').Substring(0,[Math]::Min(400,$_.Message.Length))"
        "}"
    )

    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15
        )
        crashes = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("|||")
            if len(parts) < 5:
                continue
            ts, source, level_num, eid, message = parts
            level = int(level_num) if level_num.isdigit() else 2
            explanation = _explain_source(source)
            crashes.append({
                "timestamp": ts.strip(),
                "source": source.strip(),
                "level": level,
                "level_label_en": LEVEL_LABEL.get(level, ("Error", "エラー"))[0],
                "level_label_ja": LEVEL_LABEL.get(level, ("Error", "エラー"))[1],
                "event_id": eid.strip(),
                "message": message.strip()[:400],
                "explanation_en": explanation["en"] if explanation else "",
                "explanation_ja": explanation["ja"] if explanation else "",
            })
        return crashes
    except Exception:
        return []


def summarize_events(events: list) -> dict:
    """Group events by source and severity."""
    by_source = {}
    by_level = {1: 0, 2: 0, 3: 0}
    for e in events:
        src = e["source"]
        by_source.setdefault(src, {"count": 0, "max_level": 4, "latest": ""})
        by_source[src]["count"] += 1
        by_source[src]["max_level"] = min(by_source[src]["max_level"], e["level"])
        if not by_source[src]["latest"] or e["timestamp"] > by_source[src]["latest"]:
            by_source[src]["latest"] = e["timestamp"]
        lvl = e["level"]
        if lvl in by_level:
            by_level[lvl] += 1

    sorted_sources = sorted(by_source.items(),
                            key=lambda x: (x[1]["max_level"], -x[1]["count"]))

    return {
        "total": len(events),
        "critical": by_level[1],
        "errors": by_level[2],
        "warnings": by_level[3],
        "by_source": [
            {"source": src, "count": info["count"],
             "max_level": info["max_level"], "latest": info["latest"],
             "explanation": _explain_source(src)}
            for src, info in sorted_sources[:15]
        ],
    }
