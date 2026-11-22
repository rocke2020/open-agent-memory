-- Owner-local physical lexical statistics. Retrieval still applies source,
-- generation and support eligibility before selecting returned candidates.
-- Migrations require a privileged operator identity able to bypass forced RLS.
-- Fail instead of silently scanning no owners when that identity is missing.
SET LOCAL row_security=off;
-- Hold through backfill and trigger installation; ordinary readers may proceed.
LOCK TABLE search_units IN SHARE ROW EXCLUSIVE MODE;
CREATE TABLE bm25_postings (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    term_key bytea NOT NULL,
    lexeme text NOT NULL,
    field integer NOT NULL CHECK (field BETWEEN 1 AND 4),
    unit_id text NOT NULL,
    tf integer NOT NULL CHECK (tf > 0),
    length integer NOT NULL CHECK (length >= tf),
    PRIMARY KEY (tenant_id,user_id,term_key,field,unit_id) INCLUDE (tf,length),
    FOREIGN KEY (tenant_id,user_id,unit_id)
        REFERENCES search_units(tenant_id,user_id,unit_id) ON UPDATE CASCADE ON DELETE CASCADE
);
CREATE INDEX bm25_postings_by_unit ON bm25_postings(tenant_id,user_id,unit_id);
CREATE TABLE bm25_term_stats (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    field integer NOT NULL CHECK (field BETWEEN 1 AND 4),
    term_key bytea NOT NULL,
    lexeme text NOT NULL,
    df bigint NOT NULL CHECK (df > 0),
    PRIMARY KEY (tenant_id,user_id,term_key,field)
);
CREATE TABLE bm25_corpus_stats (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    field integer NOT NULL CHECK (field BETWEEN 1 AND 4),
    n bigint NOT NULL CHECK (n > 0),
    total_length bigint NOT NULL CHECK (total_length >= n),
    PRIMARY KEY (tenant_id,user_id,field)
);

-- Keep ordinary terms compact. Long terms need bounded keys because owner IDs
-- and unit IDs also count toward PostgreSQL's per-index-tuple size limit.
-- NUL separates digests from the literal UTF-8 keys.
CREATE FUNCTION oam_bm25_term_key(lexeme text)
RETURNS bytea LANGUAGE sql STABLE STRICT PARALLEL SAFE SET search_path=pg_catalog AS $function$
    SELECT CASE WHEN octet_length(lexeme)<=128 THEN convert_to(lexeme,'UTF8')
        ELSE '\x00'::bytea || sha256(convert_to(lexeme,'UTF8')) END
$function$;

-- Field numbers follow body, caption, heading and title. Count analyzer output
-- directly: tsvector positions cap repeated terms and cannot supply exact tf.
CREATE FUNCTION oam_bm25_terms(u public.search_units)
RETURNS TABLE(field integer,lexeme text,tf integer,length integer)
LANGUAGE sql STABLE PARALLEL SAFE SET search_path=pg_catalog AS $function$
    SELECT f.field,t.lexeme,count(*)::integer,
           sum(count(*)) OVER (PARTITION BY f.field)::integer
    FROM (VALUES
        (1,'english'::regconfig,coalesce(u.lexical_english,u.lexical_text)),
        (1,'simple',coalesce(u.lexical_simple,'')),
        (2,'english',coalesce(u.lexical_caption_english,'')),
        (2,'simple',coalesce(u.lexical_caption_simple,'')),
        (3,'english',coalesce(u.lexical_heading_english,'')),
        (3,'simple',coalesce(u.lexical_heading_simple,'')),
        (4,'english',coalesce(u.lexical_title_english,'')),
        (4,'simple',coalesce(u.lexical_title_simple,''))
    ) f(field,configuration,stream)
    CROSS JOIN LATERAL ts_debug(f.configuration,f.stream) dbg
    CROSS JOIN LATERAL unnest(dbg.lexemes) t(lexeme)
    WHERE octet_length(dbg.token)<=2047 AND octet_length(t.lexeme)<=2047
      -- PostgreSQL minor versions differ on the exact byte limit. Defer to
      -- the field analyzer at that boundary without reparsing ordinary terms.
      AND CASE WHEN greatest(octet_length(dbg.token),octet_length(t.lexeme))=2047
          THEN t.lexeme=ANY(tsvector_to_array(to_tsvector(f.configuration,f.stream)))
          ELSE true END
    GROUP BY f.field,t.lexeme
$function$;

-- Reuse stored analyzer inputs, without extraction, embedding or model calls.
-- A field with no searchable lexemes is absent from its field corpus.
INSERT INTO bm25_postings(tenant_id,user_id,term_key,lexeme,field,unit_id,tf,length)
SELECT u.tenant_id,u.user_id,oam_bm25_term_key(t.lexeme),t.lexeme,t.field,u.unit_id,t.tf,t.length
FROM search_units u CROSS JOIN LATERAL oam_bm25_terms(u) t;
INSERT INTO bm25_term_stats
SELECT tenant_id,user_id,field,term_key,lexeme,count(*) FROM bm25_postings
GROUP BY tenant_id,user_id,field,term_key,lexeme;
INSERT INTO bm25_corpus_stats
SELECT tenant_id,user_id,field,count(*),sum(length)
FROM (
    SELECT tenant_id,user_id,unit_id,field,max(length) AS length
    FROM bm25_postings GROUP BY tenant_id,user_id,unit_id,field
) lengths GROUP BY tenant_id,user_id,field;

CREATE FUNCTION oam_bm25_remove(p_tenant text,p_user text,p_unit text)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $function$
BEGIN
    -- Compatible with the search_units foreign key's KEY SHARE lock, unlike
    -- FOR UPDATE; this serializes one owner's deltas without global contention.
    PERFORM 1 FROM public.users
    WHERE tenant_id=p_tenant AND user_id=p_user FOR NO KEY UPDATE;
    DELETE FROM public.bm25_term_stats s
    USING public.bm25_postings p
    WHERE (p.tenant_id,p.user_id,p.unit_id)=(p_tenant,p_user,p_unit)
      AND (s.tenant_id,s.user_id,s.field,s.term_key)=(p.tenant_id,p.user_id,p.field,p.term_key)
      AND s.df=1;
    UPDATE public.bm25_term_stats s SET df=s.df-1
    FROM public.bm25_postings p
    WHERE (p.tenant_id,p.user_id,p.unit_id)=(p_tenant,p_user,p_unit)
      AND (s.tenant_id,s.user_id,s.field,s.term_key)=(p.tenant_id,p.user_id,p.field,p.term_key);
    DELETE FROM public.bm25_corpus_stats
    WHERE tenant_id=p_tenant AND user_id=p_user AND n=1
      AND field IN (SELECT field FROM public.bm25_postings
          WHERE tenant_id=p_tenant AND user_id=p_user AND unit_id=p_unit);
    UPDATE public.bm25_corpus_stats s SET n=s.n-1,total_length=s.total_length-p.length
    FROM (
        SELECT field,max(length) AS length FROM public.bm25_postings
        WHERE tenant_id=p_tenant AND user_id=p_user AND unit_id=p_unit GROUP BY field
    ) p WHERE s.tenant_id=p_tenant AND s.user_id=p_user AND s.field=p.field;
    DELETE FROM public.bm25_postings
    WHERE tenant_id=p_tenant AND user_id=p_user AND unit_id=p_unit;
END
$function$;

CREATE FUNCTION oam_bm25_add(u public.search_units)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $function$
BEGIN
    PERFORM 1 FROM public.users
    WHERE tenant_id=u.tenant_id AND user_id=u.user_id FOR NO KEY UPDATE;
    WITH inserted AS (
        INSERT INTO public.bm25_postings(tenant_id,user_id,term_key,lexeme,field,unit_id,tf,length)
        SELECT u.tenant_id,u.user_id,public.oam_bm25_term_key(t.lexeme),
               t.lexeme,t.field,u.unit_id,t.tf,t.length
        FROM public.oam_bm25_terms(u) t
        RETURNING *
    ), terms AS (
        INSERT INTO public.bm25_term_stats
        SELECT tenant_id,user_id,field,term_key,lexeme,1 FROM inserted ORDER BY field,term_key
        ON CONFLICT (tenant_id,user_id,term_key,field)
        DO UPDATE SET df=bm25_term_stats.df+1
    )
    INSERT INTO public.bm25_corpus_stats
    SELECT tenant_id,user_id,field,1,max(length) FROM inserted
    GROUP BY tenant_id,user_id,field ORDER BY field
    ON CONFLICT (tenant_id,user_id,field)
    DO UPDATE SET n=bm25_corpus_stats.n+1,
                  total_length=bm25_corpus_stats.total_length+excluded.total_length;
END
$function$;

CREATE FUNCTION oam_bm25_projection()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $function$
BEGIN
    IF TG_OP='UPDATE' AND ROW(OLD.tenant_id,OLD.user_id,
        OLD.lexical_text,OLD.lexical_english,OLD.lexical_simple,
        OLD.lexical_caption_english,OLD.lexical_caption_simple,
        OLD.lexical_heading_english,OLD.lexical_heading_simple,
        OLD.lexical_title_english,OLD.lexical_title_simple)
        IS NOT DISTINCT FROM ROW(NEW.tenant_id,NEW.user_id,
        NEW.lexical_text,NEW.lexical_english,NEW.lexical_simple,
        NEW.lexical_caption_english,NEW.lexical_caption_simple,
        NEW.lexical_heading_english,NEW.lexical_heading_simple,
        NEW.lexical_title_english,NEW.lexical_title_simple) THEN
        RETURN NEW;
    END IF;
    IF TG_WHEN='BEFORE' THEN
        PERFORM public.oam_bm25_remove(OLD.tenant_id,OLD.user_id,OLD.unit_id);
        IF TG_OP='DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    PERFORM public.oam_bm25_add(NEW);
    RETURN NEW;
END
$function$;
CREATE TRIGGER bm25_before_change BEFORE DELETE OR UPDATE ON search_units
FOR EACH ROW EXECUTE FUNCTION oam_bm25_projection();
CREATE TRIGGER bm25_after_change AFTER INSERT OR UPDATE ON search_units
FOR EACH ROW EXECUTE FUNCTION oam_bm25_projection();

REVOKE ALL ON FUNCTION oam_bm25_term_key(text),oam_bm25_remove(text,text,text),
    oam_bm25_add(search_units),oam_bm25_terms(search_units),oam_bm25_projection() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION oam_bm25_term_key(text),oam_bm25_remove(text,text,text),
    oam_bm25_add(search_units),oam_bm25_terms(search_units),oam_bm25_projection() TO oam_content;
DO $policies$
DECLARE relation_name text;
BEGIN
    FOREACH relation_name IN ARRAY ARRAY['bm25_postings','bm25_term_stats','bm25_corpus_stats'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',relation_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',relation_name);
        EXECUTE format('CREATE POLICY %I ON %I FOR ALL TO oam_content '
            ||'USING (tenant_id=(SELECT nullif(current_setting(''oam.tenant_id'',true),'''')) '
            ||'AND user_id=(SELECT nullif(current_setting(''oam.user_id'',true),''''))) '
            ||'WITH CHECK (tenant_id=(SELECT nullif(current_setting(''oam.tenant_id'',true),'''')) '
            ||'AND user_id=(SELECT nullif(current_setting(''oam.user_id'',true),'''')))',
            relation_name||'_user_policy',relation_name);
        EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON %I TO oam_content',relation_name);
    END LOOP;
END
$policies$;
