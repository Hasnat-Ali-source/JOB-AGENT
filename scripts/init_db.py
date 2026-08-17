#!/usr/bin/env python3
"""
Database initialization script for Job Agent (Phase 0).

Creates the SQLite database, initializes the schema, and runs migrations.
"""

import sys
import logging
from pathlib import Path
from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine, Session

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Add parent directory to path so we can import job_agent
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from job_agent.models.database import (
    SearchProfile,
    PlatformAccount,
    Job,
    Application,
    AuditLog,
    EmailThread,
    MasterDocument,
    DocumentVersion,
    CandidateProfile,
    EmailDraft,
    AgentRun,
    PlatformInterruption,
)
from job_agent.utils import test_keychain_access


def get_db_path() -> Path:
    """
    Get the path to the SQLite database.
    
    Stores in: ~/Library/Application Support/job-agent/job_agent.db
    """
    app_support = Path.home() / "Library" / "Application Support" / "job-agent"
    app_support.mkdir(parents=True, exist_ok=True)
    return app_support / "job_agent.db"


def init_database() -> bool:
    """
    Initialize the SQLite database and create tables.
    
    Returns:
        True if successful, False otherwise.
    """
    try:
        db_path = get_db_path()
        logger.info(f"Initializing database at: {db_path}")
        
        # Create SQLite engine
        database_url = f"sqlite:///{db_path}"
        engine = create_engine(database_url, echo=False)
        
        # Create all tables
        SQLModel.metadata.create_all(engine)
        
        logger.info("✓ Database initialized successfully")
        logger.info("  Tables created: SearchProfile, PlatformAccount, Job, Application, "
                    "AuditLog, EmailThread, MasterDocument, DocumentVersion")
        
        return True
    except Exception as e:
        logger.error(f"✗ Failed to initialize database: {e}")
        return False


# Columns added after a table's first release. SQLModel.create_all() only
# creates missing *tables*, so an existing database needs these added by hand.
# Format: table -> [(column, SQL type + default)]
ADDITIVE_COLUMNS = {
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
}


def migrate_database() -> bool:
    """
    Add columns introduced after the database was first created.

    Idempotent: existing columns are skipped, so this is safe to run on every
    init. Only additive, nullable columns belong here — anything requiring a
    rewrite or backfill should become a real Alembic revision.

    Returns:
        True if successful, False otherwise.
    """
    try:
        db_path = get_db_path()
        engine = create_engine(f"sqlite:///{db_path}", echo=False)
        inspector = inspect(engine)

        added = 0
        with engine.begin() as conn:
            for table, columns in ADDITIVE_COLUMNS.items():
                if table not in inspector.get_table_names():
                    continue  # create_all() just made it with every column

                existing = {c["name"] for c in inspector.get_columns(table)}

                for column, sql_type in columns:
                    if column in existing:
                        continue

                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
                    logger.info(f"  + {table}.{column} ({sql_type})")
                    added += 1

        if added:
            logger.info(f"✓ Applied {added} additive column migration(s)")
        else:
            logger.info("✓ Schema is up to date")

        return True
    except Exception as e:
        logger.error(f"✗ Migration failed: {e}")
        return False


def verify_database() -> bool:
    """
    Verify that the database is properly initialized and accessible.
    
    Returns:
        True if database is valid, False otherwise.
    """
    try:
        db_path = get_db_path()
        if not db_path.exists():
            logger.error(f"✗ Database file not found at: {db_path}")
            return False
        
        database_url = f"sqlite:///{db_path}"
        engine = create_engine(database_url, echo=False)
        
        # Try to create a session and query
        with Session(engine) as session:
            # Query one of each table to verify schema
            session.query(SearchProfile).first()
            session.query(PlatformAccount).first()
            session.query(Job).first()
            session.query(Application).first()
            session.query(AuditLog).first()
            session.query(EmailThread).first()
            session.query(MasterDocument).first()
            session.query(DocumentVersion).first()
            session.query(CandidateProfile).first()
            session.query(EmailDraft).first()
            session.query(AgentRun).first()
            session.query(PlatformInterruption).first()
        
        logger.info("✓ Database verification passed")
        return True
    except Exception as e:
        logger.error(f"✗ Database verification failed: {e}")
        return False


def main():
    """Main initialization flow for Phase 0."""
    logger.info("=" * 60)
    logger.info("Job Agent — Phase 0 Database Initialization")
    logger.info("=" * 60)
    
    # Step 1: Test Keychain access
    logger.info("\n[1/4] Testing Keychain access...")
    if not test_keychain_access():
        logger.error("✗ Keychain access failed. Please check your macOS Keychain setup.")
        return 1
    logger.info("✓ Keychain is accessible")

    # Step 2: Initialize database
    logger.info("\n[2/4] Initializing database...")
    if not init_database():
        return 1

    # Step 3: Apply additive column migrations
    logger.info("\n[3/4] Checking for schema updates...")
    if not migrate_database():
        return 1

    # Step 4: Verify database
    logger.info("\n[4/4] Verifying database...")
    if not verify_database():
        return 1
    
    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("✓ Phase 0 Initialization Complete!")
    logger.info("=" * 60)
    logger.info("\nNext steps:")
    logger.info("  1. Set up Python venv: python3.11 -m venv venv")
    logger.info("  2. Install dependencies: pip install -r requirements.txt")
    logger.info("  3. Install Playwright browsers: playwright install")
    logger.info("  4. Start Phase 1: Session Manager")
    logger.info("\nDatabase location: ~/Library/Application Support/job-agent/job_agent.db")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
