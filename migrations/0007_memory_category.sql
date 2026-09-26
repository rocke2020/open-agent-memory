ALTER TABLE memories ADD COLUMN category text NOT NULL DEFAULT 'fact'
    CHECK (category IN ('fact', 'preference', 'experience'));
