-- Names containing only text-search stop words still support exact query hints.
CREATE INDEX entity_names_empty_first_word
    ON entity_names(tenant_id,user_id,(split_part(name_key,' ',1)))
    WHERE lexical=''::tsvector;
