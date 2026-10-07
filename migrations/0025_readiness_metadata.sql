-- Runtime readiness reads migration metadata without gaining schema write authority.
GRANT SELECT ON schema_migrations TO oam_api, oam_queue, oam_content;
