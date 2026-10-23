"""PC Health Agent — entry point."""
import sys, os, webbrowser
from pathlib import Path
from datetime import datetime

BASE     = Path(__file__).parent.parent
REPORTS  = BASE / "reports"
LOGS     = BASE / "logs"
REPORTS.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)

sys.path.insert(0, str(Path(__file__).parent))
from scanner import run_full_scan
from report_gen import generate

def progress(done, total):
    print(f"    {done} / {total} files hashed…", end="\r", flush=True)

def send_toast(title: str, body: str):
    try:
        import run_hidden
        script = (
            f'[Windows.UI.Notifications.ToastNotificationManager,'
            f'Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;'
            f'[Windows.Data.Xml.Dom.XmlDocument,'
            f'Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]|Out-Null;'
            f'$x=[Windows.Data.Xml.Dom.XmlDocument]::new();'
            f'$x.LoadXml(\'<toast><visual><binding template="ToastGeneric">'
            f'<text>{title}</text><text>{body}</text>'
            f'</binding></visual></toast>\');'
            f'$n=[Windows.UI.Notifications.ToastNotification]::new($x);'
            f'[Windows.UI.Notifications.ToastNotificationManager]::'
            f'CreateToastNotifier("PC Health Agent").Show($n)'
        )
        run_hidden.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            timeout=10, capture_output=True
        )
    except Exception:
        pass

def main():
    no_dupes = "--no-dupes" in sys.argv or "-n" in sys.argv
    silent   = "--silent"   in sys.argv or "-s" in sys.argv

    print()
    print("=" * 52)
    print("  PC Health Agent  v0.1")
    print("=" * 52)

    data = run_full_scan(
        include_dupes=not no_dupes,
        progress_cb=progress if not no_dupes else None,
    )
    if not no_dupes:
        print()  # clear progress line

    html = generate(data)

    ts   = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPORTS / f"report-{ts}.html"
    path.write_text(html, encoding="utf-8")

    # Log
    log_line = (
        f"{data['generated_at']} | "
        f"Free={data['disk']['fmt_free']} | "
        f"Temp={data['total_temp_fmt']} | "
        f"Dupes={data['total_dupes_fmt']} | "
        f"Startup={len(data['startup'])} | "
        f"Issues={data['issues']}"
    )
    with open(LOGS / "scan-history.txt", "a", encoding="utf-8") as f:
        f.write(log_line + "\n")

    # Toast
    issue_word = "issue" if data["issues"] == 1 else "issues"
    toast_title = (f"PC Health: {data['issues']} {issue_word} found"
                   if data["issues"] else "PC Health: All clear ✅")
    toast_body  = f"{data['total_freeable_fmt']} reclaimable. Click to open report."
    send_toast(toast_title, toast_body)

    print()
    print("=" * 52)
    print(f"  Issues       : {data['issues']}")
    print(f"  Reclaimable  : {data['total_freeable_fmt']}")
    print(f"  Startup apps : {len(data['startup'])}  ({len(data['personal_apps'])} personal)")
    print(f"  Report       : {path}")
    print("=" * 52)
    print()

    if not silent:
        webbrowser.open(path.as_uri())

if __name__ == "__main__":
    main()
