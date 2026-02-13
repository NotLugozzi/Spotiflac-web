#!/usr/bin/env python3
"""
Migration 002: Add retry mechanism and incomplete album tracking
Adds fields for tracking retry attempts and incomplete albums
"""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import get_engine
from sqlalchemy import text
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def migrate():
    """Run migration 002"""
    engine = get_engine()
    
    logger.info("Starting migration 002...")
    
    # SQLite doesn't support IF NOT EXISTS in ALTER TABLE ADD COLUMN
    # We'll try to add each column and ignore "duplicate column" errors
    migrations = [
        # Albums table - incomplete tracking
        ("ALTER TABLE albums ADD COLUMN is_incomplete BOOLEAN DEFAULT FALSE", "albums.is_incomplete"),
        ("ALTER TABLE albums ADD COLUMN missing_tracks TEXT DEFAULT '[]'", "albums.missing_tracks"),
        
        # Downloads table - retry tracking
        ("ALTER TABLE downloads ADD COLUMN retry_count INTEGER DEFAULT 0", "downloads.retry_count"),
        ("ALTER TABLE downloads ADD COLUMN max_retries INTEGER DEFAULT 3", "downloads.max_retries"),
        ("ALTER TABLE downloads ADD COLUMN failed_tracks TEXT DEFAULT '[]'", "downloads.failed_tracks"),
    ]
    
    with engine.connect() as conn:
        for sql, column_name in migrations:
            try:
                logger.info(f"Adding column {column_name}...")
                conn.execute(text(sql))
                conn.commit()
                logger.info(f"✓ Added {column_name}")
            except Exception as e:
                # If column already exists, that's OK
                if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                    logger.info(f"⊙ Column {column_name} already exists, skipping")
                else:
                    logger.error(f"✗ Error adding {column_name}: {e}")
                continue
        
        # Update existing records - only if columns were added successfully
        logger.info("Updating existing records with default values...")
        update_queries = [
            ("UPDATE albums SET is_incomplete = FALSE WHERE is_incomplete IS NULL", "albums.is_incomplete"),
            ("UPDATE albums SET missing_tracks = '[]' WHERE missing_tracks IS NULL", "albums.missing_tracks"),
            ("UPDATE downloads SET retry_count = 0 WHERE retry_count IS NULL", "downloads.retry_count"),
            ("UPDATE downloads SET max_retries = 3 WHERE max_retries IS NULL", "downloads.max_retries"),
            ("UPDATE downloads SET failed_tracks = '[]' WHERE failed_tracks IS NULL", "downloads.failed_tracks"),
        ]
        
        for sql, column_name in update_queries:
            try:
                result = conn.execute(text(sql))
                conn.commit()
                logger.info(f"✓ Updated defaults for {column_name}")
            except Exception as e:
                # Column might not exist if it failed to add
                logger.warning(f"⊙ Could not update {column_name}: {e}")
                continue
    
    logger.info("Migration 002 completed!")


def rollback():
    """Rollback migration 002"""
    engine = get_engine()
    
    logger.info("Rolling back migration 002...")
    logger.warning("Note: SQLite has limited ALTER TABLE support. Column drops may require table recreation.")
    
    # SQLite doesn't support DROP COLUMN before version 3.35.0 (released 2021-03-12)
    # For older versions, we would need to recreate the entire table
    # For simplicity, we'll just log what would be done
    
    rollback_columns = [
        "albums.is_incomplete",
        "albums.missing_tracks",
        "downloads.retry_count",
        "downloads.max_retries",
        "downloads.failed_tracks",
    ]
    
    logger.info("Columns to remove:")
    for col in rollback_columns:
        logger.info(f"  - {col}")
    
    # Try to drop columns (works on SQLite 3.35.0+)
    with engine.connect() as conn:
        # Check SQLite version
        try:
            result = conn.execute(text("SELECT sqlite_version()"))
            version = result.fetchone()[0]
            logger.info(f"SQLite version: {version}")
        except:
            version = "unknown"
        
        drop_queries = [
            ("ALTER TABLE albums DROP COLUMN is_incomplete", "albums.is_incomplete"),
            ("ALTER TABLE albums DROP COLUMN missing_tracks", "albums.missing_tracks"),
            ("ALTER TABLE downloads DROP COLUMN retry_count", "downloads.retry_count"),
            ("ALTER TABLE downloads DROP COLUMN max_retries", "downloads.max_retries"),
            ("ALTER TABLE downloads DROP COLUMN failed_tracks", "downloads.failed_tracks"),
        ]
        
        for sql, column_name in drop_queries:
            try:
                logger.info(f"Dropping column {column_name}...")
                conn.execute(text(sql))
                conn.commit()
                logger.info(f"✓ Dropped {column_name}")
            except Exception as e:
                logger.warning(f"⊙ Could not drop {column_name}: {e}")
                logger.warning(f"   Manual intervention may be required")
                continue
    
    logger.info("Rollback completed!")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "rollback":
        rollback()
    else:
        migrate()
