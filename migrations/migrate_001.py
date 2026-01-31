"""
Migration: Add progress tracking fields to downloads table
Run this if you have an existing database.
For new installations, tables will be created automatically with the new fields.
"""

import sqlite3
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def migrate_database(db_path: str = "./data/spotiflac.db"):
    logger.info(f"Running migration on database: {db_path}")
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("PRAGMA table_info(downloads)")
        columns = [row[1] for row in cursor.fetchall()]
        
        migrations_applied = []
        
        if 'current_track_name' not in columns:
            logger.info("Adding column: current_track_name")
            cursor.execute("ALTER TABLE downloads ADD COLUMN current_track_name VARCHAR(500)")
            migrations_applied.append("current_track_name")
        else:
            logger.info("Column current_track_name already exists")
        
        if 'current_track_number' not in columns:
            logger.info("Adding column: current_track_number")
            cursor.execute("ALTER TABLE downloads ADD COLUMN current_track_number INTEGER")
            migrations_applied.append("current_track_number")
        else:
            logger.info("Column current_track_number already exists")
        
        if 'total_tracks' not in columns:
            logger.info("Adding column: total_tracks")
            cursor.execute("ALTER TABLE downloads ADD COLUMN total_tracks INTEGER")
            migrations_applied.append("total_tracks")
        else:
            logger.info("Column total_tracks already exists")
        
        conn.commit()
        
        if migrations_applied:
            logger.info(f"Migration completed successfully. Added columns: {', '.join(migrations_applied)}")
        else:
            logger.info("No migration needed - all columns already exist")
        
        return True
        
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else "./data/spotiflac.db"
    success = migrate_database(db_path)
    sys.exit(0 if success else 1)
