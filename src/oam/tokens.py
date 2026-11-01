"""Versioned local token estimates for admission and context budgets."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import accumulate
from pathlib import Path

from oam.config import TokenizerConfig

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


@dataclass(frozen=True)
class TokenCounter:
    """Immutable process-local counter; identity describes behavior, never asset paths."""

    identity: str
    _count: Callable[[str], int] = field(repr=False, compare=False)

    def count(self, text: str) -> int:
        return self._count(text)


@lru_cache(maxsize=8)
def _named_counter(name: str) -> TokenCounter:
    if name == ESTIMATOR:
        return TokenCounter(name, lambda text: estimated_tokens(estimate_units(text)))
    import toktok  # type: ignore[import-untyped]

    encoding = toktok._encoding(name)
    return TokenCounter(name, encoding.count)


@lru_cache(maxsize=8)
def _asset_counter(content: bytes, add_special_tokens: bool) -> TokenCounter:
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_str(content.decode("utf-8"))
    # Saved serving limits must never silently truncate the text being counted.
    tokenizer.no_truncation()
    tokenizer.no_padding()
    identity = "tokenizer_json:" + hashlib.sha256(content).hexdigest()
    identity += ":special=" + str(add_special_tokens).lower()
    return TokenCounter(
        identity,
        lambda text: len(tokenizer.encode(text, add_special_tokens=add_special_tokens).ids),
    )


def resolve_counter(
    config: TokenizerConfig, environment: Mapping[str, str] | None = None
) -> TokenCounter:
    if config.type == "tokenizer_json":
        assert config.file_env is not None
        path = (environment or {}).get(config.file_env, "").strip()
        if not path:
            raise ValueError(f"tokenizer requires environment variable {config.file_env}")
        try:
            return _asset_counter(Path(path).expanduser().read_bytes(), config.add_special_tokens)
        except (OSError, ValueError) as error:
            raise ValueError("cannot load configured tokenizer asset") from error
    return _named_counter(config.type)
