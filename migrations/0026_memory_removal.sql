-- Exact typed Memory and Insight removal uses the existing operation record as its tombstone.
ALTER TABLE operations ADD COLUMN removal_target_kind text
    CHECK (removal_target_kind IN ('memory', 'insight'));
ALTER TABLE operations ADD COLUMN removal_target_id text;
ALTER TABLE operations ADD CONSTRAINT operations_removal_target_pair_check
    CHECK ((removal_target_kind IS NULL) = (removal_target_id IS NULL));

CREATE UNIQUE INDEX operations_removal_target_idx
    ON operations (tenant_id, user_id, removal_target_kind, removal_target_id)
    WHERE removal_target_kind IS NOT NULL;
