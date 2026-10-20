"""
Migration 003: Add downloads.image_url (cover art for playlist/URL downloads,
which have no linked album to take an image from).
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
        elif "image_url" in columns:
            logger.info("Column downloads.image_url already exists")
        else:
            conn.execute("ALTER TABLE downloads ADD COLUMN image_url VARCHAR(1000)")
            conn.commit()
            logger.info("Added column downloads.image_url")
    finally:
        conn.close()
