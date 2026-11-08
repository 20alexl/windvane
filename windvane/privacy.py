"""Redaction of what the engine writes: a memory, a rule, a checkpoint, a
session search chunk.

The same patterns as the door (hooks/door.ts), which redacts a tool result
before the model sees it; the door runs in the mod, so the two copies are kept
in step by hand and a test compares them. They cover new writes only. Text
already in the store stays as it was: rewriting it on a save could not be
undone. Pattern matching misses secrets of other shapes, so this narrows what
reaches the store and is no guarantee.
"""

from __future__ import annotations

import re

REDACTED = "[redacted]"

_PEM_BLOCK = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)")
_PREFIXED_KEY = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
# `api_key = <literal>`, `"password": "<literal>"`, `Bearer <literal>`. The first
# segment of the value is 16+ characters, so a dotted name in source
# (`password = settings.database_password`) is left alone; a dotted tail goes
# with a value that long.
_ASSIGNED_VALUE = re.compile(
    r"""\b((?:api[_ -]?key|secret|token|password|passwd)\b["']?\s*[:=]\s*["']?|bearer\s+)[A-Za-z0-9_\-/+=]{16,}(?:\.[A-Za-z0-9_\-/+=]+)*""",
    re.I,
)
_SECRET_KEY = re.compile(r"api[_ -]?key|secret|token|password|passwd", re.I)


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def well_formed(text: str) -> str:
    """Text with half a surrogate pair replaced. Text clipped through the middle
    of an emoji arrives that way, and it cannot be encoded: hashing it for an
    entry's id raised, and the memory was lost. A whole pair is one character
    in a Python string, so only a half matches."""
    return _LONE_SURROGATE.sub("�", text)


def redact(text: str) -> str:
    text = well_formed(text)
    text = _PEM_BLOCK.sub(REDACTED, text)
    text = _PREFIXED_KEY.sub(REDACTED, text)
    text = _JWT.sub(REDACTED, text)
    return _ASSIGNED_VALUE.sub(lambda m: m.group(1) + REDACTED, text)


def clean(value):
    """A structure with every string redacted, and a string held under a
    secret's own name replaced whatever it looks like."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, dict):
        return {
            k: REDACTED if isinstance(v, str) and v and _SECRET_KEY.fullmatch(str(k)) else clean(v)
            for k, v in value.items()
        }
    return value
