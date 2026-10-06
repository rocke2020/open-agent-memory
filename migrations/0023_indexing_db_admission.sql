-- Indexing slots belong to the content transaction and disappear on rollback or loss.
CREATE FUNCTION oam_indexing_db_admit(p_limit integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    indexing_namespace CONSTANT integer := 186865779;
    current_limits jsonb;
    candidate integer;
BEGIN
    IF p_limit IS NULL OR p_limit < 1 THEN
        RAISE EXCEPTION 'invalid indexing database capacity';
    END IF;
    SELECT limits INTO current_limits FROM public.capacity_settings WHERE singleton;
    IF current_limits IS NOT NULL
       AND (current_limits->>'indexing_db')::integer IS DISTINCT FROM p_limit THEN
        RAISE EXCEPTION 'indexing database capacity configuration mismatch';
    END IF;
    FOR candidate IN 1..p_limit LOOP
        IF pg_try_advisory_xact_lock(indexing_namespace, candidate) THEN
            RETURN candidate;
        END IF;
    END LOOP;
    RETURN NULL;
END $$;

ALTER FUNCTION oam_indexing_db_admit(integer) OWNER TO oam_capacity_owner;
REVOKE ALL ON FUNCTION oam_indexing_db_admit(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_indexing_db_admit(integer) TO oam_content;
