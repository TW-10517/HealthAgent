"""PC Health Agent — Sequential action planner.

Splits compound chat requests into ordered steps and tracks their execution
state so the UI can show per-step status badges.
"""
import re
import uuid

PENDING = "pending"
RUNNING = "running"
DONE    = "done"
FAILED  = "failed"

_STEP_SEP = re.compile(
    r'\s*(?:'
    r',\s*and\s+then\s+'
    r'|,\s*then\s+'
    r'|;\s*then\s+'
    r'|,?\s+then\s+'
    r'|,?\s+and\s+then\s+'
    r'|;\s+'
    # "X and <action>" — split only when the next word is an action verb
    r'|\s+and\s+(?=(?:open|close|quit|find|search|locate|go|navigate|show|list|start|stop'
    r'|minimize|launch|run|check|scan|do|begin|cancel|get|load|view|read|display)\b)'
    r')\s*',
    re.IGNORECASE,
)
_ACTION_RE = re.compile(
    r'\b(open|close|quit|kill|find|search|locate|go\s+to|navigate|show|list|start'
    r'|stop|minimize|launch|run|check)\b',
    re.IGNORECASE,
)


def split_steps(message: str) -> list[str]:
    """Split a compound request into ordered step texts."""
    parts = _STEP_SEP.split(message.strip())
    return [p.strip() for p in parts if p.strip()]


def is_multi_step(message: str) -> bool:
    """Return True if message looks like a compound multi-step request."""
    parts = split_steps(message)
    if len(parts) < 2:
        return False
    return sum(1 for p in parts if _ACTION_RE.search(p)) >= 2


def build_plan(message: str) -> list[dict]:
    """Return a list of step dicts ready for JSON serialisation."""
    return [
        {"idx": i, "text": t, "status": PENDING, "result": ""}
        for i, t in enumerate(split_steps(message))
    ]


def new_plan_id() -> str:
    return uuid.uuid4().hex[:8]
