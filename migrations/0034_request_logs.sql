CREATE TABLE request_logs (
 tenant_id text NOT NULL, user_id text NOT NULL,
 request_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 operation text NOT NULL, status integer NOT NULL,
 duration_ms double precision NOT NULL, error_code text, operation_id text,
 PRIMARY KEY (tenant_id,user_id,request_id)
);
CREATE INDEX request_logs_page ON request_logs(tenant_id,user_id,created_at DESC,request_id DESC);
ALTER TABLE request_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE request_logs FORCE ROW LEVEL SECURITY;
CREATE POLICY request_logs_scope ON request_logs TO oam_content
 USING (tenant_id = current_setting('oam.tenant_id',true) AND user_id = current_setting('oam.user_id',true))
 WITH CHECK (tenant_id = current_setting('oam.tenant_id',true) AND user_id = current_setting('oam.user_id',true));
GRANT SELECT, INSERT ON request_logs TO oam_content;
