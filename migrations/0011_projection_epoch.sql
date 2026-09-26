-- A publication identity that advances for each committed indexing transaction.
ALTER TABLE users ADD COLUMN projection_epoch bigint NOT NULL DEFAULT 0;
