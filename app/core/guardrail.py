"""Heuristic-only input guardrail for the user's raw natural-language
message -- runs before any SQL exists, at the very start of the graph.

No LLM call, no new dependency: regex + stdlib only. Deliberately does NOT
pattern-match SQL keywords (DROP/DELETE/UPDATE/...) -- that's
`app/core/validator.py`'s job on the *generated* SQL. A user typing
"DROP TABLE orders" as their message must sail through this guardrail and
reach the validator, so the UI shows the validator doing the blocking.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import settings

# --- Prompt-injection / role-override -------------------------------------

_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"ignore (all |the )?(previous|prior|above) instructions",
        r"disregard (all |the )?(previous|prior|above)",
        r"you are now\b",
        r"act as (a|an)\b",
        r"system prompt",
        r"new instructions?:",
        r"forget (all |your )?(previous|prior) (instructions|rules)",
        r"reveal your (prompt|instructions)",
    ]
]

# --- PII --------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CREDIT_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

_PII_PATTERNS = [_EMAIL_RE, _SSN_RE, _CREDIT_CARD_RE]

# fraction of non letter/digit/space/basic-punctuation chars above which
# text is treated as gibberish (a cheap heuristic, not an NLP model).
_GIBBERISH_SYMBOL_RATIO = 0.3


@dataclass
class GuardrailVerdict:
    blocked: bool
    reason: str | None = None

    def to_dict(self) -> dict:
        return {"blocked": self.blocked, "reason": self.reason}


def _is_gibberish(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if not any(c.isalpha() for c in stripped):
        return True
    ok_chars = sum(1 for c in stripped if c.isalnum() or c.isspace() or c in ".,!?'\"-")
    ratio_ok = ok_chars / len(stripped)
    return ratio_ok < (1 - _GIBBERISH_SYMBOL_RATIO)


def check_input(text: str) -> GuardrailVerdict:
    """Heuristic-only verdict on a raw user message. Order: length ->
    gibberish -> injection/role-override -> PII. First match wins."""
    if len(text) > settings.guardrail_max_input_chars:
        return GuardrailVerdict(True, "Your message is too long.")

    if _is_gibberish(text):
        return GuardrailVerdict(True, "Your message doesn't look like a valid request.")

    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return GuardrailVerdict(True, "Your message looks like a prompt-injection attempt.")

    for pattern in _PII_PATTERNS:
        if pattern.search(text):
            return GuardrailVerdict(True, "Your message appears to contain personal or sensitive information.")

    return GuardrailVerdict(False, None)
