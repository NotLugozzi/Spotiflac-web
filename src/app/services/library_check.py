"""
Library check: finds .flac files in the downloads folder that aren't FLAC
(e.g. a provider's MP3 data saved as .flac) and removes them, so the next
run of their playlist downloads them again.

  * With ALLOW_LOSSY_FALLBACK=false (default) a bad file is deleted; with
    lossy allowed it is moved to downloads/.rejected/ (a dot-folder, so
    SpotiFLAC's matcher doesn't count it as present).
  * Its track_history row is removed too; otherwise planning would count the
    track as "downloaded before (moved on)" and never fetch it again.

Runs as a scheduler job every LIBRARY_CHECK_HOURS (0 = off) and records each
scan on the Status page (component "library"). Download runs check the same
way for the files they use (scripts/spotiflac_runner.py: audio_format /
dispose_bad_file; duplicated there because the runner can't import the app).
"""

import logging
import os
import shutil
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.config import get_settings
from app.database import get_db_session
from app.models import StatusEvent, TrackHistory

logger = logging.getLogger(__name__)

REJECTED_DIR = ".rejected"
SKIP_DIRS = {".staging", REJECTED_DIR}

_lock = threading.Lock()
_last: Optional[Dict[str, Any]] = None


def audio_format(path: str) -> Optional[str]:
    """Container held by the file's bytes, from its magic bytes (same logic as
    the runner's audio_format). "unreadable" on I/O errors."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:3] == b"ID3" and len(head) >= 10:
                size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
                f.seek(10 + size)
                head = f.read(12)
    except OSError:
        return "unreadable"
    if head[:4] == b"fLaC":
        return "flac"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"RIFF":
        return "wav"
    return None


def forget_file(rel_path: str) -> int:
    """Remove the history rows for a file (path relative to downloads/)."""
    if not rel_path:
        return 0
    with get_db_session() as db:
        return db.query(TrackHistory).filter(TrackHistory.file_path == rel_path) \
            .delete(synchronize_session=False)


def _prune_empty_dirs(start: str, root: str) -> None:
    root = os.path.abspath(root)
    parent = os.path.abspath(start)
    while parent.startswith(root) and parent != root:
        try:
            os.rmdir(parent)
        except OSError:
            return
        parent = os.path.dirname(parent)


def run_check() -> Dict[str, Any]:
    """Scan downloads/ and remove every .flac that isn't FLAC. Never raises."""
    global _last
    if not _lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        settings = get_settings()
        root = settings.download_path
        lossy = settings.allow_lossy_fallback
        checked = 0
        bad: List[Dict[str, str]] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                if not name.lower().endswith(".flac"):
                    continue
                path = os.path.join(dirpath, name)
                checked += 1
                actual = audio_format(path)
                if actual in ("flac", "unreadable"):
                    continue
                rel = os.path.relpath(path, root)
                try:
                    if lossy:
                        dest = os.path.join(root, REJECTED_DIR, rel)
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        shutil.move(path, dest)
                        action = f"moved to {REJECTED_DIR}/"
                    else:
                        os.remove(path)
                        action = "deleted"
                    _prune_empty_dirs(dirpath, root)
                    rows = forget_file(rel)
                except OSError as e:
                    logger.error(f"Library check: could not remove {rel}: {e}")
                    continue
                bad.append({"path": rel, "format": actual or "unknown data", "action": action})
                logger.warning(f"Library check: {rel} is {actual or 'unknown data'}, not FLAC: "
                               f"{action}, {rows} history row(s) removed; it will be downloaded again")

        if bad:
            detail = (f"Library check: {len(bad)} non-FLAC file(s) "
                      f"{'moved to .rejected/' if lossy else 'deleted'}: "
                      + "; ".join(b["path"] for b in bad))
            state = "partial"
        else:
            detail = f"Library check: {checked} FLAC file(s), all valid"
            state = "ok"
        logger.info(detail[:300])
        try:
            with get_db_session() as db:
                db.add(StatusEvent(component="library", state=state, detail=detail[:500]))
        except Exception as e:
            logger.warning(f"Library check: could not record status event: {e}")
        _last = {"at": datetime.utcnow().isoformat() + "Z", "checked": checked, "bad": bad}
        return _last
    except Exception as e:
        logger.error(f"Library check failed: {e}", exc_info=True)
        return {"error": str(e)}
    finally:
        _lock.release()


def last_result() -> Optional[Dict[str, Any]]:
    return _last
