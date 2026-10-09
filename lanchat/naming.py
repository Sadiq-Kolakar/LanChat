"""Display-name helpers.

Display names are *labels only*. They are never used to identify a peer or to
grant privileges - peers are identified by their random ``node_id``.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Mapping

MAX_NAME_CHARS = 24
DEFAULT_NAME = "Guest"

_WS = re.compile(r"\s+")


def sanitize_name(raw: object, default: str = DEFAULT_NAME) -> str:
    """Return a safe display name: no control chars, collapsed spaces, max 24 chars."""
    if not isinstance(raw, str):
        return default
    spaced = _WS.sub(" ", raw)  # tabs/newlines become plain spaces *before* control chars are dropped
    cleaned = "".join(ch for ch in spaced if unicodedata.category(ch)[0] != "C")
    cleaned = _WS.sub(" ", cleaned).strip()[:MAX_NAME_CHARS].strip()
    return cleaned or default


def display_labels(entries: Iterable[tuple[str, str, str]]) -> Mapping[str, str]:
    """Disambiguate duplicate names.

    ``entries`` are ``(id, name, hint)`` tuples (hint is usually an IP address).
    Names that appear once stay unchanged; duplicates become ``name (hint)``.
    """
    items = list(entries)
    counts: dict[str, int] = {}
    for _, name, _ in items:
        key = name.casefold()
        counts[key] = counts.get(key, 0) + 1
    labels: dict[str, str] = {}
    for ident, name, hint in items:
        if counts[name.casefold()] > 1 and hint:
            labels[ident] = f"{name} ({hint})"
        else:
            labels[ident] = name
    return labels
