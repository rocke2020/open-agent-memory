"""Deterministic context rendering shared by recall and recorded replay."""

import json


def render_context(items: list[dict]) -> str:
    """Render all admitted facts beside their selected originals, retaining metadata ownership."""
    blocks = []
    for group in _source_groups(items):
        source_id = group[-1]["unit_id"] if len(group) > 1 else None
        blocks.extend(
            render_item(item, source_id if item["kind"] == "fact" else None) for item in group
        )
    return "\n\n".join(blocks)


def render_item(item: dict, source_id: str | None = None) -> str:
    reference = (
        f"Source: [{source_id}]"
        if source_id is not None
        else "Citations: "
        + json.dumps(item["citations"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    time_evidence = json.dumps(
        item["time_evidence"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return (
        f"[{item['unit_id']}] {item['kind']} {item['origin']}\n{item['text']}\n"
        f"{reference}\nTime: {time_evidence}"
    )


def _source_groups(items: list[dict]) -> list[list[dict]]:
    """Associate selected facts and originals without changing their text or metadata."""
    passages = {item["unit_id"]: item for item in items if item["kind"] == "passage"}
    fact_sources = {}
    identity = ("source_id", "source_version", "message_id", "message_index", "role")
    for item in items:
        if item["kind"] != "fact" or not item["citations"]:
            continue
        passage_ids = {citation.get("passage_id") for citation in item["citations"]}
        if len(passage_ids) != 1:
            continue
        passage = passages.get(next(iter(passage_ids)))
        if passage is None or len(passage["citations"]) != 1:
            continue
        locator = passage["citations"][0]
        if any(locator.get(key) is None for key in identity):
            continue
        start, end = locator.get("start"), locator.get("end")
        if type(start) is not int or type(end) is not int:
            continue
        if start < 0 or end - start != len(passage["text"]):
            continue
        for citation in item["citations"]:
            span = citation.get("span", {})
            if any(citation.get(key) != locator[key] for key in ("source_id", "source_version")):
                break
            if any(span.get(key) != locator[key] for key in identity):
                break
            first, last, quote = span.get("start"), span.get("end"), span.get("quote")
            if type(first) is not int or type(last) is not int or not isinstance(quote, str):
                break
            if (
                not start <= first < last <= end
                or passage["text"][first - start : last - start] != quote
            ):
                break
        else:
            fact_sources[item["unit_id"]] = passage["unit_id"]
    groups: dict[str, list[dict]] = {}
    for item in items:
        key = fact_sources.get(item["unit_id"], item["unit_id"])
        groups.setdefault(key, []).append(item)
    return [sorted(group, key=lambda item: item["kind"] == "passage") for group in groups.values()]
