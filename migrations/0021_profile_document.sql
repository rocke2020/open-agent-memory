ALTER TABLE profiles ADD COLUMN content text;
ALTER TABLE profiles ADD COLUMN instruction text;
ALTER TABLE profiles ADD COLUMN support_ids jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE profiles ADD COLUMN "references" jsonb NOT NULL DEFAULT '[]'::jsonb;
