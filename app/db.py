"""Database engine and initialization.

The collector (writer) and web app (reader) are separate processes sharing one
SQLite file, so we run in WAL mode for concurrent read-while-write access.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlmodel import SQLModel, create_engine

# Resolve relative to the project root so the path is stable whether the
# collector or the web app opens it (both cd to the project root).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "attractions.db"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record):
    """Enable WAL + sane durability on every new connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def _widen_attractiondistance() -> None:
    """Make `attractiondistance.walk_meters` nullable (ADR-0009).

    SQLite cannot drop a NOT NULL constraint in place, and the table is derived
    data that `python -m app.walking --rebuild` regenerates in seconds, so the
    migration simply drops it and lets create_all lay it down again. Nothing is
    lost that a rebuild does not restore.
    """
    inspector = inspect(engine)
    if "attractiondistance" not in inspector.get_table_names():
        return
    columns = {c["name"]: c for c in inspector.get_columns("attractiondistance")}
    walk = columns.get("walk_meters")
    if walk is None or walk["nullable"]:
        return
    from app.models import AttractionDistance

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE attractiondistance"))
    AttractionDistance.__table__.create(engine, checkfirst=True)
    print(
        "[db] attractiondistance rebuilt for nullable walk_meters — "
        "run `python -m app.walking --rebuild` to repopulate"
    )


def _migrate() -> None:
    """Add columns/indexes that create_all() can't add to pre-existing tables."""
    _widen_attractiondistance()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "park" not in tables:
        return
    park_cols = {c["name"] for c in inspector.get_columns("park")}
    attraction_cols = (
        {c["name"] for c in inspector.get_columns("attraction")}
        if "attraction" in tables
        else set()
    )
    destination_cols = (
        {c["name"] for c in inspector.get_columns("destination")}
        if "destination" in tables
        else set()
    )
    with engine.begin() as conn:
        if "latitude" not in park_cols:
            conn.execute(text("ALTER TABLE park ADD COLUMN latitude FLOAT"))
        if "longitude" not in park_cols:
            conn.execute(text("ALTER TABLE park ADD COLUMN longitude FLOAT"))
        if "destination_id" not in park_cols:
            conn.execute(text("ALTER TABLE park ADD COLUMN destination_id VARCHAR"))
        # Attraction coordinates (ADR-0008). Nullable: a ghost row for a ride
        # the feed no longer lists never gets one, and distance queries skip it.
        if "latitude" not in attraction_cols:
            conn.execute(text("ALTER TABLE attraction ADD COLUMN latitude FLOAT"))
        if "longitude" not in attraction_cols:
            conn.execute(text("ALTER TABLE attraction ADD COLUMN longitude FLOAT"))
        # NULL on an existing row means "never geocoded", which makes the first
        # poll after this migration seed coordinates for the whole roster.
        if "geo_fetched_at" not in destination_cols:
            conn.execute(
                text("ALTER TABLE destination ADD COLUMN geo_fetched_at VARCHAR")
            )
        # Composite index for the `attraction_id IN (...) AND observed_at >= ?`
        # scan the heavy pages run; the two single-column indexes can't serve it.
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_reading_attr_observed "
                "ON reading (attraction_id, observed_at)"
            )
        )
        # Drop the now-redundant single-column index: the composite above leads
        # with attraction_id, so it serves every attraction_id lookup on its own.
        # Removes ~86 MB and a per-insert index write. See ADR-0005.
        conn.execute(text("DROP INDEX IF EXISTS ix_reading_attraction_id"))


def init_db() -> None:
    """Create the data directory and tables if they don't exist yet."""
    # Import for its side effect: registers every table on SQLModel.metadata so
    # create_all sees them regardless of what the caller imported first.
    from app import models  # noqa: F401

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
    _migrate()
