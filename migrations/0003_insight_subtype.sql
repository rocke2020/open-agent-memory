ALTER TABLE insights
ADD COLUMN subtype text NOT NULL DEFAULT 'general'
CHECK (subtype IN ('preference', 'pattern', 'project', 'relationship', 'general'));
