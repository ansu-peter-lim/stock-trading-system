"""Shared stock-name search normalization."""

from __future__ import annotations

import re
import unicodedata


WHITESPACE = re.compile(r"\s+")


def normalize_stock_name(value: str) -> str:
    """Return an NFKC, trimmed, whitespace-collapsed search key.

    The caller remains responsible for retaining the unmodified source name.
    """
    return WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value).strip())
