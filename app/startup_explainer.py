"""Plain-English explainer for Windows Run-key startup entries.

Two layers:
  1. Local catalog — fast, grounded facts about ~70 common startup
     publishers (audio drivers, cloud sync, chat clients, GPU control
     panels, launchers, update agents). Catalog hit returns immediately
     with role + verdict and never calls Ollama.
  2. Ollama fallback — unknown entries get a one-line summary from the
     local 3B model given the name and command. Guarded against the
     user disabling AI entirely.

Narrate-only. The explainer never disables anything; `disable_startup`
in desktop_app.py remains the only path that mutates the registry.

Verdict strings:
  'keep'               — needed at boot (audio, input, GPU control)
  'optional'           — convenience, user choice
  'safe-to-disable'    — launcher/updater/background sync that can be
                         started on-demand without breaking anything
  'unknown'            — catalog miss and Ollama offline/disabled

Role strings are short noun phrases (<= 70 chars), suitable for a one-
line table cell.
"""
from __future__ import annotations

from typing import Dict

# ── Catalog: substrings matched against lowercased name + command ────
# Each entry is (role, verdict). Order matters only for display stability;
# lookup is first-hit. Keep entries narrow — a generic "update" substring
# would collide with legitimate apps.
_CATALOG: list[tuple[str, str, str]] = [
    # (substring, role, verdict)

    # Audio / input drivers (keep)
    ("realtek", "Realtek audio driver — needed for your speakers", "keep"),
    ("rtkngui", "Realtek audio control panel", "keep"),
    ("intel(r) graphics", "Intel GPU control panel", "keep"),
    ("igfxtray", "Intel graphics tray helper", "keep"),
    ("hkcmd", "Intel graphics hotkey handler", "keep"),
    ("nvbackend", "NVIDIA GeForce Experience background", "optional"),
    ("nvidia share", "NVIDIA ShadowPlay overlay (recording / screenshots)", "optional"),
    ("nvcontainer", "NVIDIA driver service helper", "keep"),
    ("amd software", "AMD GPU control panel (Radeon Software)", "keep"),
    ("synaptics", "Synaptics touchpad driver", "keep"),
    ("elan", "ELAN touchpad driver", "keep"),

    # Microsoft / Windows core (mixed)
    ("securityhealth", "Windows Security tray icon", "keep"),
    ("onedrive", "OneDrive file sync — needed if you store files there", "optional"),
    ("microsoft teams", "Microsoft Teams auto-start", "safe-to-disable"),
    ("teams.exe", "Microsoft Teams auto-start", "safe-to-disable"),
    ("microsoftedgeupdate", "Microsoft Edge updater", "safe-to-disable"),
    ("edgeupdate", "Microsoft Edge updater", "safe-to-disable"),
    ("msedge", "Microsoft Edge background tab/preload", "safe-to-disable"),
    ("skype", "Skype auto-start", "safe-to-disable"),
    ("office", "Microsoft Office auto-launch helper", "safe-to-disable"),
    ("outlook", "Outlook auto-start — mail notifications depend on it", "optional"),

    # Cloud sync
    ("dropbox", "Dropbox file sync", "optional"),
    ("google drive", "Google Drive file sync", "optional"),
    ("googledrive", "Google Drive file sync", "optional"),
    ("box.exe", "Box.com file sync", "optional"),

    # Chat / collab
    ("slack", "Slack auto-start", "safe-to-disable"),
    ("discord", "Discord auto-start", "safe-to-disable"),
    ("zoom", "Zoom auto-start — meeting notifications depend on it", "optional"),
    ("line.exe", "LINE chat auto-start", "safe-to-disable"),
    ("whatsapp", "WhatsApp Desktop auto-start", "safe-to-disable"),

    # Game launchers
    ("steam", "Steam game launcher — needed if you play games here", "optional"),
    ("epicgames", "Epic Games launcher", "safe-to-disable"),
    ("ea desktop", "EA app (Origin) launcher", "safe-to-disable"),
    ("ubisoft", "Ubisoft Connect launcher", "safe-to-disable"),
    ("gog galaxy", "GOG Galaxy launcher", "safe-to-disable"),
    ("battle.net", "Blizzard Battle.net launcher", "safe-to-disable"),
    ("riotclient", "Riot Games client auto-start", "safe-to-disable"),

    # Media
    ("spotify", "Spotify auto-start", "safe-to-disable"),
    ("itunes", "iTunes helper", "safe-to-disable"),
    ("apple mobile device", "Apple device sync for iTunes", "optional"),
    ("vlc", "VLC media player background", "safe-to-disable"),

    # Dev tools
    ("docker desktop", "Docker Desktop — Linux VM used by containers", "optional"),
    ("github desktop", "GitHub Desktop auto-launch", "safe-to-disable"),
    ("code.exe", "VS Code auto-launch", "safe-to-disable"),

    # Security / corporate (keep by default — IT-managed)
    ("eset", "ESET antivirus", "keep"),
    ("ekrn", "ESET kernel service", "keep"),
    ("egui", "ESET user interface", "keep"),
    ("optima", "OPTIMA Eye endpoint management (IT-managed)", "keep"),
    ("acall", "Acall Desktop (IT-managed)", "keep"),
    ("mcafee", "McAfee antivirus", "keep"),
    ("norton", "Norton antivirus", "keep"),
    ("avast", "Avast antivirus", "keep"),
    ("kaspersky", "Kaspersky antivirus", "keep"),
    ("avg", "AVG antivirus", "keep"),
    ("bitdefender", "Bitdefender antivirus", "keep"),

    # Adobe
    ("adobe creative cloud", "Adobe Creative Cloud sync + app launcher", "optional"),
    ("adobe gc", "Adobe license manager", "safe-to-disable"),
    ("acrobat", "Adobe Acrobat updater / helper", "safe-to-disable"),

    # Printer / scanner (varies)
    ("hp.scheduler", "HP printer status monitor", "safe-to-disable"),
    ("canon", "Canon printer helper", "safe-to-disable"),
    ("epson", "Epson printer helper", "safe-to-disable"),
    ("brother", "Brother printer helper", "safe-to-disable"),

    # Updaters (safe-to-disable almost always)
    ("google update", "Google Chrome updater", "safe-to-disable"),
    ("chrome", "Google Chrome background preload", "safe-to-disable"),
    ("firefox", "Firefox auto-launch", "safe-to-disable"),
    ("brave", "Brave browser auto-launch", "safe-to-disable"),
    ("opera", "Opera auto-launch", "safe-to-disable"),
    ("spotify-updater", "Spotify updater", "safe-to-disable"),
    ("javaupdate", "Java updater", "safe-to-disable"),

    # Misc utilities
    ("powertoys", "Microsoft PowerToys", "optional"),
    ("rainmeter", "Rainmeter desktop widgets", "optional"),
    ("f.lux", "f.lux screen-color scheduler", "optional"),
    ("notion", "Notion auto-start", "safe-to-disable"),
    ("1password", "1Password desktop auto-start", "optional"),
    ("bitwarden", "Bitwarden desktop auto-start", "optional"),
    ("logitech", "Logitech Options device software", "optional"),
    ("logi options", "Logitech Options device software", "optional"),
    ("razer", "Razer device software (Synapse)", "optional"),
]


def _match(name: str, cmd: str) -> tuple[str, str, str] | None:
    """Return (role, verdict, matched_substring) or None."""
    hay = ((name or "") + " " + (cmd or "")).lower()
    for needle, role, verdict in _CATALOG:
        if needle in hay:
            return role, verdict, needle
    return None


def explain(name: str, cmd: str, *, narrate: bool = True) -> Dict[str, str]:
    """Return `{role, verdict, source}` for a startup entry.

    `narrate=False` skips the Ollama fallback (used for bulk/scan calls
    where the UI will decide what to narrate).
    """
    hit = _match(name or "", cmd or "")
    if hit:
        role, verdict, _ = hit
        return {"role": role, "verdict": verdict, "source": "catalog"}

    if not narrate:
        return {"role": "", "verdict": "unknown", "source": "unknown"}

    # Fallback: ask the local model for a one-liner. Keep the prompt
    # tiny — qwen2.5:3b is ~37 tok/s on input.
    try:
        from app import assistant as _asst  # noqa
        prompt = (
            "You are describing a Windows startup entry in ONE short sentence "
            "(under 15 words). State only what the program does, not whether "
            "to keep or disable it. If unsure, say 'Purpose unclear'.\n"
            f"Name: {name}\nCommand: {cmd}\nDescription:"
        )
        reply = _asst.ollama_chat(prompt, context="startup_explain")
        if reply and isinstance(reply, str):
            role = reply.strip().splitlines()[0][:120]
            return {"role": role, "verdict": "unknown", "source": "ollama"}
    except Exception:
        pass

    return {"role": "", "verdict": "unknown", "source": "unknown"}
