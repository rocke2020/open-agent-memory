-- Shared admission metadata contains no source content or model credentials.
DO $$ BEGIN
    CREATE ROLE oam_capacity_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
        NOREPLICATION NOBYPASSRLS;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE capacity_settings (
    singleton boolean PRIMARY KEY CHECK (singleton),
    limits jsonb NOT NULL
);
CREATE TABLE model_permits (
    permit_id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('llm', 'embedding')),
    background boolean NOT NULL,
    state text NOT NULL CHECK (
        state IN ('reserved', 'dispatched', 'unknown', 'released', 'expired', 'forced_release')
    ),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reconciliation_reason text,
    reconciled_by text
);
CREATE INDEX model_permits_active ON model_permits(kind, background, state)
    WHERE state IN ('reserved', 'dispatched', 'unknown');
GRANT SELECT, INSERT ON capacity_settings TO oam_capacity_owner;
GRANT SELECT, INSERT, UPDATE ON model_permits TO oam_capacity_owner;

CREATE FUNCTION oam_capacity_configure(p_limits jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE current_limits jsonb;
BEGIN
    IF (p_limits->>'http')::int < 2 OR (p_limits->>'writes')::int < 1
       OR (p_limits->>'writes')::int >= (p_limits->>'http')::int
       OR (p_limits->>'llm')::int < 1 OR (p_limits->>'embedding')::int < 1
       OR (p_limits->>'background_embedding')::int < 1
       OR (p_limits->>'background_embedding')::int > (p_limits->>'embedding')::int
       OR NOT (p_limits ?& ARRAY['http','writes','llm','embedding','background_embedding'])
    THEN RAISE EXCEPTION 'invalid capacity configuration'; END IF;
    INSERT INTO public.capacity_settings VALUES (true, p_limits) ON CONFLICT DO NOTHING;
    SELECT limits INTO current_limits FROM public.capacity_settings WHERE singleton;
    RETURN current_limits = p_limits;
END $$;

-- A request owns its PostgreSQL session until its ASGI invocation has finished.
-- Closing a lost application's session also frees its HTTP admission slot.
CREATE FUNCTION oam_http_admit(p_write boolean, p_limits jsonb) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    http_namespace CONSTANT integer := 186865777;
    current_limits jsonb;
    request_cap integer;
    write_cap integer;
    candidate integer;
    ordinal integer;
BEGIN
    SELECT limits INTO current_limits FROM public.capacity_settings WHERE singleton;
    IF current_limits IS DISTINCT FROM p_limits THEN
        RAISE EXCEPTION 'capacity configuration mismatch';
    END IF;
    request_cap := (current_limits->>'http')::int;
    write_cap := (current_limits->>'writes')::int;
    FOR ordinal IN 1..CASE WHEN p_write THEN write_cap ELSE request_cap END LOOP
        candidate := CASE WHEN p_write THEN ordinal
                     ELSE ((ordinal + write_cap - 1) % request_cap) + 1 END;
        IF pg_try_advisory_lock(http_namespace, candidate) THEN RETURN candidate; END IF;
    END LOOP;
    RETURN NULL;
END $$;

CREATE FUNCTION oam_model_permit_acquire(
    p_id uuid, p_owner uuid, p_kind text, p_background boolean,
    p_seconds double precision, p_limits jsonb
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    model_namespace CONSTANT integer := 186865778;
    current_limits jsonb;
    active_count integer;
    background_count integer;
BEGIN
    IF p_kind NOT IN ('llm', 'embedding') OR p_seconds <= 0 THEN
        RAISE EXCEPTION 'invalid model reservation';
    END IF;
    SELECT limits INTO current_limits FROM public.capacity_settings WHERE singleton;
    IF current_limits IS DISTINCT FROM p_limits THEN
        RAISE EXCEPTION 'capacity configuration mismatch';
    END IF;
    PERFORM pg_advisory_xact_lock(model_namespace, CASE WHEN p_kind='llm' THEN 1 ELSE 2 END);
    UPDATE public.model_permits SET state='expired', updated_at=clock_timestamp()
    WHERE kind=p_kind AND state='reserved' AND expires_at <= clock_timestamp();
    SELECT count(*), count(*) FILTER (WHERE background)
    INTO active_count, background_count FROM public.model_permits
    WHERE kind=p_kind AND state IN ('reserved','dispatched','unknown');
    IF active_count >= (current_limits->>p_kind)::int
       OR (p_kind='embedding' AND p_background
           AND background_count >= (current_limits->>'background_embedding')::int)
    THEN RETURN false; END IF;
    INSERT INTO public.model_permits(permit_id,owner_id,kind,background,state,expires_at)
    VALUES (p_id,p_owner,p_kind,p_background,'reserved',
            clock_timestamp()+p_seconds*interval '1 second');
    RETURN true;
END $$;

CREATE FUNCTION oam_model_permit_update(p_id uuid, p_owner uuid, p_state text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE affected integer;
BEGIN
    IF p_state NOT IN ('dispatched','unknown','released') THEN
        RAISE EXCEPTION 'invalid permit transition';
    END IF;
    UPDATE public.model_permits SET state=p_state, updated_at=clock_timestamp()
    WHERE permit_id=p_id AND owner_id=p_owner AND (
        (p_state='dispatched' AND state='reserved' AND expires_at>clock_timestamp())
        OR (p_state='unknown' AND state IN ('dispatched','unknown'))
        OR (p_state='released' AND state IN (
            'reserved','dispatched','unknown','released','forced_release'))
    );
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected = 1;
END $$;

CREATE FUNCTION oam_capacity_status() RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT jsonb_build_object(
        'llm_occupied', count(*) FILTER (WHERE kind='llm' AND
            (state IN ('unknown','dispatched') OR (state='reserved' AND expires_at>clock_timestamp()))),
        'embedding_occupied', count(*) FILTER (WHERE kind='embedding' AND
            (state IN ('unknown','dispatched') OR (state='reserved' AND expires_at>clock_timestamp()))),
        'llm_unknown', count(*) FILTER (WHERE kind='llm' AND state='unknown'),
        'embedding_unknown', count(*) FILTER (WHERE kind='embedding' AND state='unknown'),
        'physical_concurrency_verified', NOT EXISTS (
            SELECT 1 FROM public.model_permits WHERE state='forced_release'))
    FROM public.model_permits
$$;

ALTER FUNCTION oam_capacity_configure(jsonb) OWNER TO oam_capacity_owner;
ALTER FUNCTION oam_http_admit(boolean,jsonb) OWNER TO oam_capacity_owner;
ALTER FUNCTION oam_model_permit_acquire(uuid,uuid,text,boolean,double precision,jsonb)
    OWNER TO oam_capacity_owner;
ALTER FUNCTION oam_model_permit_update(uuid,uuid,text) OWNER TO oam_capacity_owner;
ALTER FUNCTION oam_capacity_status() OWNER TO oam_capacity_owner;
REVOKE ALL ON FUNCTION oam_capacity_configure(jsonb), oam_http_admit(boolean,jsonb),
    oam_model_permit_acquire(uuid,uuid,text,boolean,double precision,jsonb),
    oam_model_permit_update(uuid,uuid,text), oam_capacity_status() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_capacity_configure(jsonb),
    oam_model_permit_acquire(uuid,uuid,text,boolean,double precision,jsonb),
    oam_model_permit_update(uuid,uuid,text), oam_capacity_status() TO oam_api,oam_queue;
GRANT EXECUTE ON FUNCTION oam_http_admit(boolean,jsonb) TO oam_api;
