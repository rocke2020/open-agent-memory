"""Deterministic context rendering shared by recall and recorded replay."""

import json
from datetime import datetime

from oam.config import TokenizerConfig
from oam.quotes import unique_quote
from oam.text_boundaries import punctuation_ends
from oam.tokens import (
    estimate_units,
    estimated_tokens,
    resolve_counter,
)

CONTEXT_TOKENIZER = "o200k_base"
_CONTEXT_COUNTER = resolve_counter(TokenizerConfig(type=CONTEXT_TOKENIZER))
ORIGINAL_KINDS = frozenset({"passage", "resource_passage"})


def context_token_count(text: str) -> int:
    """Count complete recall strings with the cached local context vocabulary."""
    return _CONTEXT_COUNTER.count(text)


def render_context(items: list[dict]) -> str:
    """Render compact references within conservative standalone packing costs."""
    return _ContextRenderer().render(items)[0]


class _ContextRenderer:
    """Reuse immutable item blocks only within one packing operation."""

    def __init__(self):
        self.blocks = {}

    def _block(self, key, render, *args):
        if key not in self.blocks:
            text = render(*args)
            self.blocks[key] = (text, estimate_units(text))
        return self.blocks[key]

    def render(self, items):
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
                owner = passage if fact_source else None
                blocks.append(
                    self._block(("full", id(item), fact_source), render_item, item, fact_source)
                )
                citations = [
                    *item["citations"],
                    *(
                        witness["citation"]
                        for path in item.get("paths", ())
                        for witness in path.get("alias_witnesses", ())
                    ),
                ]
                labels = tuple(
                    sources.get(source)
                    if isinstance(source := citation.get("source_id"), str)
                    else None
                    for citation in citations
                )
                key = (
                    "compact",
                    id(item),
                    references[item["unit_id"]],
                    id(owner),
                    references.get(fact_source),
                    labels,
                )
                compact_blocks.append(
                    self._block(key, _render_compact_item, item, references, sources, owner)
                )
        # Preserve the formatting choice for recorded replay. These additive units
        # choose presentation only; live admission counts the complete BPE string.
        separators = max(0, len(blocks) - 1) * 2
        legacy_units = sum(units for _, units in blocks) + separators
        compact_units = sum(units for _, units in compact_blocks) + separators
        chosen, units = (
            (compact_blocks, compact_units)
            if compact_units <= legacy_units
            else (blocks, legacy_units)
        )
        return "\n\n".join(text for text, _ in chosen), units


def _render_compact_item(item, references, sources, passage):
    header = f"[{references[item['unit_id']]}] {item['kind']} {item['origin']}" + _text_marker(item)
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
    marker = _text_marker(item)
    rendered = (
        f"[{item['unit_id']}] {item['kind']} {item['origin']}{marker}\n{item['text']}\n{reference}"
    )
    if item["time_evidence"]:
        rendered += "\n" + _render_time(item["time_evidence"])
    return rendered + _render_aliases(item)


def _text_marker(item: dict) -> str:
    marker = " native_truncated=true" if item.get("truncated") else ""
    if excerpt := item.get("excerpt"):
        marker += f" excerpt={excerpt['start']}:{excerpt['end']}/{excerpt['original_length']}"
    return marker


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
    if passage is None or passage.get("excerpt") or len(passage["citations"]) != 1:
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
                user_rows = [
                    row
                    for row in passages
                    if row["locator"].get("role") == "user"
                    and (direct is None or row.get("generation_id") == direct.get("generation_id"))
                ]
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


def excerpt_span(
    text: str, *, max_chars: int, previous: bool, starts_message: bool
) -> tuple[int, int] | None:
    """Prefer bounded sentence spans, then clauses, without inventing passage-edge boundaries."""
    sentences, clauses = punctuation_ends(text)

    for endings in (sentences, clauses):
        edges = [0, *endings] if starts_message else endings
        if len(edges) < 2:
            continue
        pairs = (
            ((start, edges[-1]) for start in edges[:-1])
            if previous
            else ((edges[0], end) for end in reversed(edges[1:]))
        )
        for start, end in pairs:
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if 0 < end - start <= max_chars:
                return start, end
    return None


def source_window_excerpts(
    fact: dict, passage_locators: list[dict], originals: dict[str, dict], *, max_chars: int
) -> list[dict]:
    """Keep boundary-facing optional user text, with passage-relative character offsets."""
    required = {
        (citation.get("source_id"), citation.get("source_version"), citation.get("passage_id"))
        for citation in fact["citations"]
    }
    selected = {}
    by_id = {row["unit_id"]: row for row in passage_locators}
    for citation in fact["citations"]:
        anchor = citation.get("span", {}).get("message_index")
        for identity in source_window_ids([{**fact, "citations": [citation]}], passage_locators):
            row = by_id[identity]
            if (
                (row["source_id"], row["source_version"], row["passage_id"]) in required
                or identity in selected
                or identity not in originals
            ):
                continue
            item = originals[identity]
            text = item["text"]
            span = excerpt_span(
                text,
                max_chars=max_chars,
                previous=row["locator"]["message_index"] < anchor,
                starts_message=row["locator"].get("passage_index") == 0,
            )
            if span is None:
                continue
            start, end = span
            if span != (0, len(text)):
                item = {
                    **item,
                    "text": text[start:end],
                    "excerpt": {"start": start, "end": end, "original_length": len(text)},
                }
            selected[identity] = item
    return list(selected.values())


def bundle_items(bundles: list[list[dict]]) -> list[dict]:
    """Return the unique items required by actually admitted roots, in admission order."""
    selected = {}
    for bundle in bundles:
        for item in bundle:
            if item["unit_id"] not in selected or not item.get("excerpt"):
                selected[item["unit_id"]] = item
    return list(selected.values())


def _fact_support_key(item: dict, group: list[dict]) -> str | None:
    """Identify only facts with identical complete, source-qualified support."""
    if item["kind"] != "fact" or item.get("paths") or item.get("truncated"):
        return None
    citations = item.get("citations", [])
    passage_ids = {citation.get("passage_id") for citation in citations}
    passages = {
        row["unit_id"]: row
        for row in group
        if row["kind"] == "passage" and not row.get("excerpt") and not row.get("truncated")
    }
    if not citations or None in passage_ids or passage_ids != set(passages):
        return None

    def normalized(value):
        if isinstance(value, dict):
            return {
                key: normalized(part)
                for key, part in value.items()
                if key not in {"passage_id", "evidence_id"}
            }
        if isinstance(value, list):
            return [normalized(part) for part in value]
        return value

    support = sorted(
        json.dumps(
            {
                "citation": normalized(citation),
                "text": passages[citation["passage_id"]]["text"],
                "time_evidence": passages[citation["passage_id"]].get("time_evidence", []),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        for citation in citations
    )
    return json.dumps(
        [item["text"], item["origin"], item.get("time_evidence", []), support],
        sort_keys=True,
        ensure_ascii=False,
    )


def assemble_context(
    candidates: list[dict],
    source_passages: list[dict],
    *,
    context_max_tokens: int,
    include_chunks: bool,
    max_results: int,
    evidence_groups: list[list[dict]] | None = None,
    optional_context: dict[str, list[dict]] | None = None,
) -> tuple[list[dict], list[str], list[list[dict]], bool]:
    """Admit complete representations in root relevance order under one rendered cap."""
    groups = {group[0]["unit_id"]: group for group in evidence_groups or []}
    admitted: list[list[dict]] = []
    selected: list[dict] = []
    omissions: list[str] = []
    considered: set[str] = set()
    candidate_ids = {item["unit_id"] for item in candidates}
    optional_ids: set[str] = set()
    fact_support_keys: set[str] = set()
    eligible_facts = {
        item["unit_id"]
        for item in candidates
        if item["kind"] == "fact" and item["unit_id"] in groups
    }
    oversized_single_fact = False
    renderer = _ContextRenderer()

    def selection(group):
        tentative = bundle_items([*admitted, group])
        originals = sum(row["kind"] in ORIGINAL_KINDS for row in tentative)
        pools_fit = originals <= max_results and len(tentative) - originals <= max_results
        tokens_fit = (
            pools_fit and context_token_count(renderer.render(tentative)[0]) <= context_max_tokens
        )
        return tentative, pools_fit, tokens_fit

    for item in [*candidates, *source_passages]:
        identity = item["unit_id"]
        if identity in considered or (identity in optional_ids and identity not in candidate_ids):
            continue
        considered.add(identity)
        if item["kind"] in ORIGINAL_KINDS:
            if not include_chunks:
                continue
            group = [item]
        else:
            group = groups.get(identity)
            if group is None:
                omissions.append(identity)
                continue
        support_key = _fact_support_key(item, group)
        if support_key is not None and support_key in fact_support_keys:
            continue
        representation = [
            row for row in group if include_chunks or row["kind"] not in ORIGINAL_KINDS
        ]
        tentative, pools_fit, tokens_fit = selection(representation)
        if not pools_fit:
            omissions.append(identity)
            continue
        if not tokens_fit:
            if (
                context_max_tokens > 0
                and not admitted
                and item["kind"] == "fact"
                and eligible_facts == {identity}
            ):
                oversized_single_fact = True
            else:
                omissions.append(identity)
                continue
        admitted.append(representation)
        if support_key is not None:
            fact_support_keys.add(support_key)
        selected = tentative
        if oversized_single_fact:
            break
        if item["kind"] == "fact" and include_chunks:
            selected_by_id = {row["unit_id"]: row for row in selected}
            for neighbor in (optional_context or {}).get(identity, []):
                current = selected_by_id.get(neighbor["unit_id"])
                if current is not None and ("excerpt" not in current or "excerpt" in neighbor):
                    continue
                tentative, pools_fit, tokens_fit = selection([neighbor])
                if pools_fit and tokens_fit:
                    admitted.append([neighbor])
                    selected = tentative
                    selected_by_id[neighbor["unit_id"]] = neighbor
                    optional_ids.add(neighbor["unit_id"])
    return selected, omissions, admitted, oversized_single_fact
