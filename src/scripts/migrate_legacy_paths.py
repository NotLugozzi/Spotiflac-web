import os
import argparse
import logging
import sys
import re

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.database import get_db_session
from app.models import Album

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
LOCK_FILE = os.path.join(os.environ.get("TMPDIR", "/tmp"), "spotiflac_migration.lock")

def migrate(dry_run=True):
    try:
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        logger.error("Another migration process is holding the lock. Exiting.")
        sys.exit(1)
    except OSError as e:
        logger.error(f"Failed to acquire lock {LOCK_FILE}: {e}")
        sys.exit(1)

    try:
        with get_db_session() as db:
            albums = db.query(Album).filter(Album.local_path.isnot(None)).all()
            for album in albums:
                try:
                    if not re.search(r'(?i)/artists/', album.local_path):
                        continue
                        
                    new_path = re.sub(r'(?i)/artists/', '/artist/', album.local_path)
                    
                    if os.path.exists(new_path):
                        logger.warning(f"Target path {new_path} already exists, skipping rename for album {album.id}.")
                        continue
                        
                    logger.info(f"{'[DRY RUN] ' if dry_run else ''}Updating Album DB path {album.local_path} -> {new_path}")
                    
                    if dry_run:
                        continue
                        
                    original_path = album.local_path
                    
                    try:
                        os.makedirs(os.path.dirname(new_path), exist_ok=True)
                        os.rename(original_path, new_path)
                    except OSError as fs_e:
                        logger.error(f"Filesystem move failed for {original_path}: {fs_e}")
                        continue
                        
                    try:
                        album.local_path = new_path
                        for track in album.tracks:
                            if track.local_path and track.local_path.startswith(original_path) and re.search(r'(?i)/artists/', track.local_path):
                                track.local_path = re.sub(r'(?i)/artists/', '/artist/', track.local_path)
                        db.commit()
                    except Exception as db_e:
                        logger.error(f"DB commit failed for album {album.id}: {db_e}. Rolling back filesystem.")
                        db.rollback()
                        try:
                            os.rename(new_path, original_path)
                        except OSError as crit_e:
                            logger.critical(f"CRITICAL DRIFT: Failed to rollback filesystem for {new_path} -> {original_path}: {crit_e}. MANUAL INTERVENTION REQUIRED.")
                except Exception as loop_e:
                    logger.error(f"Unexpected error processing album {getattr(album, 'id', 'unknown')}: {loop_e}")
                    db.rollback()
    finally:
        try:
            os.close(lock_fd)
            os.remove(LOCK_FILE)
        except OSError:
            pass

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate legacy plural paths.")
    parser.add_argument("--execute", action="store_true", help="Execute the migration. Without this flag, script runs in dry-run mode.")
    args = parser.parse_args()
    migrate(dry_run=not args.execute)
