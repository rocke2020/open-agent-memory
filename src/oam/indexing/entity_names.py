"""Lookup keys for source-supported entity names; never changes evidence text."""

import unicodedata


def normalize_entity_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def entity_name_key(name: str, *, max_bytes: int) -> str:
    key = normalize_entity_text(name)
    if not key or max(len(name.encode()), len(key.encode())) > max_bytes:
        raise ValueError("entity name is blank or exceeds its UTF-8 byte limit")
    return key
