"""Owner-scoped retrieval for the current passages of plain-text Evidence Resources."""

from oam.db.eligibility import ELIGIBLE_PASSAGE_UNITS

NEIGHBOR_RADIUS = 3

ONLY_PLAIN_TEXT = """
SELECT count(*) > 0 AND count(*) FILTER (
    WHERE s.input_role='evidence_resource' AND v.status='active' AND v.media_type='text/plain'
)=count(*) AS only_plain_text
FROM users owner JOIN sources s USING (tenant_id,user_id)
LEFT JOIN source_versions v ON (v.tenant_id,v.user_id,v.source_id,v.source_version)
    =(s.tenant_id,s.user_id,s.source_id,s.active_version)
WHERE (owner.tenant_id,owner.user_id)=(%(ann_tenant)s,%(ann_user)s)
  AND owner.state='active' AND owner.index_generation_id=%(generation)s
  AND s.status='active' AND s.input_role IN ('interaction','evidence_resource')
"""

# All paths bind the same active version, owner, generation and media type.
_CATALOG = """
WITH catalog AS (
    SELECT s.tenant_id,s.user_id,s.source_id,s.active_version AS source_version,v.byte_length
    FROM sources s JOIN source_versions v ON (v.tenant_id,v.user_id,v.source_id,v.source_version)
           =(s.tenant_id,s.user_id,s.source_id,s.active_version)
    JOIN users owner ON (owner.tenant_id,owner.user_id)=(s.tenant_id,s.user_id)
    WHERE (s.tenant_id,s.user_id)=(%(ann_tenant)s,%(ann_user)s)
      AND owner.state='active' AND owner.index_generation_id=%(generation)s
      AND s.status='active' AND s.input_role='evidence_resource'
      AND v.status='active' AND v.media_type='text/plain'
)
"""

_PASSAGES = (
    _CATALOG
    + ", eligible AS NOT MATERIALIZED ("
    + ELIGIBLE_PASSAGE_UNITS
    + """), text_units AS NOT MATERIALIZED (
        SELECT u.* FROM eligible u JOIN catalog c USING (tenant_id,user_id,source_id,source_version)
        WHERE u.kind='resource_passage' AND u.origin='evidence' AND u.generation_id=%(generation)s
    )
    """
)

FIRST_PASSAGE_ANCHOR = (
    _PASSAGES
    + "SELECT EXISTS(SELECT 1 FROM text_units u WHERE u.ordinal=0 AND ({predicate})) AS matched"
)

_QUERY = """
, query_terms AS MATERIALIZED (
    SELECT DISTINCT lexeme,oam_bm25_term_key(lexeme) AS term_key
    FROM unnest(tsvector_to_array(to_tsvector('english',%(english)s))
                || tsvector_to_array(to_tsvector('simple',%(simple)s))) AS words(lexeme)
)
"""

DOCUMENT_RANK = (
    _PASSAGES
    + _QUERY
    + """
    , corpus AS MATERIALIZED (SELECT count(*) AS n,avg(byte_length) AS avg_length FROM catalog),
    rare AS MATERIALIZED (
        SELECT q.term_key,q.lexeme FROM query_terms q JOIN bm25_term_stats t
            ON (t.term_key,t.lexeme)=(q.term_key,q.lexeme)
        WHERE (t.tenant_id,t.user_id,t.field)=(%(ann_tenant)s,%(ann_user)s,1)
        ORDER BY t.df,q.lexeme LIMIT 8
    ), sought AS MATERIALIZED (
        SELECT p.tenant_id,p.user_id,p.unit_id,r.lexeme,p.tf
        FROM rare r JOIN bm25_postings p ON (p.term_key,p.lexeme)=(r.term_key,r.lexeme)
        WHERE (p.tenant_id,p.user_id,p.field)=(%(ann_tenant)s,%(ann_user)s,1)
    ), source_terms AS MATERIALIZED (
        SELECT u.source_id,p.lexeme,sum(p.tf) AS tf
        FROM sought p JOIN text_units u USING (tenant_id,user_id,unit_id)
        GROUP BY u.source_id,p.lexeme
    ), weighted AS (
        SELECT source_id,tf,count(*) OVER (PARTITION BY lexeme) AS df FROM source_terms
    )
    SELECT w.source_id FROM weighted w JOIN catalog c USING(source_id) CROSS JOIN corpus a
    GROUP BY w.source_id ORDER BY
        sum(ln(1+(a.n-w.df+0.5)/(w.df+0.5))*w.tf*2.2 /
            (w.tf+1.2*(0.25+0.75*c.byte_length/nullif(a.avg_length,0)))) DESC,w.source_id
    LIMIT %(limit)s
    """
)

_SOURCE_EVIDENCE = """
JOIN evidence e ON (e.tenant_id,e.user_id,e.source_id,e.source_version,e.evidence_id)
    =(u.tenant_id,u.user_id,u.source_id,u.source_version,u.evidence_id) AND e.status='active'
"""

OPENINGS = (
    _CATALOG
    + """
    , wanted AS MATERIALIZED (
        SELECT u.tenant_id,u.user_id,u.unit_id,u.source_id,u.source_version,u.generation_id,
               u.evidence_id,u.passage_id,u.kind,u.origin
        FROM search_units u
        WHERE (u.tenant_id,u.user_id)=(%(ann_tenant)s,%(ann_user)s)
          AND u.unit_id=ANY(%(ids)s) AND u.generation_id=%(generation)s
          AND u.kind='resource_passage' AND u.origin='evidence'
    ), candidates AS MATERIALIZED (
    """
    + ELIGIBLE_PASSAGE_UNITS.replace("FROM search_units u", "FROM wanted u")
    + """
    ), originals AS MATERIALIZED (
        SELECT u.tenant_id,u.user_id,u.source_id,u.source_version,u.evidence_id,
               left(e.content_text,160) AS opening
        FROM (SELECT DISTINCT tenant_id,user_id,source_id,source_version,evidence_id
              FROM candidates) u JOIN catalog c USING (tenant_id,user_id,source_id,source_version)
    """
    + _SOURCE_EVIDENCE
    + """
    )
    SELECT c.unit_id,c.source_id,o.opening FROM candidates c JOIN originals o
      USING (tenant_id,user_id,source_id,source_version,evidence_id)
    """
)

NEIGHBORS = (
    _PASSAGES
    + """
    , seeds AS MATERIALIZED (
        SELECT tenant_id,user_id,source_id,source_version,generation_id,unit_id,ordinal
        FROM text_units WHERE unit_id=ANY(%(ids)s)
    )
    SELECT DISTINCT ON (s.unit_id,n.ordinal) s.unit_id AS seed_id,n.unit_id
    FROM seeds s CROSS JOIN LATERAL (
        SELECT u.unit_id,u.ordinal FROM text_units u
        WHERE (u.tenant_id,u.user_id,u.source_id,u.source_version,u.generation_id)
            =(s.tenant_id,s.user_id,s.source_id,s.source_version,s.generation_id)
          AND u.ordinal BETWEEN s.ordinal-%(radius)s AND s.ordinal+%(radius)s
        -- Keep this range lookup on the seed's source index before joining eligible passages.
        OFFSET 0
    ) n
    ORDER BY s.unit_id,n.ordinal,(n.unit_id=s.unit_id) DESC,n.unit_id
    """
)

CLAUSE_RANK = (
    _PASSAGES
    + _QUERY
    + """
    , selected AS MATERIALIZED (
        SELECT tenant_id,user_id,source_id,source_version,evidence_id,unit_id,ordinal
        FROM text_units WHERE source_id=ANY(%(source_ids)s)
    ), openings AS MATERIALIZED (
        SELECT u.source_id,(regexp_match(e.content_text,
            '^(?:[^[:alnum:]]*[[:alnum:]]+){1,40}'))[1] AS opening
        FROM (SELECT DISTINCT ON (source_id) * FROM selected ORDER BY source_id,ordinal,unit_id) u
    """
    + _SOURCE_EVIDENCE
    + """
    ), terms AS MATERIALIZED (
        SELECT * FROM (
            SELECT t.*,count(*) FILTER (WHERE residual) OVER (PARTITION BY source_id) AS kept
            FROM (SELECT o.source_id,q.term_key,q.lexeme,
                NOT(q.lexeme=ANY(tsvector_to_array(to_tsvector('english',coalesce(o.opening,'')))
                     || tsvector_to_array(to_tsvector('simple',coalesce(o.opening,'')))))
                    AS residual
                FROM openings o CROSS JOIN query_terms q) t
        ) counted WHERE residual OR kept<2
    ), corpus AS (
        SELECT u.source_id,count(*) AS n,avg(coalesce(weight.length,0)) AS avg_length
        FROM selected u LEFT JOIN LATERAL (
            SELECT p.length FROM bm25_postings p
            WHERE (p.tenant_id,p.user_id,p.unit_id,p.field)=(u.tenant_id,u.user_id,u.unit_id,1)
            LIMIT 1
        ) weight ON true GROUP BY u.source_id
    ), hits AS (
        SELECT u.source_id,u.unit_id,u.ordinal,p.tf,p.length,
            count(*) OVER (PARTITION BY u.source_id,t.lexeme) AS df
        FROM selected u JOIN terms t USING (source_id)
        JOIN bm25_postings p ON (p.tenant_id,p.user_id,p.unit_id,p.term_key,p.lexeme,p.field)
            =(u.tenant_id,u.user_id,u.unit_id,t.term_key,t.lexeme,1)
    ), scores AS (
        SELECT source_id,unit_id,ordinal,
            sum(greatest(1e-6,ln((c.n-h.df+0.5)/(h.df+0.5)))*h.tf*2.2 /
                (h.tf+1.2*(0.25+0.75*h.length/nullif(c.avg_length,0)))) AS score
        FROM hits h JOIN corpus c USING(source_id) GROUP BY source_id,unit_id,ordinal
    ), ranked AS (
        SELECT source_id,unit_id,row_number() OVER
            (PARTITION BY source_id ORDER BY score DESC,ordinal,unit_id) AS position FROM scores
    )
    SELECT source_id,unit_id FROM ranked WHERE position<=%(limit)s ORDER BY source_id,position
    """
)
