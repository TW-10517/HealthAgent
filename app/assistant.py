"""PC Health Agent — Natural-language assistant engine.

Intent → action registry. Uses regex for fast matching of common phrases,
falls back to Ollama for free-form queries. Never executes unrestricted commands.
"""
import re
import json

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:3b"

# Intent registry: list of (pattern, action_id, params)
_INTENTS = [
    # Navigation — broad verbs (show me the X, navigate to X, take me to X)
    (r'(go to|navigate to|take me to|show me the|open|show)\s+(the\s+)?(dashboard|overview|home)\b', 'nav', {'page': 'overview'}),
    (r'(go to|navigate to|take me to|open|show)\s+(me\s+)?(the\s+)?(storage|disk)\s*(page|tab|section)?$', 'nav', {'page': 'storage'}),
    (r'(go to|navigate to|take me to|open|show)\s+(the\s+)?(monitor|system monitor|performance)\b', 'nav', {'page': 'monitor'}),
    (r'(go to|navigate to|take me to|open|show)\s+(the\s+)?(startup|startup apps)\b', 'nav', {'page': 'startup'}),
    # insights nav: only explicit navigation verbs — "show insights" alone routes to get_insights action below
    (r'(go to|navigate to|take me to|open)\s+(me\s+)?(the\s+)?(insights)\b', 'nav', {'page': 'insights'}),
    (r'(go to|navigate to|take me to|open|show)\s+(the\s+)?(organiz|file organiz)', 'nav', {'page': 'organize'}),
    (r'(go to|navigate to|take me to|open|show)\s+(the\s+)?(events|event log|windows events)\b', 'nav', {'page': 'events'}),
    (r'(go to|navigate to|take me to|open|show)\s+(the\s+)?(privacy)\b', 'nav', {'page': 'privacy'}),
    (r'(go to|navigate to|take me to|open|show)\s+(the\s+)?(fix|troubleshoot|diagnos)', 'nav', {'page': 'troubleshoot'}),
    (r'(go to|navigate to|take me to|open|show)\s+(the\s+)?(history|scan history)\b', 'nav', {'page': 'history'}),
    (r'(go to|navigate to|take me to|open|show)\s+(the\s+)?(settings|preferences)\b', 'nav', {'page': 'settings'}),
    # assistant nav: require end-of-string or "page" after "ai" so "show ai suggestions" doesn't match
    (r'(go to|navigate to|take me to|open|show)\s+(the\s+)?(assistant|chat)\b', 'nav', {'page': 'assistant'}),
    (r'(go to|navigate to|take me to|open|show)\s+ai\s*(page|tab|assistant)?\s*$', 'nav', {'page': 'assistant'}),
    # Scan control
    (r'(run|start|do|begin|scan)\s+(a\s+)?(full scan|full|scan)$', 'scan', {'no_dupes': False}),
    (r'(run|start|do|begin)\s+(a\s+)?(quick scan|quick|fast scan)', 'scan', {'no_dupes': True}),
    (r'(begin|start)\s+scan(ning)?', 'scan', {'no_dupes': False}),
    (r'^(scan|scanning)\s*(my\s+)?(pc|computer|system)?$', 'scan', {'no_dupes': False}),
    (r'(cancel|stop|abort)\s+(scan|scanning)', 'cancel_scan', {}),
    # Health queries
    (r'(how is|what is|show me)\s+(my\s+)?(pc|computer|system)\s*(health|status|score)?', 'query_health', {}),
    (r'(how much|what is my|check|show me)\s+(free|available|my\s+)?\s*(disk|storage|space)', 'query_disk', {}),
    (r'(how much|what is my|check|show me)\s+(my\s+)?(ram|memory)\s*(usage|used)?', 'query_ram', {}),
    (r'(how much|what is my|check|show me)\s+(my\s+)?(cpu|processor)\s*(usage|load|speed|percent)?', 'query_cpu', {}),
    (r'(how long|since when).{0,25}(running|on|up|boot|started|restarted|restart)', 'query_uptime', {}),
    (r'(uptime|last\s+(boot|restart|reboot))', 'query_uptime', {}),
    (r'(what are|show me|list|what)\s+(the\s+)?(issues?|problems?|findings?)(\s+(you\s+)?(found|detected|discovered))?', 'query_issues', {}),
    (r'(what are|show me|list)\s+(the\s+|my\s+)?(startup|startup apps|startup programs)', 'query_startup', {}),
    # Insights action — must come before nav catch-alls
    (r'(run|start|show|load)\s+(insights|ai\s+suggestions?|suggestions?)', 'get_insights', {}),
    # Diagnose action
    (r'(run|start|show|do)\s+(diagnos|troubleshoot)', 'run_diagnose', {}),
    (r'^troubleshoot(\s+(my|the|this)\s+)?(pc|computer|system|problem|issue)?', 'run_diagnose', {}),
    # Cleanup
    (r'(clean up|cleanup|delete temp|remove temp)\s*(files?)?', 'cleanup_confirm', {}),
    # Window
    (r'(minimize|hide)\s+(the\s+)?(app|window|application)', 'minimize_window', {}),
    # Help
    (r'(what can you do|help|commands|what do you support|what can i ask)', 'help', {}),
    # File open by extension — must be BEFORE sys_search so "find resume.docx" hits open_file
    (r'(find|open)\s+(\S+\.\w{2,5})\s*$', 'open_file', {}),
    # System queries — more specific patterns first
    (r'(find|search\s+for|locate|where\s+is)\s+(.+?)(\s+files?|\s+in\s+.+)?$', 'sys_search', {}),
    (r'(show|list|what).{0,12}(running\s+)?(processes?|tasks?|apps?\s+running)', 'sys_processes', {}),
    (r'(what.{0,12}ip(\s+address)?|show\s+(my\s+)?(ip|network)|network\s+(info|config|status)|ipconfig)', 'sys_network', {}),
    (r'(go to|navigate to|take me to|open|show)\s+(the\s+)?(system\s+info|hardware\s+info|sysinfo|hardware)\s*(page|tab|section)?$', 'nav', {'page': 'sysinfo'}),
    (r'(system\s+info|computer\s+info|show\s+sysinfo|about\s+(my\s+)?(pc|computer|system))', 'sys_sysinfo', {}),
    (r'(recent|recently\s+(modified|created|opened|changed))\s+files?', 'sys_recent', {}),
    (r'(who\s+am\s+i|my\s+username|show\s+env|environment\s+var)', 'sys_env', {}),
    # sys_list: consume optional "me/my" so page-name lookahead fires on the real word
    (r'(list|show|what.s\s+in)\s+(me\s+|my\s+)?(files?\s+in\s+|contents?\s+of\s+)?((?!insights|dashboard|monitor|startup|organiz|events|privacy|troubleshoot|history|settings|assistant|sysinfo|storage).+)', 'sys_list', {}),
    # Explain issue
    (r'(explain|why|what\s+is|tell\s+me\s+about)\s+(issue|problem|finding|error)[\s#]*(\d+)?', 'explain_issue', {}),
    (r'why\s+(is|are|do)\s+(my|the)\s+.*(low|high|slow|full|bad|many|few)', 'explain_issue', {}),
    # Folder open — must come BEFORE app catch-all
    (r'(open|show|go to|launch|take me to)\s+(my\s+)?(documents?|downloads?|desktop|pictures?|videos?|music|onedrive)\s*(folder|directory)?$', 'open_folder', {}),
    # File open by name — file has extension (e.g. "open report.docx") OR explicit "find and open"
    (r'(find\s+and\s+open|open)\s+(\S+\.\w{2,5})\s*$', 'open_file', {}),
    (r'(find\s+and\s+open)\s+(.+)', 'open_file', {}),
    # App close — before catch-all open
    (r'(close|quit|exit|shutdown)\s+(the\s+)?(.+?)(\s+app|\s+application|\s+window)?$', 'close_app', {}),
    # App launcher — catch-all "open / launch / start <app>" (must come last)
    (r'(open|launch|start)\s+(.+)', 'open_app', {}),
]

# ── Known apps the assistant is allowed to launch ─────────────────────────────
# Whitelist only — no arbitrary command execution.
APP_LAUNCH: dict[str, tuple[str, list]] = {
    # alias                 → (display name,            subprocess args)
    "edge":                 ("Microsoft Edge",           ["cmd", "/c", "start", "msedge"]),
    "microsoft edge":       ("Microsoft Edge",           ["cmd", "/c", "start", "msedge"]),
    "chrome":               ("Google Chrome",            ["cmd", "/c", "start", "chrome"]),
    "google chrome":        ("Google Chrome",            ["cmd", "/c", "start", "chrome"]),
    "firefox":              ("Firefox",                  ["cmd", "/c", "start", "firefox"]),
    "vscode":               ("VS Code",                  ["code"]),
    "vs code":              ("VS Code",                  ["code"]),
    "visual studio code":   ("VS Code",                  ["code"]),
    "notepad":              ("Notepad",                  ["notepad.exe"]),
    "notepad++":            ("Notepad++",                ["notepad++"]),
    "calculator":           ("Calculator",               ["calc.exe"]),
    "calc":                 ("Calculator",               ["calc.exe"]),
    "explorer":             ("File Explorer",            ["explorer.exe"]),
    "file explorer":        ("File Explorer",            ["explorer.exe"]),
    "task manager":         ("Task Manager",             ["taskmgr.exe"]),
    "taskmgr":              ("Task Manager",             ["taskmgr.exe"]),
    "cmd":                  ("Command Prompt",           ["cmd.exe"]),
    "command prompt":       ("Command Prompt",           ["cmd.exe"]),
    "powershell":           ("PowerShell",               ["powershell.exe"]),
    "paint":                ("Paint",                    ["mspaint.exe"]),
    "snipping tool":        ("Snipping Tool",            ["snippingtool.exe"]),
    "teams":                ("Microsoft Teams",          ["cmd", "/c", "start", "msteams:"]),
    "microsoft teams":      ("Microsoft Teams",          ["cmd", "/c", "start", "msteams:"]),
    "outlook":              ("Outlook",                  ["cmd", "/c", "start", "outlook:"]),
    "word":                 ("Microsoft Word",           ["cmd", "/c", "start", "winword"]),
    "microsoft word":       ("Microsoft Word",           ["cmd", "/c", "start", "winword"]),
    "excel":                ("Microsoft Excel",          ["cmd", "/c", "start", "excel"]),
    "microsoft excel":      ("Microsoft Excel",          ["cmd", "/c", "start", "excel"]),
    "powerpoint":           ("PowerPoint",               ["cmd", "/c", "start", "powerpnt"]),
    "settings":             ("Windows Settings",         ["cmd", "/c", "start", "ms-settings:"]),
    "windows settings":     ("Windows Settings",         ["cmd", "/c", "start", "ms-settings:"]),
    "spotify":              ("Spotify",                  ["cmd", "/c", "start", "spotify:"]),
    "slack":                ("Slack",                    ["cmd", "/c", "start", "slack:"]),
    "discord":              ("Discord",                  ["cmd", "/c", "start", "discord:"]),
    "zoom":                 ("Zoom",                     ["cmd", "/c", "start", "zoommtg:"]),
    "steam":                ("Steam",                    ["cmd", "/c", "start", "steam:"]),
    "notion":               ("Notion",                   ["cmd", "/c", "start", "notion:"]),
    "figma":                ("Figma",                    ["cmd", "/c", "start", "figma:"]),
    "terminal":             ("Windows Terminal",         ["wt.exe"]),
    "windows terminal":     ("Windows Terminal",         ["wt.exe"]),
    "snip":                 ("Snipping Tool",            ["snippingtool.exe"]),
    "clock":                ("Clock",                    ["cmd", "/c", "start", "ms-clock:"]),
    "maps":                 ("Maps",                     ["cmd", "/c", "start", "bingmaps:"]),
    "weather":              ("Weather",                  ["cmd", "/c", "start", "msnweather:"]),
    "store":                ("Microsoft Store",          ["cmd", "/c", "start", "ms-windows-store:"]),
    "microsoft store":      ("Microsoft Store",          ["cmd", "/c", "start", "ms-windows-store:"]),
}


def extract_app_name(text: str) -> str:
    """Extract the app name from an 'open X' request."""
    m = re.search(r'(?:open|launch|start)\s+(.+)', text.lower().strip())
    return m.group(1).strip() if m else text.strip()


def extract_close_target(text: str) -> str:
    """Extract the app name from a 'close/quit X' request."""
    m = re.search(
        r'(?:close|quit|exit|shutdown)\s+(?:the\s+)?(.+?)(?:\s+app|\s+application|\s+window)?$',
        text.lower().strip(),
    )
    return m.group(1).strip() if m else text.strip()


def resolve_app(app_name: str):
    """Return (display_name, args) from the whitelist, or (None, None) if unknown."""
    key = app_name.lower().strip()
    if key in APP_LAUNCH:
        return APP_LAUNCH[key]
    # Partial match only when key is at least 4 chars (prevents "rm" matching "terminal")
    if len(key) >= 4:
        for alias, spec in APP_LAUNCH.items():
            if alias in key or key in alias:
                return spec
    return None, None


def find_and_launch(app_name: str) -> tuple[str, bool]:
    """Find any installed app by name and launch it. Returns (message, success)."""
    import os, glob, shutil, subprocess

    name_raw   = app_name.strip()
    name_lower = name_raw.lower()

    # 1. Whitelist fast-path
    label, args = resolve_app(name_lower)
    if label and args:
        try:
            subprocess.Popen(args, shell=False,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Opening {label}…", True
        except FileNotFoundError:
            pass   # fall through to dynamic search
        except Exception as e:
            return f"Could not open {label}: {e}", False

    # 2. PATH search (handles apps like `code`, `git`, etc.)
    candidates = [name_raw, name_raw + ".exe",
                  name_lower.replace(" ", ""), name_lower.replace(" ", "") + ".exe"]
    for c in candidates:
        found = shutil.which(c)
        if found:
            try:
                subprocess.Popen([found],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return f"Opening {name_raw}…", True
            except Exception:
                pass

    # 3. Start Menu .lnk search (covers virtually all installed apps)
    start_menus = [
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
    ]
    best_lnk   = None
    best_score = 0
    words      = [w for w in name_lower.split() if len(w) > 2]

    for base in start_menus:
        for lnk in glob.glob(os.path.join(base, "**", "*.lnk"), recursive=True):
            lnk_stem = os.path.splitext(os.path.basename(lnk))[0].lower()
            if name_lower == lnk_stem:
                score = 100
            elif name_lower in lnk_stem:
                score = 85 - len(lnk_stem)          # prefer shorter match
            elif lnk_stem in name_lower:
                score = 75
            elif words and all(w in lnk_stem for w in words):
                score = 65
            elif words and any(w in lnk_stem for w in words):
                score = 40
            else:
                continue
            if score > best_score:
                best_score = score
                best_lnk   = lnk

    if best_lnk:
        try:
            os.startfile(best_lnk)
            display = os.path.splitext(os.path.basename(best_lnk))[0]
            return f"Opening {display}…", True
        except Exception as e:
            return f"Found {name_raw} but couldn't open it: {e}", False

    # 4. Last resort: ask Windows shell to resolve the name
    words_count = len(name_lower.split())
    if words_count <= 3:
        try:
            subprocess.Popen(["cmd", "/c", "start", "", name_raw],
                             shell=False,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Trying to open {name_raw}…", True
        except Exception:
            pass

    return f"I couldn't find **{name_raw}** on this PC. Try saying the exact app name.", False


def match_intent(text: str):
    """Return (action_id, params) or (None, None)."""
    t = text.lower().strip()
    for pattern, action_id, params in _INTENTS:
        if re.search(pattern, t):
            return action_id, dict(params)
    return None, None


def ollama_chat(message: str, context: str = "", lang: str = "en",
                history: list | None = None) -> str | None:
    """Ask Ollama about PC health. Returns response string or None if unavailable."""
    import urllib.request
    import urllib.error

    # Load persistent app/machine knowledge cache
    try:
        import agent_cache
        kb = agent_cache.load_cache()
    except Exception:
        kb = ""

    system = (
        "You are PC Health Agent's built-in assistant. "
        "Answer concisely in 2-3 sentences. "
        "Only answer questions about this app, PC health, storage, performance, "
        "startup apps, events, and diagnostics. "
        "Politely redirect if asked about unrelated topics.\n\n"
        "KNOWLEDGE BASE:\n" + kb
    )
    if context:
        system += f"\n\nCURRENT STATE: {context}"

    # Build multi-turn conversation context
    turns = ""
    if history:
        for h in history[-8:]:   # last 4 exchanges
            role = "User" if h.get("role") == "user" else "Assistant"
            turns += f"\n{role}: {h.get('content', '')}"
    prompt = f"{system}{turns}\n\nUser: {message}\nAssistant:"

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.4, "num_predict": 150},
    }).encode()

    try:
        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())["response"].strip()
    except Exception:
        return None
