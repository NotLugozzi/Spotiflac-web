"""
Spotiflac web - Database Connection and Session Management
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool
from contextlib import contextmanager
import os
import logging

from app.models import Base
from app.config import get_settings

logger = logging.getLogger(__name__)

# Global engine and session factory
_engine = None
_SessionLocal = None


def get_database_url() -> str:
    """Get the database URL, ensuring proper SQLite path."""
    settings = get_settings()
    url = settings.database_url
    
    # Ensure data directory exists for SQLite
    if url.startswith("sqlite"):
        # Extract path from URL
        db_path = url.replace("sqlite:///", "").replace("sqlite://", "")
        if db_path.startswith("./"):
            db_path = db_path[2:]
        
        # Create directory if needed
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            logger.info(f"Created database directory: {db_dir}")
    
    return url


def init_db() -> None:
    """Initialize the database engine and create tables."""
    global _engine, _SessionLocal
    
    url = get_database_url()
    logger.info(f"Initializing database: {url}")
    
    # SQLite-specific settings
    if url.startswith("sqlite"):
        in_memory = ":memory:" in url or url.rstrip("/") in ("sqlite:", "sqlite:/")
        engine_kwargs = {
            # timeout: wait for a lock instead of failing with "database is locked"
            "connect_args": {"check_same_thread": False, "timeout": 30},
            "echo": False,
        }
        if in_memory:
            # One shared connection is only right for an in-memory DB
            engine_kwargs["poolclass"] = StaticPool
        # File DB: SQLAlchemy's default pool, one connection per thread. A
        # single StaticPool connection shared by the download worker and the
        # web requests interleaved their transactions ("cannot commit - no
        # transaction is active", "database is locked").
        _engine = create_engine(url, **engine_kwargs)

        @event.listens_for(_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            if not in_memory:
                # busy_timeout first, so the WAL switch below can wait for a lock too
                cursor.execute("PRAGMA busy_timeout=30000")
                # WAL: readers don't block the writer (download progress vs UI polling)
                cursor.execute("PRAGMA journal_mode=WAL")
                # Safe with WAL and much cheaper commits on SD-card/USB storage,
                # so the background writer holds the lock only briefly
                cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()
            if not in_memory:
                # Let SQLAlchemy issue BEGIN itself (see below)
                dbapi_connection.isolation_level = None

        if not in_memory:
            @event.listens_for(_engine, "begin")
            def begin(conn):
                # Background writers (get_db_session) begin IMMEDIATE: a deferred
                # transaction that reads then writes can't wait on busy_timeout
                # when upgrading to a write lock and fails at once with
                # "database is locked". Request handlers stay deferred: under
                # WAL their reads never wait for the writer (IMMEDIATE for
                # everything made page loads queue behind the download worker).
                if conn.get_execution_options().get("sqlite_immediate"):
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    conn.exec_driver_sql("BEGIN")
    else:
        _engine = create_engine(url, echo=False)
    
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    
    # Create all tables
    Base.metadata.create_all(bind=_engine)
    logger.info("Database tables created successfully")


def get_engine():
    """Get the database engine, initializing if needed."""
    global _engine
    if _engine is None:
        init_db()
    return _engine


def get_session_factory():
    """Get the session factory, initializing if needed."""
    global _SessionLocal
    if _SessionLocal is None:
        init_db()
    return _SessionLocal


def get_db() -> Session:
    """
    Dependency for FastAPI to get a database session.
    Used with Depends() in route handlers.
    """
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_session(auto_commit: bool = True) -> Session:
    """
    Context manager for getting a database session.
    Used in background tasks and services.
    
    Args:
        auto_commit: If True, automatically commit on exit. If False, manual commit is required.
    """
    SessionLocal = get_session_factory()
    session = SessionLocal()
    if _engine is not None and _engine.dialect.name == "sqlite":
        # Background work writes: take the write lock up front (see init_db)
        session.bind = _engine.execution_options(sqlite_immediate=True)
    try:
        yield session
        if auto_commit:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def close_db() -> None:
    """Close the database connection."""
    global _engine, _SessionLocal
    if _engine:
        _engine.dispose()
        _engine = None
        _SessionLocal = None
        logger.info("Database connection closed")
