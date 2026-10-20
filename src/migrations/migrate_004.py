"""
Migration 004: Add downloads.priority ("Download next": higher runs first;
the queue is rebuilt in this order after a restart).
Idempotent: safe to run multiple times. New installations get the column
from create_all().
"""

import sqlite3
import logging

logger = logging.getLogger(__name__)


def migrate(db_path: str = "./data/spotiflac.db"):
    conn = sqlite3.connect(db_path)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(downloads)")]
        if not columns:
            logger.info("downloads table not created yet, nothing to migrate")
        elif "priority" in columns:
            logger.info("Column downloads.priority already exists")
        else:
            conn.execute("ALTER TABLE downloads ADD COLUMN priority INTEGER DEFAULT 0")
            conn.commit()
            logger.info("Added column downloads.priority")
    finally:
        conn.close()
