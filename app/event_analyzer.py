"""PC Health Agent — Windows Event Log analyzer."""
import run_hidden
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

# Known error patterns with plain-language explanations.
# Keys matched case-insensitive as substrings against the event source.
KNOWN_PATTERNS = {
    "Application Error": {
        "en": "An app crashed. If it keeps happening, update or reinstall that app.",
        "ja": "アプリがクラッシュしました。頻発する場合は更新または再インストール。",
    },
    "Application Hang": {
        "en": "An app froze and stopped responding. Often caused by low RAM or a software bug.",
        "ja": "アプリが応答しなくなりました。メモリ不足かバグの可能性。",
    },
    ".NET Runtime": {
        "en": "A .NET-based app ran into an internal error. Usually safe — the app may have reopened.",
        "ja": ".NETアプリで内部エラー。多くの場合、アプリが自動復帰します。",
    },
    "Windows Error Reporting": {
        "en": "Windows saved a crash report to help diagnose a problem. No action needed from you.",
        "ja": "Windowsがクラッシュレポートを保存しました。特に対応は不要です。",
    },
    "Kernel-Power": {
        "en": "Your PC lost power or shut down unexpectedly (not a clean shutdown).",
        "ja": "PCが突然電源喪失または強制シャットダウンしました。",
    },
    "BugCheck": {
        "en": "A blue screen (BSOD) happened. Likely a driver or hardware issue.",
        "ja": "ブルースクリーン(BSOD)が発生。ドライバーかハードウェアの問題。",
    },
    "Disk": {
        "en": "The disk reported an error. Back up important files and check drive health.",
        "ja": "ディスクエラー。重要なファイルをバックアップし健康状態を確認。",
    },
    "Ntfs": {
        "en": "A file-system problem was detected. Running chkdsk can repair it.",
        "ja": "ファイルシステムの問題。chkdskで修復可能。",
    },
    "WHEA-Logger": {
        "en": "The CPU or memory reported a hardware fault. Watch for a pattern.",
        "ja": "CPUやメモリのハードウェア障害が記録されました。頻発に注意。",
    },
    "Service Control Manager": {
        "en": "A Windows background service failed to start or stopped. Usually recovers on its own.",
        "ja": "Windowsサービスの起動失敗。多くの場合は自動復旧します。",
    },
    "EventLog": {
        "en": "The event-log service itself had a hiccup. Rarely a real problem.",
        "ja": "イベントログサービス自体に問題。通常は問題ありません。",
    },
    "volmgr": {
        "en": "The disk-volume manager had trouble writing crash data. Not urgent.",
        "ja": "ボリュームマネージャーの問題。緊急ではありません。",
    },
    "DistributedCOM": {
        "en": "Two Windows components couldn't talk to each other. Usually harmless and self-recovers.",
        "ja": "Windows内部コンポーネントの通信エラー。通常は自動復旧。",
    },
    "DCOM": {
        "en": "Windows internal component couldn't start. Usually harmless.",
        "ja": "Windows内部コンポーネントの起動失敗。通常は無害。",
    },
    "Perflib": {
        "en": "A performance-counter plugin failed to load. Doesn't affect how your PC works.",
        "ja": "パフォーマンスカウンタの読み込み失敗。動作には影響しません。",
    },
    "SideBySide": {
        "en": "An app is missing a shared Windows component. Reinstalling that app usually fixes it.",
        "ja": "アプリに必要なWindowsコンポーネントが不足。再インストールで解決。",
    },
    "WLAN": {
        "en": "A Wi-Fi connection event (connected, disconnected, or lost signal).",
        "ja": "Wi-Fi接続のイベント（接続・切断・信号喪失）。",
    },
    "Dhcp": {
        "en": "The PC had trouble getting an IP address from your router.",
        "ja": "ルーターからのIPアドレス取得に問題。",
    },
    "TCPIP": {
        "en": "A networking issue — a dropped connection or routing problem.",
        "ja": "ネットワークの問題 — 接続切断またはルーティングの問題。",
    },
    "StorPort": {
        "en": "The storage driver reported a delay or timeout. If frequent, your drive may be failing.",
        "ja": "ストレージドライバの遅延。頻発するならドライブ劣化の可能性。",
    },
    "disk": {
        "en": "The disk reported a problem. Back up important files.",
        "ja": "ディスクで問題。重要なファイルをバックアップ。",
    },
    "SearchIndexer": {
        "en": "Windows Search had trouble indexing a file. Search still works.",
        "ja": "Windowsサーチのインデックス処理に問題。検索機能は使えます。",
    },
    "WinInit": {
        "en": "Startup component reported an issue. Only matters if your PC won't boot.",
        "ja": "起動時のコンポーネントの問題。起動に支障がなければ無視可能。",
    },
    "Wininit": {
        "en": "Startup component reported an issue. Only matters if your PC won't boot.",
        "ja": "起動時のコンポーネントの問題。起動に支障がなければ無視可能。",
    },
    "WudfHost": {
        "en": "A device driver's user-mode framework hiccuped. Usually self-recovers.",
        "ja": "デバイスドライバの一部でエラー。通常は自動復旧。",
    },
    "Microsoft-Windows-Kernel": {
        "en": "A low-level Windows component logged an issue. Watch for a pattern.",
        "ja": "Windowsカーネルのイベント。頻発に注意。",
    },
    "VSS": {
        "en": "Backup/restore (Volume Shadow Copy) had trouble. Try the operation again.",
        "ja": "バックアップ(ボリュームシャドウコピー)に問題。再試行してください。",
    },
    "Schannel": {
        "en": "An encrypted connection (HTTPS/TLS) failed. Often a certificate or time-sync issue.",
        "ja": "暗号化接続(TLS)の失敗。証明書か時刻同期の問題。",
    },
    "Microsoft-Windows-Time-Service": {
        "en": "The PC's clock couldn't sync with the internet time server.",
        "ja": "PCの時刻をインターネットと同期できません。",
    },
    "Winlogon": {
        "en": "A sign-in or sign-out event ran into trouble.",
        "ja": "サインインまたはサインアウトで問題発生。",
    },
    "User Profile Service": {
        "en": "Loading or saving your user profile hit a snag.",
        "ja": "ユーザープロファイルの読み書きに問題。",
    },
    "SecurityCenter": {
        "en": "Windows Security Center logged a change in security settings.",
        "ja": "Windowsセキュリティセンターで設定変更が記録されました。",
    },
    "Microsoft-Windows-WindowsUpdateClient": {
        "en": "Windows Update event — installation, download, or error.",
        "ja": "Windows Update のイベント（インストール・ダウンロード・エラー）。",
    },
}


def _explain_source(source: str) -> dict | None:
    """Return a plain-language explanation. Always returns something — if no
    exact pattern matches, falls back to a severity/source heuristic so the
    user never sees a raw unexplained row."""
    s_low = (source or "").lower()
    for pattern, explanation in KNOWN_PATTERNS.items():
        if pattern.lower() in s_low:
            return explanation
    # ── Heuristic fallbacks by substring ─────────────────────────────────
    if "driver" in s_low:
        return {"en": "A device driver reported a problem. Only act if a device isn't working.",
                "ja": "デバイスドライバの問題。実際に機器が動かない場合のみ対応。"}
    if "service" in s_low:
        return {"en": "A Windows background service had a problem. Usually recovers on its own.",
                "ja": "Windowsバックグラウンドサービスの問題。通常は自動復旧。"}
    if "network" in s_low or "net" in s_low or "wlan" in s_low or "dhcp" in s_low:
        return {"en": "A networking event — connection, disconnect, or routing hiccup.",
                "ja": "ネットワーク関連のイベント。"}
    if "security" in s_low or "auth" in s_low:
        return {"en": "A security or sign-in event was recorded. Not necessarily an attack.",
                "ja": "セキュリティまたはサインインのイベント。攻撃とは限りません。"}
    if "update" in s_low:
        return {"en": "Windows Update activity was logged.",
                "ja": "Windows Update の活動が記録されました。"}
    if "install" in s_low or "setup" in s_low or "msi" in s_low:
        return {"en": "A software install or uninstall event.",
                "ja": "ソフトウェアのインストール/アンインストールのイベント。"}
    if "power" in s_low or "battery" in s_low:
        return {"en": "A power or battery event — sleep, wake, or power loss.",
                "ja": "電源またはバッテリーのイベント（スリープ・復帰・電源喪失）。"}
    if "kernel" in s_low:
        return {"en": "A low-level Windows event. Only concerning if it repeats.",
                "ja": "Windowsカーネルのイベント。頻発する場合のみ注意。"}
    # Generic fallback — strip provider prefixes for readability
    short = source.replace("Microsoft-Windows-", "").replace("-", " ")
    return {
        "en": f"System component '{short}' logged an event. Expand for the raw message.",
        "ja": f"システムコンポーネント「{short}」のイベント。詳細を展開してください。",
    }


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
        r = run_hidden.run(
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
        r = run_hidden.run(
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
