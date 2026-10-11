"""Source-supported entity identity and names, resolved only during indexing."""

from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from oam.config import OAMConfig
from oam.db.eligibility import ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS
from oam.db.entity_eligibility import (
    ELIGIBLE_ENTITY_MEMBERSHIPS,
    ELIGIBLE_ENTITY_NAME_SUPPORT,
    ENTITY_PROJECTIONS_FROM_FILTERED_FACTS,
)
from oam.indexing.entity_names import entity_name_key
from oam.indexing.store import stable_id
from oam.retrieval.fts import index_streams


class EntityRepository:
    def __init__(
        self, connection: Connection, config: OAMConfig, *, generation_id: str, key: tuple[str, str]
    ):
        self.connection = connection
        self.config = config
        self.generation_id = generation_id
        self.key = key
        connection.execute(
            "SELECT set_config('oam.tenant_id',%s,true),set_config('oam.user_id',%s,true)", key
        )

    def _rows(self, sql: str, parameters: tuple = ()) -> list[dict[str, Any]]:
        with self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql, parameters)
            return cursor.fetchall()

    def _name_key(self, name: str) -> str:
        return entity_name_key(name, max_bytes=self.config.index.entity_name_max_bytes)

    def _reference(self, name: str, references: tuple[dict, ...]) -> dict | None:
        offered = [
            ref["reference_id"]
            for ref in references
            if self._name_key(ref["name"]) == self._name_key(name)
        ]
        if not offered:
            return None
        rows = self._rows(
            f"SELECT DISTINCT binding.entity_id,binding.name_id,binding.name_kind FROM "
            f"({ELIGIBLE_ENTITY_MEMBERSHIPS}) member JOIN ({ELIGIBLE_ENTITY_NAME_SUPPORT}) binding "
            "USING(tenant_id,user_id,entity_id) WHERE member.unit_id=ANY(%s) "
            "AND binding.name_key=%s AND binding.generation_id=%s "
            "ORDER BY binding.entity_id,binding.name_id LIMIT 2",
            (offered, self._name_key(name), self.generation_id),
        )
        return rows[0] if len({row["entity_id"] for row in rows}) == 1 else None

    def prepare_fact(
        self, mentions: tuple[str, ...], aliases: tuple[dict, ...], references: tuple[dict, ...]
    ) -> dict[str, dict | None]:
        names = tuple(
            dict.fromkeys(
                (*mentions, *(a["canonical_name"] for a in aliases), *(a["alias"] for a in aliases))
            )
        )
        result = {}
        for name in names:
            try:
                result[self._name_key(name)] = self._reference(name, references)
            except ValueError:
                continue
        return result

    def reference_entities(
        self, ids: list[str], *, preferred_name_ids: tuple[str, ...] = ()
    ) -> dict[str, list[dict]]:
        if not ids:
            return {}
        rows = self._rows(
            "WITH filtered AS MATERIALIZED (SELECT * FROM ("
            + ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS
            + ") eligible WHERE kind='fact')"
            + ENTITY_PROJECTIONS_FROM_FILTERED_FACTS
            + " SELECT member.unit_id,names.name_text,names.quote,names.name_id,names.name_kind "
            "FROM memberships member CROSS JOIN LATERAL "
            "(SELECT distinct_names.* FROM (SELECT DISTINCT ON (binding.name_id) binding.* "
            "FROM name_support binding "
            "WHERE (binding.tenant_id,binding.user_id,binding.entity_id)="
            "(member.tenant_id,member.user_id,member.entity_id) "
            "ORDER BY binding.name_id,binding.support_id) distinct_names "
            "ORDER BY (distinct_names.name_id=ANY(%s)) DESC,distinct_names.name_id LIMIT %s) names "
            "WHERE member.unit_id=ANY(%s) "
            "ORDER BY member.unit_id,names.name_kind,names.name_id",
            (list(preferred_name_ids), self.config.index.entity_search_names_per_entity, ids),
        )
        output: dict[str, list[dict]] = {}
        for row in rows:
            metadata = {"name": row["name_text"], "quote": row["quote"]}
            target = output.setdefault(row["unit_id"], [])
            if (
                metadata not in target
                and len(target)
                < self.config.index.entity_resolution_candidates
                * self.config.index.entity_search_names_per_entity
            ):
                target.append(metadata)
        return output

    def _add_name(self, entity_id: str, name: str, kind: str) -> str:
        name_id = stable_id(
            "entity_name", *self.key, self.generation_id, entity_id, self._name_key(name)
        )
        streams = index_streams(name)
        self.connection.execute(
            "INSERT INTO entity_names VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                *self.key,
                name_id,
                entity_id,
                name,
                self._name_key(name),
                kind,
                streams.english,
                streams.simple,
            ),
        )
        return name_id

    def _support(
        self, name_id: str, memory_id: str, quote: str, *, anchor: str | None = None
    ) -> None:
        support = self._rows(
            "SELECT locator->>'passage_id' AS passage FROM search_units "
            "WHERE tenant_id=%s AND user_id=%s AND unit_id=%s AND generation_id=%s",
            (*self.key, memory_id, self.generation_id),
        )
        if not support:
            return
        self.connection.execute(
            "INSERT INTO entity_name_support VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (
                *self.key,
                stable_id("entity_support", memory_id, name_id, quote),
                name_id,
                memory_id,
                support[0]["passage"],
                quote,
                "alias" if anchor else "canonical",
                anchor,
            ),
        )

    def _new_identity(self, memory_id: str, name: str, quote: str) -> dict:
        entity_id = stable_id(
            "entity", *self.key, self.generation_id, memory_id, self._name_key(name)
        )
        self.connection.execute(
            "INSERT INTO entity_units VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (*self.key, entity_id, self.generation_id, name),
        )
        name_id = self._add_name(entity_id, name, "canonical")
        self._support(name_id, memory_id, quote)
        return {"entity_id": entity_id, "name_id": name_id, "name_kind": "canonical"}

    def publish_fact(
        self,
        memory_id: str,
        mentions: tuple[str, ...],
        quote: str,
        *,
        aliases: tuple[dict, ...] = (),
        references: tuple[dict, ...] = (),
        prepared: dict[str, dict | None] | None = None,
    ) -> tuple[str, ...]:
        selected: dict[str, dict] = {}
        self.diagnostics: list[str] = []
        valid_mentions = []
        for name in mentions:
            try:
                self._name_key(name)
            except ValueError:
                self.diagnostics.append("invalid_entity_name")
                continue
            valid_mentions.append(name)

        rejected_references = set()

        def resolve(name):
            name_key = self._name_key(name)
            binding = self._reference(name, references)
            if binding is None and any(
                self._name_key(ref["name"]) == name_key for ref in references
            ):
                rejected_references.add(name_key)
            if prepared is not None and binding != prepared.get(name_key):
                self.diagnostics.append("changed_entity_reference")
                rejected_references.add(name_key)
                return None
            return binding

        for assertion in aliases:
            first, second = assertion["canonical_name"], assertion["alias"]
            first_key, second_key = self._name_key(first), self._name_key(second)
            first_binding = selected.get(first_key) or resolve(first)
            second_binding = selected.get(second_key) or resolve(second)
            bindings = [binding for binding in (first_binding, second_binding) if binding]
            if len({binding["entity_id"] for binding in bindings}) > 1:
                self.diagnostics.append("entity_identity_conflict")
                continue
            anchor = next(
                (binding for binding in bindings if binding["name_kind"] == "canonical"), None
            )
            if bindings and anchor is None:
                anchors = (
                    []
                    if {first_key, second_key} & rejected_references
                    else self._rows(
                        "SELECT DISTINCT binding.entity_id,binding.name_id,"
                        "binding.name_kind,binding.name_key "
                        f"FROM ({ELIGIBLE_ENTITY_NAME_SUPPORT}) binding "
                        "WHERE binding.entity_id=%s AND binding.generation_id=%s "
                        "AND binding.name_key=ANY(%s) AND binding.name_kind='canonical' "
                        "AND binding.support_kind='canonical' ORDER BY binding.name_id LIMIT 2",
                        (bindings[0]["entity_id"], self.generation_id, [first_key, second_key]),
                    )
                )
                if len(anchors) != 1:
                    self.diagnostics.append("dependent_alias_anchor")
                    continue
                recovered = anchors[0]
                anchor = {key: recovered[key] for key in ("entity_id", "name_id", "name_kind")}
                if recovered["name_key"] == first_key:
                    first_binding = anchor
                else:
                    second_binding = anchor
            if anchor is None:
                anchor = self._new_identity(memory_id, first, quote)
                first_binding = anchor
            self._support(anchor["name_id"], memory_id, quote)
            # A validated declaration is itself an observed same-object mention.
            if first not in valid_mentions:
                valid_mentions.append(first)
            for name, name_key, binding in (
                (first, first_key, first_binding),
                (second, second_key, second_binding),
            ):
                if binding and binding["name_id"] == anchor["name_id"]:
                    selected[name_key] = anchor
                    continue
                name_id = self._add_name(anchor["entity_id"], name, "alias")
                self._support(name_id, memory_id, assertion["quote"], anchor=anchor["name_id"])
                selected[name_key] = {
                    "entity_id": anchor["entity_id"],
                    "name_id": name_id,
                    "name_kind": "alias",
                }

        for name in valid_mentions:
            name_key = self._name_key(name)
            if name_key in selected:
                continue
            binding = resolve(name)
            if binding is None:
                binding = self._new_identity(memory_id, name, quote)
            elif binding["name_kind"] == "canonical":
                self._support(binding["name_id"], memory_id, quote)
            selected[name_key] = binding
        published = []
        for name in valid_mentions:
            resolved = selected[self._name_key(name)]
            if resolved["entity_id"] in published:
                continue
            self.connection.execute(
                "INSERT INTO unit_entities(tenant_id,user_id,unit_id,entity_key,entity_id,name_id,"
                "observed_mention,mention_quote,generation_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (
                    *self.key,
                    memory_id,
                    self._name_key(name),
                    resolved["entity_id"],
                    resolved["name_id"],
                    name,
                    quote,
                    self.generation_id,
                ),
            )
            published.append(resolved["entity_id"])
        return tuple(published)
