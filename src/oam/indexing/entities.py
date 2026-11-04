"""Source-supported entity identity and names, resolved only during indexing."""

from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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
            "AND binding.name_key=%s "
            "AND (%s::text[] IS NULL OR binding.name_id=ANY(%s)) "
            "ORDER BY binding.entity_id,binding.name_id LIMIT 2",
            (
                offered,
                self._name_key(name),
                [ref["name_id"] for ref in references if ref.get("name_id")] or None,
                [ref["name_id"] for ref in references if ref.get("name_id")] or None,
            ),
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
            metadata = {"name": row["name_text"], "quote": row["quote"], "name_id": row["name_id"]}
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
        existing = self._rows(
            "SELECT name_id FROM entity_names WHERE tenant_id=%s AND user_id=%s "
            "AND entity_id=%s AND name_key=%s",
            (*self.key, entity_id, self._name_key(name)),
        )
        if existing:
            return existing[0]["name_id"]
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

    def _new_identity(self, memory_id: str, name: str, quote: str, *, object_key="") -> dict:
        identity = (*self.key, self.generation_id, memory_id, self._name_key(name))
        entity_id = stable_id("entity", *identity, *((object_key,) if object_key else ()))
        self.connection.execute(
            "INSERT INTO entity_units(tenant_id,user_id,entity_id,generation_id,canonical_name) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (*self.key, entity_id, self.generation_id, name),
        )
        name_id = self._add_name(entity_id, name, "canonical")
        self._support(name_id, memory_id, quote)
        if object_key:
            self._label(entity_id, name)
        return {"entity_id": entity_id, "name_id": name_id, "name_kind": "canonical"}

    def _label(self, entity_id: str, name: str) -> None:
        base_key = self._name_key(name)
        row = self._rows(
            "SELECT canonical_key FROM entity_units WHERE tenant_id=%s "
            "AND user_id=%s AND entity_id=%s",
            (*self.key, entity_id),
        )[0]
        if row["canonical_key"] is not None:
            return
        family = self._rows(
            "SELECT DISTINCT e.entity_id,e.canonical_name,e.canonical_key "
            f"FROM ({ELIGIBLE_ENTITY_NAME_SUPPORT}) n JOIN entity_units e "
            "USING(tenant_id,user_id,entity_id) WHERE n.name_key=%s "
            "AND n.support_kind='canonical' AND e.entity_id<>%s ORDER BY e.entity_id",
            (base_key, entity_id),
        )

        def assign(target, base, numbered):
            suffix = 1 if numbered else 0
            while True:
                tail = f"-{suffix}" if suffix else ""
                prefix = base
                while True:
                    try:
                        key = self._name_key(prefix + tail)
                        break
                    except ValueError:
                        prefix = prefix[:-1]
                        if not prefix:
                            raise
                taken = self._rows(
                    "SELECT entity_id FROM entity_units WHERE tenant_id=%s AND user_id=%s "
                    "AND canonical_key=%s AND entity_id<>%s",
                    (*self.key, key, target),
                )
                legacy = self._rows(
                    f"SELECT 1 FROM ({ELIGIBLE_ENTITY_NAME_SUPPORT}) n JOIN entity_units e "
                    "USING(tenant_id,user_id,entity_id) WHERE e.canonical_key IS NULL "
                    "AND n.support_kind='canonical' AND n.name_key=%s AND e.entity_id<>%s LIMIT 1",
                    (key, target),
                )
                if legacy:
                    suffix += 1
                    continue
                if not taken:
                    self.connection.execute(
                        "UPDATE entity_units SET canonical_name=%s,canonical_key=%s,"
                        "canonical_base_key=%s WHERE tenant_id=%s AND user_id=%s AND entity_id=%s",
                        (prefix + tail, key, self._name_key(base), *self.key, target),
                    )
                    return
                holder = taken[0]["entity_id"]
                supported = self._rows(
                    f"SELECT 1 FROM ({ELIGIBLE_ENTITY_NAME_SUPPORT}) n JOIN entity_units e "
                    "USING(tenant_id,user_id,entity_id) WHERE e.entity_id=%s "
                    "AND n.name_key=e.canonical_base_key AND n.support_kind='canonical' LIMIT 1",
                    (holder,),
                )
                if not supported:
                    self.connection.execute(
                        "UPDATE entity_units SET canonical_key=NULL "
                        "WHERE tenant_id=%s AND user_id=%s AND entity_id=%s",
                        (*self.key, holder),
                    )
                    continue
                suffix += 1

        if len(family) == 1 and family[0]["canonical_key"] in (None, base_key):
            assign(family[0]["entity_id"], family[0]["canonical_name"], True)
        assign(entity_id, name, bool(family))

    def _exact(self, name: str) -> list[dict]:
        return self._rows(
            "SELECT DISTINCT entity_id,name_id,name_kind FROM "
            f"({ELIGIBLE_ENTITY_NAME_SUPPORT}) n WHERE n.name_key=%s "
            "ORDER BY entity_id,name_id LIMIT %s",
            (self._name_key(name), self.config.index.entity_resolution_candidates + 1),
        )

    def _publish_resolved(self, memory_id, mentions, aliases, objects, candidates):
        published = []
        for object_key in dict.fromkeys(mention["object"] for mention in mentions):
            group = [m for m in mentions if m["object"] == object_key]
            first = group[0]
            if first["decision"] == "unresolved":
                self.diagnostics.append("unresolved_entity")
                continue
            exact = self._exact(first["name"])
            all_exact = {
                row["entity_id"] for mention in group for row in self._exact(mention["name"])
            }
            if len(exact) > self.config.index.entity_resolution_candidates:
                self.diagnostics.append("incomplete_entity_candidates")
                continue
            known = {binding["entity_id"] for binding in objects.values() if binding}
            for candidate in candidates:
                if self._name_key(candidate["name"]) in {self._name_key(m["name"]) for m in group}:
                    binding = self._reference(candidate["name"], (candidate,))
                    if binding:
                        known.add(binding["entity_id"])
            if all_exact - known:
                self.diagnostics.append("incomplete_entity_candidates")
                continue
            resolved = objects.get(object_key)
            if resolved is None and first["decision"] == "same":
                reference = {"name": first["name"], "reference_id": first.get("reference_id")}
                if first.get("reference_name_id"):
                    reference["name_id"] = first["reference_name_id"]
                selected = [
                    candidate
                    for candidate in candidates
                    if candidate["reference_id"] == reference["reference_id"]
                    and self._name_key(candidate["name"]) == self._name_key(first["name"])
                    and (
                        not reference.get("name_id") or candidate["name_id"] == reference["name_id"]
                    )
                ]
                resolved = self._reference(first["name"], tuple(selected))
            elif resolved is None and first["decision"] in {"new", "different"}:
                if first["decision"] == "new" and exact:
                    self.diagnostics.append("unresolved_entity")
                    continue
                if any(target not in objects for target in first.get("different_from", ())):
                    self.diagnostics.append("invalid_entity_difference")
                    continue
                resolved = self._new_identity(
                    memory_id, first["name"], first["quote"], object_key=object_key
                )
            if resolved is None:
                self.diagnostics.append("unresolved_entity")
                continue
            # A local object handle carries identity, never proof for another spelling.
            observed = next(
                (
                    binding
                    for binding in self._exact(first["name"])
                    if binding["entity_id"] == resolved["entity_id"]
                ),
                None,
            )
            if observed is None:
                self.diagnostics.append("unresolved_entity")
                continue
            bindings = {}
            representative = first
            resolved = observed
            for mention in group:
                binding = next(
                    (
                        row
                        for row in self._exact(mention["name"])
                        if row["entity_id"] == resolved["entity_id"]
                    ),
                    None,
                )
                if binding is None:
                    continue
                bindings[self._name_key(mention["name"])] = binding
                if binding["name_kind"] == "canonical":
                    self._support(binding["name_id"], memory_id, mention["quote"])
                    self._label(binding["entity_id"], mention["name"])
                    resolved, representative = binding, mention
            objects[object_key] = resolved
            for assertion in aliases:
                if assertion.get("object") not in (None, object_key):
                    continue
                assertion_keys = {
                    self._name_key(assertion[key]) for key in ("canonical_name", "alias")
                }
                if not assertion_keys.intersection(bindings):
                    continue
                asserted = [
                    self._reference(name, tuple(candidates))
                    for name in (assertion["canonical_name"], assertion["alias"])
                ]
                if any(
                    binding and binding["entity_id"] != resolved["entity_id"]
                    for binding in asserted
                ):
                    self.diagnostics.append("entity_identity_conflict")
                    continue
                anchor = resolved
                if anchor["name_kind"] != "canonical":
                    anchors = self._rows(
                        "SELECT DISTINCT name_id,name_key,name_text FROM "
                        f"({ELIGIBLE_ENTITY_NAME_SUPPORT}) n "
                        "WHERE entity_id=%s AND name_kind='canonical' AND support_kind='canonical' "
                        "AND name_key=ANY(%s) ORDER BY name_id LIMIT 2",
                        (resolved["entity_id"], list(assertion_keys)),
                    )
                    if len(anchors) != 1:
                        self.diagnostics.append("dependent_alias_anchor")
                        continue
                    anchor = {
                        **resolved,
                        "name_id": anchors[0]["name_id"],
                        "name_kind": "canonical",
                    }
                    self._support(anchor["name_id"], memory_id, assertion["quote"])
                    self._label(anchor["entity_id"], anchors[0]["name_text"])
                anchor_key = self._rows(
                    "SELECT name_key FROM entity_names WHERE tenant_id=%s "
                    "AND user_id=%s AND name_id=%s",
                    (*self.key, anchor["name_id"]),
                )[0]["name_key"]
                for name in (assertion["canonical_name"], assertion["alias"]):
                    key = self._name_key(name)
                    if key == anchor_key:
                        bindings[key] = anchor
                        continue
                    name_id = self._add_name(resolved["entity_id"], name, "alias")
                    self._support(name_id, memory_id, assertion["quote"], anchor=anchor["name_id"])
                    bindings[key] = {
                        "entity_id": resolved["entity_id"],
                        "name_id": name_id,
                        "name_kind": "alias",
                    }
            source_mentions = [
                {
                    "name": m["name"],
                    "quote": m["quote"],
                    "span": m.get("span"),
                    "kind": bindings[self._name_key(m["name"])]["name_kind"],
                    "name_id": bindings[self._name_key(m["name"])]["name_id"],
                }
                for m in group
                if self._name_key(m["name"]) in bindings
            ]
            self.connection.execute(
                "INSERT INTO unit_entities(tenant_id,user_id,unit_id,entity_key,entity_id,name_id,"
                "observed_mention,mention_quote,generation_id,source_mentions) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id,user_id,unit_id,entity_id) WHERE entity_id IS NOT NULL "
                "DO UPDATE SET name_id=CASE WHEN (SELECT name_kind FROM entity_names n "
                "WHERE (n.tenant_id,n.user_id,n.name_id)="
                "(EXCLUDED.tenant_id,EXCLUDED.user_id,EXCLUDED.name_id))='canonical' "
                "THEN EXCLUDED.name_id ELSE unit_entities.name_id END,"
                "observed_mention=CASE WHEN (SELECT name_kind FROM entity_names n "
                "WHERE (n.tenant_id,n.user_id,n.name_id)="
                "(EXCLUDED.tenant_id,EXCLUDED.user_id,EXCLUDED.name_id))='canonical' "
                "THEN EXCLUDED.observed_mention ELSE unit_entities.observed_mention END,"
                "mention_quote=CASE WHEN (SELECT name_kind FROM entity_names n "
                "WHERE (n.tenant_id,n.user_id,n.name_id)="
                "(EXCLUDED.tenant_id,EXCLUDED.user_id,EXCLUDED.name_id))='canonical' "
                "THEN EXCLUDED.mention_quote ELSE unit_entities.mention_quote END,"
                "source_mentions=(SELECT jsonb_agg(DISTINCT value) FROM "
                "jsonb_array_elements(unit_entities.source_mentions||EXCLUDED.source_mentions))",
                (
                    *self.key,
                    memory_id,
                    "identity:" + resolved["entity_id"],
                    resolved["entity_id"],
                    resolved["name_id"],
                    representative["name"],
                    representative["quote"],
                    self.generation_id,
                    Jsonb(source_mentions),
                ),
            )
            if resolved["entity_id"] not in published:
                published.append(resolved["entity_id"])
        return tuple(published)

    def publish_fact(
        self,
        memory_id: str,
        mentions: tuple[str, ...],
        quote: str,
        *,
        aliases: tuple[dict, ...] = (),
        references: tuple[dict, ...] = (),
        prepared: dict[str, dict | None] | None = None,
        entity_mentions: tuple[dict, ...] | None = None,
        objects: dict | None = None,
        candidates: tuple[dict, ...] = (),
    ) -> tuple[str, ...]:
        selected: dict[str, dict] = {}
        self.diagnostics: list[str] = []
        if entity_mentions is not None:
            return self._publish_resolved(
                memory_id,
                entity_mentions,
                aliases,
                objects if objects is not None else {},
                candidates,
            )
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
