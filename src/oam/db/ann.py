"""Transaction-local planning for bounded, owner-qualified vector discovery."""

from __future__ import annotations

from oam.config import OAMConfig

ANN_SETTINGS_SQL = (
    "set_config('jit','off',true), "
    "set_config('plan_cache_mode',%s,true), "
    "set_config('enable_seqscan','off',true), "
    "set_config('enable_sort','off',true), "
    "set_config('hnsw.iterative_scan',%s,true), "
    "set_config('hnsw.max_scan_tuples',%s,true)"
)


def ann_parameters(config: OAMConfig, *, semantic: bool = False) -> tuple[str, str, str]:
    # A shared query's generic owner parameters cannot imply any owner's partial
    # index predicate. Dedicated/automatic queries expose their owner to a custom plan.
    plan = (
        "force_generic_plan"
        if config.database.vector_index_mode == "small"
        else "force_custom_plan"
    )
    if semantic:
        return (
            plan,
            config.index.semantic_link_ann_iterative_scan,
            str(config.index.semantic_link_ann_scan_max_tuples),
        )
    return plan, config.search.ann_iterative_scan, str(config.search.ann_scan_max_tuples)


def configure_ann(connection, config: OAMConfig, *, semantic: bool = False) -> None:
    connection.execute("SELECT " + ANN_SETTINGS_SQL, ann_parameters(config, semantic=semantic))


def owner_scope(alias: str = "u") -> str:
    """Parameters are resolved by a custom plan so owner partial indexes can match."""
    return f"{alias}.tenant_id=%(ann_tenant)s AND {alias}.user_id=%(ann_user)s "
