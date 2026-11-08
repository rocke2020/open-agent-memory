\set ON_ERROR_STOP on
\getenv api_password OAM_API_DB_PASSWORD
\getenv queue_password OAM_QUEUE_DB_PASSWORD
\getenv content_password OAM_CONTENT_DB_PASSWORD

SELECT format('CREATE ROLE oam_api_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', :'api_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'oam_api_login') \gexec
SELECT format('CREATE ROLE oam_queue_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', :'queue_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'oam_queue_login') \gexec
SELECT format('CREATE ROLE oam_content_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', :'content_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'oam_content_login') \gexec

GRANT oam_api TO oam_api_login;
GRANT oam_queue TO oam_queue_login;
GRANT oam_content TO oam_content_login;
