-- Add source-supported identities without reinterpreting legacy spelling keys.
CREATE TABLE entity_units (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    entity_id text NOT NULL,
    generation_id text NOT NULL,
    canonical_name text NOT NULL,
    PRIMARY KEY (tenant_id,user_id,entity_id),
    FOREIGN KEY (tenant_id,user_id) REFERENCES users(tenant_id,user_id)
);
CREATE TABLE entity_names (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    name_id text NOT NULL,
    entity_id text NOT NULL,
    name_text text NOT NULL,
    name_key text NOT NULL,
    name_kind text NOT NULL CHECK(name_kind IN ('canonical','alias')),
    lexical_english text NOT NULL,
    lexical_simple text NOT NULL,
    lexical tsvector GENERATED ALWAYS AS (
        to_tsvector('english',lexical_english)||to_tsvector('simple',lexical_simple)
    ) STORED,
    PRIMARY KEY (tenant_id,user_id,name_id),
    UNIQUE (tenant_id,user_id,entity_id,name_key),
    UNIQUE (tenant_id,user_id,entity_id,name_id),
    FOREIGN KEY (tenant_id,user_id,entity_id) REFERENCES entity_units(tenant_id,user_id,entity_id)
);
CREATE TABLE entity_name_support (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    support_id text NOT NULL,
    name_id text NOT NULL,
    memory_id text NOT NULL,
    passage_id text NOT NULL,
    quote text NOT NULL,
    support_kind text NOT NULL CHECK(support_kind IN ('canonical','alias')),
    anchor_name_id text,
    PRIMARY KEY (tenant_id,user_id,support_id),
    FOREIGN KEY (tenant_id,user_id,name_id) REFERENCES entity_names(tenant_id,user_id,name_id),
    FOREIGN KEY (tenant_id,user_id,anchor_name_id) REFERENCES entity_names(tenant_id,user_id,name_id),
    FOREIGN KEY (tenant_id,user_id,memory_id) REFERENCES memories(tenant_id,user_id,memory_id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id,user_id,passage_id) REFERENCES passages(tenant_id,user_id,passage_id) ON DELETE CASCADE,
    CHECK ((support_kind='canonical' AND anchor_name_id IS NULL)
        OR (support_kind='alias' AND anchor_name_id IS NOT NULL))
);
ALTER TABLE unit_entities ADD COLUMN entity_id text;
ALTER TABLE unit_entities ADD COLUMN name_id text;
ALTER TABLE unit_entities ADD COLUMN observed_mention text;
ALTER TABLE unit_entities ADD COLUMN mention_quote text;
ALTER TABLE unit_entities ADD COLUMN generation_id text;
ALTER TABLE unit_entities ADD FOREIGN KEY (tenant_id,user_id,entity_id,name_id)
    REFERENCES entity_names(tenant_id,user_id,entity_id,name_id);
ALTER TABLE unit_entities ADD CHECK (
    (entity_id IS NULL AND name_id IS NULL AND observed_mention IS NULL
        AND mention_quote IS NULL AND generation_id IS NULL)
    OR (entity_id IS NOT NULL AND name_id IS NOT NULL AND observed_mention IS NOT NULL
        AND mention_quote IS NOT NULL AND generation_id IS NOT NULL)
);
CREATE UNIQUE INDEX unit_entities_identity ON unit_entities(tenant_id,user_id,unit_id,entity_id)
    WHERE entity_id IS NOT NULL;
CREATE INDEX unit_entities_resolved ON unit_entities(tenant_id,user_id,entity_id,unit_id)
    WHERE entity_id IS NOT NULL;
CREATE INDEX entity_names_exact ON entity_names(tenant_id,user_id,name_key);
CREATE INDEX entity_names_fts ON entity_names USING gin(lexical);
CREATE INDEX entity_name_support_name ON entity_name_support(tenant_id,user_id,name_id);
CREATE INDEX entity_name_support_anchor ON entity_name_support(tenant_id,user_id,anchor_name_id);
DO $policies$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['entity_units','entity_names','entity_name_support'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',table_name);
        EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO oam_content USING '
            ||'(oam_user_visible(tenant_id,user_id)) WITH CHECK '
            ||'(oam_user_visible(tenant_id,user_id))',table_name||'_user_policy',table_name);
        EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON %I TO oam_content',table_name);
    END LOOP;
END
$policies$;
