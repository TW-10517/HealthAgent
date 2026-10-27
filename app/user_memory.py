"""Helix — User memory layer.

Persists short, useful facts the user has shared (or the app has observed)
so Ollama replies can get personal over time. Backed by `db.user_memory`.

Design:
- Facts are short English sentences. Pre-categorized (games/hardware/apps/
  habits/preferences). Confidence is 0.0–1.0, explicit facts start at 0.9,
  observed facts at 0.4–0.5.
- `recall_for_prompt()` returns at most N facts, newest+highest-confidence
  first, formatted as a tiny bullet block that fits inside the 3B model's
  tight prompt budget.
- Observation hook: `note_game_interest(name)` is fired from the advisor
  turn so Helix starts remembering what games the user asks about.
"""
from __future__ import annotations
import re
from typing import Iterable

import db as _db

# ── Category heuristics (used when caller doesn't name one) ─────────────────
_CAT_PATTERNS = [
    ("games",       r"\b(play|game|gaming|fps|rpg|mmo|moba|valorant|cyberpunk|minecraft|elden|monster hunter|ff\d+|dmc\d*|zelda|mario)\b"),
    ("hardware",    r"\b(cpu|gpu|ram|ssd|hdd|disk|graphics|processor|intel|amd|nvidia|iris xe|radeon|geforce|monitor|battery)\b"),
    ("apps",        r"\b(uses?|installs?|chrome|edge|firefox|teams|slack|discord|vs code|vscode|photoshop|blender|office|excel)\b"),
    ("habits",      r"\b(every (day|morning|night|week)|usually|often|always|at \d|at night|in the morning)\b"),
    ("preferences", r"\b(prefers?|likes?|loves?|hates?|dislikes?|wants?|wishes?|would rather|rather have)\b"),
    ("role",        r"\b(developer|engineer|designer|student|teacher|writer|artist|gamer|data scientist|work for|job)\b"),
]

def _categorize(fact: str) -> str | None:
    s = (fact or "").lower()
    for cat, pat in _CAT_PATTERNS:
        if re.search(pat, s):
            return cat
    return None


# ── Public API ───────────────────────────────────────────────────────────────

def remember(fact: str, *, category: str = None, source: str = "observed",
             confidence: float = None) -> int:
    """Save or refresh a fact. Returns the row id (0 on empty)."""
    fact = (fact or "").strip().rstrip(".").strip()
    if not fact:
        return 0
    # Cap length — don't let a one-shot megaphrase bloat the table.
    if len(fact) > 200:
        fact = fact[:200].rstrip() + "…"
    if category is None:
        category = _categorize(fact)
    if confidence is None:
        confidence = 0.9 if source == "explicit" else 0.45
    return _db.remember_fact(fact, category=category, source=source,
                             confidence=confidence)

def forget(fact_or_id) -> int:
    return _db.forget_fact(fact_or_id)

def list_all(limit: int = 50, category: str = None,
             min_confidence: float = 0.0) -> list[dict]:
    return _db.list_memory(limit=limit, category=category,
                           min_confidence=min_confidence)

def clear(category: str = None) -> int:
    return _db.clear_memory(category=category)

def count() -> int:
    return _db.memory_count()


def recall_for_prompt(max_items: int = 6, max_chars: int = 400,
                      min_confidence: float = 0.4) -> str:
    """Return a compact bullet block for the Ollama system prompt.

    Empty string if memory is empty or disabled — caller can safely prepend.
    """
    if _db.get_setting("memory_enabled") != "true":
        return ""
    rows = _db.list_memory(limit=max_items * 2, min_confidence=min_confidence)
    if not rows:
        return ""
    # Rank: explicit > observed; higher confidence first; break ties by recency
    rows.sort(key=lambda r: (
        0 if r["source"] == "explicit" else 1,
        -float(r["confidence"]),
        r["last_seen"],
    ))
    rows = rows[:max_items]
    lines = []
    used = 0
    for r in rows:
        line = f"- {r['fact']}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return "What you know about this user:\n" + "\n".join(lines)


# ── Observation hooks (fired from desktop_app) ──────────────────────────────

def note_game_interest(name: str, verdict: str = None) -> None:
    """Called after an advisor turn. Records that the user asked about <name>."""
    if not name:
        return
    name = name.strip().lower()
    if not name:
        return
    fact = f"user asked about running {name}"
    remember(fact, category="games", source="observed", confidence=0.5)
    # Also note the verdict as a lower-confidence fact — useful for recall:
    # "last time we checked X it didn't meet the min specs".
    if verdict in ("pass", "fail", "borderline"):
        remember(f"{name} verdict on this pc: {verdict}",
                 category="games", source="observed", confidence=0.4)


# ── Explicit "remember that …" extraction ───────────────────────────────────

_REMEMBER_PATTERNS = [
    # (pattern, template) — if template is set, it formats group(1) into a
    # third-person fact; else the whole captured body is normalized below.
    (re.compile(r"^\s*(?:please\s+)?remember\s+(?:that\s+)?(.+?)[\s!?.]*$", re.I), None),
    (re.compile(r"^\s*note\s+(?:that\s+)?(.+?)[\s!?.]*$", re.I), None),
    (re.compile(
        r"^\s*(?:i\s*am|i'?m|im)\s+(?:an?\s+)?"
        r"(developer|engineer|designer|student|teacher|writer|artist|gamer|data\s+scientist)[\s!?.]*$",
        re.I), "user is a {0}"),
    (re.compile(r"^\s*i\s+play\s+(.+?)[\s!?.]*$", re.I), "user plays {0}"),
    (re.compile(r"^\s*i\s+use\s+(.+?)\s+(?:daily|every day|a lot|often)[\s!?.]*$", re.I),
     "user uses {0} regularly"),
]

_MIN_FACT_BODY = 3   # characters; shorter bodies are almost always noise
                     # ("that", "a", "it", "ok") and would clutter recall.

def extract_explicit_fact(message: str) -> str | None:
    """Pull the actual fact text out of an explicit remember-style phrasing.

    Returns a third-person normalized string ("user plays valorant") so that
    it reads naturally when injected into the Ollama system prompt via
    `recall_for_prompt()`. Returns None if the captured body is too short to
    be a useful fact (e.g. "remember that" alone captures just "that").
    """
    msg = (message or "").strip()
    for pat, template in _REMEMBER_PATTERNS:
        m = pat.match(msg)
        if not m:
            continue
        body = m.group(1).strip()
        if len(body) < _MIN_FACT_BODY:
            return None
        if body.lower() in {"that", "it", "this", "a", "an"}:
            return None
        if template:
            return template.format(body.lower())
        # Freeform body (from "remember that …" / "note that …"): normalize
        # first-person to third-person so it reads cleanly.
        lower = body.lower()
        if lower.startswith("i'm ") or lower.startswith("im "):
            body = "user is " + re.sub(r"^(i'?m|im)\s+", "", body, flags=re.I)
        elif lower.startswith("i am "):
            body = "user is " + body[5:]
        elif lower.startswith("i "):
            # "I play X" → "user plays X" — simple verb-s where it reads better.
            rest = body[2:]
            verb_match = re.match(r"(\w+)(\s+.*)?$", rest)
            if verb_match:
                verb, tail = verb_match.group(1), verb_match.group(2) or ""
                # Add -s to the verb if it ends in a consonant (naive but
                # fine for the common cases: play→plays, use→uses, run→runs).
                if verb.endswith(("s", "x", "z")):
                    pass  # already plural-ish
                elif verb.endswith("y") and not verb.endswith(("ay","ey","iy","oy","uy")):
                    verb = verb[:-1] + "ies"
                elif verb.endswith("e"):
                    verb = verb + "s"
                else:
                    verb = verb + "s"
                body = f"user {verb}{tail}"
            else:
                body = "user " + rest
        elif lower.startswith("my "):
            body = "user's " + body[3:]
        return body
    return None


_FORGET_PATTERNS = [
    re.compile(r"^\s*forget\s+(?:that\s+)?(.+?)[\s!?.]*$", re.I),
    re.compile(r"^\s*(?:please\s+)?remove\s+(?:the\s+)?(?:memory|fact)\s+(?:about\s+|that\s+)?(.+?)[\s!?.]*$", re.I),
]

def extract_forget_target(message: str) -> str | None:
    """Return the fact text to forget, normalized to third person so it
    matches the way facts are stored. "forget that I play X" → stored as
    "user plays X" via `remember`, so we apply the same transform here.
    """
    msg = (message or "").strip()
    for pat in _FORGET_PATTERNS:
        m = pat.match(msg)
        if not m:
            continue
        body = m.group(1).strip()
        if len(body) < _MIN_FACT_BODY:
            return None
        # Reuse the first-person → third-person normalization by faking a
        # "remember that <body>" phrasing. That way "I play X" becomes
        # "user plays X" and matches what was stored originally.
        normalized = extract_explicit_fact(f"remember that {body}")
        return normalized or body
    return None


_RECALL_TRIGGERS = re.compile(
    r"^\s*(?:what\s+do\s+you\s+(?:know|remember)(?:\s+about\s+me)?|"
    r"what('?s|\s+is)\s+in\s+(?:your\s+)?memory|"
    r"show\s+(?:me\s+)?(?:your\s+)?memory|"
    r"list\s+(?:your\s+)?memor(?:y|ies)|"
    r"what\s+have\s+you\s+learned)[\s!?.]*$",
    re.I,
)

def is_recall_query(message: str) -> bool:
    return bool(_RECALL_TRIGGERS.match((message or "").strip()))
