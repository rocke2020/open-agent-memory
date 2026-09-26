-- New generations store routed English and Chinese terms. Existing users must
-- rebuild their projections before a process using the new recipe can search.
ALTER TABLE search_units ADD COLUMN lexical_english text;
ALTER TABLE search_units ADD COLUMN lexical_simple text;
ALTER TABLE search_units ADD COLUMN lexical_caption_english text;
ALTER TABLE search_units ADD COLUMN lexical_caption_simple text;
ALTER TABLE search_units ADD COLUMN lexical_heading_english text;
ALTER TABLE search_units ADD COLUMN lexical_heading_simple text;
ALTER TABLE search_units ADD COLUMN lexical_title_english text;
ALTER TABLE search_units ADD COLUMN lexical_title_simple text;
ALTER TABLE search_units ADD COLUMN lexical_bilingual tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('english', COALESCE(lexical_english, lexical_text)), 'D') ||
    setweight(to_tsvector('simple', COALESCE(lexical_simple, '')), 'D') ||
    setweight(to_tsvector('english', COALESCE(lexical_caption_english, '')), 'C') ||
    setweight(to_tsvector('simple', COALESCE(lexical_caption_simple, '')), 'C') ||
    setweight(to_tsvector('english', COALESCE(lexical_heading_english, '')), 'B') ||
    setweight(to_tsvector('simple', COALESCE(lexical_heading_simple, '')), 'B') ||
    setweight(to_tsvector('english', COALESCE(lexical_title_english, '')), 'A') ||
    setweight(to_tsvector('simple', COALESCE(lexical_title_simple, '')), 'A')
) STORED;
CREATE INDEX search_units_lexical_bilingual ON search_units USING gin(lexical_bilingual);
