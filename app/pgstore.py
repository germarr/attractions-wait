"""Postgres connection for the Serving Store (ADR-0007).

Shared by the publisher on this box and the reader in the cloud, so it must
stay free of pandas and of anything the SQLite side drags in.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Populate os.environ from .env when present (local runs; Azure sets real env)."""
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def dsn() -> str:
    """libpq connection string. sslmode=require — Azure Postgres rejects plaintext."""
    _load_dotenv()
    missing = [k for k in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")
               if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"missing Postgres settings: {', '.join(missing)}")
    return (
        f"host={os.environ['PGHOST']} port={os.environ['PGPORT']} "
        f"dbname={os.environ['PGDATABASE']} user={os.environ['PGUSER']} "
        f"password={os.environ['PGPASSWORD']} sslmode=require"
    )


def connect(**kwargs):
    """One short-lived connection. Used by the publisher (one per pass)."""
    import psycopg

    return psycopg.connect(dsn(), **kwargs)


def apply_schema() -> None:
    """Run sql/pub_schema.sql. Idempotent — every statement is IF NOT EXISTS."""
    ddl = (PROJECT_ROOT / "sql" / "pub_schema.sql").read_text()
    with connect() as conn:
        conn.execute(ddl)
        conn.commit()


if __name__ == "__main__":
    apply_schema()
    print("pub schema applied")
