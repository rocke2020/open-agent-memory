-- Store bounded JSONL chunks without duplicating retained original files.
CREATE TABLE export_parts (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    export_id text NOT NULL,
    part_index integer NOT NULL CHECK (part_index >= 0),
    content bytea NOT NULL,
    content_sha256 bytea NOT NULL CHECK (octet_length(content_sha256) = 32),
    byte_length integer NOT NULL CHECK (byte_length >= 0),
    PRIMARY KEY (tenant_id,user_id,export_id,part_index),
    FOREIGN KEY (tenant_id,user_id,export_id)
        REFERENCES exports(tenant_id,user_id,export_id),
    CHECK (octet_length(content) = byte_length)
);
ALTER TABLE export_parts ENABLE ROW LEVEL SECURITY;
ALTER TABLE export_parts FORCE ROW LEVEL SECURITY;
CREATE POLICY export_parts_user_policy ON export_parts FOR ALL TO oam_content
    USING (oam_user_visible(tenant_id,user_id))
    WITH CHECK (oam_user_visible(tenant_id,user_id));
GRANT SELECT, INSERT, UPDATE, DELETE ON export_parts TO oam_content;
