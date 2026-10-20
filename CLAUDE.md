# PC Health Agent

Local-first Windows 11 desktop utility for PC health scanning and safe maintenance.
Built with Python + pywebview (Edge WebView2). No cloud APIs, no telemetry.

**Project root**: `C:\Users\10517\Documents\Code\PCHealthAgent`

## Architecture

```
PCHealthAgent/
├── app/
│   ├── desktop_app.py    # Main app: AgentAPI class + embedded SPA (~5800+ lines)
│   ├── assistant.py      # NL assistant engine: intent registry + Ollama fallback
│   ├── sys_executor.py   # Read-only system query engine (files, processes, network, sysinfo)
│   ├── action_planner.py # Multi-step chat: split_steps(), build_plan(), is_multi_step()
│   ├── agent_cache.py    # Persistent Ollama context cache → data/agent_context.txt
│   ├── chat_popup.py     # Standalone chat popup window (ChatPopupAPI + HTML, bilingual)
│   ├── launcher.py       # Win32 floating segment-wheel radial menu (6 segments)
│   ├── main.py           # CLI entry point (standalone scan → HTML report)
│   ├── scanner.py        # Disk scan: temp files, duplicates, startup apps, disk usage
│   ├── suggestions.py    # AI-powered suggestions engine (local Ollama qwen2.5:3b)
│   ├── report_gen.py     # HTML report generator
│   ├── organizer.py      # File organizer: scan + move files into category subfolders
│   ├── monitor.py        # Real-time system monitor (CPU/RAM/Disk/Battery/Temp)
│   ├── event_analyzer.py # Windows Event Log analyzer (Get-WinEvent queries)
│   ├── troubleshooter.py # Rule-based diagnostics engine with safe actions
│   └── db.py             # SQLite storage (scan history, settings)
├── data/
│   ├── pchealthagent.db  # SQLite database (in %LOCALAPPDATA%\PCHealthAgent\data\)
│   └── agent_context.txt # Ollama knowledge cache (rebuilt on app start + post-scan)
├── reports/              # Generated HTML scan reports
├── logs/
└── app_icon.ico
```

> **Note**: `pchealthagent.db` is stored at `%LOCALAPPDATA%\PCHealthAgent\data\pchealthagent.db`
> (not in the project folder) — set by `DB_DIR` in `db.py`.

## Design system (v2 — October 2026 redesign)

| Token | Value | Use |
|---|---|---|
| `--bg` | `#0B0F17` | App background |
| `--bg2` / `--nav-bg` | `#0E131D` | Titlebar, sidebar |
| `--surface` | `#121826` | Cards |
| `--surface2` | `#182133` | Active nav, nested cards |
| `--border` | `#1E2738` | Default borders |
| `--border2` | `#2A3550` | Emphasis borders |
| `--brand` | `#2DD4B0` | CTA buttons, active icons |
| `--fg` | `#E6EBF3` | Primary text |
| `--fg2` | `#A3AEC2` | Secondary text |
| `--fg3` / `--fg4` | `#8E9AAF` / `#6F7B90` | Muted text |
| `--warn` | `#F5B544` | Warnings |
| `--error` | `#FF9C97` | Errors, badges |
| `--info` | `#7DB3FF` | Info, memory metric |
| `--font` | IBM Plex Sans JP + Segoe UI Variable Text | All text |

**No** glass blur, glows, gradient nav highlights, or coloured top card borders.

## Key patterns

- **SPA in pywebview**: All HTML/CSS/JS is embedded in `desktop_app.py` as `APP_HTML` string. Python↔JS bridge via `class AgentAPI` exposed as `window.pywebview.api`.
- **Page navigation**: `showPage(name)` shows/hides `.page` divs. Nav items use `data-page` attributes. Wire new pages in both `showPage()` and as a nav button + page div.
- **Bilingual**: All SPA text uses `<span class="en">…</span><span class="ja">…</span>` pattern. `setLang()` toggles via `data-lang` on `<html>`. chat_popup.py uses a JS `_T = {en:{…}, ja:{…}}` dict instead (separate window, no DOM spans).
- **Theme**: Flat dark design, teal brand `#2DD4B0`. No glass blur or glows. Light mode via `:root[data-theme="light"]` overrides. Toggle in Settings page, persisted in `localStorage('pcha_theme')`.
- **Metrics polling**: Monitor page polls `get_monitor_data()` every 3s. Background `monitor.py` thread collects samples into `deque(maxlen=60)`.
- **Scan**: Daemon thread (`_do_scan`). Progress overlay inside `#page-overview`. Statusbar shows scan% on all pages. `data["score"]` is always written back into `_last_scan_data` after scan.
- **Assistant**: `chat()` intent registry → Ollama async fallback. Chat history (last 20) passed to Ollama for context. Multi-step detection via `action_planner.py`.
- **Analysis panels**: Each data page has `<div id="analysis-[page]">` rendered by `render*Analysis()` JS functions from live data. No extra backend needed.
- **Health watch**: Daemon thread `_start_health_watch()` checks psutil every 15 min; fires Windows notification if disk < threshold or RAM > threshold.
- **Ollama status**: `_check_ollama_status()` daemon pings Ollama 3s after startup; result exposed via `get_ai_status()`. Status pill on Assistant page turns green/red.
- **Subprocess encoding**: All `subprocess.run()` calls use `encoding='utf-8', errors='replace'` to prevent `UnicodeDecodeError` on Japanese Windows (cp932 default).

## Security constraints (MUST follow)

- **PROTECTED paths** (never delete/modify): OneDrive, `C:\Windows`, `C:\Program Files`, `C:\Program Files (x86)`
- **IT-managed tools** (never touch): ESET Security, OPTIMA Eye, Acall Desktop
- **IT_PATTERN**: `{"eset","optima","acall","onedrive","teams","microsoftedge","securityhealth","windowsdefender"}`
- **`_CLOSE_BLOCKED`** frozenset in `desktop_app.py` includes: `"eset"`, `"optima"`, `"acall"`, `"onedrive"`, `"microsoftedge"`, `"msedge"`, `"teams"`, `"securityhealth"`, `"windowsdefender"`
- **PROHIBITED_ACTIONS**: `{"modify_registry","disable_firewall","disable_antivirus","delete_system_files","alter_drivers","terminate_critical_process","reset_windows","execute_arbitrary_command"}`
- **Organizer NEVER deletes files** — only moves into subfolders; writes `.organize-log.json` in the target folder after every move
- **All data stays local** — no cloud APIs, telemetry, analytics, or external uploads
- **Never invent** metrics, errors, diagnoses, or test results
- **Assistant**: never executes shell commands; all actions go through the typed `chat()` handler registry
- **`resolve_app()`**: partial-match only when query ≥ 4 chars (prevents short strings like "rm" matching "terminal")
- **sys_executor.py**: read-only; scope locked to user home + OneDrive; no shell=True; 8s timeout; results capped at 25
- **open_path()**: blocks executable extensions (.exe, .bat, .cmd, .ps1, .vbs, .js, .wsf, .jar, .msi, .scr, .pif, .hta, .cpl, .inf)

## Running

```powershell
cd "C:\Users\10517\Documents\Code\PCHealthAgent\app"
python desktop_app.py     # GUI app (also run: ollama serve for AI features)
python main.py             # CLI scan only
```

Restart shortcut:
```powershell
Stop-Process -Name python -Force -ErrorAction SilentlyContinue
Start-Sleep 1
Start-Process python -ArgumentList "desktop_app.py" -WorkingDirectory "C:\Users\10517\Documents\Code\PCHealthAgent\app" -WindowStyle Hidden
```

## Editing tips

- `desktop_app.py` is ~5800+ lines. Use `Grep` with `head_limit` to find sections. Never read the whole file.
- CSS: search `<style>` — starts around line **1834** (preceded by Google Fonts `<link>`).
- JS: search `<script>` — starts after all page HTML (~line 3600+).
- Page HTML blocks: marked with `<!-- ══ PAGE: Name ══` comments.
- API methods: `class AgentAPI` at line 114.
- Adding a new page: nav button + `page div` + JS function + wire in `showPage()`.
- **Sidebar live widget**: IDs `lp-cpu-bar/v`, `lp-ram-bar/v`, `lp-disk-bar/v` — updated every 3s by `updateLivePills()` called from the monitor poll.
- **Merged pages**: Performance and Problems use `switchTab(group,panel,btn)` + JS init functions that mirror old page content into tab panels at first visit.
- After any PowerShell file write: check for BOM (`EF BB BF` first 3 bytes).

## Pages

### Active nav (sidebar shows 6 + Assistant + Settings)

| Nav label   | data-page   | Description |
|-------------|-------------|-------------|
| Overview    | overview    | Health score ring, "Needs attention" issue list, key stat tiles, score trend |
| Performance | performance | **Tabs**: Live (CPU/RAM/Disk/Battery + chart + top processes) · Startup apps · System info |
| Storage     | storage     | **Tabs**: Files (disk usage, temp, dupes, recycle bin) · Organize (file organizer) |
| Security    | privacy     | Browser cache, network connections, sensitive file radar (data-page stays "privacy") |
| Problems    | problems    | **Tabs**: Fix (diagnostics + safe actions) · Event log (Windows events) |
| History     | history     | Scan history + health score trend chart |
| Ask assistant | assistant | NL chat (intent registry → Ollama fallback) |
| Settings    | settings    | Language, theme, tray, notifications, launcher, health watch |

### Legacy pages (still in DOM, not in sidebar nav)
`monitor`, `startup`, `insights`, `organize`, `events`, `troubleshoot`, `sysinfo` — content mirrored into Performance/Problems tabs at first visit via `_initPerfPage()` and `_initProblemsPage()`.

## Keyboard shortcuts (global)

| Key | Action |
|-----|--------|
| `Ctrl+K` | Command palette (search pages + actions) |
| `?` | Keyboard shortcuts overlay |
| `Ctrl+Shift+S` | Full scan |
| `Ctrl+Shift+Q` | Quick scan |
| `Ctrl+Shift+T` | Toggle light/dark theme |
| `Alt+1–9` | Navigate to page |
| `Esc` | Close any open overlay |

## API methods (AgentAPI)

| Method | Description |
|--------|-------------|
| `start_scan(no_dupes)` | Start full/quick scan in daemon thread |
| `cancel_scan()` | Signal scan cancellation |
| `get_scan_progress()` | Polled by JS every 300ms — scanning%, done, total |
| `chat(message, lang)` | NL assistant: intent → Ollama fallback; multi-step via action_planner |
| `get_suggestions()` | Run all proactive checks |
| `get_ai_summary(suggestions, lang)` | Ollama summary of suggestions |
| `get_ai_status()` | Returns `{status: "online"\|"offline"\|"checking"}` — Ollama ping result (pill starts in `checking` state) |
| `get_monitor_data()` | Live CPU/RAM/Disk/Battery/Temp snapshot |
| `get_events(hours, max_level)` | Windows Event Log query |
| `run_troubleshoot()` | Rule-based diagnostics |
| `execute_action(action_id)` | Run a safe troubleshoot action |
| `scan_organize(folder)` | Scan any folder for organization candidates |
| `apply_organize(folder, category, file_paths)` | Move files to category subfolder; appends to `.organize-log.json` |
| `get_organize_history(folder)` | Read `.organize-log.json` — returns `{sessions: [...]}` newest-first |
| `get_known_folders()` | Returns `{downloads, documents, desktop, pictures}` real paths |
| `get_settings()` / `save_settings(dict)` | App settings CRUD |
| `get_hardware_info()` | WMI: CPU/RAM/GPU/Disk/OS/Machine dict (cached in `_hw_cache`) |
| `get_recycle_bin()` | SHQueryRecycleBinW → count + size |
| `empty_recycle_bin()` | SHEmptyRecycleBinW (no dialog) |
| `get_large_files(min_mb)` | Files > N MB in user space |
| `get_old_files(days)` | Files not modified in N days |
| `load_privacy()` | Concurrent: browser cache + network connections + sensitive files |
| `close_app(name)` | EnumWindows PostMessage WM_CLOSE (IT tools blocked via `_CLOSE_BLOCKED`) |
| `confirm_action(token)` | Execute a queued confirmation action |
| `set_launcher_enabled(bool)` | Enable/disable floating launcher |
| `toggle_launcher_visibility()` | Show/hide launcher without disabling |
| `reset_launcher_position()` | Move launcher to default corner |
| `browse_folder()` | Opens native folder picker dialog |

## ChatPopupAPI methods (chat_popup.py)

| Method | Description |
|--------|-------------|
| `chat(message, lang)` | Delegates to AgentAPI.chat() |
| `navigate_main(page)` | Shows main window and navigates to page |
| `get_popup_settings()` | Returns `{lang}` from DB — used by popup JS on load |
| `close_popup()` | Hides popup window |

## Settings keys (saved to DB via save_settings)

| Key | Values | Description |
|-----|--------|-------------|
| `language` | `en` / `ja` | Display language |
| `theme` | `dark` / `light` | UI theme (also stored in localStorage) |
| `tray_on_close` | `true`/`false` | Minimize to tray on window close |
| `show_notifications` | `true`/`false` | Post-scan balloon notifications |
| `history_retention_days` | `30`–`365` | Days to keep scan records |
| `scan_on_startup` | `true`/`false` | Auto quick-scan 2s after app launch (wired to Settings toggle + loadSettings trigger) |
| `health_watch_enabled` | `true`/`false` | Background disk/RAM alert daemon |
| `health_watch_disk_gb` | `3`/`5`/`10`/`20` | Free disk GB threshold for alert |
| `health_watch_ram_pct` | `85`/`90`/`92`/`95` | RAM % threshold for alert |

## Analysis panels

Every major data page has an analysis panel injected into `<div id="analysis-[page]">` after data loads. Shared builder: `_apHTML(id, title, status, items[], rec)`. Status values: `good` / `warn` / `crit`. Panels are pure client-side JS from existing data — no new backend required.

Pages with analysis: `overview`, `storage`, `startup`, `monitor`, `events`, `privacy`.

## Organize page

The Organize page supports **any folder** (not just Downloads):
- **Quick Folders row**: Downloads, Documents, Desktop, Pictures — one-click buttons that call `get_known_folders()` then scan
- **Browse button**: native folder picker for custom paths
- **Move History section**: reads `.organize-log.json` via `get_organize_history()`. Shows sessions (newest first) with per-file `filename → Category/` arrow visualization, color-coded by category, collapsible, with "Open file" and "Open folder" buttons.
- `orgLoadHistory()` is called after every `orgScan()` and `orgApply()`.

## Assistant capabilities (chat() intents)

| What you say | Action |
|---|---|
| `go to storage` / `show me the storage page` | Navigate to page |
| `run full scan` / `begin scanning` | Start scan |
| `check my RAM` / `show me disk space` | RAM / disk query |
| `check my CPU` / `what is my CPU usage` | Live CPU % + frequency via psutil |
| `uptime` / `how long has my PC been running` | Uptime + last boot time |
| `show hardware info` / `go to system info` | Navigate to sysinfo page |
| `what are my startup programs` | Startup list |
| `how is my pc` | Health query |
| `find resume.docx` | Open file by name+extension |
| `find resume` | File search (home + OneDrive) |
| `list my desktop` | Folder contents |
| `open edge` / `open vscode` | App launcher (whitelist + Start Menu) |
| `close notepad` | Close app window (IT tools blocked) |
| `show running processes` | Read-only tasklist |
| `what is my ip` | ipconfig IPv4 |
| `system info` | OS/RAM/CPU summary |
| `explain issue 2` | Ollama explanation with context |
| `find a PDF then open it` | Multi-step plan execution |
| `run a scan and show results` | Multi-step (split on "and <action-verb>") |

## action_planner.py

`_STEP_SEP` splits on:
- `, then` / `; then` / `and then` variants
- `\s+and\s+(?=action-verb)` — splits "X and show/run/check/list/find/open Y" (lookahead prevents splitting inside single phrases like "find and open")

`is_multi_step(msg)` → True if split produces ≥ 2 parts each containing an action verb.

## Floating Launcher (launcher.py)

- Win32 layered window, 6 segments (60° each): Open App, Quick Chat, Run Scan, Monitor HUD, Hide, Exit
- `ICON_SIZE=56`, `ORBIT_RADIUS=62`, `SAT_RADIUS=16`
- Chat satellite opens standalone chat popup window (bilingual)
- Hides when main window closes (tray_on_close), shows when window reopens

## Onboarding

First-launch 4-step modal (guarded by `localStorage('pcha_onboarded')`). Steps: welcome → first scan → assistant → shortcuts. `_initOnboarding()` called 1.2s after `pywebviewready`. `finishOnboarding()` sets the flag.

## Command palette

`openCmdPalette()` — `Ctrl+K`. Fuzzy-filters `_CMD_ITEMS` array (18 entries: pages + actions). Keyboard-navigable (`↑↓ Enter Esc`). `_cmdExec(idx)` closes palette and fires the action. Placeholder text is bilingual (updated by `setLang()`).

## System Info page (sysinfo)

`loadSysInfo()` → `get_hardware_info()` → PowerShell WMI → `_renderHW(d)`. Cached in `_hw_cache` after first load. Refresh button sets `force=true` to re-query. Renders: CPU card, RAM card, GPU cards, Disk cards, OS + Machine cards via `_hwCard(accentColor, label, name, specs[], badge)`.

## Notifications

`_show_notification(title, message)` — PowerShell `NotifyIcon` balloon tip. Called:
1. After scan (if `show_notifications = "true"`)
2. By health watch daemon (if thresholds crossed)

## Known limitations

- Temperature sensor availability depends on hardware/drivers (card hidden when unavailable)
- Ollama must be running for AI summaries and free-form Assistant queries (`ollama serve`)
- Ollama status pill reflects state 4s after app start (async check)
- Windows notifications flash a brief tray icon (PowerShell NotifyIcon approach)
- `sys_executor.py` file search covers home + OneDrive only
- `get_hardware_info()` WMI call takes ~5s on first load (cached after that)

## Dependencies

- Python 3.12, pywebview, pystray, Pillow, **psutil** (required — health watch + HUD)
- PowerShell (CIM/WMI for hardware, event logs, notifications)
- Ollama at 127.0.0.1:11434 with qwen2.5:3b (optional — AI summaries + Assistant fallback)
