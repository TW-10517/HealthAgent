# Helix (PC Health Agent)

Local-first Windows 11 desktop utility for PC health scanning and safe maintenance.
Built with Python + pywebview (Edge WebView2). No cloud APIs, no telemetry.

Product name is **Helix** (window title, exe, "Orbit Pulse" logo via `_create_shield_icon`).
The repo/package id remains `PCHealthAgent`.

**Project root**: `C:\Users\10517\Documents\Code\PCHealthAgent`
**GitHub**: `https://github.com/TW-10517/HealthAgent.git` (branch `main`)
**Live site**: https://helix-57o.pages.dev (Cloudflare Pages, free tier, unlimited bandwidth)
**Download URLs**: `/Helix.exe` (23 MiB, UPX-compressed), `/Helix.zip`, `/version.json`
**Current released version**: `1.1.1` (deployed to Cloudflare 2026-10-28; verify with `curl https://helix-57o.pages.dev/version.json` before assuming users are on it)

## Session handoff — 2026-10-28 → 2026-10-29 (unpushed candidate 1.1.2)

The previous session batched four improvements over the live 1.1.1 release but did NOT push. The working tree carries all of them. In a fresh chat, running `git diff --stat` shows the staged state. Bump `APP_VERSION` to `1.1.2` only at push time (user's standing rule).

**Changes in the working tree (ready to ship):**

1. **Updater — endless-terminals fix** (`app/desktop_app.py` `download_and_install_update`): the `apply_update.cmd` script was rewritten with a bounded 20-try `move` loop, `timeout /t` instead of `ping` as the sleep, and the parent `subprocess.Popen(["cmd.exe", "/c", ...])` now sets `CREATE_NO_WINDOW | DETACHED_PROCESS | NEW_GROUP`. Root cause the user hit in 1.1.1: unbounded retry loop + missing CREATE_NO_WINDOW on cmd.exe = terminal windows opening forever during update.
2. **No Ollama warmup on startup** (`_preload_hw_cache`): removed the proactive `ollama_chat("ok", context="warmup", ...)` call. First Assistant chat now takes the one-time 5–10 s model-load hit; everyone else saves CPU + fan + RAM. User-reported "fan speed increasing when app opens" was this.
3. **BELOW_NORMAL CPU + IOPRIO_LOW** at the top of `run()` right after `SetCurrentProcessExplicitAppUserModelID`. Helix now yields to any foreground app automatically. Verified at smoke test: launched exe shows `priority=BelowNormal`.
4. **Updater security hardening** (`AgentAPI.check_for_update` + `download_and_install_update`):
   - `_TRUSTED_UPDATE_HOST = "helix-57o.pages.dev"` — exe URL parsed, rejected unless scheme=https AND hostname matches exactly (subdomain-trick safe).
   - Mandatory sha256 verification: manifest must carry a 64-hex-char `sha256`; downloaded exe is streamed through `hashlib.sha256` and compared; mismatch → staged file deleted, install refused.
   - 128 MiB download cap guards against disk-fill via hostile manifest.
   - Trusted `sha256`, `exe_url`, `size` stashed in private `_update_state["_expected_*"]` so a JS-side forged info dict can't bypass checks.
   - `_show_notification` rewritten to use `-EncodedCommand` with the title/message as PS here-string literals — PowerShell injection now impossible regardless of input.
5. **"What's new" after-upgrade popup** (new API: `get_whats_new`, `acknowledge_whats_new`; new settings keys: `last_known_version`, `last_update_version`, `last_update_notes`, `last_update_date`, `whats_new_popup_pending`): on launch, `_detect_fresh_update()` compares `APP_VERSION` to the previous launch's version; on upgrade it quietly fetches release notes from the manifest and sets a pending-popup flag. First JS poll of `get_whats_new()` fires the `#whats-new-modal` once per upgrade, then `acknowledge_whats_new()` clears the flag. A gradient `#whats-new-row` strip in Settings → About stays visible for 3 days (configurable via `_WHATS_NEW_DAYS = 3`) regardless of popup state. Notes rendered via `textContent` (no XSS from manifest text).
6. **Syntax-warning cleanup** (`app/chat_popup.py`): `CHAT_POPUP_HTML` string prefix changed from `"""` to `r"""` — removes the Python 3.12 SyntaxWarning on `\*` inside the embedded JS regex.

**QA passed (127/127 tests on 2026-10-28)**:
- 47 pytest
- 25 AST parse + 25 module import
- 22 functional (DB settings/history, user_memory CRUD, action_planner, protected 3-layer, scanner.is_protected, sys_executor._clean, open_path refusal, monitor.collect_metrics, get_hardware_info WMI, check_for_update, updater URL-trust refusal, get_about, whats_new default + fresh-upgrade + acknowledge, list_profiles, thresholds, fmt_size, event_analyzer live query, error_triage detector, game_advisor resolve_name, scan history CRUD)
- 8 security grep + lint
- 1 production PyInstaller build (23.13 MiB in 49 s)
- 1 launch smoke (BelowNormal confirmed, 0 Helix-parented conhosts)

**At-push checklist for the next session:**
1. Confirm user says "push".
2. Bump `app/metadata.py` `APP_VERSION` from `1.1.1` → `1.1.2` ONCE.
3. Rewrite the `notes:` string in `website/version.json` (CI will regenerate but seed it with the four batched improvements so the "What's new" popup is meaningful).
4. `git add -A ; git commit -m "..."` (single commit summarizing the four improvements).
5. `git push origin main`.
6. Watch CI run → auto-commit of binaries → `helix-57o.pages.dev/version.json` serves 1.1.2 (watch for the Cloudflare git-connect flakiness noted in CI/release lesson #6).

**Still open from this session:**
- Known SPA XSS exposure (141 `innerHTML` sites, user-controlled text in some). Pre-existing. Needs its own audit cycle.
- 25-ish `unused import` pyflakes warnings. Cosmetic. Not fixed.

## Working rules

- **Never auto-deploy.** Do not run `wrangler pages deploy …` and do not `git push origin main` unless the user explicitly says "deploy", "push", "ship it" or similar. Build locally, commit locally if asked, then **stop and wait**. The user keeps a stated rule about this; it's in auto-memory too (`feedback_no_auto_deploy.md`).
- **Website binaries live in `website/`** and are committed to git (so Cloudflare git-connected auto-deploy sees them). This is intentional — do not add `website/Helix.*` to `.gitignore`.
- **Never remove the Vercel references assuming they're stale**: Vercel is fully deleted. Only Cloudflare Pages serves the site now.

## No-visible-terminals rule (CRITICAL — do NOT regress)

The `--windowed` PyInstaller build has NO console. Every subprocess MUST be launched with `CREATE_NO_WINDOW` (0x08000000) or the user sees a flashing black window — in the worst case (a retry loop), windows open forever and the user cannot stop it. This happened in v1.1.1: `apply_update.cmd` was launched with `DETACHED_PROCESS` only, and its unbounded `move` retry loop fired `ping 127.0.0.1 -n 2` on every miss while a visible cmd console was attached. The user could not close it.

**Rules for every new subprocess call:**
1. **Default to `run_hidden.run()` / `run_hidden.Popen()`** instead of raw `subprocess.*`. The helper lives in `app/run_hidden.py` and injects `CREATE_NO_WINDOW` when the target is a console tool.
2. **If you must call `subprocess.Popen` with custom `creationflags`** (e.g. `DETACHED_PROCESS`, `CREATE_NEW_PROCESS_GROUP`), **OR in `CREATE_NO_WINDOW` explicitly** — `run_hidden` SKIPS injection when the caller already sets `creationflags`. One missed `| 0x08000000` = visible terminal.
3. **No `ping` as sleep.** Use `timeout /t N /nobreak >nul 2>&1` in `.cmd` scripts. Reasons: (a) less alarming to a user who sees it, (b) `ping` output goes to a console even with `>nul` in some edge cases, (c) doesn't look like a network probe.
4. **Every retry loop MUST have a hard cap.** No infinite `goto try_X`. The updater's move loop caps at 20 attempts (20s); after that it writes a staged file and exits silently so the next launch picks up the update.
5. **New console-spawning tools** (powershell.exe, cmd.exe, powercfg.exe, typeperf.exe, wmic.exe, etc.) must be added to `_CONSOLE_EXES` in `run_hidden.py` AND the call site should prefer the helper.

Before shipping any change that spawns a process, grep `subprocess\.(Popen|run|call)` across `app/` and verify every hit either goes through `run_hidden` OR explicitly sets `creationflags` with `CREATE_NO_WINDOW` ORed in.

## CI/release lessons (keep these — hard-earned)

Mistakes made during the GitHub Actions build-release pipeline rollout. Avoid repeating them:

1. **`app_icon.ico` MUST stay tracked in git.** It is not generated at runtime (comment was wrong). PyInstaller crashes `FileNotFoundError: Icon input file ...app_icon.ico not found` if it's gitignored. If you see "generated at runtime" next to a static asset in `.gitignore`, verify before trusting the comment.
2. **No `cefpython3` in CI pip install.** pywebview uses EdgeChromium on Windows, not CEF. Adding `cefpython3` bloats the exe past Cloudflare's 25 MiB cap. Add `--exclude-module=cefpython3` to the PyInstaller command and leave it out of `pip install`.
3. **Beware single-letter PowerShell functions.** `function H` collides with the built-in `Get-History` alias — the parser binds `H 'file.exe'` to `Get-History -Id 'file.exe'` → "Cannot convert to Int64". Use descriptive names (`HashOf`, `SizeOf`). Same risk for `R`, `S`, `D`, etc.
4. **Build step must use `shell: cmd` with `> file 2>&1`.** PowerShell's `Tee-Object` / `*> file` / `2>&1 | Out-File` all silently drop PyInstaller's output on GitHub Actions. cmd.exe redirection is the only reliable way to capture `pyinstaller.log`.
5. **GitHub API from unauthenticated polling gets rate-limited fast.** Prefer `git fetch origin main ci-logs` to check if CI auto-committed (success) or pushed logs (failure) — no API quota needed.
6. **Cloudflare Pages git-connected auto-deploy can silently fail.** A green CI build with a `[skip ci]` auto-commit to `main` does NOT guarantee Cloudflare rebuilt. Always verify by curling `https://helix-57o.pages.dev/version.json` after a release. If it's stale >5 min, the Pages ↔ GitHub connection likely needs manual retry or reconnection in the Cloudflare dashboard.
7. **Bump `APP_VERSION` in `app/metadata.py` ONLY at push time, not on every edit.** Accumulate multiple fixes under the current version in-progress; right before `git push`, bump once to the next version with notes covering everything in the batch. Reasons: (a) one version = one user-visible update; three bumps in a day creates three "Install update" prompts for the same work, (b) the user explicitly asked for this (2026-10-28). Without any bump at push time, CI rebuilds the exe but `version.json` stays at the current version and the in-app updater never advertises an update — so still bump exactly once at the push step.

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
├── app_icon.ico
└── website/                # Cloudflare Pages site (committed to git)
    ├── index.html          # Landing page (GitHub/view-source links stripped per 2026-10-26)
    ├── styles.css
    ├── favicon.svg
    ├── Helix.exe           # Current released build (23 MiB UPX)
    ├── Helix.zip           # Same exe, zipped (avoids Chrome's "uncommon download" warning)
    ├── version.json        # Update manifest served to the in-app updater
    ├── vercel.json         # vestigial, Cloudflare ignores it; safe to delete
    └── .gitignore          # excludes only .vercel/
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

## Update-pipeline security (MUST follow — hard-coded invariants)

The auto-updater ships a new executable to users' machines. Weakening any of these turns Helix into a malware delivery vector. Do not bypass "for debugging".

1. **`AgentAPI._TRUSTED_UPDATE_HOST = "helix-57o.pages.dev"`** — exe URL is parsed and REJECTED if its scheme isn't `https` or its hostname isn't exactly this host. If the host ever changes, update it in exactly one place and also in `_TRUSTED_UPDATE_HOST`.
2. **`UPDATE_MANIFEST_URL` must be HTTPS.** Enforced at runtime in `check_for_update`.
3. **sha256 is required.** A manifest without a valid 64-hex-char `sha256` field is rejected. The downloaded exe is streamed through `hashlib.sha256` and compared against the manifest value BEFORE anything else can happen with the file. On mismatch: delete the staged exe, write `phase=error`, return.
4. **sha256 and exe_url are stashed privately in `_update_state["_expected_sha256"]` / `_expected_exe_url` by `check_for_update`.** `download_and_install_update` reads from those private fields, NOT from an `info` dict a caller could forge. Do not pass expected hash from the JS side.
5. **Download size is capped at 128 MiB** so a hostile manifest can't fill the disk. Bump the cap if the exe ever grows past ~50 MiB; never remove it.
6. **TLS cert + hostname verification stays on.** `ssl.create_default_context()` is correct — don't swap it for an unverified context "to fix a connection issue". A real TLS issue means investigating the issue, not disabling verification.
7. **Downloaded exe is never executed by `download_and_install_update` directly** — the swap goes through `apply_update.cmd` which runs after the main process exits. If someone proposes "just subprocess.Popen the new exe to test it", refuse.

## PowerShell injection guard (MUST follow)

Any user-controllable string that reaches PowerShell MUST either (a) be passed as a named parameter via `-File` / `-EncodedCommand`, or (b) be sanitized against `[\r\n;&$`"']` AND wrapped in a here-string. Replacing `"` and `'` alone is NOT enough — backtick, `$`, `;`, and newline can all break out. `_show_notification` uses `-EncodedCommand` with the title/message as literal here-string contents, which is the preferred pattern for new call sites. If a new call site inlines any variable into a `powershell -Command` string, flag it.

## Known SPA XSS exposure (triage item)

`app/web/index.html` uses `innerHTML` in ~141 places. Several render user-controllable text (filenames from scans, event-log messages, chat content, Ollama replies). A hostile file with a name like `<img src=x onerror=…>` could XSS the SPA and call `window.pywebview.api.*`. This is a known gap, not yet fixed — needs its own audit pass. When adding a new render path, prefer `textContent =` over `innerHTML =` for anything derived from scan data, file names, event text, or chat input. If `innerHTML` is unavoidable (e.g. you need HTML structure), run the user-sourced parts through a small escape helper (`.replace(/[&<>"']/g, …)`) first.

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

PyInstaller onefile windowed build → `dist/Helix.exe` (~23 MiB **with UPX**; ~30 MiB without). Requires the WebView2 runtime on the target (ships with Win11). **UPX is mandatory** because Cloudflare Pages caps individual file assets at 25 MiB — without UPX the exe is too big to host.

UPX install path on this machine: `C:\Users\10517\AppData\Local\Microsoft\WinGet\Packages\UPX.UPX_Microsoft.Winget.Source_8wekyb3d8bbwe\upx-5.2.1-win64\` (installed via `winget install UPX.UPX`).

```powershell
$SCRATCH = 'C:\Users\10517\AppData\Local\Temp\…\scratchpad'  # session scratchpad
$upxDir  = 'C:\Users\10517\AppData\Local\Microsoft\WinGet\Packages\UPX.UPX_Microsoft.Winget.Source_8wekyb3d8bbwe\upx-5.2.1-win64'
cd C:\Users\10517\Documents\Code\PCHealthAgent\app
python -m PyInstaller --noconfirm --onefile --windowed --name Helix `
  --icon "C:/Users/10517/Documents/Code/PCHealthAgent/app_icon.ico" `
  --add-data "C:/Users/10517/Documents/Code/PCHealthAgent/app/web;web" `
  --collect-all webview --collect-all clr_loader --collect-all pythonnet --collect-all proxy_tools `
  --upx-dir "$upxDir" `
  --distpath "C:/Users/10517/Documents/Code/PCHealthAgent/dist" `
  --workpath "$SCRATCH/build" --specpath "$SCRATCH/build" desktop_app.py
```

Before building: `Get-Process Helix -EA SilentlyContinue | Stop-Process -Force` — PyInstaller fails `PermissionError: Access is denied` if the old exe is still running.

- **Frozen-aware paths**: when `getattr(sys,"frozen",False)`, `BASE` = `%LOCALAPPDATA%\PCHealthAgent` (else repo parent). `launcher.py` startup entry uses `sys.executable` directly when frozen.
- `dist/`, `build/`, `*.spec`, `app_icon.ico` are gitignored — the dev `dist/Helix.exe` is **not** committed. The **released** exe lives at `website/Helix.exe` and IS committed (Cloudflare serves from there).
- UPX side effect: slightly higher antivirus false-positive rate. SmartScreen already shows "Windows protected your PC" on first run of any unsigned exe — UPX adds no new user-visible warning on top of that.

## Release pipeline

### Hosting

- **Cloudflare Pages** project `helix`, URL `https://helix-57o.pages.dev` (the `-57o` suffix is a Cloudflare-generated random tag because `helix.pages.dev` was globally taken; same policy as Vercel's free tier).
- **Account**: `Gokuldharsan03@gmail.com` (id `15feca14c3b15012f9f31aa2250ab106`).
- GitHub ↔ Pages connection: live. **Build output directory = `website`**. Every push to `main` triggers a Cloudflare build; preview URL comes back in ~30s.
- Wrangler CLI: `npm i -g wrangler` (installed, v4.152.0). Auth lives in `C:\Users\10517\AppData\Roaming\xdg.config\.wrangler\`.
- Classic Pages was created with `wrangler pages project create helix --production-branch=main --force` (the `--force` opts out of Cloudflare's new default of delegating `pages` commands to Workers — leave Pages, don't use Workers; Pages gives the cleaner `*.pages.dev` URL and git-connected auto-deploy).
- **Hobby/free tier**: unlimited bandwidth, commercial-use OK, per-file cap **25 MiB**.
- Vercel is completely removed. If a prior conversation referenced `gethelix-ten.vercel.app` or `helix-seven-inky.vercel.app`, those are deleted.

### Update manifest (`website/version.json`)

```json
{
  "version": "1.0.1",
  "released": "2026-10-26",
  "exe_url":     "https://helix-57o.pages.dev/Helix.exe",
  "zip_url":     "https://helix-57o.pages.dev/Helix.zip",
  "website_url": "https://helix-57o.pages.dev",
  "sha256":      "<lowercase hex>",
  "size_bytes":  24194242,
  "min_supported": "1.0.0",
  "notes": "Human-readable changelog (shown verbatim in-app)."
}
```

The in-app updater reads this. `metadata.py` holds the baked-in `WEBSITE_URL` and `UPDATE_MANIFEST_URL` — change these together whenever the host or subdomain changes.

### In-app auto-updater

Three Python API methods (see `AgentAPI` in `desktop_app.py`):

| Method | Behaviour |
|---|---|
| `check_for_update(force=False)` | GETs `version.json` (4 s timeout). Returns `{ok, current, latest, has_update, exe_url, notes, size_bytes, can_install, website_url}`. Non-blocking from UI; failures return `ok:false` so UI silently falls back. |
| `get_update_progress()` | UI poll (600 ms) returns `{phase, pct, msg, tmp_exe, latest}`. Phases: `idle` → `downloading` → `ready` → `installing` → `error`. |
| `download_and_install_update()` | Only works when `sys.frozen`. Downloads new exe to `BASE/updates/Helix-new-<ts>.exe`, writes `apply_update.cmd` that waits ~2 s, moves the staged file over the running exe path, relaunches, and self-deletes. Then this process `os._exit(0)` after ~1.2 s. |

JS side (`web/index.html`):
- Settings → About has an **"Install update"** row (`#update-row`) with `btn-check-update`, `btn-get-update`, progress bar `#update-progress-bar`.
- Sidebar version label `#nav-ver` becomes a glowing-dot "Update available" button when newer version is detected.
- Auto-check fires once 2.5 s after `pywebviewready`.

### Full release flow

Follow the no-auto-deploy rule — do each step and **stop before** pushing/deploying unless the user explicitly says to.

1. Make changes in `app/`, test locally with `python desktop_app.py`.
2. Bump `APP_VERSION` in `app/metadata.py`.
3. Kill any running Helix: `Get-Process Helix -EA SilentlyContinue | Stop-Process -Force`.
4. Rebuild with the PyInstaller + UPX command above → `dist/Helix.exe`.
5. Copy to website: `Copy-Item dist/Helix.exe website/Helix.exe -Force`.
6. Rezip: `Remove-Item website/Helix.zip -Force; Compress-Archive website/Helix.exe website/Helix.zip -CompressionLevel Optimal`.
7. Compute SHA256 + size, update `website/version.json` (bump `version`, update `sha256`, `size_bytes`, `released`, `notes`).
8. **Pause.** Report ready-to-deploy status.
9. On the user's go-ahead: `git add` the five files (`app/metadata.py`, `website/Helix.exe`, `website/Helix.zip`, `website/version.json`, any UI/code changes), commit, `git push origin main`. Cloudflare auto-deploys. No wrangler needed.
10. Verify: `Invoke-WebRequest https://helix-57o.pages.dev/version.json` and the three asset HEAD requests.

### Website edits (HTML/CSS only, no exe change)

Edit `website/index.html` or `website/styles.css` → commit → push (on user go-ahead). Cloudflare auto-rebuilds. Skip the exe/zip/version.json dance entirely.

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
| `check_for_update(force=False)` | GET `UPDATE_MANIFEST_URL` (4 s timeout), semver-compare → `{ok, current, latest, has_update, exe_url, notes, size_bytes, can_install, website_url}` |
| `get_whats_new()` | Returns `{ok, version, notes, date, within_window, days_remaining, show_popup}`. `show_popup=true` ONCE per upgrade (within 3 days of the upgrade). Notes remain available via `within_window` for 3 days total. |
| `acknowledge_whats_new()` | Clear the one-time popup flag after the UI has displayed the modal. |
| `get_update_progress()` | Poll endpoint: `{phase, pct, msg, tmp_exe, latest}` where phase ∈ `idle`/`downloading`/`ready`/`installing`/`error` |
| `download_and_install_update()` | Frozen-only: stream exe to `BASE/updates/`, write `apply_update.cmd`, detach-spawn it, `os._exit(0)` after 1.2 s |

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
| `last_known_version` | semver | Version seen on the previous launch; used to detect fresh upgrades |
| `last_update_version` | semver | Version from the manifest at the moment of the detected upgrade |
| `last_update_notes` | text | Release notes captured from `version.json` after upgrade |
| `last_update_date` | `YYYY-MM-DD` | When the upgrade was detected; "What's new" strip hides after 3 days |
| `whats_new_popup_pending` | `true`/`false` | Fires the one-time modal; `acknowledge_whats_new()` flips it to false |

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
5. **No proactive Ollama warmup on startup.** Loading qwen2.5:3b on CPU pins the CPU for 5-10s and spins the fan on every launch, even when the user never opens Assistant that session. Keep the model lazy — let the first real chat take the one-time load hit. If someone adds back a startup warmup `_asst.ollama_chat("ok", context="warmup", ...)`, delete it. The user explicitly flagged this (2026-10-28) as "fan speed increasing" when the app opens.
6. **Helix process runs at BELOW_NORMAL CPU priority and IOPRIO_LOW disk priority** (set in `run()` right after `SetCurrentProcessExplicitAppUserModelID`). This is deliberate: Helix must never slow down whatever the user is actively using — a game, Teams call, VS Code build, browser video. On an idle system Helix still gets full CPU and feels native-fast; the only effect is that when another app needs CPU/disk, Windows gives it to that app first. Do NOT raise this to NORMAL to "make Helix feel faster" — the user explicitly asked for this behavior (2026-10-28). If a specific operation is too slow at low priority (e.g. a scan feels sluggish on an otherwise idle system), diagnose that operation; don't bump global priority.

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
- **SmartScreen "Windows protected your PC"** on first run — the exe is unsigned (no code-signing cert). The user must click "More info" → "Run anyway". Zipping to `Helix.zip` on the site reduces Chrome's "uncommon download" warning but does not remove SmartScreen itself.
- **UPX bumps the AV false-positive rate.** Microsoft Defender has been fine so far; some third-party AVs may quarantine. If this becomes a user-visible problem, the only fixes are code signing (paid) or dropping UPX (requires paid Cloudflare plan to raise the 25 MiB file cap).
- **Cloudflare Pages hard limits**: 25 MiB per file (why UPX exists), 20,000 files per deployment, 500 builds/month on free tier.

## Regression gotchas (v1.0.1)

- **Duplicate `loadSysInfo()` in `web/index.html`.** A second definition at ~line 3651 referenced a nonexistent `#sysinfo-area` and shadowed the real one at ~line 2797, leaving Performance → System info blank. If sysinfo blanks again, grep for `function loadSysInfo` — there should be exactly one.
- **Delete-to-Recycle-Bin vs scanner.PROTECTED.** `delete_to_recycle_bin()` must NOT reuse `scanner.is_protected()` — that path's protection set includes OneDrive, so on corporate laptops where Documents/Desktop/Downloads are OneDrive-synced every user-initiated delete was refused. The narrower `_delete_blocked` list covers only Windows + Program Files + ESET + Optima. Scan-protection and delete-protection are different trust boundaries — keep them separate.

## Roadmap: AI-first direction

Standing direction (also in auto-memory `project_helix_direction.md`): the user wants local-Ollama AI to be the main interaction surface, not a side tab. All inference stays local on `qwen2.5:3b` (~8–13 tok/s output on this machine). Features must fit a 3B-model envelope: short prompts, short outputs, Python code does the fact-gathering and hands structured context to the model to narrate/advise.

### Top 3 to start

1. **"Can my PC run X?" + Installer Watchdog** — user says "I'm about to install Valorant" (or any game/app); Helix knows CPU/RAM/GPU/disk-free from existing hardware data, grabs the game's requirements (steam/web lookup), tells them yes/no in plain language with the bottleneck named. Watchdog mode optionally hooks installer launches via a file-system / clipboard trigger.
2. **"What's wrong?" event-log explainer** — `event_analyzer.py` already pulls Windows events; feed the last 24 h of errors/warnings to qwen2.5 and ask for a human summary ("Your webcam driver crashed twice last night; here's what that means"). Keep it strictly narrative, no auto-fix.
3. **User memory (SQLite)** — a `user_memory` table (fact, confidence, last_seen, source) so Helix remembers: "user plays Valorant", "user complains about fan noise", "user runs Teams + Chrome + VS Code daily". Fed back into the system prompt so advice gets personal over time.

### Longer-list ideas (brainstorm bank)

4. Proactive morning briefing: "Your disk filled 2 GB overnight (OneDrive sync); CPU averaged 40% during a Teams call at 3 AM — scheduled task?"
5. Conversational cleanup: "I need 10 GB free" → AI picks temp caches + old installers + big video files, previews the list, user confirms per-group.
6. Game-mode narrator: when a known-game process starts, HUD whisper-shows "Closed 4 background apps, freed 2.1 GB RAM, you're good."
7. Error-triage chat: user pastes a crash message → Helix cross-references event log timestamps around the crash → explains what probably caused it.
8. "Why is my PC slow right now?" live triage: snapshot CPU/RAM/disk/net top offenders → qwen2.5 narrates ("Chrome has 47 tabs eating 6 GB; Teams is 20% CPU").
9. Weekly health letter: emailed-style summary rendered in-app ("This week your average boot time went from 48 s to 71 s — three new startup apps added themselves").
10. Smart startup pruning: AI explains each startup app's purpose and suggests disable candidates based on how often the user actually uses them (requires foreground-window logging).
11. Battery coaching: "You're running on battery 60% of the day — enable efficiency mode? Your Chrome is the biggest drain."
12. Context-aware notifications: silence health-watch during gaming/presentation detection.
13. Install assistant: "I want to try Blender" → downloads from vendor, checks hash, flags installer options, warns about bundled bloatware.

### Feature #4 landed (2026-10-27)

`app/briefing.py` — once-per-calendar-day summary of overnight PC deltas.
Compares the latest two completed scans (score, issues, disk free) and the
last 12 h of Windows Error/Critical events, builds 2-4 plain-English
highlights, then asks Ollama for an optional 2-sentence narration grounded
in those exact numbers.

- **Intents**: `morning briefing`, `daily briefing`, `briefing`,
  `give me the briefing`, `what(s|'s|'s) up today`, `what's new today`.
  Bare "good morning" intentionally stays on the greet handler.
- **Settings**: `briefing_enabled` (default true), `last_briefing_date`
  (set via `mark_shown()` after each delivery).
- **API**: `AgentAPI.get_briefing(force=False, narrate=True)`,
  `dismiss_briefing()`.
- Narration prompt holds strict "do NOT invent numbers" guardrails and lists
  the exact facts available (score, issues, disk free, event counts, top
  event sources).

### Feature #7 landed (2026-10-27)

`app/error_triage.py` — user pastes a crash/stack/BSOD → Helix detects it,
pulls a ±5 min window of Windows events (or last 2 h fallback), hands both
to Ollama for a 2-3 sentence explanation. Never executes anything.

- **Detection**: `looks_like_error()` recognises traceback/exception/BSOD
  keywords, stop codes (`0x...`), faulting modules, kernel panics, and
  multi-line stack frames.
- **Timestamp hints**: three formats parsed (ISO, US m/d/y + AM/PM,
  European d/m/y 24h); extracts the first plausible timestamp from the
  paste so correlated events come from the right window.
- **Explicit intents**: `triage this error …`, `explain this error`,
  `help me with this stack trace`, `help me understand this crash`,
  `what does this error mean`, `why did my pc/app/chrome crash`. These
  patterns live above the generic `help` intent — order matters.
- **Implicit routing**: when no intent matches but `looks_like_error()` is
  true, the chat handler auto-routes to triage, so pasting a raw traceback
  "just works".
- **Force mode**: `triage(text, force=True)` runs correlation + narration
  even if the input doesn't look like an error paste — used when the user
  explicitly asks ("why did my pc crash") without providing one.
- **API**: `AgentAPI.triage_error(text, narrate=True)`.

### Feature #3 landed (2026-10-27)

`app/user_memory.py` + new `user_memory` SQLite table (`db.py`). Short facts
("user plays valorant", "user is a developer") persist across conversations
and are auto-injected into every Ollama system prompt via
`user_memory.recall_for_prompt()` so advice gets personal over time.

- **Explicit intents**: `remember that I play X`, `note that …`, `I play X`,
  `I use X daily`, `I'm a developer/gamer/…` → `remember_me`. `what do you
  know about me` / `show your memory` / `list memories` → `recall_me`.
  `forget that …` / `remove the memory about …` → `forget_me`.
- **Observation**: every advisor turn in `check_pc_runs` fires
  `note_game_interest(name, verdict)` so Helix remembers which games the user
  has asked about and what the verdict was.
- **Prompt budget**: `recall_for_prompt()` returns at most 6 bullets capped
  at 400 chars so the 3B model's prompt budget stays small; explicit facts
  outrank observed; higher-confidence outranks newer.
- **Toggle**: Settings key `memory_enabled` (default `true`) gates the
  recall block; set `false` to disable personalization entirely.
- **CRUD API**: `AgentAPI.memory_list / memory_add / memory_forget /
  memory_clear / memory_count` for future UI; CLI/intent paths already
  wired.
- Pytest: `scratchpad/qa/mem_batch1.py` covers 10 logic cases + 16 intent
  routings; `mem_batch2_live.py` is the live Ollama round-trip.

### Feature #1 landed (2026-10-27)

`app/game_advisor.py` — local catalog of ~75 games/apps with min specs, CPU/GPU tier model (`_cpu_tier` / `_gpu_tier` cover Intel i3/i5/i7/i9 gens 6–14, Ryzen 3/5/7/9 gens 1–7, Xeon/Celeron/Pentium, plus NVIDIA GTX/RTX, AMD RX, and all common integrated families). Cached (`_SPECS_CACHE`, 5-min TTL) + pre-warmed from `_preload_hw_cache` at startup so queries after boot are instant.

**Japan-market coverage is first-class.** The catalog includes the full Dospara GALLERIA 推奨PC roster (Thirdwave Corporation) — these are the exact titles Dospara ships pre-configured gaming PCs for and must never silently fail to answer:
- **Square Enix**: FFXIV (ff14/ffxiv), FFXVI (ff16/ffxvi), FF7 Rebirth (ff7r/ffvii rebirth), Dragon Quest X/XI (dqx/dq xi/dq11), NieR Automata, NieR Replicant
- **Capcom**: Monster Hunter Rise / World / Wilds (mhr/mhw/mh wilds/monhun), Resident Evil 4 Remake (re4), Resident Evil Village (re8), Devil May Cry 5 (dmc5/dmcv), Dragon's Dogma 2 (dd2), Street Fighter 6 (sf6)
- **Sega / Atlus / RGG**: Like a Dragon: Infinite Wealth (yakuza 8/lad8/infinite wealth), Yakuza 0, Persona 5 Royal (p5r), Persona 3 Reload (p3r), Metaphor ReFantazio
- **Bandai Namco**: Tekken 8, Blue Protocol
- **FromSoftware**: Elden Ring, Armored Core VI (ac6), Dark Souls III (ds3), Sekiro
- **Cygames / Kuro / HoYoverse / Shift Up / Koei Tecmo**: Granblue Fantasy Relink (gbfr/relink), Wuthering Waves (wuwa), Genshin Impact (genshin), Honkai Star Rail (hsr/star rail), Zenless Zone Zero (zzz/zenless), NIKKE, Atelier Ryza 3 (ryza 3)
- **SEGA**: Phantasy Star Online 2 New Genesis (pso2/pso2 ngs/ngs)

Every title above has at least one short-form alias (katakana aliases are a TODO; current intent regex is lowercase ASCII). When adding more JP titles, follow this exact pattern — add to `KNOWN_APPS` AND add aliases to `_ALIASES`. Test with `scratchpad/qa_dospara.py` which asserts 90-case resolution coverage.

### Guardrails specific to AI-first

- Never let the model execute shell commands. All actions still go through typed `chat()` handlers.
- Never send user data off-machine — Ollama only. No cloud fallback, no telemetry.
- Keep prompts <~800 tokens of context; qwen2.5:3b at ~37 tok/s input means a 4k-token prompt is a ~110 s wait.
- Verify short JA↔EN translations before trusting them — 3B models sometimes echo input back.
- Any new AI feature should degrade gracefully when `ai_enabled=false` or Ollama is offline.

## Dependencies

- Python 3.12, pywebview, pystray, Pillow, **psutil** (required — health watch + HUD)
- PowerShell (CIM/WMI for hardware, event logs, notifications)
- Ollama at 127.0.0.1:11434 with qwen2.5:3b (optional — AI summaries + Assistant fallback)
