-- Seed presentation metadata without changing supported text or search vectors.
UPDATE search_units u
SET locator = jsonb_set(u.locator, '{title}', to_jsonb(left(
    COALESCE(NULLIF(btrim(regexp_replace(
        u.locator->>'retrieval_text', '[[:space:]]+', ' ', 'g'
    )), ''), btrim(regexp_replace(u.content_text, '[[:space:]]+', ' ', 'g'))), 120)))
FROM insights i
WHERE (u.tenant_id,u.user_id,u.insight_id,u.insight_revision) =
    (i.tenant_id,i.user_id,i.insight_id,i.revision)
    AND u.kind = 'insight' AND i.visible_to IS NULL AND u.content_text ~ '[^[:space:]]'
    AND COALESCE(u.locator->>'title', '') !~ '[^[:space:]]';
