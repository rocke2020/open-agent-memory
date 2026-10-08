"""Exact source quote validation shared by ingestion, indexing and recall."""


def unique_quote(content: str, quote: object) -> bool:
    if not isinstance(quote, str) or not quote:
        return False
    first = content.find(quote)
    return first >= 0 and content.find(quote, first + 1) < 0
