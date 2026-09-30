ALTER TABLE insights DROP CONSTRAINT insights_subtype_check;

UPDATE insights SET subtype = CASE subtype
    WHEN 'preference' THEN 'preferences'
    WHEN 'pattern' THEN 'patterns'
    WHEN 'project' THEN 'projects'
    WHEN 'relationship' THEN 'relationships'
    WHEN 'general' THEN 'general_insights'
END;

ALTER TABLE insights ALTER COLUMN subtype SET DEFAULT 'general_insights';
ALTER TABLE insights ADD CONSTRAINT insights_subtype_check
    CHECK (subtype IN ('preferences', 'patterns', 'projects', 'relationships', 'experiences', 'general_insights'));
