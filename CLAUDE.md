# Helix (PC Health Agent)

Local-first Windows 11 desktop utility for PC health scanning and safe maintenance.
Built with Python + pywebview (Edge WebView2). No cloud APIs, no telemetry.

Product name is **Helix** (window title, exe, "Orbit Pulse" logo via `_create_shield_icon`).
The repo/package id remains `PCHealthAgent`.

**Project root**: `C:\Users\10517\Documents\Code\PCHealthAgent`
**GitHub**: `https://github.com/TW-10517/HealthAgent.git` (branch `main`)

## Architecture

```
PCHealthAgent/
├── app/
│   ├── desktop_app.py    # Main app: AgentAPI class + SPA loader (~2800 lines)
│   ├── web/
│   │   └── index.html    # Full SPA: HTML + inline <style> + inline <script> (~4300 lines)
│   ├── metadata.py       # Single source of truth: APP_NAME, APP_ID, APP_VERSION, GITHUB_URL
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
│   ├── thresholds.py     # Shared numeric constants (CPU/RAM/temp warn+crit)
│   ├── protected.py      # 3-layer protected-apps logic (critical floor + IT defaults + user extras)
│   ├── run_hidden.py     # subprocess helper: CREATE_NO_WINDOW for console tools
│   ├── monitor_hud.py    # Floating HUD mini-window (idle-aware, hidden-gated polling)
│   └── db.py             # SQLite storage (scan history, settings)
├── tests/                # pytest suite (37 tests): test_safety.py, test_logic.py, conftest.py
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

- **SPA in pywebview**: HTML/CSS/JS lives in `app/web/index.html` (~4300 lines). `desktop_app.py::_load_app_html()` reads it at startup into `APP_HTML`. Python↔JS bridge via `class AgentAPI` exposed as `window.pywebview.api`. When editing the SPA, edit `web/index.html` directly — never re-embed the string.
- **Page navigation**: `showPage(name)` shows/hides `.page` divs. Nav items use `data-page` attributes. Wire new pages in both `showPage()` and as a nav button + page div. Legacy sub-page names (`monitor`, `startup`, `sysinfo`, `organize`, `events`, `troubleshoot`) are physically **moved** into tab panels at runtime, so `showPage()` holds a `_SUBPAGE` redirect map that routes each to its container page + tab (via `_activateTab(group,panel)`) instead of blanking the screen. Any KPI tile / finding / command-palette entry targeting those names goes through the redirect.
- **Bilingual**: All SPA text uses `<span class="en">…</span><span class="ja">…</span>` pattern. `setLang()` toggles via `data-lang` on `<html>`. chat_popup.py uses a JS `_T = {en:{…}, ja:{…}}` dict instead (separate window, no DOM spans).
- **Theme**: Flat dark design, teal brand `#2DD4B0`. No glass blur or glows. Light mode via `:root[data-theme="light"]` overrides. Toggle in Settings page, persisted in `localStorage('pcha_theme')`.
- **Metrics polling**: A single `_globalPoll` timer (3s) drives the sidebar live pills always, and the Performance ▸ Live gauges only when that page+tab is active. Background `monitor.py` thread collects samples into `deque(maxlen=60)`. `monitor.py` reads CPU/RAM/battery via **psutil** (not per-call PowerShell — that cut `collect_metrics()` from ~9.4s to ~0.002s); CPU meter is primed at import; thermal-zone temp is the one PowerShell read, cached 30s (`_TEMP_TTL`).
- **Scan**: Daemon thread (`_do_scan`). Progress overlay inside `#page-overview`. Statusbar shows scan% on all pages. `data["score"]` is always written back into `_last_scan_data` after scan.
- **Assistant**: `chat()` intent registry → Ollama async fallback. Chat history (last 20) passed to Ollama for context. Multi-step detection via `action_planner.py`.
- **Analysis panels**: Each data page has `<div id="analysis-[page]">` rendered by `render*Analysis()` JS functions from live data. No extra backend needed.
- **Health watch**: Daemon thread `_start_health_watch()` checks psutil every 15 min; fires Windows notification if disk < threshold or RAM > threshold.
- **Ollama status**: `_check_ollama_status()` daemon pings Ollama 3s after startup; result exposed via `get_ai_status()`. Status pill on Assistant page turns green/red.
- **Subprocess encoding**: All `subprocess.run()` calls use `encoding='utf-8', errors='replace'` to prevent `UnicodeDecodeError` on Japanese Windows (cp932 default).
- **Subprocess no-window**: `desktop_app.py` patches `subprocess.Popen.__init__` at import to force `CREATE_NO_WINDOW` (0x08000000) on every child unless the caller set `creationflags`. The `--windowed` exe has no console, so without this every powershell/cmd/powercfg call flashes a black window. One patch covers every module (they share the `subprocess` module object). New subprocess calls need nothing extra.

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
Stop-Process -Name python,pythonw,Helix -Force -ErrorAction SilentlyContinue
Start-Sleep 1
Start-Process python -ArgumentList "desktop_app.py" -WorkingDirectory "C:\Users\10517\Documents\Code\PCHealthAgent\app" -WindowStyle Hidden
```

## Packaging (Helix.exe)

PyInstaller onefile windowed build → `dist/Helix.exe` (~30 MB). Requires the
WebView2 runtime on the target (ships with Win11).

```powershell
python -m PyInstaller --noconfirm --onefile --windowed --name Helix `
  --icon "C:/Users/10517/Documents/Code/PCHealthAgent/app_icon.ico" `
  --add-data "web;web" `
  --collect-all webview --collect-all clr_loader --collect-all pythonnet --collect-all proxy_tools `
  --hidden-import clr --hidden-import bottle --hidden-import pystray._win32 `
  --distpath "<root>/dist" --workpath "<scratchpad>/build" --specpath "<scratchpad>/build" desktop_app.py
```

- **Frozen-aware paths**: when `getattr(sys,"frozen",False)`, `BASE` = `%LOCALAPPDATA%\PCHealthAgent` (else repo parent). `launcher.py` startup entry uses `sys.executable` directly when frozen.
- `dist/`, `build/`, `*.spec`, `app_icon.ico` are gitignored — the exe is **not** committed. Rebuild and redistribute the exe after any source change testers run.

## Editing tips

- `desktop_app.py` is ~2800 lines (Python only). SPA markup/CSS/JS lives in `app/web/index.html` (~4300 lines).
- CSS: in `web/index.html`, search `<style>` (near top of file).
- JS: in `web/index.html`, search `<script>` (near bottom).
- Page HTML blocks in `web/index.html`: marked with `<!-- ══ PAGE: Name ══` comments.
- API methods: `class AgentAPI` in `desktop_app.py` (grep for `class AgentAPI`).
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
`monitor`, `startup`, `insights`, `organize`, `events`, `troubleshoot`, `sysinfo` — content **moved** (not cloned, so IDs stay unique) into Performance/Storage/Problems tabs at first visit via `_initPerfPage()`, `_initStoragePage()`, `_initProblemsPage()` (each calls `_mirrorIntoTab`). Because the page is moved out of `.page`, `showPage('<legacy>')` would blank the screen — the `_SUBPAGE` redirect in `showPage()` handles this (see Key patterns). `insights` is the exception: still a standalone `.page`.

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
| `boost_preview()` | List memory-hog processes ≥ PROCESS_RAM_HIGH_MB (critical/IT filtered). Never auto-closes — UI calls close_app per-row |
| `get_protected_apps()` / `set_protected_apps(defaults_on, extras[])` | Editable protected keyword list (3-layer: critical floor + IT defaults + user extras) |
| `get_ai_status()` / `ollama_chat(...)` | Honour `ai_enabled` setting; return `{status:'disabled'}` / None when AI is off |
| `apply_first_run_mode('work'\|'personal'\|'minimal')` / `get_first_run_mode()` | First-launch presets; writes `onboarded_mode` |
| `create_restore_point(description)` / `list_restore_points()` | Windows System Restore via WMI |
| `disable_startup(name)` / `enable_startup(name)` / `list_disabled_startup()` | HKCU Run entry disable with undo archive (`logs/startup-disabled.json`) |
| `capture_metrics_snapshot(label)` / `compare_snapshots(before, after)` | In-memory before/after metric delta |
| `run_schedule_now()` | Fire the in-process scheduler loop immediately (otherwise checks every 10 min) |
| `undo_organize(folder)` | Reverse the last organize-move batch |
| `generate_battery_report()` / `get_battery_capacity()` | powercfg /batteryreport to `logs/battery/` + WMI design vs full capacity + wear% |
| `scan_browser_cache()` / `clean_browser_cache(browser, token)` | Edge/Chrome/Firefox/Brave; two-call confirm; refuses when browser running |
| `list_profiles()` / `save_profile(name)` / `apply_profile(name)` / `delete_profile(name)` | Per-app settings presets (_PROFILE_KEYS = 11 settings; stored in `profiles_json`) |
| `get_about()` / `open_error_log()` | Version info + opener for `<BASE>/logs/error.log` |
| `report_js_error(msg, src, line, col, stack)` | JS → Python error pipe to `logs/js-errors.log` |

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
| `ai_enabled` | `true`/`false` | Master switch for Ollama/AI features (default true) |
| `protected_apps_defaults` | `true`/`false` | Toggle shipping IT_PATTERN defaults (eset, optima, acall, …) |
| `protected_apps_extra` | comma-separated | User-added protected keywords (merged with floor + defaults) |
| `onboarded_mode` | `work`/`personal`/`minimal` | First-launch mode preset applied |
| `schedule_enabled` | `true`/`false` | In-process scheduler on/off |
| `schedule_cadence` | `daily`/`weekly`/`monthly` | Scheduler frequency |
| `schedule_actions` | CSV: `quick_scan`,`temp_cleanup`,`snapshot` | What the schedule runs |
| `schedule_last_run` | ISO timestamp | Last scheduler run time |
| `profiles_json` | JSON | Named settings-preset dictionaries |
| `active_profile` | profile name | Currently applied preset |

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

## Feature guide (tutorial)

Titlebar **?** button (`#btn-guide`) → `openGuide()` opens `#guide-modal`, a bilingual
feature tutorial built once from the `_GUIDE` array by `_buildGuide()` (sections →
icon/name/description rows). Closed by ✕, click-outside, or `Esc`. To document a new
feature, add an entry to `_GUIDE` — no other wiring needed.

## Command palette

`openCmdPalette()` — `Ctrl+K`. Fuzzy-filters `_CMD_ITEMS` array (18 entries: pages + actions). Keyboard-navigable (`↑↓ Enter Esc`). `_cmdExec(idx)` closes palette and fires the action. Placeholder text is bilingual (updated by `setLang()`).

## System Info page (sysinfo)

`loadSysInfo()` → `get_hardware_info()` → PowerShell WMI → `_renderHW(d)`. Cached in `_hw_cache` after first load. Refresh button sets `force=true` to re-query. Renders: CPU card, RAM card, GPU cards, Disk cards, OS + Machine cards via `_hwCard(accentColor, label, name, specs[], badge)`.

## Notifications

`_show_notification(title, message)` — PowerShell `NotifyIcon` balloon tip. Called:
1. After scan (if `show_notifications = "true"`)
2. By health watch daemon (if thresholds crossed)

## Performance guardrails (do NOT regress)

Root cause of a 74%→0% CPU fix on 2026-10 — three invariants to preserve:

1. **`document.hidden` gate every live timer.** `_globalPoll` in desktop_app.py AND `_hudPoll`/clock in `monitor_hud.py` both check `document.hidden` and skip the sample. Reason: a hidden HUD was burning a full core via `psutil.process_iter(memory_info)` every second.
2. **No `typeperf` child per tick.** `monitor.py` must not spawn `typeperf` (or any native perfcounter) in `_collect_*`. The Step-15 GPU sampler was removed for this.
3. **Idle-aware monitor sampler.** `monitor.request_samples()` is called by UI on each sidebar/live-pill refresh. Without recent calls the worker sleeps `_IDLE_INTERVAL` (30s) instead of `_ACTIVE_INTERVAL` (3s). Don't delete `request_samples()` plumbing when wiring new live panels — call it on refresh.
4. **psutil CPU meter primed at import** and `_collect_cpu_ram` uses `interval=0.15` (NOT `interval=None`) — otherwise first sample is 0 and the Live gauges show 0,0,0.

## JS defensive patterns (do NOT regress)

- **localStorage is NOT guaranteed in WebView2.** A `SecurityError` at startup halts the entire script and triggers cascading `Cannot access 'X' before initialization` TDZ errors. All localStorage access MUST go through `_lsGet(k)` / `_lsSet(k,v)` try/catch helpers.
- `_monTimer`, `_globalMonTimer` are declared with `var` (not `let`) to eliminate TDZ risk if script hoisting is disturbed by an early error.
- Every page should start cleanly even with localStorage disabled.
- JS errors pipe to `logs/js-errors.log` via `window.addEventListener('error'/'unhandledrejection')` → `report_js_error()`. Check this file when debugging a blank page.

## Protected apps (3-layer, `app/protected.py`)

Live keyword set = **critical floor** ∪ (**IT defaults** if `protected_apps_defaults=true`) ∪ **user extras**.

- **Critical floor** (always on, never removable): `system, lsass, csrss, winlogon, smss, wininit, services, svchost, msmpeng, mssenses`
- **IT defaults** (shipping, toggleable): `eset, ekrn, egui, optima, acall, onedrive, teams, microsoftedge, msedge, securityhealth, windowsdefender`
- **User extras**: comma-separated via `set_protected_apps()`.

Pytest `test_safety.py` enforces: critical floor always present; defaults toggle off correctly; user extras merge in.

## Boost (Overview + Ctrl+K)

🚀 Boost button on idle-state Overview and in command palette opens `#boost-modal`, which calls `boost_preview()` → list of memory-hog PIDs (name, mem_mb) filtered by protected keywords + self-PID. Each row has a Close button → `close_app(name)` (graceful WM_CLOSE, IT-protected names refused). Never auto-closes.

## Scheduler

In-process watcher thread (`_start_schedule_watch`) wakes every 10 min, checks `schedule_cadence` against `schedule_last_run`, runs `schedule_actions` CSV (any of `quick_scan`, `temp_cleanup`, `snapshot`). `run_schedule_now()` bypasses the cadence check.

## Testing

```powershell
cd "C:\Users\10517\Documents\Code\PCHealthAgent"
python -m pytest tests -q
```

37 tests across `test_safety.py` (protected paths, blocked exts, injection strings, 3-layer protected-apps logic) and `test_logic.py` (threshold ordering, intent matcher, multi-step splitter, organizer collision + undo round-trip). Must stay green before any commit.

## Deferred UI work

All six deferred UI items are now wired on the Settings page (About section
is an expandable "Details" block inside the Settings → About card; First-run
picker auto-shows ~1.8s after `pywebviewready` when `onboarded_mode` is unset,
and can be reopened via Settings → Setup Mode).

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
