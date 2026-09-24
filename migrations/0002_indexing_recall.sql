-- Source-backed retrieval projections. The operator supplies the vector dimension.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE index_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    embedding_dimensions integer NOT NULL CHECK (embedding_dimensions BETWEEN 1 AND 2000)
);
INSERT INTO index_settings(embedding_dimensions) VALUES (__OAM_EMBEDDING_DIMENSIONS__);
GRANT SELECT ON index_settings TO oam_content;

ALTER TABLE scopes ADD COLUMN index_generation_id text;
ALTER TABLE operations ADD COLUMN stage_state jsonb NOT NULL DEFAULT '{}';

CREATE TABLE index_stage_results (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    source_id text NOT NULL,
    source_version integer NOT NULL,
    generation_id text NOT NULL,
    stage_key text NOT NULL,
    input_digest bytea NOT NULL CHECK (octet_length(input_digest) = 32),
    output jsonb NOT NULL,
    usage jsonb NOT NULL DEFAULT '{}',
    work_unit_id text NOT NULL,
    attempt_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,scope_id,source_id,source_version,generation_id,stage_key,input_digest),
    FOREIGN KEY(tenant_id,scope_id,source_id,source_version)
        REFERENCES source_versions(tenant_id,scope_id,source_id,source_version),
    FOREIGN KEY(tenant_id,scope_id,work_unit_id,attempt_id)
        REFERENCES attempts(tenant_id,scope_id,work_unit_id,attempt_id)
);

CREATE TABLE search_units (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    unit_id text NOT NULL,
    kind text NOT NULL CHECK(kind IN ('passage','fact','insight','resource_passage')),
    origin text NOT NULL CHECK(origin IN ('memory','evidence')),
    source_id text,
    source_version integer,
    evidence_id text,
    passage_id text,
    memory_id text,
    insight_id text,
    insight_revision integer,
    generation_id text NOT NULL,
    content_text text NOT NULL,
    locator jsonb NOT NULL,
    ordinal integer NOT NULL DEFAULT 0,
    embedding vector(__OAM_EMBEDDING_DIMENSIONS__) NOT NULL,
    lexical_text text NOT NULL,
    lexical tsvector GENERATED ALWAYS AS (to_tsvector('simple', lexical_text)) STORED,
    mentioned_at timestamptz,
    source_created_at timestamptz,
    source_updated_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,scope_id,unit_id),
    FOREIGN KEY(tenant_id,scope_id,source_id,source_version)
        REFERENCES source_versions(tenant_id,scope_id,source_id,source_version),
    FOREIGN KEY(tenant_id,scope_id,evidence_id) REFERENCES evidence(tenant_id,scope_id,evidence_id),
    FOREIGN KEY(tenant_id,scope_id,passage_id) REFERENCES passages(tenant_id,scope_id,passage_id),
    FOREIGN KEY(tenant_id,scope_id,memory_id) REFERENCES memories(tenant_id,scope_id,memory_id),
    FOREIGN KEY(tenant_id,scope_id,insight_id,insight_revision)
        REFERENCES insights(tenant_id,scope_id,insight_id,revision),
    CHECK ((kind='fact' AND memory_id IS NOT NULL AND passage_id IS NULL AND insight_id IS NULL)
        OR (kind='insight' AND insight_id IS NOT NULL AND insight_revision IS NOT NULL
            AND memory_id IS NULL AND passage_id IS NULL)
        OR (kind IN ('passage','resource_passage') AND passage_id IS NOT NULL
            AND memory_id IS NULL AND insight_id IS NULL)),
    CHECK ((kind='resource_passage') = (origin='evidence'))
);

CREATE TABLE unit_times (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    annotation_id text NOT NULL,
    unit_id text NOT NULL,
    role text NOT NULL CHECK(role IN ('event','date_reference')),
    time_span tstzrange NOT NULL CHECK(NOT isempty(time_span) AND NOT lower_inf(time_span)
                                    AND NOT upper_inf(time_span)),
    detail jsonb NOT NULL,
    PRIMARY KEY(tenant_id,scope_id,annotation_id),
    UNIQUE(tenant_id,scope_id,unit_id,annotation_id),
    FOREIGN KEY(tenant_id,scope_id,unit_id) REFERENCES search_units(tenant_id,scope_id,unit_id)
);

CREATE TABLE unit_entities (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    unit_id text NOT NULL,
    entity_key text NOT NULL,
    PRIMARY KEY(tenant_id,scope_id,unit_id,entity_key),
    FOREIGN KEY(tenant_id,scope_id,unit_id) REFERENCES search_units(tenant_id,scope_id,unit_id)
);

CREATE TABLE unit_links (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    source_id text NOT NULL,
    target_id text NOT NULL,
    kind text NOT NULL CHECK(kind IN ('semantic','caused_by','updates')),
    weight double precision NOT NULL CHECK(weight>0 AND weight<=1),
    PRIMARY KEY(tenant_id,scope_id,source_id,target_id,kind),
    FOREIGN KEY(tenant_id,scope_id,source_id) REFERENCES search_units(tenant_id,scope_id,unit_id),
    FOREIGN KEY(tenant_id,scope_id,target_id) REFERENCES search_units(tenant_id,scope_id,unit_id),
    CHECK(source_id<>target_id)
);

CREATE TABLE temporal_links (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    time_basis text NOT NULL CHECK(time_basis IN ('event','referenced','mentioned','created','updated')),
    left_id text NOT NULL,
    right_id text NOT NULL,
    left_annotation_id text,
    right_annotation_id text,
    gap_seconds double precision NOT NULL CHECK(gap_seconds>=0),
    weight double precision NOT NULL CHECK(weight>0 AND weight<=1),
    PRIMARY KEY(tenant_id,scope_id,time_basis,left_id,right_id),
    FOREIGN KEY(tenant_id,scope_id,left_id) REFERENCES search_units(tenant_id,scope_id,unit_id),
    FOREIGN KEY(tenant_id,scope_id,right_id) REFERENCES search_units(tenant_id,scope_id,unit_id),
    FOREIGN KEY(tenant_id,scope_id,left_id,left_annotation_id)
        REFERENCES unit_times(tenant_id,scope_id,unit_id,annotation_id),
    FOREIGN KEY(tenant_id,scope_id,right_id,right_annotation_id)
        REFERENCES unit_times(tenant_id,scope_id,unit_id,annotation_id),
    CHECK(left_id<right_id),
    CHECK ((time_basis IN ('event','referenced') AND left_annotation_id IS NOT NULL AND right_annotation_id IS NOT NULL)
        OR (time_basis IN ('mentioned','created','updated') AND left_annotation_id IS NULL AND right_annotation_id IS NULL))
);

CREATE INDEX search_units_source ON search_units(tenant_id,scope_id,source_id,source_version);
CREATE INDEX search_units_lexical ON search_units USING gin(lexical);
CREATE INDEX search_units_embedding ON search_units USING hnsw(embedding vector_cosine_ops);
CREATE INDEX search_units_mentioned ON search_units(tenant_id,scope_id,mentioned_at,unit_id);
CREATE INDEX search_units_created ON search_units(tenant_id,scope_id,source_created_at,unit_id);
CREATE INDEX search_units_updated ON search_units(tenant_id,scope_id,source_updated_at,unit_id);
CREATE INDEX unit_times_span ON unit_times USING gist(time_span);
CREATE INDEX unit_times_unit ON unit_times(tenant_id,scope_id,unit_id,role);
CREATE INDEX unit_entities_entity ON unit_entities(tenant_id,scope_id,entity_key,unit_id);
CREATE INDEX unit_links_forward ON unit_links(tenant_id,scope_id,source_id,kind,weight DESC,target_id);
CREATE INDEX unit_links_reverse ON unit_links(tenant_id,scope_id,target_id,kind,weight DESC,source_id);
CREATE INDEX temporal_links_left ON temporal_links(tenant_id,scope_id,time_basis,left_id,weight DESC,right_id);
CREATE INDEX temporal_links_right ON temporal_links(tenant_id,scope_id,time_basis,right_id,weight DESC,left_id);

DO $policies$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['index_stage_results','search_units','unit_times',
                                     'unit_entities','unit_links','temporal_links'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',table_name);
        EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO oam_content USING '
            ||'(oam_scope_visible(tenant_id,scope_id)) WITH CHECK '
            ||'(oam_scope_visible(tenant_id,scope_id))',table_name||'_scope_policy',table_name);
        EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON %I TO oam_content',table_name);
    END LOOP;
END
$policies$;
