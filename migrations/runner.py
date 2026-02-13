"""
Automatic migration runner for Spotiflac web
Runs all pending migrations in order on application startup
"""

import sys
import os
import logging
from pathlib import Path

# Add parent directory to path so we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

logger = logging.getLogger(__name__)


def run_migrations(db_path: str = None):
    """
    Run all migrations in order.
    Safe to run multiple times - migrations are idempotent.
    
    Args:
        db_path: Optional database path. If not provided, will try to get from settings.
    """
    logger.info("Checking for pending database migrations...")
    
    # Determine database path
    if db_path is None:
        try:
            from app.config import get_settings
            settings = get_settings()
            db_url = settings.database_url
            
            if not db_url.startswith("sqlite"):
                logger.warning("Migration runner only supports SQLite, skipping migrations")
                return
            
            # Extract database path
            db_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
            if db_path.startswith("./"):
                db_path = db_path[2:]
        except Exception as e:
            logger.warning(f"Could not determine database path: {e}")
            db_path = "./data/spotiflac.db"  # Default fallback
    
    # Check if database file exists
    if not os.path.exists(db_path):
        logger.info("Database doesn't exist yet, migrations will run after table creation")
        return
    
    # Run migrations in order
    try:
        logger.info("Running migration 001: Add download progress fields")
        from migrations.migrate_001 import migrate_database as migrate_001
        migrate_001(db_path)
        
        logger.info("Running migration 002: Add retry and incomplete tracking")
        from migrations.migrate_002 import migrate as migrate_002
        migrate_002()
        
        logger.info("✓ All migrations completed successfully")
        
    except Exception as e:
        logger.error(f"Migration failed: {e}", exc_info=True)
        logger.warning("Application will continue, but database schema may be outdated")
        # Don't crash the application, just log the error


if __name__ == "__main__":
    # Allow running migrations manually
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    
    # Get database path from command line or use default
    db_path = sys.argv[1] if len(sys.argv) > 1 else "./data/spotiflac.db"
    run_migrations(db_path)
