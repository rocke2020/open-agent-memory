-- Both historical version-12 paths must restrict the asserted-user function.
ALTER FUNCTION public.oam_register_asserted_user(text, text) OWNER TO oam_auth_owner;
REVOKE ALL ON FUNCTION public.oam_register_asserted_user(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.oam_register_asserted_user(text, text) TO oam_api;
