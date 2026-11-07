"""Versioned local token estimates for admission and context budgets."""

from __future__ import annotations

import gzip
import hashlib
import re
from bisect import bisect_right
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
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
    _truncate: Callable[[str, int], str] | None = field(default=None, repr=False, compare=False)

    def count(self, text: str) -> int:
        return self._count(text)

    def truncate(self, text: str, max_tokens: int) -> str:
        """Retain an original Unicode prefix, verified under the complete counting policy."""
        if self.count(text) <= max_tokens:
            return text
        if self._truncate is None:
            raise ValueError("token counter does not support truncation")
        candidate = self._truncate(text, max_tokens)
        if not text.startswith(candidate):
            raise ValueError("token counter truncation must preserve an original text prefix")
        if self.count(candidate) > max_tokens:
            raise ValueError("token counter truncation exceeds the token limit")
        return candidate


@lru_cache(maxsize=8)
def _named_counter(name: str) -> TokenCounter:
    if name == ESTIMATOR:
        return TokenCounter(
            name,
            lambda text: estimated_tokens(estimate_units(text)),
            lambda text, limit: text[
                : bisect_right(cumulative_units(text), limit * UNITS_PER_TOKEN) - 1
            ],
        )
    import toktok  # type: ignore[import-untyped]

    encoding = toktok._encoding(name)
    return TokenCounter(name, encoding.count, lambda text, limit: encoding.truncate(text, limit)[0])


@lru_cache(maxsize=8)
def _asset_counter(content: bytes, add_special_tokens: bool) -> TokenCounter:
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_str(content.decode("utf-8"))
    # Saved serving limits must never silently truncate the text being counted.
    tokenizer.no_truncation()
    tokenizer.no_padding()
    identity = "tokenizer_json:" + hashlib.sha256(content).hexdigest()
    identity += ":special=" + str(add_special_tokens).lower()

    def truncate(text: str, limit: int) -> str:
        encoded = tokenizer.encode(text, add_special_tokens=add_special_tokens)
        available = limit - sum(encoded.special_tokens_mask)
        offsets = [
            (start, end)
            for (start, end), special in zip(
                encoded.offsets, encoded.special_tokens_mask, strict=True
            )
            if not special and 0 <= start < end <= len(text)
        ]
        if available <= 0 or not offsets:
            return ""
        position = min(available, len(offsets)) - 1
        end = offsets[position][1]
        neighbor = offsets[position + 1][0] if position + 1 < len(offsets) else len(text)
        near = max(end, neighbor)
        # Neighboring starts preserve original combining sequences omitted by normalization.
        candidates = {point for pair in offsets for point in pair if 0 < point <= near}
        candidates.add(near)
        for cut in sorted(candidates, reverse=True):
            candidate = text[:cut]
            if len(tokenizer.encode(candidate, add_special_tokens=add_special_tokens).ids) <= limit:
                return candidate
        return ""

    return TokenCounter(
        identity,
        lambda text: len(tokenizer.encode(text, add_special_tokens=add_special_tokens).ids),
        truncate,
    )


@lru_cache(maxsize=2)
def qwen_embedding_counter(variant: str = "0.6b") -> TokenCounter:
    """Load a pinned embedding vocabulary offline, including the saved EOS policy."""
    asset, digest = (
        (
            "qwen3-embedding-0.6b.json.gz",
            "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a",
        )
        if variant == "0.6b"
        else (
            "qwen3-embedding-4b-8b.json.gz",
            "83cdf8c3a34f68862319cb1810ee7b1e2c0a44e0864ae930194ddb76bb7feb8d",
        )
    )
    content = gzip.decompress(files("oam").joinpath("tokenizer_assets", asset).read_bytes())
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("bundled Qwen embedding tokenizer digest mismatch")
    return _asset_counter(content, True)


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
