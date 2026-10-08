"""Deterministic context rendering shared by recall and recorded replay."""

import json

from oam.quotes import unique_quote
from oam.tokens import estimate_units, estimated_tokens


def render_context(items: list[dict]) -> str:
    """Render all admitted facts beside their selected originals, retaining metadata ownership."""
    blocks = []
    for group in _source_groups(items):
        passage = group[-1] if len(group) > 1 and group[-1]["kind"] == "passage" else None
        source_id = passage["unit_id"] if passage is not None else None
        ordered = [passage, *group[:-1]] if passage is not None else group
        blocks.extend(
            render_item(item, source_id if item["kind"] == "fact" else None) for item in ordered
        )
    return "\n\n".join(blocks)


def render_item(item: dict, source_id: str | None = None) -> str:
    reference = (
        f"Source: [{source_id}]" if source_id is not None else _render_citations(item["citations"])
    )
    rendered = f"[{item['unit_id']}] {item['kind']} {item['origin']}\n{item['text']}\n{reference}"
    if item["time_evidence"]:
        rendered += "\n" + _render_time(item["time_evidence"])
    return rendered


def _render_citations(citations: list[dict]) -> str:
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
        reference = (
            f"{citation['source_id']}@{citation['source_version']} "
            f"{locator['role']} message:{locator['message_id']}"
        )
        if "span" in citation:
            reference += f" quote:{locator['quote']!r}"
        chat.append(reference)
    if len(chat) == len(citations):
        return "Source: " + "; ".join(chat)
    return "Citations: " + json.dumps(
        citations, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _render_time(entries: list[dict]) -> str:
    if any(
        not isinstance(entry, dict) or not isinstance(entry.get("role"), str) for entry in entries
    ):
        return "Time: " + json.dumps(
            entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return "Time: " + "; ".join(
        " ".join(
            [
                entry["role"],
                *(f"{key}={_time_value(value)}" for key, value in entry.items() if key != "role"),
            ]
        )
        for entry in entries
    )


def _time_value(value: object) -> str:
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


class ContextUnitCosts:
    """Reuse rendered token costs while one recall assembles its context."""

    def __init__(self):
        self._costs: dict[tuple[int, str | None], tuple[dict, int]] = {}

    def units(self, item: dict, source_id: str | None = None) -> int:
        key = (id(item), source_id)
        if key not in self._costs:
            self._costs[key] = (item, estimate_units(render_item(item, source_id)))
        return self._costs[key][1]


class ContextUnits:
    """Maintain the exact rendered estimate while ranked items are appended."""

    def __init__(
        self, initial: list[dict], pool: list[dict], costs: ContextUnitCosts | None = None
    ):
        costs = costs or ContextUnitCosts()
        items = {item["unit_id"]: item for item in [*initial, *pool]}
        passages = {
            identifier: item for identifier, item in items.items() if item["kind"] == "passage"
        }
        self.fact_sources = {
            identifier: source
            for identifier, item in items.items()
            if (source := _supported_passage_id(item, passages)) is not None
        }
        self.passage_facts: dict[str, list[str]] = {}
        for fact_id, passage_id in self.fact_sources.items():
            self.passage_facts.setdefault(passage_id, []).append(fact_id)
        self.raw_units = {identifier: costs.units(item) for identifier, item in items.items()}
        self.grouped_units = {
            identifier: costs.units(items[identifier], source)
            for identifier, source in self.fact_sources.items()
        }
        self.selected_ids: set[str] = set()
        self.selected_passages: set[str] = set()
        self.units = 0
        for item in initial:
            self.add(item)

    def candidate_units(self, item: dict) -> int:
        identifier = item["unit_id"]
        if identifier in self.selected_ids:
            return self.units
        source = self.fact_sources.get(identifier)
        item_units = (
            self.grouped_units[identifier]
            if source in self.selected_passages
            else self.raw_units[identifier]
        )
        change = item_units + (2 if self.selected_ids else 0)
        if item["kind"] == "passage":
            for fact_id in self.passage_facts.get(identifier, []):
                if fact_id in self.selected_ids:
                    change += self.grouped_units[fact_id] - self.raw_units[fact_id]
        return self.units + change

    def add(self, item: dict) -> None:
        identifier = item["unit_id"]
        if identifier in self.selected_ids:
            return
        self.units = self.candidate_units(item)
        self.selected_ids.add(identifier)
        if item["kind"] == "passage":
            self.selected_passages.add(identifier)


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


def pack_ranked(
    items: list[dict],
    budget: int,
    limit: int,
    initial: list[dict] | None = None,
    *,
    costs: ContextUnitCosts | None = None,
):
    """Admit ranked items against the declared online token estimate."""
    costs = costs or ContextUnitCosts()
    chosen = list(initial or [])
    seen = {item["unit_id"] for item in chosen}
    units = sum(costs.units(item) + 2 for item in chosen)
    for item in items:
        if len(chosen) >= limit:
            break
        if item["unit_id"] in seen:
            continue
        additional = costs.units(item) + 2
        if estimated_tokens(units + additional) > budget:
            continue
        chosen.append(item)
        seen.add(item["unit_id"])
        units += additional
    return chosen


def source_window_ids(seed_items: list[dict], passage_locators: list[dict]) -> list[str]:
    """Find cited passages and adjacent user turns within eligible source revisions."""
    by_source = {}
    by_id = {}
    for row in passage_locators:
        locator = row["locator"]
        if not isinstance(locator, dict) or type(locator.get("message_index")) is not int:
            continue
        key = (row["source_id"], row["source_version"])
        by_source.setdefault(key, []).append(row)
        by_id[row["passage_id"]] = row
    for group in by_source.values():
        group.sort(
            key=lambda row: (
                row["locator"]["message_index"],
                row["locator"].get("passage_index", 0),
                row["unit_id"],
            )
        )

    groups = {}
    seen = set()
    for item in seed_items:
        if item["kind"] != "fact":
            continue
        for citation in item["citations"]:
            key = (citation.get("source_id"), citation.get("source_version"))
            index = citation.get("span", {}).get("message_index")
            if type(index) is not int or key not in by_source:
                continue
            passages = by_source[key]
            direct = by_id.get(citation.get("passage_id"))
            selected = [direct] if direct is not None and direct in passages else []
            user_rows = [row for row in passages if row["locator"].get("role") == "user"]
            before = [row for row in user_rows if row["locator"]["message_index"] < index]
            after = [row for row in user_rows if row["locator"]["message_index"] > index]
            selected.extend(before[-1:] + after[:1])
            for row in selected:
                if row["unit_id"] not in seen:
                    groups.setdefault(key, []).append(row)
                    seen.add(row["unit_id"])
    for group in groups.values():
        group.sort(
            key=lambda row: (
                row["locator"]["message_index"],
                row["locator"].get("passage_index", 0),
                row["unit_id"],
            )
        )
    return [
        group[index]["unit_id"]
        for index in range(max(map(len, groups.values()), default=0))
        for group in groups.values()
        if index < len(group)
    ]


def assemble_context(
    candidates: list[dict],
    seed_items: list[dict],
    source_passages: list[dict],
    *,
    budget: int,
    seed_budget: int,
    detail_fraction: float,
    max_results: int,
    costs: ContextUnitCosts | None = None,
) -> tuple[list[dict], list[str]]:
    """Protect ranked facts and bounded source windows, then fill from ranked evidence."""
    costs = costs or ContextUnitCosts()
    primary = [
        item
        for item in seed_items
        if not (
            item["kind"] == "passage"
            and item["citations"]
            and item["citations"][0].get("role") == "assistant"
        )
    ]
    primary_budget = int(seed_budget * (1 - detail_fraction))
    detail_budget = int(seed_budget * detail_fraction)
    primary_selected = pack_ranked(primary, primary_budget, max_results, costs=costs)
    detail_selected = pack_ranked(source_passages, detail_budget, max_results, costs=costs)
    chosen = pack_ranked(primary_selected + detail_selected, seed_budget, max_results, costs=costs)
    chosen = pack_ranked(primary + source_passages, seed_budget, max_results, chosen, costs=costs)
    seen = {item["unit_id"] for item in chosen}
    counter = ContextUnits(chosen, candidates, costs)
    budget_omissions = []
    for item in candidates:
        if len(chosen) >= max_results:
            break
        if item["unit_id"] in seen:
            continue
        if estimated_tokens(counter.candidate_units(item)) > budget:
            budget_omissions.append(item["unit_id"])
            continue
        chosen.append(item)
        seen.add(item["unit_id"])
        counter.add(item)
    return chosen, budget_omissions
