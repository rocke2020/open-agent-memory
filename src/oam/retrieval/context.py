"""Deterministic context rendering shared by recall and recorded replay."""

import json
from datetime import datetime

from oam.quotes import unique_quote
from oam.tokens import ESTIMATOR, UNITS_PER_TOKEN, estimate_units, estimated_tokens

CONTEXT_TOKENIZER = ESTIMATOR
ORIGINAL_KINDS = frozenset({"passage", "resource_passage"})


def context_token_count(text: str) -> int:
    """Estimate recall tokens with the compiled Unicode regex counter."""
    return estimated_tokens(estimate_units(text))


def render_context(items: list[dict]) -> str:
    """Render compact references within conservative standalone packing costs."""
    references = {item["unit_id"]: str(index) for index, item in enumerate(items, 1)}
    sources = {}
    for item in items:
        for citation in item["citations"]:
            if isinstance(source := citation.get("source_id"), str):
                sources.setdefault(source, f"S{len(sources) + 1}")
    blocks, compact_blocks = [], []
    for group in _source_groups(items):
        passage = group[-1] if len(group) > 1 and group[-1]["kind"] == "passage" else None
        source_id = passage["unit_id"] if passage is not None else None
        ordered = [passage, *group[:-1]] if passage is not None else group
        for item in ordered:
            fact_source = source_id if item["kind"] == "fact" else None
            blocks.append(render_item(item, fact_source))
            compact_blocks.append(
                _render_compact_item(item, references, sources, passage if fact_source else None)
            )
    legacy = "\n\n".join(blocks)
    compact = "\n\n".join(compact_blocks)
    return compact if estimate_units(compact) <= estimate_units(legacy) else legacy


def _render_compact_item(item, references, sources, passage):
    header = f"[{references[item['unit_id']]}] {item['kind']} {item['origin']}"
    if item.get("truncated"):
        header += " native_truncated=true"
    inherited_time = bool(
        passage and item["time_evidence"] and item["time_evidence"] == passage["time_evidence"]
    )
    if passage:
        header += f" source=[{references[passage['unit_id']]}]"
    if inherited_time:
        header += " time=source"
    rendered = header + "\n" + item["text"]
    if passage is None:
        rendered += "\n" + _render_citations(item["citations"], sources)
    if item["time_evidence"] and not inherited_time:
        rendered += "\n" + _render_time(item["time_evidence"], compact=True)
    return rendered + _render_aliases(item, sources)


def render_item(item: dict, source_id: str | None = None) -> str:
    reference = (
        f"Source: [{source_id}]" if source_id is not None else _render_citations(item["citations"])
    )
    marker = " native_truncated=true" if item.get("truncated") else ""
    rendered = (
        f"[{item['unit_id']}] {item['kind']} {item['origin']}{marker}\n{item['text']}\n{reference}"
    )
    if item["time_evidence"]:
        rendered += "\n" + _render_time(item["time_evidence"])
    return rendered + _render_aliases(item)


def _render_aliases(item: dict, sources: dict | None = None) -> str:
    rendered = ""
    witnesses = {}
    for path in item.get("paths", ()):
        for witness in path.get("alias_witnesses", ()):
            witnesses.setdefault(witness["support_id"], witness)
    for witness in witnesses.values():
        rendered += (
            f"\nAlias: {witness['quote']!r} ({_render_citations([witness['citation']], sources)})"
        )
    return rendered


def _render_citations(citations: list[dict], sources: dict | None = None) -> str:
    chat = []
    for citation in citations:
        locator = citation.get("span", citation)
        if (
            not isinstance(locator, dict)
            or not isinstance(citation.get("source_id"), str)
            or type(citation.get("source_version")) is not int
            or not isinstance(locator.get("message_id"), str)
            or not isinstance(locator.get("role"), str)
            or ("span" in citation and not isinstance(locator.get("quote"), str))
        ):
            break
        source_id = (sources or {}).get(citation["source_id"], citation["source_id"])
        reference = (
            f"{source_id}@{citation['source_version']} "
            f"{locator['role']} message:{locator['message_id']}"
        )
        if "span" not in citation and isinstance(citation.get("passage_id"), str):
            reference += f" chunk:{citation['passage_id']}"
            if "passage_index" in locator:
                reference += f" index:{locator['passage_index']}"
        if "span" in citation:
            reference += f" quote:{locator['quote']!r}"
        chat.append(reference)
    if len(chat) == len(citations):
        return "Source: " + "; ".join(chat)
    return "Citations: " + json.dumps(
        citations, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _render_time(entries: list[dict], *, compact: bool = False) -> str:
    if any(
        not isinstance(entry, dict) or not isinstance(entry.get("role"), str) for entry in entries
    ):
        return "Time: " + json.dumps(
            entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    parts = []
    for entry in entries:
        if compact and set(entry) == {"role", "at"}:
            parts.append(f"{entry['role']}={_time_value(entry['at'], utc=True)}")
        else:
            parts.append(
                " ".join(
                    [
                        entry["role"],
                        *(
                            f"{key}={_time_value(value, utc=compact and key == 'at')}"
                            for key, value in entry.items()
                            if key != "role"
                        ),
                    ]
                )
            )
    return "Time: " + "; ".join(parts)


def _time_value(value: object, *, utc: bool = False) -> str:
    if utc and isinstance(value, str) and value.endswith("+00:00"):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            pass
        else:
            if parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0:
                value = value[:-6] + "Z"
    if isinstance(value, str) and not any(ch.isspace() or ch == ";" for ch in value):
        return value
    return repr(value)


def _source_groups(items: list[dict]) -> list[list[dict]]:
    """Associate selected facts and originals without changing their text or metadata."""
    passages = {item["unit_id"]: item for item in items if item["kind"] == "passage"}
    fact_sources = {
        item["unit_id"]: source
        for item in items
        if (source := _supported_passage_id(item, passages)) is not None
    }
    groups: dict[str, list[dict]] = {}
    for item in items:
        key = fact_sources.get(item["unit_id"], item["unit_id"])
        groups.setdefault(key, []).append(item)
    return [sorted(group, key=lambda item: item["kind"] == "passage") for group in groups.values()]


def _supported_passage_id(item: dict, passages: dict[str, dict]) -> str | None:
    if item["kind"] != "fact" or not item["citations"]:
        return None
    passage_ids = {citation.get("passage_id") for citation in item["citations"]}
    if len(passage_ids) != 1:
        return None
    passage = passages.get(next(iter(passage_ids)))
    if passage is None or len(passage["citations"]) != 1:
        return None
    locator = passage["citations"][0]
    identity = (
        "source_id",
        "source_version",
        "message_id",
        "message_index",
        "role",
        "passage_index",
    )
    if any(locator.get(key) is None for key in identity):
        return None
    for citation in item["citations"]:
        span = citation.get("span", {})
        if any(citation.get(key) != locator[key] for key in ("source_id", "source_version")):
            return None
        if any(span.get(key) != locator[key] for key in identity):
            return None
        if not unique_quote(passage["text"], span.get("quote")):
            return None
    return passage["unit_id"]


def source_diverse_seed_order(items: list[dict]) -> list[dict]:
    """Protect the highest-ranked user fact or original from each source."""
    groups: dict[tuple, list[dict]] = {}
    for item in items:
        sources = {
            (citation.get("source_id"), citation.get("source_version"))
            for citation in item.get("citations", ())
            if isinstance(citation.get("source_id"), str)
            and type(citation.get("source_version")) is int
        }
        key = ("source", *next(iter(sources))) if len(sources) == 1 else ("unit", item["unit_id"])
        groups.setdefault(key, []).append(item)
    if len(groups) <= 1:
        return list(items)

    representatives = []
    protected = set()
    for group in groups.values():
        facts = [item for item in group if item["kind"] == "fact"]
        user_facts = [
            item
            for item in facts
            if any(
                isinstance(citation.get("span"), dict) and citation["span"].get("role") == "user"
                for citation in item["citations"]
            )
        ]
        representative = (user_facts or facts or group)[0]
        representatives.append(representative)
        protected.add(representative["unit_id"])
    return [*representatives, *(item for item in items if item["unit_id"] not in protected)]


def pack_ranked(items: list[dict], budget: int, limit: int, initial: list[dict] | None = None):
    """Pack rendered blocks for historical trace diagnostics using their original estimate."""
    chosen = list(initial or [])
    seen = {item["unit_id"] for item in chosen}
    units = sum(estimate_units(render_item(item)) + 2 for item in chosen)
    for item in items:
        if len(chosen) >= limit:
            break
        if item["unit_id"] in seen:
            continue
        cost = estimate_units(render_item(item)) + 2
        if estimated_tokens(units + cost) > budget:
            continue
        chosen.append(item)
        seen.add(item["unit_id"])
        units += cost
    return chosen


def source_window_ids(
    seed_items: list[dict], passage_locators: list[dict], *, policy: str = "l1_window"
) -> list[str]:
    """Select stable direct chunks, with adjacent user turns only for ordinary facts."""
    if policy not in {"l1_window", "l2_direct"}:
        raise ValueError("unknown source selection policy")
    direct_only = policy == "l2_direct"

    def position(row):
        return (
            row["locator"].get("message_index", float("inf")),
            row["locator"].get("passage_index", 0),
            row["unit_id"],
        )

    by_source = {}
    by_id = {}
    for row in passage_locators:
        locator = row["locator"]
        if not isinstance(locator, dict) or (
            not direct_only and type(locator.get("message_index")) is not int
        ):
            continue
        key = (row["source_id"], row["source_version"])
        by_source.setdefault(key, []).append(row)
        by_id[row["passage_id"]] = row
    for group in by_source.values():
        group.sort(key=position)

    groups = {}
    seen = set()
    for item in seed_items:
        if item["kind"] != "fact":
            continue
        for citation in item["citations"]:
            key = (citation.get("source_id"), citation.get("source_version"))
            index = citation.get("span", {}).get("message_index")
            if (not direct_only and type(index) is not int) or key not in by_source:
                continue
            passages = by_source[key]
            direct = by_id.get(citation.get("passage_id"))
            selected = [direct] if direct is not None and direct in passages else []
            if not direct_only:
                user_rows = [row for row in passages if row["locator"].get("role") == "user"]
                before = [row for row in user_rows if row["locator"]["message_index"] < index]
                after = [row for row in user_rows if row["locator"]["message_index"] > index]
                selected.extend(before[-1:] + after[:1])
            for row in selected:
                if row["unit_id"] not in seen:
                    groups.setdefault(key, []).append(row)
                    seen.add(row["unit_id"])
    for group in groups.values():
        group.sort(key=position)
    return [
        group[index]["unit_id"]
        for index in range(max(map(len, groups.values()), default=0))
        for group in groups.values()
        if index < len(group)
    ]


def assemble_context(
    candidates: list[dict],
    source_passages: list[dict],
    *,
    fact_budget: int,
    chunk_budget: int,
    include_chunks: bool,
    max_results: int,
    evidence_groups: list[list[dict]] | None = None,
) -> tuple[list[dict], list[str]]:
    """Select learned text and originals independently, preserving complete support groups."""
    groups = {group[0]["unit_id"]: group for group in evidence_groups or []}
    pool = {
        item["unit_id"]: item
        for item in [*candidates, *source_passages, *(item for g in groups.values() for item in g)]
    }
    costs = {key: estimate_units(item["text"]) for key, item in pool.items()}
    fact_limit = fact_budget * UNITS_PER_TOKEN
    chunk_limit = chunk_budget * UNITS_PER_TOKEN
    facts: dict[str, dict] = {}
    originals: dict[str, dict] = {}
    fact_units = chunk_units = 0
    omissions = []
    first_ordinary = None
    for item in candidates:
        identifier = item["unit_id"]
        if item["kind"] in ORIGINAL_KINDS or identifier in facts:
            continue
        if item["kind"] != "insight" and first_ordinary is None:
            first_ordinary = item
        group = groups.get(identifier)
        if item["kind"] == "insight" and group is None:
            omissions.append(identifier)
            continue
        additions = {row["unit_id"]: row for row in group or [item]}
        new_facts = {
            key: row
            for key, row in additions.items()
            if row["kind"] not in ORIGINAL_KINDS and key not in facts
        }
        new_originals = {
            key: row
            for key, row in additions.items()
            if row["kind"] in ORIGINAL_KINDS and key not in originals
        }
        added_fact_units = sum(costs[key] for key in new_facts)
        added_original_units = sum(costs[key] for key in new_originals)
        if (
            fact_units + added_fact_units > fact_limit
            or chunk_units + added_original_units > chunk_limit
            or len(facts) + len(new_facts) > max_results
            or len(originals) + len(new_originals) > max_results
        ):
            omissions.append(identifier)
            continue
        facts.update(new_facts)
        originals.update(new_originals)
        fact_units += added_fact_units
        chunk_units += added_original_units
    if not facts and fact_budget > 0 and first_ordinary is not None:
        identifier = first_ordinary["unit_id"]
        facts[identifier] = first_ordinary
        omissions = [node for node in omissions if node != identifier]
    if include_chunks:
        original_pool = [
            *candidates,
            *source_passages,
            *(
                item
                for group in groups.values()
                for item in group
                if item["kind"] in ORIGINAL_KINDS
            ),
        ]
        for item in original_pool:
            identifier = item["unit_id"]
            if item["kind"] not in ORIGINAL_KINDS or identifier in originals:
                continue
            if len(originals) >= max_results or chunk_units >= chunk_limit:
                omissions.append(identifier)
                continue
            originals[identifier] = item
            chunk_units += costs[identifier]
    return [*facts.values(), *originals.values()], list(dict.fromkeys(omissions))
