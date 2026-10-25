"""Pinned English/Chinese lexical representation shared by indexing and recall."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import jieba

_JIEBA_VERSION = "0.42.1"
_DICTIONARY_SHA256 = "7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8"
FTS_RECIPE = f"jieba-{_JIEBA_VERSION}-{_DICTIONARY_SHA256}-hmm-false-english-simple-v1"

if (
    jieba.__version__ != _JIEBA_VERSION
    or hashlib.sha256(Path(jieba.__file__).with_name("dict.txt").read_bytes()).hexdigest()
    != _DICTIONARY_SHA256
):
    raise RuntimeError("installed Chinese FTS dictionary differs from the pinned recipe")

jieba.setLogLevel(30)
_PART = re.compile(
    r"[\u3400-\u9fff]+|[A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f\u1e00-\u1eff0-9]+"
)
_HAN = re.compile(r"^[\u3400-\u9fff]+$")
_PROTECTED_ENGLISH = frozenset({"no", "not", "before", "after", "because", "from", "to", "until"})
_CHINESE_STOP = frozenset({"的", "了", "吗", "呢", "啊", "吧", "我", "你", "您"})
_CHINESE_MODIFIERS = frozenset(
    {"不", "没", "无", "未", "非", "前", "后", "之前", "之后", "以前", "以后"}
)
_MAX_QUERY_TERMS = 64


@dataclass(frozen=True)
class IndexStreams:
    english: str
    simple: str


@dataclass(frozen=True)
class QueryTerms:
    candidates: tuple[tuple[str, str], ...]
    primary: tuple[tuple[str, str], ...]
    modifiers: tuple[tuple[str, str], ...]


def _parts(text: str):
    return _PART.findall(unicodedata.normalize("NFKC", text).casefold())


def index_streams(text: str) -> IndexStreams:
    """Route each original source field once; retain query stop words in evidence."""
    english: list[str] = []
    simple: list[str] = []
    for part in _parts(text):
        if _HAN.fullmatch(part):
            simple.extend(jieba.cut_for_search(part, HMM=False))
        elif any(character.isdigit() for character in part):
            simple.append(part)
        else:
            english.append(part)
            if part in _PROTECTED_ENGLISH:
                simple.append(part)
    return IndexStreams(" ".join(english), " ".join(simple))


def query_terms(
    text: str, *, prioritized_names: Sequence[str] = (), max_terms: int | None = _MAX_QUERY_TERMS
) -> QueryTerms:
    """Keep search expansions in OR candidates and primary terms in full-match ranking."""
    candidates: list[tuple[str, str]] = []
    primary: list[tuple[str, str]] = []
    modifiers: list[tuple[str, str]] = []
    for part in _parts(text):
        if _HAN.fullmatch(part):
            for word in jieba.cut_for_search(part, HMM=False):
                if word not in _CHINESE_STOP and word not in _CHINESE_MODIFIERS:
                    candidates.append(("simple", word))
            for word in jieba.cut(part, HMM=False):
                if word in _CHINESE_STOP:
                    continue
                target = modifiers if word in _CHINESE_MODIFIERS else primary
                target.append(("simple", word))
        elif any(character.isdigit() for character in part):
            candidates.append(("simple", part))
            primary.append(("simple", part))
        elif part in _PROTECTED_ENGLISH:
            modifiers.append(("simple", part))
        else:
            candidates.append(("english", part))
            primary.append(("english", part))
    preferred = {
        term for name in prioritized_names for term in query_terms(name, max_terms=None).candidates
    }

    def bounded(terms: list[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
        distinct = dict.fromkeys(terms)
        ordered = sorted(distinct, key=lambda term: term not in preferred)
        return tuple(ordered)[:max_terms]

    return QueryTerms(bounded(candidates), bounded(primary), bounded(modifiers))
