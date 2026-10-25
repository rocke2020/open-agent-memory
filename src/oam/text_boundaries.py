"""Shared multilingual punctuation boundaries at unchanged source offsets."""

import unicodedata

import regex  # type: ignore[import-untyped]

_PUNCTUATION_BOUNDARY = regex.compile(r"""(?P<marks>\p{Terminal_Punctuation}+)[\p{Pe}\p{Pf}"']*""")
# The distinct Greek question mark terminates a sentence despite its Unicode
# canonical equivalence to an ordinary semicolon, which remains a clause break.
_SENTENCE_TERMINAL = regex.compile(r"[\p{Sentence_Terminal}\u037e]")
_PRECEDING_WORD = regex.compile(r"[\p{L}.]+$")
_INITIALS = regex.compile(r"(?:[A-Za-z]\.)*[A-Za-z]")
_ABBREVIATIONS = frozenset({"dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "vs", "etc"})


def punctuation_ends(text: str) -> tuple[list[int], list[int]]:
    """Return sentence and clause ends, including attached closing quotes."""
    sentences, clauses = [], []
    for match in _PUNCTUATION_BOUNDARY.finditer(text):
        marks = unicodedata.normalize("NFKC", match["marks"])
        at = match.start()
        following = text[match.end() : match.end() + 1]
        preceding = text[at - 1 : at] if at else ""
        if marks in {".", ",", ":"} and preceding.isdecimal() and following.isdecimal():
            continue
        if marks == ".":
            if following and not following.isspace() and match.end() == at + 1:
                continue
            word = _PRECEDING_WORD.search(text, 0, at)
            if word:
                token = unicodedata.normalize("NFKC", word[0])
                if token.casefold() in _ABBREVIATIONS or _INITIALS.fullmatch(token):
                    continue
        if marks == ":" and following in {"/", "\\"}:
            continue
        clauses.append(match.end())
        if _SENTENCE_TERMINAL.search(match["marks"]):
            sentences.append(match.end())

    return sentences, clauses
