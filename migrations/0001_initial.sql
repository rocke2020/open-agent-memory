-- Open Agent Memory v0.0.1 initial PostgreSQL schema.

DO $roles$
BEGIN
    CREATE ROLE oam_content NOLOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL;
END
$roles$;

DO $roles$
BEGIN
    CREATE ROLE oam_api NOLOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL;
END
$roles$;

DO $roles$
BEGIN
    CREATE ROLE oam_queue NOLOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL;
END
$roles$;

DO $roles$
BEGIN
    CREATE ROLE oam_auth_owner NOLOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL;
END
$roles$;

DO $roles$
BEGIN
    CREATE ROLE oam_queue_owner NOLOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL;
END
$roles$;

GRANT oam_content TO oam_api;

CREATE TABLE schema_migrations (
    version integer PRIMARY KEY,
    name text NOT NULL,
    sha256 text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE tenants (
    tenant_id text PRIMARY KEY,
    state text NOT NULL CHECK (state IN ('active', 'disabled')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE principals (
    tenant_id text NOT NULL,
    principal_id text NOT NULL,
    role text NOT NULL,
    state text NOT NULL CHECK (state IN ('active', 'disabled')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, principal_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id)
);

CREATE TABLE credentials (
    tenant_id text NOT NULL,
    credential_id text NOT NULL,
    principal_id text NOT NULL,
    secret_digest bytea NOT NULL UNIQUE CHECK (octet_length(secret_digest) = 32),
    state text NOT NULL CHECK (state IN ('active', 'revoked')),
    expires_at timestamptz,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, credential_id),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES principals (tenant_id, principal_id),
    CHECK ((state = 'revoked') = (revoked_at IS NOT NULL))
);

CREATE TABLE bootstrap_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    phase text NOT NULL CHECK (phase IN ('uninitialized', 'committed', 'handed_off', 'recovery_required')),
    winner_id text,
    tenant_id text,
    principal_id text,
    credential_id text,
    committed_at timestamptz,
    handed_off_at timestamptz,
    CHECK ((phase = 'uninitialized') = (winner_id IS NULL))
);

INSERT INTO bootstrap_state (singleton, phase) VALUES (true, 'uninitialized');

CREATE TABLE scopes (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    subject text NOT NULL,
    namespace text NOT NULL,
    lifetime text NOT NULL CHECK (lifetime IN ('persistent', 'run')),
    run_id text,
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'deleted')),
    accepted_seq bigint NOT NULL DEFAULT 0,
    published_seq bigint NOT NULL DEFAULT 0,
    policy_epoch bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, scope_id),
    FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),
    CHECK ((lifetime = 'run') = (run_id IS NOT NULL))
);

CREATE TABLE grants (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    principal_id text NOT NULL,
    capabilities text[] NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, scope_id, principal_id),
    FOREIGN KEY (tenant_id, scope_id) REFERENCES scopes (tenant_id, scope_id),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES principals (tenant_id, principal_id)
);

CREATE TABLE sources (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    source_id text NOT NULL,
    input_role text NOT NULL CHECK (input_role IN ('interaction', 'evidence_resource', 'profile_resource', 'imported_claim')),
    resource_type text,
    active_version integer,
    status text NOT NULL CHECK (status IN ('active', 'superseded', 'deleting', 'deleted')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, scope_id, source_id),
    FOREIGN KEY (tenant_id, scope_id) REFERENCES scopes (tenant_id, scope_id)
);

CREATE TABLE source_versions (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    source_id text NOT NULL,
    source_version integer NOT NULL CHECK (source_version > 0),
    media_type text NOT NULL,
    original_bytes bytea NOT NULL,
    content_sha256 bytea NOT NULL CHECK (octet_length(content_sha256) = 32),
    byte_length bigint NOT NULL CHECK (byte_length >= 0),
    status text NOT NULL CHECK (status IN ('active', 'superseded', 'excluded')),
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, scope_id, source_id, source_version),
    FOREIGN KEY (tenant_id, scope_id, source_id) REFERENCES sources (tenant_id, scope_id, source_id),
    CHECK (octet_length(original_bytes) = byte_length)
);

ALTER TABLE sources ADD CONSTRAINT sources_active_version_fk
    FOREIGN KEY (tenant_id, scope_id, source_id, active_version)
    REFERENCES source_versions (tenant_id, scope_id, source_id, source_version)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE evidence (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    evidence_id text NOT NULL,
    source_id text NOT NULL,
    source_version integer NOT NULL,
    accepted_seq bigint NOT NULL,
    content_text text,
    event_time timestamptz,
    learning_eligible boolean NOT NULL DEFAULT false,
    status text NOT NULL CHECK (status IN ('active', 'excluded')),
    PRIMARY KEY (tenant_id, scope_id, evidence_id),
    FOREIGN KEY (tenant_id, scope_id, source_id, source_version)
        REFERENCES source_versions (tenant_id, scope_id, source_id, source_version)
);

CREATE TABLE passages (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    passage_id text NOT NULL,
    evidence_id text NOT NULL,
    parse_revision integer NOT NULL,
    locator jsonb NOT NULL,
    normalized_text text NOT NULL,
    generation_id text NOT NULL,
    visible_from bigint NOT NULL,
    visible_to bigint,
    PRIMARY KEY (tenant_id, scope_id, passage_id),
    FOREIGN KEY (tenant_id, scope_id, evidence_id) REFERENCES evidence (tenant_id, scope_id, evidence_id)
);

CREATE TABLE memories (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    memory_id text NOT NULL,
    canonical_claim text NOT NULL,
    assertion_kind text NOT NULL CHECK (assertion_kind IN ('stated', 'inferred', 'imported')),
    valid_from timestamptz,
    valid_to timestamptz,
    generation_id text NOT NULL,
    lifecycle_state text NOT NULL DEFAULT 'active' CHECK (lifecycle_state IN ('active', 'excluded')),
    visible_from bigint NOT NULL,
    visible_to bigint,
    PRIMARY KEY (tenant_id, scope_id, memory_id),
    FOREIGN KEY (tenant_id, scope_id) REFERENCES scopes (tenant_id, scope_id)
);

CREATE TABLE support (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    memory_id text NOT NULL,
    evidence_id text NOT NULL,
    passage_id text NOT NULL,
    support_kind text NOT NULL,
    span jsonb NOT NULL,
    PRIMARY KEY (tenant_id, scope_id, memory_id, evidence_id, passage_id),
    FOREIGN KEY (tenant_id, scope_id, memory_id) REFERENCES memories (tenant_id, scope_id, memory_id),
    FOREIGN KEY (tenant_id, scope_id, evidence_id) REFERENCES evidence (tenant_id, scope_id, evidence_id),
    FOREIGN KEY (tenant_id, scope_id, passage_id) REFERENCES passages (tenant_id, scope_id, passage_id)
);

CREATE TABLE insights (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    insight_id text NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    synthesis_text text NOT NULL,
    input_digest bytea NOT NULL CHECK (octet_length(input_digest) = 32),
    recipe_id text NOT NULL,
    generation_id text NOT NULL,
    visible_from bigint NOT NULL,
    visible_to bigint,
    PRIMARY KEY (tenant_id, scope_id, insight_id, revision),
    FOREIGN KEY (tenant_id, scope_id) REFERENCES scopes (tenant_id, scope_id)
);

CREATE TABLE insight_support (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    insight_id text NOT NULL,
    insight_revision integer NOT NULL,
    memory_id text NOT NULL,
    support_revision integer NOT NULL,
    PRIMARY KEY (tenant_id, scope_id, insight_id, insight_revision, memory_id),
    FOREIGN KEY (tenant_id, scope_id, insight_id, insight_revision)
        REFERENCES insights (tenant_id, scope_id, insight_id, revision),
    FOREIGN KEY (tenant_id, scope_id, memory_id) REFERENCES memories (tenant_id, scope_id, memory_id)
);

CREATE TABLE profiles (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    profile_id text NOT NULL,
    profile_revision integer NOT NULL CHECK (profile_revision > 0),
    sections jsonb NOT NULL,
    input_identity bytea NOT NULL CHECK (octet_length(input_identity) = 32),
    policy_epoch bigint NOT NULL,
    generation_id text NOT NULL,
    visible_from bigint NOT NULL,
    visible_to bigint,
    PRIMARY KEY (tenant_id, scope_id, profile_id, profile_revision),
    FOREIGN KEY (tenant_id, scope_id) REFERENCES scopes (tenant_id, scope_id)
);

CREATE TABLE operations (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    operation_id text NOT NULL,
    principal_id text NOT NULL,
    source_id text,
    source_version integer,
    accepted_seq bigint,
    kind text NOT NULL,
    schema_version integer NOT NULL,
    status text NOT NULL CHECK (status IN ('accepted', 'running', 'succeeded', 'partial_failed', 'failed_terminal', 'cancelled')),
    dedup_key text,
    request_hash bytea,
    plan_sealed boolean NOT NULL DEFAULT false,
    cancellation_revision bigint NOT NULL DEFAULT 0,
    retry_of_operation_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, scope_id, operation_id),
    FOREIGN KEY (tenant_id, scope_id) REFERENCES scopes (tenant_id, scope_id),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES principals (tenant_id, principal_id),
    FOREIGN KEY (tenant_id, scope_id, source_id, source_version)
        REFERENCES source_versions (tenant_id, scope_id, source_id, source_version),
    FOREIGN KEY (tenant_id, scope_id, retry_of_operation_id)
        REFERENCES operations (tenant_id, scope_id, operation_id),
    UNIQUE (tenant_id, scope_id, principal_id, kind, dedup_key)
);

CREATE TABLE work_units (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    work_unit_id text NOT NULL,
    operation_id text NOT NULL,
    stage text NOT NULL,
    chunk_group_id text NOT NULL DEFAULT '',
    input_digest bytea NOT NULL CHECK (octet_length(input_digest) = 32),
    generation_id text NOT NULL,
    dependency_ids text[] NOT NULL DEFAULT '{}',
    status text NOT NULL CHECK (status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed_terminal', 'cancelled')),
    owner_id text,
    fence bigint NOT NULL DEFAULT 0,
    lease_expires_at timestamptz,
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    attempts_remaining integer NOT NULL CHECK (attempts_remaining >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, scope_id, work_unit_id),
    FOREIGN KEY (tenant_id, scope_id, operation_id) REFERENCES operations (tenant_id, scope_id, operation_id),
    UNIQUE (tenant_id, scope_id, operation_id, stage, chunk_group_id, input_digest, generation_id)
);

CREATE TABLE attempts (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    work_unit_id text NOT NULL,
    attempt_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    owner_id text NOT NULL,
    fence bigint NOT NULL,
    disposition text NOT NULL,
    request_ref text,
    response_ref text,
    usage jsonb,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    PRIMARY KEY (tenant_id, scope_id, work_unit_id, attempt_id),
    FOREIGN KEY (tenant_id, scope_id, work_unit_id) REFERENCES work_units (tenant_id, scope_id, work_unit_id),
    UNIQUE (tenant_id, scope_id, work_unit_id, ordinal)
);

CREATE TABLE usage_records (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    usage_id text NOT NULL,
    work_unit_id text NOT NULL,
    attempt_id text NOT NULL,
    model_role text NOT NULL,
    meter text NOT NULL,
    quantity numeric NOT NULL CHECK (quantity >= 0),
    accounting_state text NOT NULL CHECK (accounting_state IN ('known', 'estimated', 'unknown')),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, scope_id, usage_id),
    FOREIGN KEY (tenant_id, scope_id, work_unit_id, attempt_id)
        REFERENCES attempts (tenant_id, scope_id, work_unit_id, attempt_id)
);

CREATE TABLE turn_deadlines (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    turn_key text NOT NULL,
    deadline_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, scope_id, turn_key),
    FOREIGN KEY (tenant_id, scope_id) REFERENCES scopes (tenant_id, scope_id)
);

CREATE TABLE traces (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    trace_id text NOT NULL,
    read_seq bigint NOT NULL,
    generation_id text NOT NULL,
    policy_epoch bigint NOT NULL,
    decisions jsonb NOT NULL,
    content jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, scope_id, trace_id),
    FOREIGN KEY (tenant_id, scope_id) REFERENCES scopes (tenant_id, scope_id)
);

CREATE TABLE exports (
    tenant_id text NOT NULL,
    scope_id text NOT NULL,
    export_id text NOT NULL,
    policy_epoch bigint NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'ready', 'failed', 'expired')),
    manifest jsonb NOT NULL,
    artifact_bytes bytea,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, scope_id, export_id),
    FOREIGN KEY (tenant_id, scope_id) REFERENCES scopes (tenant_id, scope_id)
);

CREATE INDEX credentials_secret_digest_idx ON credentials (secret_digest);
CREATE INDEX grants_principal_idx ON grants (tenant_id, principal_id, scope_id);
CREATE INDEX source_versions_active_idx ON source_versions (tenant_id, scope_id, source_id, status);
CREATE INDEX evidence_source_idx ON evidence (tenant_id, scope_id, source_id, source_version);
CREATE INDEX support_evidence_idx ON support (tenant_id, scope_id, evidence_id);
CREATE INDEX insight_support_memory_idx ON insight_support (tenant_id, scope_id, memory_id);
CREATE INDEX work_units_schedule_idx ON work_units (status, available_at, tenant_id, scope_id);
CREATE INDEX attempts_identity_idx ON attempts (tenant_id, scope_id, work_unit_id, ordinal);
CREATE INDEX usage_records_attempt_idx ON usage_records (tenant_id, scope_id, work_unit_id, attempt_id);
CREATE INDEX traces_expiry_idx ON traces (expires_at);
CREATE INDEX exports_expiry_idx ON exports (expires_at);

CREATE FUNCTION oam_scope_visible(row_tenant_id text, row_scope_id text)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL SAFE
SET search_path = pg_catalog
AS $function$
    SELECT NULLIF(current_setting('oam.tenant_id', true), '') = row_tenant_id
       AND NULLIF(current_setting('oam.scope_id', true), '') = row_scope_id
$function$;

REVOKE ALL ON FUNCTION oam_scope_visible(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_scope_visible(text, text) TO oam_content;

ALTER TABLE scopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE scopes FORCE ROW LEVEL SECURITY;
CREATE POLICY scopes_scope_policy ON scopes FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE grants FORCE ROW LEVEL SECURITY;
CREATE POLICY grants_scope_policy ON grants FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE sources FORCE ROW LEVEL SECURITY;
CREATE POLICY sources_scope_policy ON sources FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE source_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY source_versions_scope_policy ON source_versions FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence FORCE ROW LEVEL SECURITY;
CREATE POLICY evidence_scope_policy ON evidence FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE passages ENABLE ROW LEVEL SECURITY;
ALTER TABLE passages FORCE ROW LEVEL SECURITY;
CREATE POLICY passages_scope_policy ON passages FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories FORCE ROW LEVEL SECURITY;
CREATE POLICY memories_scope_policy ON memories FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE support ENABLE ROW LEVEL SECURITY;
ALTER TABLE support FORCE ROW LEVEL SECURITY;
CREATE POLICY support_scope_policy ON support FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE insights ENABLE ROW LEVEL SECURITY;
ALTER TABLE insights FORCE ROW LEVEL SECURITY;
CREATE POLICY insights_scope_policy ON insights FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE insight_support ENABLE ROW LEVEL SECURITY;
ALTER TABLE insight_support FORCE ROW LEVEL SECURITY;
CREATE POLICY insight_support_scope_policy ON insight_support FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE profiles FORCE ROW LEVEL SECURITY;
CREATE POLICY profiles_scope_policy ON profiles FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE operations FORCE ROW LEVEL SECURITY;
CREATE POLICY operations_scope_policy ON operations FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE work_units ENABLE ROW LEVEL SECURITY;
ALTER TABLE work_units FORCE ROW LEVEL SECURITY;
CREATE POLICY work_units_scope_policy ON work_units FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY attempts_scope_policy ON attempts FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE usage_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_records FORCE ROW LEVEL SECURITY;
CREATE POLICY usage_records_scope_policy ON usage_records FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE turn_deadlines ENABLE ROW LEVEL SECURITY;
ALTER TABLE turn_deadlines FORCE ROW LEVEL SECURITY;
CREATE POLICY turn_deadlines_scope_policy ON turn_deadlines FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE traces ENABLE ROW LEVEL SECURITY;
ALTER TABLE traces FORCE ROW LEVEL SECURITY;
CREATE POLICY traces_scope_policy ON traces FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));
ALTER TABLE exports ENABLE ROW LEVEL SECURITY;
ALTER TABLE exports FORCE ROW LEVEL SECURITY;
CREATE POLICY exports_scope_policy ON exports FOR ALL TO oam_content
    USING (oam_scope_visible(tenant_id, scope_id)) WITH CHECK (oam_scope_visible(tenant_id, scope_id));

GRANT SELECT, INSERT, UPDATE, DELETE ON
    scopes, grants, sources, source_versions, evidence, passages, memories, support,
    insights, insight_support, profiles, operations, work_units, attempts, usage_records,
    turn_deadlines, traces, exports
TO oam_content;

CREATE POLICY scopes_auth_owner_policy ON scopes FOR SELECT TO oam_auth_owner USING (true);
CREATE POLICY grants_auth_owner_policy ON grants FOR SELECT TO oam_auth_owner USING (true);
GRANT SELECT ON tenants, principals, credentials, scopes, grants TO oam_auth_owner;

CREATE FUNCTION oam_lookup_credential(p_secret_digest bytea)
RETURNS TABLE (
    credential_id text,
    tenant_id text,
    principal_id text,
    principal_role text,
    expires_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
    SELECT c.credential_id, c.tenant_id, c.principal_id, p.role, c.expires_at
    FROM public.credentials AS c
    JOIN public.principals AS p
      ON (p.tenant_id, p.principal_id) = (c.tenant_id, c.principal_id)
    JOIN public.tenants AS t ON t.tenant_id = c.tenant_id
    WHERE c.secret_digest = p_secret_digest
      AND c.state = 'active'
      AND p.state = 'active'
      AND t.state = 'active'
      AND (c.expires_at IS NULL OR c.expires_at > statement_timestamp())
$function$;

ALTER FUNCTION oam_lookup_credential(bytea) OWNER TO oam_auth_owner;
REVOKE ALL ON FUNCTION oam_lookup_credential(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_lookup_credential(bytea) TO oam_api;

CREATE FUNCTION oam_authorize_scope(
    p_tenant_id text,
    p_principal_id text,
    p_scope_id text,
    p_capability text
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM public.scopes AS s
        JOIN public.grants AS g
          ON (g.tenant_id, g.scope_id) = (s.tenant_id, s.scope_id)
        WHERE s.tenant_id = p_tenant_id
          AND s.scope_id = p_scope_id
          AND s.state = 'active'
          AND g.principal_id = p_principal_id
          AND p_capability = ANY (g.capabilities)
    )
$function$;

ALTER FUNCTION oam_authorize_scope(text, text, text, text) OWNER TO oam_auth_owner;
REVOKE ALL ON FUNCTION oam_authorize_scope(text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_authorize_scope(text, text, text, text) TO oam_api;

CREATE POLICY work_units_queue_owner_policy ON work_units FOR ALL TO oam_queue_owner
    USING (true) WITH CHECK (true);
GRANT SELECT, UPDATE ON work_units TO oam_queue_owner;
CREATE POLICY operations_queue_owner_policy ON operations FOR SELECT TO oam_queue_owner
    USING (true);
GRANT SELECT ON operations TO oam_queue_owner;
CREATE POLICY attempts_queue_owner_policy ON attempts FOR ALL TO oam_queue_owner
    USING (true) WITH CHECK (true);
GRANT SELECT, INSERT ON attempts TO oam_queue_owner;

CREATE FUNCTION oam_claim_work(p_worker_id text, p_attempt_id text, p_lease_seconds integer)
RETURNS TABLE (
    tenant_id text,
    scope_id text,
    work_unit_id text,
    operation_id text,
    stage text,
    attempt_id text,
    fence bigint,
    lease_expires_at timestamptz
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
    WITH candidate AS (
        SELECT w.tenant_id, w.scope_id, w.work_unit_id, w.fence
        FROM public.work_units AS w
        JOIN public.operations AS operation
          ON (operation.tenant_id, operation.scope_id, operation.operation_id) =
             (w.tenant_id, w.scope_id, w.operation_id)
        WHERE (
                w.status IN ('queued', 'retry_wait')
                OR (w.status = 'running' AND w.lease_expires_at <= clock_timestamp())
              )
          AND w.available_at <= clock_timestamp()
          AND w.attempts_remaining > 0
          AND operation.status IN ('accepted', 'running')
          AND operation.cancellation_revision = 0
          AND (operation.plan_sealed OR w.stage = 'plan')
          AND NOT EXISTS (
              SELECT 1 FROM public.attempts AS unresolved
              WHERE unresolved.tenant_id = w.tenant_id
                AND unresolved.scope_id = w.scope_id
                AND unresolved.work_unit_id = w.work_unit_id
                AND unresolved.disposition = 'unknown_external_outcome'
          )
          AND NOT EXISTS (
              SELECT 1
              FROM unnest(w.dependency_ids) AS dependency(work_unit_id)
              LEFT JOIN public.work_units AS prior
                ON prior.tenant_id = w.tenant_id
               AND prior.scope_id = w.scope_id
               AND prior.work_unit_id = dependency.work_unit_id
              WHERE prior.status IS DISTINCT FROM 'succeeded'
          )
        ORDER BY w.available_at, w.created_at, w.tenant_id, w.scope_id, w.work_unit_id
        FOR UPDATE OF w SKIP LOCKED
        LIMIT 1
    )
    , claimed AS (
        UPDATE public.work_units AS work
        SET status = 'running',
            owner_id = p_worker_id,
            fence = candidate.fence + 1,
            lease_expires_at = clock_timestamp() + (p_lease_seconds * interval '1 second'),
            attempts_remaining = work.attempts_remaining - 1,
            updated_at = clock_timestamp()
        FROM candidate
        WHERE (work.tenant_id, work.scope_id, work.work_unit_id) =
              (candidate.tenant_id, candidate.scope_id, candidate.work_unit_id)
        RETURNING work.tenant_id, work.scope_id, work.work_unit_id,
                  work.operation_id, work.stage, work.fence, work.lease_expires_at
    )
    , numbered AS (
        SELECT claimed.*,
               COALESCE((
                   SELECT max(existing.ordinal)
                   FROM public.attempts AS existing
                   WHERE existing.tenant_id = claimed.tenant_id
                     AND existing.scope_id = claimed.scope_id
                     AND existing.work_unit_id = claimed.work_unit_id
               ), 0) + 1 AS ordinal
        FROM claimed
    )
    , inserted AS (
        INSERT INTO public.attempts
            (tenant_id, scope_id, work_unit_id, attempt_id, ordinal, owner_id, fence, disposition)
        SELECT tenant_id, scope_id, work_unit_id, p_attempt_id, ordinal,
               p_worker_id, fence, 'claimed'
        FROM numbered
        RETURNING tenant_id, scope_id, work_unit_id, attempt_id
    )
    SELECT numbered.tenant_id, numbered.scope_id, numbered.work_unit_id,
           numbered.operation_id, numbered.stage, inserted.attempt_id,
           numbered.fence, numbered.lease_expires_at
    FROM numbered
    JOIN inserted USING (tenant_id, scope_id, work_unit_id)
$function$;

ALTER FUNCTION oam_claim_work(text, text, integer) OWNER TO oam_queue_owner;
REVOKE ALL ON FUNCTION oam_claim_work(text, text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_claim_work(text, text, integer) TO oam_queue;

-- Credential administration always authenticates the actor from the current digest.
GRANT INSERT ON credentials TO oam_auth_owner;
GRANT UPDATE (state, revoked_at) ON credentials TO oam_auth_owner;

CREATE FUNCTION oam_credential_admin_tenant(p_actor_secret_digest bytea)
RETURNS text
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    actor record;
BEGIN
    SELECT * INTO actor FROM public.oam_lookup_credential(p_actor_secret_digest);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'credential is not currently valid' USING ERRCODE = '28000';
    END IF;
    IF actor.principal_role <> 'administrator' THEN
        RAISE EXCEPTION 'credential administration requires an administrator'
            USING ERRCODE = '42501';
    END IF;
    RETURN actor.tenant_id;
END
$function$;

ALTER FUNCTION oam_credential_admin_tenant(bytea) OWNER TO oam_auth_owner;
REVOKE ALL ON FUNCTION oam_credential_admin_tenant(bytea) FROM PUBLIC;

CREATE FUNCTION oam_list_credentials(
    p_actor_secret_digest bytea,
    p_limit integer,
    p_after text
)
RETURNS TABLE (
    credential_id text,
    principal_id text,
    state text,
    expires_at timestamptz,
    revoked_at timestamptz,
    created_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    actor_tenant text;
BEGIN
    actor_tenant := public.oam_credential_admin_tenant(p_actor_secret_digest);
    IF p_limit IS NULL OR p_limit <= 0 THEN
        RAISE EXCEPTION 'credential page limit must be positive' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
        SELECT c.credential_id, c.principal_id, c.state, c.expires_at,
               c.revoked_at, c.created_at
        FROM public.credentials AS c
        WHERE c.tenant_id = actor_tenant
          AND (p_after IS NULL OR c.credential_id > p_after)
        ORDER BY c.credential_id
        LIMIT p_limit::bigint + 1;
END
$function$;

ALTER FUNCTION oam_list_credentials(bytea, integer, text) OWNER TO oam_auth_owner;
REVOKE ALL ON FUNCTION oam_list_credentials(bytea, integer, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_list_credentials(bytea, integer, text) TO oam_api;

CREATE FUNCTION oam_issue_credential(
    p_actor_secret_digest bytea,
    p_principal_id text,
    p_credential_id text,
    p_secret_digest bytea,
    p_expires_at timestamptz
)
RETURNS TABLE (
    credential_id text,
    principal_id text,
    state text,
    expires_at timestamptz,
    revoked_at timestamptz,
    created_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    actor_tenant text;
BEGIN
    actor_tenant := public.oam_credential_admin_tenant(p_actor_secret_digest);
    IF p_expires_at IS NOT NULL AND p_expires_at <= statement_timestamp() THEN
        RAISE EXCEPTION 'credential expiration must be in the future' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
        INSERT INTO public.credentials AS c
            (tenant_id, credential_id, principal_id, secret_digest, state, expires_at)
        SELECT actor_tenant, p_credential_id, p.principal_id, p_secret_digest, 'active', p_expires_at
        FROM public.principals AS p
        WHERE p.tenant_id = actor_tenant
          AND p.principal_id = p_principal_id
          AND p.state = 'active'
        RETURNING c.credential_id, c.principal_id, c.state, c.expires_at,
                  c.revoked_at, c.created_at;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'principal is unavailable' USING ERRCODE = 'P0002';
    END IF;
END
$function$;

ALTER FUNCTION oam_issue_credential(bytea, text, text, bytea, timestamptz) OWNER TO oam_auth_owner;
REVOKE ALL ON FUNCTION oam_issue_credential(bytea, text, text, bytea, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_issue_credential(bytea, text, text, bytea, timestamptz) TO oam_api;

CREATE FUNCTION oam_revoke_credential(p_actor_secret_digest bytea, p_credential_id text)
RETURNS bytea
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    actor_tenant text;
    revoked_digest bytea;
BEGIN
    actor_tenant := public.oam_credential_admin_tenant(p_actor_secret_digest);
    UPDATE public.credentials AS c
    SET state = 'revoked', revoked_at = COALESCE(c.revoked_at, clock_timestamp())
    WHERE c.tenant_id = actor_tenant AND c.credential_id = p_credential_id
    RETURNING c.secret_digest INTO revoked_digest;
    RETURN revoked_digest;
END
$function$;

ALTER FUNCTION oam_revoke_credential(bytea, text) OWNER TO oam_auth_owner;
REVOKE ALL ON FUNCTION oam_revoke_credential(bytea, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_revoke_credential(bytea, text) TO oam_api;
