"""Bounded, read-only connection probes for required PostgreSQL roles."""

import psycopg

DATABASE_READINESS_TIMEOUT_SECONDS = 5.0


def postgres_connection_ready(dsn: str) -> bool:
    try:
        with psycopg.connect(
            dsn, connect_timeout=int(DATABASE_READINESS_TIMEOUT_SECONDS)
        ) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute(
                "SELECT set_config('statement_timeout',%s,true)",
                (str(int(DATABASE_READINESS_TIMEOUT_SECONDS * 1000)),),
            )
            return connection.execute("SELECT 1").fetchone() == (1,)
    except (psycopg.Error, TimeoutError):
        return False
