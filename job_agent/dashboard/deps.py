"""
Shared FastAPI dependencies (Phase 3).

Provides a single database engine + session dependency for all route modules,
so routers don't each build their own engine.
"""

import logging
from typing import Annotated, Generator, Optional

from fastapi import Depends
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from job_agent.config import settings

logger = logging.getLogger(__name__)

# Module-level engine singleton (created lazily on first request)
_engine: Optional[Engine] = None


def get_engine() -> Engine:
    """
    Get (or lazily create) the shared SQLModel engine.

    Uses settings.database_url_computed so an unset DATABASE_URL falls back to
    ~/Library/Application Support/job-agent/job_agent.db.

    Returns:
        SQLAlchemy Engine bound to the Job Agent database
    """
    global _engine

    if _engine is None:
        url = settings.database_url_computed
        logger.info(f"Creating database engine for {url}")

        _engine = create_engine(
            url,
            echo=False,
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(_engine)

    return _engine


def reset_engine() -> None:
    """
    Dispose of the shared engine (used by tests to swap databases).
    """
    global _engine

    if _engine is not None:
        _engine.dispose()
        _engine = None


def get_session() -> Generator[Session, None, None]:
    """
    Yield a database session for dependency injection.

    Yields:
        SQLModel Session bound to the shared engine
    """
    with Session(get_engine()) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
