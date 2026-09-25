"""Versioned local token estimates for admission and context budgets."""

from __future__ import annotations

import re
from itertools import accumulate

ESTIMATOR = "oam-unicode-quarter-v1"
UNITS_PER_TOKEN = 4
_CJK_EXTRA_UNITS = 5
_OTHER_ASTRAL_EXTRA_UNITS = 7
_CJK = re.compile(
    r"[\u3000-\u303F\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF"
    r"\uF900-\uFAFF\uFF00-\uFFEF\u1100-\u11FF\u3130-\u318F\U00020000-\U0002EBEF]"
)
_OTHER_ASTRAL = re.compile(r"[\U00010000-\U0001FFFF\U0002EBF0-\U0010FFFF]")


def estimate_units(text: str) -> int:
    """Return unrounded quarter-token units for a whole string."""
    return (
        len(text)
        + _CJK_EXTRA_UNITS * len(_CJK.findall(text))
        + _OTHER_ASTRAL_EXTRA_UNITS * len(_OTHER_ASTRAL.findall(text))
    )


def estimated_tokens(units: int) -> int:
    """Round one accumulated unit total to estimated tokens."""
    return (units + UNITS_PER_TOKEN - 1) // UNITS_PER_TOKEN


def cumulative_units(text: str) -> list[int]:
    """Map source character offsets to unrounded units for structural chunking."""
    weights = [1] * len(text)
    for match in _CJK.finditer(text):
        weights[match.start()] += _CJK_EXTRA_UNITS
    for match in _OTHER_ASTRAL.finditer(text):
        weights[match.start()] += _OTHER_ASTRAL_EXTRA_UNITS
    return [0, *accumulate(weights)]
