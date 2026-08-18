"""
Additive schema migrations, applied whenever the database is opened.

`SQLModel.metadata.create_all()` creates missing *tables*. It does nothing
about a column added to a model whose table already exists, so a running
install silently keeps the old shape and every query touching the new column
fails at runtime. The project already had this list, but only `init_db.py` ran
it — so the fix required knowing to run a script, and the app itself started
happily against a schema it could not use.

Running it at engine creation makes the application self-healing: opening the
database is what brings it up to date.

Only additive, nullable columns belong here. Anything needing a table rewrite
or a data backfill is a real migration and should not be smuggled in as one of
these.
"""

import logging
from typing import Dict, List, Tuple

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# table -> [(column, SQL type)]
ADDITIVE_COLUMNS: Dict[str, List[Tuple[str, str]]] = {
    "platform_accounts": [
        ("search_url", "VARCHAR"),  # Phase 3: generic connector search entry point
        ("connector_kind", "VARCHAR"),  # Which connector drives a user-added station
        ("requires_signin", "BOOLEAN"),  # Whether a user-added station needs a login
    ],
    "applications": [
        # Phase 4: links to the DocumentVersion records behind the stored paths
        ("resume_version_id", "INTEGER"),
        ("cover_letter_version_id", "INTEGER"),
        # Phase 5: form context captured when the agent fills an application
        ("form_url", "VARCHAR"),
        ("candidate_profile_id", "INTEGER"),
        ("filled_at", "DATETIME"),
        ("reviewed_at", "DATETIME"),
    ],
    "jobs": [
        # Which master resume was in use when this posting was collected.
        # The wire shows only the current resume's jobs, so that changing
        # resumes changes what you are looking at.
        ("matched_master_id", "INTEGER"),
    ],
    "search_profiles": [
        # The master resume this profile's terms were derived from, so the
        # desk can say whether the search still reflects the resume in use.
        ("derived_from_master_id", "INTEGER"),
    ],
}


def apply_additive_migrations(engine: Engine) -> int:
    """
    Add any columns the models declare that the database is missing.

    Idempotent: existing columns are skipped, so this is safe on every open.

    Args:
        engine: The engine to migrate

    Returns:
        How many columns were added
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    added = 0

    with engine.begin() as conn:
        for table, columns in ADDITIVE_COLUMNS.items():
            if table not in tables:
                continue  # create_all() just made it with every column

            existing = {column["name"] for column in inspector.get_columns(table)}

            for column, sql_type in columns:
                if column in existing:
                    continue

                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
                )
                logger.info(f"Added column {table}.{column} ({sql_type})")
                added += 1

    if added:
        logger.info(f"Applied {added} additive column migration(s)")

    return added
