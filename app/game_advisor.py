"""Helix — "Can my PC run X?" advisor.

Pure-local: a seeded dictionary of common games/apps and their requirements,
plus a comparator that scores the current PC against them.

No network calls. If a title is not in the DB, the caller falls back to
Ollama for a general-knowledge answer (still local, qwen2.5:3b).

Tier model:
  cpu_tier    1 (ancient) → 10 (bleeding edge). See _cpu_tier().
  gpu_tier    0 (none) → 10 (RTX 4090 class). See _gpu_tier().
  min_ram_gb  integer GB.
  disk_gb     install size in GB.
  gpu_required True → integrated graphics fails outright.
"""
from __future__ import annotations
import re
from typing import Any

# ── Known apps/games catalog ────────────────────────────────────────────────
# Keys are lowercase canonical names; aliases map alt spellings → canonical.
# Numbers are "min playable", not "recommended". Keep this list small and
# curated — a 3B model fills gaps for anything missing.
KNOWN_APPS: dict[str, dict[str, Any]] = {
    # ── Esports / low-spec ─────────────────────────────────────────────────
    "valorant":        {"cpu_tier": 3, "ram_gb":  4, "gpu_tier": 1, "gpu_required": False, "disk_gb": 30,  "kind": "game"},
    "league of legends":{"cpu_tier": 3, "ram_gb":  4, "gpu_tier": 1, "gpu_required": False, "disk_gb": 20,  "kind": "game"},
    "cs2":             {"cpu_tier": 4, "ram_gb":  8, "gpu_tier": 3, "gpu_required": False, "disk_gb": 85,  "kind": "game"},
    "counter-strike 2":{"cpu_tier": 4, "ram_gb":  8, "gpu_tier": 3, "gpu_required": False, "disk_gb": 85,  "kind": "game"},
    "fortnite":        {"cpu_tier": 2, "ram_gb":  8, "gpu_tier": 1, "gpu_required": False, "disk_gb": 40,  "kind": "game"},
    "minecraft":       {"cpu_tier": 3, "ram_gb":  4, "gpu_tier": 1, "gpu_required": False, "disk_gb":  4,  "kind": "game"},
    "roblox":          {"cpu_tier": 3, "ram_gb":  4, "gpu_tier": 1, "gpu_required": False, "disk_gb":  2,  "kind": "game"},
    "apex legends":    {"cpu_tier": 5, "ram_gb":  8, "gpu_tier": 4, "gpu_required": False, "disk_gb": 75,  "kind": "game"},
    "overwatch 2":     {"cpu_tier": 4, "ram_gb":  6, "gpu_tier": 1, "gpu_required": False, "disk_gb": 50,  "kind": "game"},
    "rocket league":   {"cpu_tier": 3, "ram_gb":  4, "gpu_tier": 2, "gpu_required": False, "disk_gb": 20,  "kind": "game"},
    "dota 2":          {"cpu_tier": 4, "ram_gb":  4, "gpu_tier": 2, "gpu_required": False, "disk_gb": 40,  "kind": "game"},
    "stardew valley":  {"cpu_tier": 2, "ram_gb":  2, "gpu_tier": 1, "gpu_required": False, "disk_gb":  1,  "kind": "game"},
    "hollow knight":   {"cpu_tier": 2, "ram_gb":  4, "gpu_tier": 1, "gpu_required": False, "disk_gb":  9,  "kind": "game"},
    "terraria":        {"cpu_tier": 2, "ram_gb":  2, "gpu_tier": 1, "gpu_required": False, "disk_gb":  1,  "kind": "game"},
    "among us":        {"cpu_tier": 2, "ram_gb":  2, "gpu_tier": 1, "gpu_required": False, "disk_gb":  1,  "kind": "game"},
    "palworld":        {"cpu_tier": 6, "ram_gb": 16, "gpu_tier": 5, "gpu_required": True,  "disk_gb": 40,  "kind": "game"},

    # ── AAA / heavy ────────────────────────────────────────────────────────
    "gta v":           {"cpu_tier": 3, "ram_gb":  4, "gpu_tier": 2, "gpu_required": True,  "disk_gb": 95,  "kind": "game"},
    "gta 5":           {"cpu_tier": 3, "ram_gb":  4, "gpu_tier": 2, "gpu_required": True,  "disk_gb": 95,  "kind": "game"},
    "cyberpunk 2077":  {"cpu_tier": 6, "ram_gb": 12, "gpu_tier": 6, "gpu_required": True,  "disk_gb": 70,  "kind": "game"},
    "elden ring":      {"cpu_tier": 5, "ram_gb": 12, "gpu_tier": 4, "gpu_required": True,  "disk_gb": 60,  "kind": "game"},
    "red dead redemption 2": {"cpu_tier": 5, "ram_gb":  8, "gpu_tier": 3, "gpu_required": True, "disk_gb": 150, "kind": "game"},
    "rdr2":            {"cpu_tier": 5, "ram_gb":  8, "gpu_tier": 3, "gpu_required": True,  "disk_gb": 150, "kind": "game"},
    "the witcher 3":   {"cpu_tier": 4, "ram_gb":  8, "gpu_tier": 2, "gpu_required": True,  "disk_gb": 50,  "kind": "game"},
    "baldur's gate 3": {"cpu_tier": 5, "ram_gb":  8, "gpu_tier": 4, "gpu_required": True,  "disk_gb": 150, "kind": "game"},
    "hogwarts legacy": {"cpu_tier": 5, "ram_gb": 16, "gpu_tier": 3, "gpu_required": True,  "disk_gb": 85,  "kind": "game"},
    "starfield":       {"cpu_tier": 6, "ram_gb": 16, "gpu_tier": 5, "gpu_required": True,  "disk_gb": 125, "kind": "game"},
    "monster hunter wilds": {"cpu_tier": 6, "ram_gb": 16, "gpu_tier": 6, "gpu_required": True, "disk_gb": 75, "kind": "game"},
    "black myth wukong": {"cpu_tier": 7, "ram_gb": 16, "gpu_tier": 7, "gpu_required": True, "disk_gb": 130, "kind": "game"},

    # ── Dospara GALLERIA 推奨PC program / major JP titles ─────────────────
    # Covers the Japanese gaming market Dospara (Thirdwave / GALLERIA) partners
    # with. Specs from each publisher's official minimum requirements.
    "final fantasy xiv":    {"cpu_tier": 5, "ram_gb":  8, "gpu_tier": 4, "gpu_required": False, "disk_gb": 140, "kind": "game"},
    "final fantasy xvi":    {"cpu_tier": 7, "ram_gb": 16, "gpu_tier": 6, "gpu_required": True,  "disk_gb": 170, "kind": "game"},
    "final fantasy vii rebirth": {"cpu_tier": 6, "ram_gb": 16, "gpu_tier": 6, "gpu_required": True, "disk_gb": 155, "kind": "game"},
    "monster hunter rise":  {"cpu_tier": 3, "ram_gb":  8, "gpu_tier": 3, "gpu_required": False, "disk_gb":  36, "kind": "game"},
    "monster hunter world": {"cpu_tier": 4, "ram_gb":  8, "gpu_tier": 3, "gpu_required": False, "disk_gb":  52, "kind": "game"},
    "dragon quest x":       {"cpu_tier": 2, "ram_gb":  2, "gpu_tier": 1, "gpu_required": False, "disk_gb":   8, "kind": "game"},
    "dragon quest xi":      {"cpu_tier": 3, "ram_gb":  8, "gpu_tier": 2, "gpu_required": False, "disk_gb":  32, "kind": "game"},
    "phantasy star online 2 new genesis": {"cpu_tier": 4, "ram_gb": 8, "gpu_tier": 4, "gpu_required": False, "disk_gb": 100, "kind": "game"},
    "granblue fantasy relink": {"cpu_tier": 5, "ram_gb": 8, "gpu_tier": 4, "gpu_required": False, "disk_gb": 90, "kind": "game"},
    "blue protocol":        {"cpu_tier": 5, "ram_gb":  8, "gpu_tier": 4, "gpu_required": False, "disk_gb":  40, "kind": "game"},
    "genshin impact":       {"cpu_tier": 4, "ram_gb":  8, "gpu_tier": 2, "gpu_required": False, "disk_gb":  60, "kind": "game"},
    "honkai star rail":     {"cpu_tier": 5, "ram_gb":  8, "gpu_tier": 4, "gpu_required": False, "disk_gb":  30, "kind": "game"},
    "zenless zone zero":    {"cpu_tier": 5, "ram_gb":  8, "gpu_tier": 4, "gpu_required": False, "disk_gb":  50, "kind": "game"},
    "wuthering waves":      {"cpu_tier": 5, "ram_gb": 16, "gpu_tier": 4, "gpu_required": False, "disk_gb":  45, "kind": "game"},
    "nikke":                {"cpu_tier": 3, "ram_gb":  4, "gpu_tier": 2, "gpu_required": False, "disk_gb":  10, "kind": "game"},
    "street fighter 6":     {"cpu_tier": 5, "ram_gb":  8, "gpu_tier": 4, "gpu_required": False, "disk_gb":  60, "kind": "game"},
    "tekken 8":             {"cpu_tier": 5, "ram_gb":  8, "gpu_tier": 3, "gpu_required": False, "disk_gb": 100, "kind": "game"},
    "resident evil 4 remake": {"cpu_tier": 5, "ram_gb": 8, "gpu_tier": 3, "gpu_required": False, "disk_gb": 60, "kind": "game"},
    "resident evil village":{"cpu_tier": 5, "ram_gb": 8, "gpu_tier": 4, "gpu_required": False, "disk_gb":  50, "kind": "game"},
    "devil may cry 5":      {"cpu_tier": 4, "ram_gb":  8, "gpu_tier": 2, "gpu_required": False, "disk_gb":  35, "kind": "game"},
    "dragons dogma 2":      {"cpu_tier": 5, "ram_gb": 16, "gpu_tier": 5, "gpu_required": True,  "disk_gb":  65, "kind": "game"},
    "like a dragon infinite wealth": {"cpu_tier": 4, "ram_gb": 8, "gpu_tier": 3, "gpu_required": False, "disk_gb": 86, "kind": "game"},
    "yakuza 0":             {"cpu_tier": 3, "ram_gb":  4, "gpu_tier": 2, "gpu_required": False, "disk_gb":  30, "kind": "game"},
    "persona 5 royal":      {"cpu_tier": 3, "ram_gb":  8, "gpu_tier": 2, "gpu_required": False, "disk_gb":  40, "kind": "game"},
    "persona 3 reload":     {"cpu_tier": 4, "ram_gb":  8, "gpu_tier": 3, "gpu_required": False, "disk_gb":  30, "kind": "game"},
    "metaphor refantazio":  {"cpu_tier": 5, "ram_gb":  8, "gpu_tier": 4, "gpu_required": False, "disk_gb":  75, "kind": "game"},
    "nier automata":        {"cpu_tier": 3, "ram_gb":  4, "gpu_tier": 2, "gpu_required": False, "disk_gb":  50, "kind": "game"},
    "nier replicant":       {"cpu_tier": 3, "ram_gb":  8, "gpu_tier": 2, "gpu_required": False, "disk_gb":  26, "kind": "game"},
    "atelier ryza 3":       {"cpu_tier": 4, "ram_gb":  8, "gpu_tier": 3, "gpu_required": False, "disk_gb":  30, "kind": "game"},
    "armored core vi":      {"cpu_tier": 5, "ram_gb": 12, "gpu_tier": 4, "gpu_required": True,  "disk_gb":  60, "kind": "game"},
    "dark souls iii":       {"cpu_tier": 4, "ram_gb":  8, "gpu_tier": 3, "gpu_required": False, "disk_gb":  25, "kind": "game"},
    "sekiro":               {"cpu_tier": 4, "ram_gb":  4, "gpu_tier": 3, "gpu_required": False, "disk_gb":  25, "kind": "game"},

    # ── Dev / creative apps ────────────────────────────────────────────────
    "visual studio code": {"cpu_tier": 2, "ram_gb": 4, "gpu_tier": 1, "gpu_required": False, "disk_gb": 1, "kind": "app"},
    "vscode":          {"cpu_tier": 2, "ram_gb":  4, "gpu_tier": 1, "gpu_required": False, "disk_gb":  1,  "kind": "app"},
    "visual studio":   {"cpu_tier": 4, "ram_gb":  8, "gpu_tier": 1, "gpu_required": False, "disk_gb": 30,  "kind": "app"},
    "android studio":  {"cpu_tier": 5, "ram_gb": 16, "gpu_tier": 2, "gpu_required": False, "disk_gb": 20,  "kind": "app"},
    "docker desktop":  {"cpu_tier": 4, "ram_gb":  8, "gpu_tier": 1, "gpu_required": False, "disk_gb":  5,  "kind": "app"},
    "unity":           {"cpu_tier": 5, "ram_gb":  8, "gpu_tier": 4, "gpu_required": False, "disk_gb": 10,  "kind": "app"},
    "unreal engine":   {"cpu_tier": 6, "ram_gb": 16, "gpu_tier": 6, "gpu_required": True,  "disk_gb": 100, "kind": "app"},
    "blender":         {"cpu_tier": 4, "ram_gb":  8, "gpu_tier": 3, "gpu_required": False, "disk_gb":  3,  "kind": "app"},
    "davinci resolve": {"cpu_tier": 5, "ram_gb": 16, "gpu_tier": 5, "gpu_required": True,  "disk_gb":  5,  "kind": "app"},
    "premiere pro":    {"cpu_tier": 5, "ram_gb": 16, "gpu_tier": 4, "gpu_required": True,  "disk_gb":  8,  "kind": "app"},
    "photoshop":       {"cpu_tier": 3, "ram_gb":  8, "gpu_tier": 2, "gpu_required": False, "disk_gb":  4,  "kind": "app"},
    "figma":           {"cpu_tier": 2, "ram_gb":  4, "gpu_tier": 1, "gpu_required": False, "disk_gb":  1,  "kind": "app"},
    "obs studio":      {"cpu_tier": 4, "ram_gb":  4, "gpu_tier": 2, "gpu_required": False, "disk_gb":  1,  "kind": "app"},
}

_ALIASES = {
    "valo":            "valorant",
    "lol":             "league of legends",
    "league":          "league of legends",
    "counterstrike":   "cs2",
    "csgo":            "cs2",
    "cs:go":           "cs2",
    "gta":             "gta v",
    "gtav":            "gta v",
    "witcher 3":       "the witcher 3",
    "witcher3":        "the witcher 3",
    "bg3":             "baldur's gate 3",
    "baldurs gate 3":  "baldur's gate 3",
    "cp2077":          "cyberpunk 2077",
    "cyberpunk":       "cyberpunk 2077",
    "rdr 2":           "red dead redemption 2",
    "code":            "visual studio code",
    "vs code":         "visual studio code",
    "wukong":          "black myth wukong",
    "pal world":       "palworld",
    "ue5":             "unreal engine",
    "ue4":             "unreal engine",
    "resolve":         "davinci resolve",
    "ps":              "photoshop",
    "obs":             "obs studio",

    # ── JP titles / Dospara 推奨PC aliases ──
    "ff14":            "final fantasy xiv",
    "ffxiv":           "final fantasy xiv",
    "final fantasy 14":"final fantasy xiv",
    "ff16":            "final fantasy xvi",
    "ffxvi":           "final fantasy xvi",
    "final fantasy 16":"final fantasy xvi",
    "ff7 rebirth":     "final fantasy vii rebirth",
    "ffvii rebirth":   "final fantasy vii rebirth",
    "ff7r":            "final fantasy vii rebirth",
    "mhr":             "monster hunter rise",
    "mhrise":          "monster hunter rise",
    "mh rise":         "monster hunter rise",
    "mhw":             "monster hunter world",
    "mh world":        "monster hunter world",
    "mh wilds":        "monster hunter wilds",
    "monhun wilds":    "monster hunter wilds",
    "dqx":             "dragon quest x",
    "dragon quest 10": "dragon quest x",
    "dqxi":            "dragon quest xi",
    "dq xi":           "dragon quest xi",
    "dq 11":           "dragon quest xi",
    "dq11":            "dragon quest xi",
    "dragon quest 11": "dragon quest xi",
    "pso2":            "phantasy star online 2 new genesis",
    "pso2 ngs":        "phantasy star online 2 new genesis",
    "ngs":             "phantasy star online 2 new genesis",
    "gbfr":            "granblue fantasy relink",
    "granblue relink": "granblue fantasy relink",
    "relink":          "granblue fantasy relink",
    "bp":              "blue protocol",
    "genshin":         "genshin impact",
    "hsr":             "honkai star rail",
    "star rail":       "honkai star rail",
    "zzz":             "zenless zone zero",
    "zenless":         "zenless zone zero",
    "wuwa":            "wuthering waves",
    "wuthering":       "wuthering waves",
    "goddess of victory nikke": "nikke",
    "sf6":             "street fighter 6",
    "street fighter":  "street fighter 6",
    "tekken":          "tekken 8",
    "tekken8":         "tekken 8",
    "re4":             "resident evil 4 remake",
    "re4 remake":      "resident evil 4 remake",
    "re village":      "resident evil village",
    "re8":             "resident evil village",
    "dmc5":            "devil may cry 5",
    "dmcv":            "devil may cry 5",
    "dd2":             "dragons dogma 2",
    "dragon's dogma 2":"dragons dogma 2",
    "like a dragon":   "like a dragon infinite wealth",
    "like a dragon 8": "like a dragon infinite wealth",
    "yakuza 8":        "like a dragon infinite wealth",
    "lad8":            "like a dragon infinite wealth",
    "infinite wealth": "like a dragon infinite wealth",
    "p5r":             "persona 5 royal",
    "persona 5":       "persona 5 royal",
    "p3r":             "persona 3 reload",
    "persona 3":       "persona 3 reload",
    "metaphor":        "metaphor refantazio",
    "nier":            "nier automata",
    "automata":        "nier automata",
    "ryza 3":          "atelier ryza 3",
    "atelier ryza":    "atelier ryza 3",
    "ac6":             "armored core vi",
    "armored core 6":  "armored core vi",
    "ds3":             "dark souls iii",
    "dark souls 3":    "dark souls iii",
}


def extract_name(message: str) -> str:
    """Pull the title out of a user message like 'can my pc run valorant?'."""
    m = (message or "").strip()
    patterns = [
        r'(?:can|will|would|does)\s+(?:my|this)\s+(?:pc|computer|laptop|machine|system)\s+'
        r'(?:run|handle|play|support)\s+(.+?)[\s!?.]*$',
        r'(?:am\s+i|is\s+(?:my\s+)?(?:pc|laptop|computer))\s+(?:able|good|ok|okay|fine|ready)\s+to\s+'
        r'(?:run|play|install)\s+(.+?)[\s!?.]*$',
        r"(?:i(?:'m|m|\s+am)?)\s+(?:going\s+to|about\s+to|planning\s+to|wanna|want\s+to)\s+"
        r'(?:install|download|play|try)\s+(.+?)[\s,!?.]*(?:is\s+(?:this|it|that).*)?$',
        r'(?:spec|specs|requirements?)\s+for\s+(.+?)[\s!?.]*$',
        r'(?:will|would|does|can)\s+(.+?)\s+(?:run|work|play)\s+on\s+(?:my|this)\s+'
        r'(?:pc|computer|laptop|machine|system)[\s!?.]*$',
    ]
    low = m.lower()
    for p in patterns:
        mo = re.search(p, low)
        if mo:
            return mo.group(1).strip(" '\".,?!")
    return m.strip(" '\".,?!")


def resolve_name(query: str) -> str | None:
    """Normalize a user-typed name to a KNOWN_APPS key, or None."""
    q = (query or "").strip().lower()
    q = re.sub(r"[^\w\s':\-]", "", q)
    q = re.sub(r"\s+", " ", q).strip()
    if not q:
        return None
    if q in KNOWN_APPS:
        return q
    if q in _ALIASES:
        return _ALIASES[q]
    # Loose contains-match — only if query carries at least one 4-char alpha
    # word (prevents pure-digit matches like "2077" -> "cyberpunk 2077").
    if len(q) >= 4 and re.search(r"[a-z]{4,}", q):
        for key in KNOWN_APPS:
            if q in key or key in q:
                return key
    return None


# ── CPU / GPU tiering ──────────────────────────────────────────────────────

def _cpu_tier(cpu_name: str) -> int:
    """Rough score 1 (ancient) → 10 (top). Based on Intel/AMD family + gen."""
    n = (cpu_name or "").lower()
    if not n:
        return 4

    # Intel Core
    m = re.search(r"i([3579])[\-\s]*(\d{3,5})", n)
    if m:
        tier = int(m.group(1))
        gen_num = m.group(2)
        if len(gen_num) >= 5:
            gen = int(gen_num[:2])
        elif len(gen_num) == 4 and gen_num[0] == "1":
            gen = int(gen_num[:2])
        else:
            gen = int(gen_num[0])
        is_u = ("u" in n.split(gen_num)[-1][:4]) or " u " in n or n.endswith("u")
        # i3=3, i5=5, i7=7, i9=9 … but modern i5 ≈ older i7
        base = {3: 3, 5: 5, 7: 7, 9: 9}[tier]
        if gen >= 12: base += 1
        if gen <= 7:  base -= 1
        if is_u:      base -= 1    # U-series mobile: lower sustained power
        return max(1, min(10, base))

    # AMD Ryzen
    m = re.search(r"ryzen\s*([3579])\s*(\d{3,4})", n)
    if m:
        tier = int(m.group(1))
        gen = int(m.group(2)[0])
        base = {3: 4, 5: 6, 7: 7, 9: 9}[tier]
        if gen >= 5: base += 1
        if gen <= 2: base -= 1
        return max(1, min(10, base))

    # Pentium / Celeron / Atom
    if any(x in n for x in ("celeron", "atom", "pentium")):
        return 2
    # Xeon / Threadripper
    if any(x in n for x in ("xeon", "threadripper")):
        return 8
    return 4


def _gpu_tier(gpu_name: str) -> tuple[int, bool]:
    """Return (tier 0-10, is_discrete). Integrated GPUs cap at ~3."""
    n = (gpu_name or "").lower()
    if not n:
        return (0, False)
    n = re.sub(r"\(r\)|\(tm\)", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    integrated_marks = ("intel", "iris", "uhd", "hd graphics", "vega 7", "vega 8",
                        "vega 10", "radeon graphics", "amd graphics", "radeon vega")
    is_integrated = any(m in n for m in integrated_marks)

    # NVIDIA
    m = re.search(r"(rtx|gtx)\s*(\d{3,4})", n)
    if m:
        kind, num = m.group(1), int(m.group(2))
        if kind == "rtx":
            if num >= 4080: return (10, True)
            if num >= 4070: return (9, True)
            if num >= 3070 or num >= 4060: return (8, True)
            if num >= 3060 or num >= 2070: return (7, True)
            if num >= 2060 or num >= 3050: return (6, True)
            return (5, True)
        else:  # gtx
            if num >= 1660: return (5, True)
            if num >= 1060: return (4, True)
            if num >= 1050: return (3, True)
            return (2, True)

    # AMD Radeon (not integrated Vega)
    m = re.search(r"(rx)\s*(\d{3,4})", n)
    if m:
        num = int(m.group(2))
        if num >= 7800: return (10, True)
        if num >= 7700 or num >= 6800: return (9, True)
        if num >= 6700 or num >= 7600: return (8, True)
        if num >= 6600 or num >= 5700: return (7, True)
        if num >= 5600 or num >= 6500: return (6, True)
        return (4, True)

    if is_integrated:
        if "iris xe" in n: return (3, False)
        if "uhd" in n or "hd graphics" in n: return (1, False)
        if "vega" in n or "radeon graphics" in n: return (2, False)
        return (2, False)

    return (3, False)


# ── Current PC snapshot ────────────────────────────────────────────────────

_SPECS_CACHE: dict[str, Any] = {}
_SPECS_CACHE_AT: float = 0.0
_SPECS_TTL = 300.0  # 5 min — hardware doesn't change, disk/ram re-polled below


def get_pc_specs(force: bool = False) -> dict[str, Any]:
    """Snapshot the current PC in advisor-shaped terms.

    Cached for 5 min to avoid the ~6 s double-WMI penalty on every chat query.
    RAM/disk are re-polled live each call (they drift), only CPU/GPU names are cached.
    """
    import time as _t
    import psutil
    global _SPECS_CACHE, _SPECS_CACHE_AT
    now = _t.time()
    cached = _SPECS_CACHE if (not force and _SPECS_CACHE and (now - _SPECS_CACHE_AT) < _SPECS_TTL) else None

    specs = {
        "cpu_name": "", "cpu_cores": 0, "cpu_tier": 0,
        "gpu_name": "", "gpu_tier": 0, "gpu_discrete": False,
        "ram_gb": 0, "disk_free_gb": 0, "disk_total_gb": 0,
    }

    vm = psutil.virtual_memory()
    specs["ram_gb"] = round(vm.total / (1 << 30))

    try:
        du = psutil.disk_usage("C:\\")
        specs["disk_free_gb"]  = round(du.free  / (1 << 30))
        specs["disk_total_gb"] = round(du.total / (1 << 30))
    except Exception:
        pass

    if cached:
        specs["cpu_name"]    = cached["cpu_name"]
        specs["cpu_cores"]   = cached["cpu_cores"]
        specs["cpu_tier"]    = cached["cpu_tier"]
        specs["gpu_name"]    = cached["gpu_name"]
        specs["gpu_tier"]    = cached["gpu_tier"]
        specs["gpu_discrete"]= cached["gpu_discrete"]
        return specs

    try:
        import subprocess
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=6,
        )
        specs["cpu_name"] = (r.stdout or "").strip()
    except Exception:
        pass
    specs["cpu_cores"] = psutil.cpu_count(logical=False) or 0
    specs["cpu_tier"] = _cpu_tier(specs["cpu_name"])

    try:
        import subprocess
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_VideoController).Name"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=6,
        )
        gpus = [g.strip() for g in (r.stdout or "").splitlines() if g.strip()]
        best = (0, False, "")
        for g in gpus:
            t, d = _gpu_tier(g)
            if t > best[0]:
                best = (t, d, g)
        if best[2]:
            specs["gpu_name"] = best[2]
            specs["gpu_tier"] = best[0]
            specs["gpu_discrete"] = best[1]
    except Exception:
        pass

    _SPECS_CACHE = {
        "cpu_name": specs["cpu_name"], "cpu_cores": specs["cpu_cores"], "cpu_tier": specs["cpu_tier"],
        "gpu_name": specs["gpu_name"], "gpu_tier": specs["gpu_tier"], "gpu_discrete": specs["gpu_discrete"],
    }
    _SPECS_CACHE_AT = now
    return specs


# ── Comparison ──────────────────────────────────────────────────────────────

def check_pc_for(query: str, specs: dict | None = None) -> dict[str, Any]:
    """Compare the current PC against an app's requirements.

    Returns:
      {found, name, kind, verdict, bottleneck, reasons[], specs, req}
      verdict ∈ "pass"/"borderline"/"fail"/"unknown"
    """
    specs = specs or get_pc_specs()
    key = resolve_name(query)
    if not key:
        return {"found": False, "query": query, "specs": specs}

    req = KNOWN_APPS[key]
    reasons: list[str] = []
    fails:  list[tuple[str, str]] = []
    marg:   list[tuple[str, str]] = []

    # CPU — equal to min = pass with "low settings" note (not borderline).
    if specs["cpu_tier"] < req["cpu_tier"]:
        fails.append(("cpu", f"CPU tier {specs['cpu_tier']} vs required {req['cpu_tier']} (your {specs['cpu_name'] or 'CPU'})"))
    elif specs["cpu_tier"] == req["cpu_tier"]:
        reasons.append("CPU meets minimum — expect low/medium settings")
    else:
        reasons.append("CPU is comfortably above the minimum")

    # RAM — tight headroom IS borderline (background apps eat RAM).
    if specs["ram_gb"] < req["ram_gb"]:
        fails.append(("ram", f"{specs['ram_gb']} GB RAM vs required {req['ram_gb']} GB"))
    elif specs["ram_gb"] < req["ram_gb"] + 2:
        marg.append(("ram", f"{specs['ram_gb']} GB RAM — tight, close background apps"))
    else:
        reasons.append(f"RAM headroom OK ({specs['ram_gb']} GB)")

    # GPU — equal to min = pass with "low settings"; discrete-required is a hard fail.
    if req["gpu_required"] and not specs["gpu_discrete"]:
        fails.append(("gpu", f"needs a discrete GPU — your {specs['gpu_name'] or 'GPU'} is integrated"))
    elif specs["gpu_tier"] < req["gpu_tier"]:
        fails.append(("gpu", f"GPU tier {specs['gpu_tier']} vs required {req['gpu_tier']} (your {specs['gpu_name'] or 'GPU'})"))
    elif specs["gpu_tier"] == req["gpu_tier"]:
        reasons.append("GPU meets minimum — expect low settings")
    else:
        reasons.append(f"GPU OK ({specs['gpu_name'] or '-'})")

    # Disk
    if specs["disk_free_gb"] < req["disk_gb"]:
        fails.append(("disk", f"{specs['disk_free_gb']} GB free vs {req['disk_gb']} GB install size"))
    else:
        reasons.append(f"Disk OK ({specs['disk_free_gb']} GB free, needs {req['disk_gb']} GB)")

    if fails:
        verdict = "fail"
        bottleneck = fails[0][0]
    elif marg:
        verdict = "borderline"
        bottleneck = marg[0][0]
    else:
        verdict = "pass"
        bottleneck = None

    return {
        "found": True, "query": query, "name": key, "kind": req["kind"],
        "verdict": verdict, "bottleneck": bottleneck,
        "fails": [f[1] for f in fails],
        "margins": [m[1] for m in marg],
        "ok": reasons,
        "specs": specs, "req": req,
    }


def summary_text(result: dict) -> str:
    """Plain-text one-shot summary for when Ollama is offline."""
    if not result.get("found"):
        s = result["specs"]
        return (f"I don't have **{result['query']}** in my local catalog. "
                f"Your PC: {s['cpu_name'] or 'CPU unknown'}, {s['ram_gb']} GB RAM, "
                f"{s['gpu_name'] or 'GPU unknown'}, {s['disk_free_gb']} GB free. "
                f"If you can share the game's minimum spec I can compare.")

    v = result["verdict"]
    name = result["name"].title()
    head = {"pass": f"✅ Your PC can run **{name}**.",
            "borderline": f"⚠️ **{name}** will run but it's tight.",
            "fail": f"❌ **{name}** probably won't run well on this PC."}[v]

    lines = [head]
    if result["fails"]:    lines += [f"• {m}" for m in result["fails"]]
    if result["margins"]:  lines += [f"• {m}" for m in result["margins"]]
    if v == "pass" and result["ok"]:
        lines += [f"• {m}" for m in result["ok"][:2]]
    return "\n".join(lines)
