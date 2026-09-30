CREATE INDEX search_units_fact_memory
    ON search_units (tenant_id, user_id, memory_id, generation_id)
    WHERE kind = 'fact';
