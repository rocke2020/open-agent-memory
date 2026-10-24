"""Selection of partial conversation matches from direct user-fact evidence."""

from collections.abc import Sequence


def _position(citation: dict) -> tuple[str, int, int, str] | None:
    locator = citation.get("span", citation)
    if not isinstance(locator, dict):
        return None
    source = citation.get("source_id", locator.get("source_id"))
    version = citation.get("source_version", locator.get("source_version"))
    index, role = locator.get("message_index"), locator.get("role")
    if (
        not isinstance(source, str)
        or not source
        or type(version) is not int
        or version < 1
        or type(index) is not int
        or index < 0
        or role not in {"user", "assistant"}
    ):
        return None
    return source, version, index, role


def local_evidence_ids(
    items: Sequence[dict],
    dense_facts: Sequence[dict],
    independent_ids: set[str],
    *,
    reply_fact_ids: set[str],
    reply_parent_keys: set[tuple[str, int, str]],
) -> set[str]:
    """Filter already-eligible L0/L1 roots without changing required support."""
    witnesses, reply_witnesses, reply_original_witnesses = set(), set(), set()
    for fact in dense_facts:
        if fact["kind"] != "fact" or fact.get("origin") != "memory":
            continue
        for citation in fact.get("citations", []):
            position = _position(citation)
            if position is None or position[3] != "user":
                continue
            key = position[:3]
            witnesses.add(key)
            if fact["unit_id"] in reply_fact_ids:
                reply_witnesses.add(key)
                parent = (position[0], position[1], citation.get("passage_id"))
                if parent in reply_parent_keys:
                    reply_original_witnesses.add(key)

    def witnessed(item: dict) -> bool:
        for citation in item.get("citations", []):
            position = _position(citation)
            if position is None:
                continue
            source, version, index, role = position
            user_index = index if role == "user" else index - 1
            eligible = witnesses if role == "user" else reply_witnesses
            if role == "assistant" and item["kind"] == "passage":
                eligible = reply_original_witnesses
            if (source, version, user_index) in eligible:
                return True
        return False

    return {
        item["unit_id"]
        for item in items
        if item["unit_id"] in independent_ids
        or item.get("origin") != "memory"
        or item["kind"] not in {"fact", "passage"}
        or witnessed(item)
    }
