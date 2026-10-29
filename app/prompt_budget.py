"""Prompt budget helpers for the local 3B model.

qwen2.5:3b on CPU runs ~37 tok/s on the input side. A 4,000-token prompt
is a ~110 s wait before the first output byte. The product rule is to
stay under ~800 tokens of context so interactive turns feel responsive.

This module centralizes the arithmetic every caller used to redo by
hand. It is intentionally conservative: tokens are estimated as
``max(1, len(text) // 4)`` which over-counts slightly for English and
right-counts for Japanese. We never silently drop *all* of a section —
each `trim_*` helper returns a non-empty tail unless the input was
already empty.

Callers that already fit under the budget see no behaviour change: the
helpers are no-ops below the cap.
"""
from __future__ import annotations

DEFAULT_BUDGET = 800     # total context tokens for qwen2.5:3b on CPU
RESERVE_OUTPUT = 150     # headroom reserved for the model's reply


def estimate_tokens(text: str) -> int:
    """Cheap token estimate. ~4 chars per token for mixed en/ja."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def clip_to_tokens(text: str, max_tokens: int) -> str:
    """Hard-clip a string so its estimated tokens stay under max_tokens.

    Trims from the *end* (recent context survives). Returns the input
    unchanged when it already fits.
    """
    if max_tokens <= 0 or not text:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    # 4 chars ≈ 1 token
    return text[: max_tokens * 4]


def trim_history(history: list | None, max_tokens: int) -> list:
    """Keep the most recent turns that fit under max_tokens.

    history is a list of {"role": ..., "content": ...} dicts.
    """
    if not history or max_tokens <= 0:
        return []
    out: list = []
    used = 0
    for h in reversed(history):
        c = str(h.get("content", "") or "")
        t = estimate_tokens(c) + 4   # +4 for the "User:/Assistant:" label
        if used + t > max_tokens:
            break
        out.insert(0, h)
        used += t
    return out


def pack_prompt(
    system: str,
    history: list | None,
    user_msg: str,
    *,
    budget: int = DEFAULT_BUDGET,
    reserve_output: int = RESERVE_OUTPUT,
) -> tuple[str, list, str]:
    """Return (system, history, user_msg) resized to fit the budget.

    Priority order when the raw prompt is too large:
      1. Keep the user message intact (that's the question we're answering).
      2. Trim the oldest conversation turns.
      3. Clip the system block from the end (knowledge base goes before
         user memory, so recent sections survive — see assistant.ollama_chat).

    When the inputs already fit, this function is an identity op.
    """
    input_budget = max(50, budget - reserve_output)
    user_tokens = estimate_tokens(user_msg)
    remaining = max(50, input_budget - user_tokens)

    # Try with full system + progressively less history
    sys_tokens = estimate_tokens(system)
    hist_budget = max(0, remaining - sys_tokens)
    trimmed_hist = trim_history(history, hist_budget)

    # Still over? clip system block.
    while True:
        total = sys_tokens + sum(estimate_tokens(h.get("content", "")) + 4
                                 for h in trimmed_hist) + user_tokens
        if total <= input_budget or sys_tokens <= 50:
            break
        # drop ~25% of system block each pass
        system = system[: int(len(system) * 0.75)]
        sys_tokens = estimate_tokens(system)

    return system, trimmed_hist, user_msg
