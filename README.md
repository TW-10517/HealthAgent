# Helix

**Local-first PC health assistant for Windows 10 / 11.** Scan, diagnose, clean, and now chat with a local AI about your machine — without a single byte leaving the PC.

> Website · [helix-57o.pages.dev](https://helix-57o.pages.dev)
> Latest release · **v1.1.0** (2026-10-27)
> Platform · Windows 10 / 11, 64-bit

---

## Install

Pick whichever suits you — they install the **same** app with identical features.

| | Installer (recommended) | Portable |
|---|---|---|
| Download | [`HelixSetup.zip`](https://helix-57o.pages.dev/HelixSetup.zip) (~25 MB) | [`Helix.zip`](https://helix-57o.pages.dev/Helix.zip) (~23 MB) |
| Install flow | Unzip → double-click `HelixSetup.exe` → Windows setup wizard | Unzip → double-click `Helix.exe` |
| Added to | Start Menu, Settings → Apps, optional desktop shortcut | Nowhere — runs from the folder you drop it in |
| Admin needed? | No (per-user install into `AppData`) | No |
| Uninstall | Settings → Apps → Uninstall | Delete the file |
| User data preserved on uninstall | Yes (`AppData\Local\PCHealthAgent`) | N/A |

Both downloads are served as `.zip` because browsers increasingly block raw `.exe` downloads from unknown publishers (SmartScreen, "This file is uncommon…", etc.). Unzip once, then you'll see one `.exe` to run.

**SmartScreen on first run** will show *"Windows protected your PC"* because the binary isn't code-signed. Click **More info → Run anyway**. We don't yet have a commercial signing cert.

**WebView2 runtime** is required and already shipped with Windows 11 and fully-updated Windows 10. If it's missing, Windows will prompt you to install it the first time Helix launches.

---

## What Helix does

### Classic PC-health features

- **Health score** — one 0–100 number summarising disk, RAM, startup bloat, Windows events, and open issues. Shows a ring on the Overview page with the delta vs. your previous scan.
- **Scan** — quick (~5 s) or full (~30–60 s). Finds temp junk, duplicate files, big files, recycle-bin bloat, startup bloat, pending reboots, SMART warnings.
- **Live monitor** — CPU / RAM / Disk / Battery gauges updating every 3 s, with a 60-sample history chart and a live "top processes" table. Pauses while the tab is hidden to save battery.
- **Storage** — temp file cleanup (Recycle-Bin only, never permanent delete), duplicate finder (size → hash), file organiser that moves (never deletes) with full undo history.
- **Problems** — rule-based diagnostics with safe one-click fixes. ~20 checks across disk, RAM, startup, Windows Update, SMART, boot time, etc.
- **Event log** — the last 24 h of Windows Error / Warning events, grouped by source so you see signal not noise.
- **Startup manager** — see what runs at boot. One-click disable (archived to an undo log, not deleted).
- **System info** — CPU, GPU, RAM slots, disk tiers (HDD/SSD/NVMe), OS build, machine model via WMI.
- **Browser cache cleaner** — Edge / Chrome / Firefox / Brave. Only runs when the browser is closed.
- **Scheduled maintenance** — daily / weekly / monthly runs of quick scan + temp cleanup + metric snapshot.
- **Health watch daemon** — background alerts when disk gets low or RAM runs hot.
- **Floating launcher** — optional on-screen radial menu for one-click Scan / Chat / Monitor HUD.

### AI-first features (new in v1.1.0 — all local via Ollama `qwen2.5:3b`)

1. **"Can my PC run X?" advisor** — natural language, instant verdict. Knows ~75 games including the complete **Dospara GALLERIA 推奨PC** roster (FFXIV, FFXVI, Monster Hunter Rise/World/Wilds, Elden Ring, Yakuza 8, Persona 5 Royal, Blue Protocol, Granblue Fantasy Relink, Wuthering Waves, Honkai Star Rail, PSO2 NGS, and more). Picks the single bottleneck component (CPU / GPU / RAM / disk) and names it in plain English with the expected setting level.
2. **User memory** — Helix remembers facts about you across sessions ("user plays Apex", "user is a developer", "user uses Chrome daily"). Explicit facts you tell it get a star; observed facts auto-collected from conversations rank lower. Advice gets more personal over time. Full CRUD in chat and in Settings → Memory.
3. **Morning briefing** — once per calendar day, Helix opens with a 2-sentence summary of what changed since yesterday (health score delta, free disk delta, overnight Windows errors). Fires only if something changed; stays quiet on a healthy day.
4. **Error triage** — paste any crash, BSOD, stack trace, or kernel panic → Helix detects it, pulls Windows events from ±5 min around the timestamp, and asks the local AI for a 2–3 sentence explanation with correlated events. The "Why did my PC crash?" shortcut works without a paste.

Everything runs on your machine via [Ollama](https://ollama.com/) with `qwen2.5:3b` (~2 GB download). No cloud, no telemetry, no API keys. If Ollama isn't installed Helix still works — only the free-form AI features gracefully fall back to a message pointing you at `ollama.com`.

### Help system

Click the **💡** in the top-right of the window. Opens a two-tab modal:

- **📍 This page** — deep per-page explanation with usage steps, realistic example input/output, and a *▶ Try it* button that fires the feature live.
- **📚 All features** — the full Helix catalog, grouped by section (Getting started, Performance, Storage, Security, Problems, Automation, Launchers, Chat, History, Settings).

### Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl + K` | Command palette (fuzzy-search every page and action) |
| `?` | Keyboard-shortcut overlay |
| `Ctrl + Shift + S` | Full scan |
| `Ctrl + Shift + Q` | Quick scan |
| `Ctrl + Shift + T` | Toggle dark / light theme |
| `Alt + 1–9` | Jump to a page by index |
| `Esc` | Close any open overlay |

---

## Privacy & security

- **Fully local.** No cloud calls, no API keys, no telemetry, no analytics. Helix uses Windows APIs (WMI, Event Log, psutil, PowerShell) for data gathering and Ollama for AI inference. Nothing is sent to the internet except the single update-manifest fetch (`version.json`).
- **No destructive actions.** Every file delete goes through the Recycle Bin. Startup disables are archived (reversible). The organiser only *moves* files and logs every move for undo. The assistant can never run shell commands or edit the registry.
- **3-layer protected-apps shield.** A critical floor of Windows system processes (lsass, csrss, winlogon, …) is always protected and never removable. A shippable IT-defaults layer protects corporate tools (ESET, OPTIMA, Acall, OneDrive, Teams, Edge, Defender) — toggleable. Users can add their own extras.
- **File-search scope limited** to the user's home folder + OneDrive. Never scans the whole drive.
- **8-second timeout** on every subprocess call. No `shell=True` anywhere.
- **Executable extensions blocked** from the file opener (`.exe`, `.bat`, `.cmd`, `.ps1`, `.vbs`, `.js`, `.wsf`, `.jar`, `.msi`, `.scr`, `.pif`, `.hta`, `.cpl`, `.inf`).

---

## How updates work

1. Helix checks `helix-57o.pages.dev/version.json` ~2.5 s after launch (tiny JSON fetch, ~500 bytes).
2. If a newer version is available, the sidebar shows a glowing teal dot and Settings → About shows an **Install update** button.
3. Click **Install update** → the new exe downloads to a staging folder (your old one is untouched).
4. A tiny `.cmd` script waits for Helix to exit, swaps the new exe over the old one, and relaunches.
5. Your settings, scan history, and memory facts live in a separate SQLite file (`%LOCALAPPDATA%\PCHealthAgent\data\pchealthagent.db`) and are **never** touched by the exe swap.

No admin rights needed. Atomic — if the download fails the old version keeps running.

> **Note for v1.0.0 users:** the updater baked into v1.0.0 points at an old Vercel URL that no longer exists. One manual download of v1.1.0 is required. Every version from v1.0.1 onward self-updates fine.

---

## System requirements

- Windows 10 (any recent build) or Windows 11, **64-bit only**
- About 60 MB disk space for the app
- About 2 GB disk space if you want the AI features (Ollama + `qwen2.5:3b`)
- 4 GB RAM minimum, 8 GB+ recommended
- WebView2 runtime (ships with Windows 11; auto-installs on Windows 10 if missing)
- Optional: [Ollama](https://ollama.com/) with `qwen2.5:3b` for AI features

---

## Project structure

```
PCHealthAgent/
├── app/                      # The desktop app
│   ├── desktop_app.py        # Main entry — pywebview + AgentAPI bridge
│   ├── web/index.html        # SPA (HTML + inline CSS + inline JS)
│   ├── metadata.py           # Version, URLs, names (single source of truth)
│   ├── assistant.py          # NL intent registry + Ollama fallback
│   ├── scanner.py            # Disk/file scan
│   ├── monitor.py            # Live metrics (psutil)
│   ├── event_analyzer.py     # Windows Event Log reader
│   ├── troubleshooter.py     # Rule-based diagnostics + safe actions
│   ├── game_advisor.py       # Can-my-PC-run catalog + tier model
│   ├── user_memory.py        # Persistent fact store (SQLite)
│   ├── briefing.py           # Morning briefing generator
│   ├── error_triage.py       # Crash explainer + event correlator
│   ├── db.py                 # SQLite storage (settings, scans, memory)
│   ├── protected.py          # 3-layer protected-apps logic
│   ├── launcher.py           # Floating radial menu (Win32)
│   ├── monitor_hud.py        # Always-on-top mini-gauge window
│   └── …
├── installer/
│   └── Helix.iss             # Inno Setup script → HelixSetup.exe
├── website/                  # Cloudflare Pages (auto-deploys from main)
│   ├── index.html, styles.css
│   ├── Helix.exe             # Portable build (used by in-app updater)
│   ├── Helix.zip             # Portable download
│   ├── HelixSetup.exe        # Installer build
│   ├── HelixSetup.zip        # Installer download
│   └── version.json          # Update manifest
├── tests/                    # pytest suite (37 tests)
└── CLAUDE.md                 # Internal engineering notes
```

---

## Website

Published at **[helix-57o.pages.dev](https://helix-57o.pages.dev)** via Cloudflare Pages, free tier, no bandwidth cap. The site is git-connected — every push to `main` deploys automatically from the `website/` directory in this repo. No separate hosting account needed, no CDN config, no cache purge.

---

## License & credits

Internal tool published for distribution to end users. Built on top of:

- [pywebview](https://pywebview.flowrl.com/) + Microsoft Edge WebView2 (desktop shell)
- [psutil](https://github.com/giampaolo/psutil) (metrics)
- [Ollama](https://ollama.com/) + [qwen2.5:3b](https://ollama.com/library/qwen2.5:3b) (local AI)
- [PyInstaller](https://pyinstaller.org/) + [UPX](https://upx.github.io/) (packaging)
- [Inno Setup](https://jrsoftware.org/isinfo.php) (installer)

No third-party cloud services or APIs are used at runtime.
