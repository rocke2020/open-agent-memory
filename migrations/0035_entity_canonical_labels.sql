-- Navigation labels are separate from source-backed name bindings.
ALTER TABLE entity_units ADD COLUMN canonical_key text;
ALTER TABLE entity_units ADD COLUMN canonical_base_key text;
CREATE UNIQUE INDEX entity_units_canonical_key
    ON entity_units(tenant_id,user_id,canonical_key) WHERE canonical_key IS NOT NULL;
ALTER TABLE unit_entities ADD COLUMN source_mentions jsonb NOT NULL DEFAULT '[]'::jsonb
    CHECK(jsonb_typeof(source_mentions)='array');
