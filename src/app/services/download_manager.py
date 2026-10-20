"""
Spotiflac web - Download Manager Service
"""

import os
import re
import glob
import logging
import threading
import queue
import time
import json
from typing import Optional, List, Dict, Any, Callable, Tuple
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from enum import Enum
import subprocess
import signal
import contextlib
import shutil
import tempfile
import sys
from io import StringIO
from contextlib import redirect_stdout, redirect_stderr
from mutagen import File as MutagenFile

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Download, Album, DownloadStatus, TrackHistory
from app.database import get_db_session
from app.config import get_settings
from app.services.url_parser import parse_spotify_url, SpotifyUrlType
from app.services.spotify_service import get_spotify_service
from app.services.spotify_service import get_spotify_service
from app.services.break_monitor import get_break_monitor, format_minutes, format_utc
from app.services import track_failures

logger = logging.getLogger(__name__)


def fetch_cover_url(spotify_url: Optional[str]) -> Optional[str]:
    """Cover art for a Spotify playlist/album/track link via Spotify's public
    oEmbed endpoint: no credentials and no Web API quota (whose /playlists
    endpoint gets rate limited for hours)."""
    if not spotify_url or "open.spotify.com" not in spotify_url:
        return None
    try:
        import httpx
        resp = httpx.get(
            "https://open.spotify.com/oembed",
            params={"url": spotify_url.split("?")[0]},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("thumbnail_url") or None
    except Exception as e:
        logger.debug(f"No cover for {spotify_url}: {e}")
        return None

# Written by scripts/spotiflac_runner.py


class TeeOutput:
    def __init__(self, download_id: int, parser_callback: Callable):
        self.buffer = StringIO()
        self.download_id = download_id
        self.parser_callback = parser_callback
    
    def write(self, text: str):
        self.buffer.write(text)
        if text.strip():
            logger.info(f"[SpotiFLAC] {text.rstrip()}")
            self.parser_callback(text.strip(), self.download_id)
        return len(text)
    
    def flush(self):
        pass
    
    def getvalue(self):
        return self.buffer.getvalue()


@dataclass
class DownloadTask:
    download_id: int
    album_id: Optional[int] 
    spotify_url: str
    output_path: str
    services: List[str]
    retry_minutes: int
    title: Optional[str] = None
    artist_name: Optional[str] = None
    album_name: Optional[str] = None
    total_tracks: Optional[int] = None
    # Files placed by the runner across all attempts of this task
    downloaded_files: List[str] = field(default_factory=list)
    # True once the runner reported the collection's missing tracks; the
    # stored failed_tracks are then the whole story (moved-on tracks aren't
    # on disk, so file counts undercount)
    missing_reported: bool = False


@dataclass
class DownloadResult:
    """Result of a SpotiFLAC download operation."""
    success: bool
    tracks_expected: List[str] = field(default_factory=list)
    tracks_found: List[str] = field(default_factory=list)
    tracks_failed: List[Dict[str, str]] = field(default_factory=list)  # {name, error}
    error_message: Optional[str] = None
    logs: str = ""
    # Set from the runner's ___RESULT___ line; retrying while verification is
    # paused upstream only extends the pause, so the retry loop stops early
    verification_blocked: bool = False
    # Final paths of the files the runner placed, and the playlist .m3u8 it wrote
    files: List[str] = field(default_factory=list)
    m3u: Optional[str] = None
    # Set when the runner stopped early because the shared download service
    # (tidal/qobuz/amazon/deezer) is rate limited until this unix time
    rate_limited_until: Optional[float] = None
    # Set when the app stopped the runner on purpose: "break" (a scheduled
    # break is due), "down" (servers down for BREAK_DOWN_STOP_MINUTES), "cap"
    # (daily or HOURLY_TRACK_CAP reached) or "disk" (free space below MIN_FREE_GB)
    stopped_for: Optional[str] = None
    # Set when the runner printed nothing for RUNNER_IDLE_MINUTES and was killed
    hung: bool = False
    # True when the runner reported the collection's missing tracks
    # (tracks_failed is then the authoritative list for this run)
    missing_reported: bool = False


# Runner output that's noise in the container log: debug records (unless
# SPOTIFLAC_DEBUG is on), per-request HTTP chatter, structured ___TAG___=
# lines for the app, and blank lines
_RUNNER_RECORD = re.compile(r" - ([\w.]+) - (DEBUG|INFO|WARNING|ERROR|CRITICAL) - ")
_RUNNER_NOISY_INFO = ("httpx", "httpcore", "hpack", "websockets", "pydoll",
                      "SpotiFLAC.core.signed_session_mobile", "SpotiFLAC.extensions.runtime")


def free_gb() -> float:
    """Free space on the downloads disk, in GB (GiB, like df -h)."""
    try:
        return shutil.disk_usage(get_settings().download_path).free / 1024 ** 3
    except OSError:
        return float("inf")


def low_disk() -> bool:
    minimum = get_settings().min_free_gb
    return minimum > 0 and free_gb() < minimum


def record_status_event(component: str, state: str, detail: str) -> None:
    """A Status page event (shown in its event list)."""
    try:
        from app.models import StatusEvent
        with get_db_session() as db:
            db.add(StatusEvent(component=component, state=state, detail=detail[:500]))
    except Exception as e:
        logger.warning(f"Could not record status event: {e}")


def daily_downloads(window_hours: float = 24) -> List[datetime]:
    """When each track downloaded in the last 24 h was filed, oldest first
    (track history rows fetched by us, not matched existing files)."""
    since = datetime.utcnow() - timedelta(hours=window_hours)
    with get_db_session() as db:
        rows = db.query(TrackHistory.first_seen_at).filter(
            TrackHistory.source == "downloaded", TrackHistory.first_seen_at >= since
        ).order_by(TrackHistory.first_seen_at).all()
    return [r[0] for r in rows]


# Which caps were hit and are still above their resume level
# (CAP_RESUME_RATIO); in memory, so a restart resumes at the cap itself
_cap_latched = {"daily": False, "hourly": False}


def _cap_check(name: str, times: List[datetime], cap: int, window: timedelta, ratio: float):
    """(reached, resume_at) for one rolling window. Once hit, a cap holds
    until the count is down to ratio x cap, so a job doesn't ride the
    window's edge (one aged-out download, one new one)."""
    count = len(times)
    floor = min(cap - 1, int(cap * ratio))
    reached = count >= cap or (_cap_latched[name] and count > floor)
    _cap_latched[name] = reached
    if not reached:
        return False, None
    # Below the resume level once the (count - floor) oldest have aged out
    oldest_needed = times[count - floor - 1]
    return True, (oldest_needed + window).replace(tzinfo=timezone.utc).timestamp()


def daily_cap_state() -> Dict[str, Any]:
    """{"cap", "count", "reached", "resume_at" (unix, when below the resume
    level again), "dynamic", "limit" ("daily"/"hourly"/None), "hour_cap",
    "hour_count"}. With QUOTA_MODE=dynamic the cap comes from
    app.services.quota; HOURLY_TRACK_CAP limits bursts within the day."""
    settings = get_settings()
    dynamic = settings.quota_mode == "dynamic"
    cap = settings.daily_track_cap
    hour_cap = max(0, settings.hourly_track_cap)
    ratio = settings.cap_resume_ratio
    if cap <= 0 and not dynamic and not hour_cap:
        return {"cap": 0, "count": 0, "reached": False, "resume_at": None, "dynamic": False,
                "limit": None, "hour_cap": 0, "hour_count": 0}
    times = daily_downloads()
    count = len(times)
    if dynamic:
        from app.services import quota
        cap = quota.tick(count)["cap"]
    hour_since = datetime.utcnow() - timedelta(hours=1)
    hour_times = [t for t in times if t >= hour_since]
    limit, resume_at = None, None
    if cap > 0:
        reached, at = _cap_check("daily", times, cap, timedelta(hours=24), ratio)
        if reached:
            limit, resume_at = "daily", at
    if hour_cap:
        reached, at = _cap_check("hourly", hour_times, hour_cap, timedelta(hours=1), ratio)
        if reached and (resume_at is None or at > resume_at):
            limit, resume_at = "hourly", at
    return {"cap": max(cap, 0), "count": count, "reached": limit is not None, "resume_at": resume_at,
            "dynamic": dynamic, "limit": limit, "hour_cap": hour_cap, "hour_count": len(hour_times)}


def cap_text(state: Dict[str, Any]) -> str:
    """"daily cap (461/460 in 24 h)" / "hourly cap (120/120 in 1 h)"."""
    if state.get("limit") == "hourly":
        return f"hourly cap ({state['hour_count']}/{state['hour_cap']} in 1 h)"
    return f"daily cap ({state['count']}/{state['cap']} in 24 h)"


def runner_line_wanted(line: str) -> bool:
    text = line.strip()
    if not text or text.startswith("___"):
        return False
    m = _RUNNER_RECORD.search(text)
    if not m:
        return True  # SpotiFLAC's own prints: [RUN] lines, failures, session summary
    name, level = m.groups()
    if level == "DEBUG":
        return os.environ.get("SPOTIFLAC_DEBUG", "").lower() in ("1", "true", "yes")
    if level == "INFO" and name.startswith(_RUNNER_NOISY_INFO):
        return False
    return True


class DownloadManager:
    """Service for managing SpotiFLAC downloads."""
    
    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False
        self._current_download: Optional[DownloadTask] = None
        self._current_process: Optional[subprocess.Popen] = None
        # Set by prioritize(): the download the current job must step aside for
        self._preempt_for: Optional[int] = None
        self._callbacks: List[Callable] = []
        self._spotiflac_available = self._check_spotiflac()
    
    def _check_spotiflac(self) -> bool:
        """Check if SpotiFLAC is available."""
        # First try importing as Python module
        try:
            from SpotiFLAC import SpotiFLAC
            logger.info("SpotiFLAC Python module is available")
            return True
        except ImportError:
            pass
        
        # Try finding the isolated python environment
        python_executable = "/opt/spotiflac-venv/bin/python"
        if os.path.exists(python_executable):
            try:
                # Test import in the isolated environment
                subprocess.check_call([python_executable, "-c", "import SpotiFLAC"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                logger.info("SpotiFLAC Python module is available in isolated environment")
                return True
            except subprocess.CalledProcessError:
                logger.warning("Isolated python exists, but SpotiFLAC module is missing")
        
        # Check for local launcher.py (legacy)
        if os.path.exists("launcher.py"):
            logger.info("SpotiFLAC launcher.py found")
            return True
        
        logger.warning("SpotiFLAC not found. Downloads will not work until configured.")
        return False
    
    @property
    def is_available(self) -> bool:
        """Check if SpotiFLAC is available."""
        return self._spotiflac_available
    
    @property
    def is_running(self) -> bool:
        """Check if the download worker is running."""
        return self._running
    
    @property
    def current_download(self) -> Optional[Dict[str, Any]]:
        """Get info about the current download."""
        if not self._current_download:
            return None
        
        with get_db_session() as db:
            download = db.query(Download).get(self._current_download.download_id)
            return download.to_dict() if download else None
    
    @property
    def queue_size(self) -> int:
        """Get the number of items in the queue."""
        return self._queue.qsize()
    
    def add_progress_callback(self, callback: Callable):
        """Add a callback to be called on download progress updates."""
        self._callbacks.append(callback)
    
    def start_download(
        self, 
        db: Session, 
        spotify_url: str,
        url_type: str,
        title: Optional[str] = None,
        artist_name: Optional[str] = None
    ) -> Download:
        """
        Add a download to the queue to be processed by the worker thread.
        Despite the name, this now uses the queue for sequential processing.
        
        Args:
            db: Database session
            spotify_url: Spotify URL or URI
            url_type: Type of URL (album, track, artist, playlist)
            title: Optional title for display
            artist_name: Optional artist name for display
            
        Returns:
            Download record
        """
        # Ensure worker is running
        if not self._running:
            self.start()
        
        settings = get_settings()
        
        # Fetch metadata from Spotify API if not provided
        parsed_url = parse_spotify_url(spotify_url)
        total_tracks = None
        
        if not title or not artist_name:
            try:
                from app.services.spotify_service import SpotifyService
                spotify_service = SpotifyService()
                
                if parsed_url.url_type == SpotifyUrlType.ALBUM:
                    album_data = spotify_service.get_album(parsed_url.spotify_id)
                    if album_data:
                        title = album_data.get('name') or title
                        artist_name = album_data.get('artist_name') or artist_name
                        total_tracks = album_data.get('total_tracks')
                        logger.info(f"Fetched album metadata: {title} by {artist_name} ({total_tracks} tracks)")
                
                elif parsed_url.url_type == SpotifyUrlType.TRACK:
                    track_data = spotify_service.get_track(parsed_url.spotify_id)
                    if track_data:
                        title = track_data.get('name') or title
                        artist_name = track_data.get('artist_name') or artist_name
                        total_tracks = 1
                        logger.info(f"Fetched track metadata: {title} by {artist_name}")
                
                elif parsed_url.url_type == SpotifyUrlType.ARTIST:
                    artist_data = spotify_service.get_artist(parsed_url.spotify_id)
                    if artist_data:
                        artist_name = artist_data.get('name') or artist_name
                        title = f"All albums by {artist_name}" if artist_name else title
                        logger.info(f"Fetched artist metadata: {artist_name}")
                        
            except Exception as e:
                logger.warning(f"Could not fetch Spotify metadata: {e}")
        
        # Fallback titles if still not available
        if not title:
            title = f"Manual {url_type.title()} Download"
        if not total_tracks and url_type == 'track':
            total_tracks = 1
        
        # Create download record
        download = Download(
            album_id=None,
            spotify_url=spotify_url,
            url_type=url_type,
            title=title,
            artist_name=artist_name,
            total_tracks=total_tracks,
            status=DownloadStatus.QUEUED,
            service=",".join(settings.spotiflac_services),
            output_path=settings.download_path,
        )
        download.image_url = fetch_cover_url(spotify_url)
        db.add(download)
        db.flush()
        
        # Use base download path - SpotiFLAC handles artist/album folders based on config
        download_dir = settings.download_path
        
        task = DownloadTask(
            download_id=download.id,
            album_id=None,
            spotify_url=spotify_url,
            output_path=download_dir,
            services=settings.spotiflac_services,
            retry_minutes=settings.spotiflac_retry_minutes,
            title=title,
            artist_name=artist_name,
            album_name=title,
            total_tracks=total_tracks,
        )
        
        # Add to queue for sequential processing
        self._queue.put(task)
        
        logger.info(f"Added download to queue: {spotify_url} - {title}")
        
        return download
    
    def _notify_progress(self, download_id: int, status: str, progress: float = 0, error: str = None):
        """Notify all callbacks of progress update."""
        for callback in self._callbacks:
            try:
                callback({
                    "download_id": download_id,
                    "status": status,
                    "progress": progress,
                    "error": error,
                })
            except Exception as e:
                logger.error(f"Error in progress callback: {e}")
    
    def start(self):
        """Start the download worker and recover interrupted tasks."""
        if self._running:
            logger.warning("Download manager is already running")
            return
            
        self._running = True
        
        # Recover queued and downloading tasks from database
        from app.database import get_db_session
        from app.models import Download, DownloadStatus
        from app.config import get_settings
        settings = get_settings()
        
        with get_db_session() as db:
            pending_downloads = db.query(Download).filter(
                Download.status.in_([DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING])
            ).order_by(Download.priority.desc(), Download.created_at.asc()).all()
            
            for download in pending_downloads:
                if download.status == DownloadStatus.DOWNLOADING:
                    if download.output_path and os.path.exists(download.output_path):
                        safe_dir = os.path.abspath(settings.download_path)
                        try:
                            target = os.path.abspath(download.output_path)
                            if os.path.commonpath([safe_dir, target]) == safe_dir and target != safe_dir:
                                import shutil
                                shutil.rmtree(target)
                        except (ValueError, OSError) as e:
                            logger.error(f"Failed to cleanup orphaned path {download.output_path}: {e}")
                    download.status = DownloadStatus.QUEUED
                    download.error_message = None  # e.g. "exited with code -15" from the restart
                # Runs always use the current provider settings; keep the shown list in step
                download.service = ",".join(settings.spotiflac_services)

                task = DownloadTask(
                    download_id=download.id,
                    album_id=download.album_id,
                    spotify_url=download.spotify_url,
                    output_path=download.output_path or settings.download_path,
                    services=settings.spotiflac_services,
                    retry_minutes=settings.spotiflac_retry_minutes,
                    title=download.title,
                    artist_name=download.artist_name,
                    album_name=download.title,
                    total_tracks=download.total_tracks,
                )
                self._queue.put(task)
            
            db.commit()
            
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        logger.info("Download manager started and queue recovered")
        threading.Thread(target=self._backfill_covers, daemon=True).start()

    def _backfill_covers(self) -> None:
        """One-off: fetch covers for URL downloads created before image_url existed."""
        try:
            with get_db_session() as db:
                todo = [(d.id, d.spotify_url) for d in db.query(Download).filter(
                    Download.album_id.is_(None), Download.image_url.is_(None),
                    Download.spotify_url.isnot(None)).all()]
            for download_id, url in todo:
                cover = fetch_cover_url(url)
                if cover:
                    with get_db_session() as db:
                        download = db.query(Download).get(download_id)
                        if download:
                            download.image_url = cover
            if todo:
                logger.info(f"Fetched covers for {len(todo)} earlier download(s)")
        except Exception as e:
            logger.warning(f"Cover backfill failed: {e}")
    
    def stop(self):
        """Stop the download worker."""
        self._running = False
        
        # Cancel current download if any
        if self._current_process:
            self._current_process.terminate()
        
        # Wait for worker to finish
        if self._worker_thread and self._worker_thread.is_alive():
            self._queue.put(None)  # Sentinel to wake up the worker
            self._worker_thread.join(timeout=5)
        
        logger.info("Download manager stopped")
    
    def add_to_queue(self, db: Session, album: Album) -> Download:
        """
        Add an album to the download queue.
        
        Args:
            db: Database session
            album: Album to download
            
        Returns:
            Download record
        """
        if not album.spotify_url:
            raise ValueError(f"Album {album.name} has no Spotify URL")
        
        # Ensure worker is running
        if not self._running:
            self.start()
        
        settings = get_settings()
        
        # Check if already in queue or downloading
        existing = db.query(Download).filter(
            Download.album_id == album.id,
            Download.status.in_([DownloadStatus.PENDING, DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING])
        ).first()
        
        if existing:
            logger.warning(f"Album {album.name} is already in the download queue")
            return existing
        
        # Use base download path - SpotiFLAC handles artist/album folders based on config
        download_dir = settings.download_path
        
        # Create download record
        download = Download(
            album_id=album.id,
            status=DownloadStatus.PENDING,
            service=",".join(settings.spotiflac_services),
            output_path=download_dir,
        )
        db.add(download)
        db.flush()
        
        # Create task
        task = DownloadTask(
            download_id=download.id,
            album_id=album.id,
            spotify_url=album.spotify_url,
            output_path=download_dir,
            services=settings.spotiflac_services,
            retry_minutes=settings.spotiflac_retry_minutes,
            artist_name=album.artist.name if album.artist else "Unknown Artist",
            album_name=album.name,
            total_tracks=album.total_tracks,
        )
        
        # Add to queue
        self._queue.put(task)
        download.status = DownloadStatus.QUEUED
        
        logger.info(f"Added album to download queue: {album.name}")
        
        return download
    
    def add_url_to_queue(
        self, 
        db: Session, 
        spotify_url: str,
        url_type: str,
        title: Optional[str] = None,
        artist_name: Optional[str] = None
    ) -> Download:
        """
        Add a Spotify URL directly to the download queue (no album record needed).
        Fetches metadata from Spotify API if available.
        
        Args:
            db: Database session
            spotify_url: Spotify URL or URI
            url_type: Type of URL (album, track, artist, playlist)
            title: Optional title for display (will be fetched from Spotify if not provided)
            artist_name: Optional artist name for display (will be fetched from Spotify if not provided)
            
        Returns:
            Download record
        """
        # Ensure worker is running
        if not self._running:
            self.start()
        
        settings = get_settings()
        
        # Check if same URL already in queue
        existing = db.query(Download).filter(
            Download.spotify_url == spotify_url,
            Download.status.in_([DownloadStatus.PENDING, DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING])
        ).first()
        
        if existing:
            logger.warning(f"URL {spotify_url} is already in the download queue")
            return existing
        
        parsed_url = parse_spotify_url(spotify_url)
        
        total_tracks = None
        if not title or not artist_name:
            try:
                from app.services.spotify_service import SpotifyService
                spotify_service = SpotifyService()
                
                if parsed_url.url_type == SpotifyUrlType.ALBUM:
                    album_data = spotify_service.get_album(parsed_url.spotify_id)
                    if album_data:
                        title = album_data.get('name') or title
                        artist_name = album_data.get('artist_name') or artist_name
                        total_tracks = album_data.get('total_tracks')
                        logger.info(f"Fetched album metadata: {title} by {artist_name} ({total_tracks} tracks)")
                
                elif parsed_url.url_type == SpotifyUrlType.TRACK:
                    # For single track, we could get track metadata
                    # For now, keep simple
                    total_tracks = 1
                
                elif parsed_url.url_type == SpotifyUrlType.PLAYLIST:
                    playlist_data = spotify_service.get_playlist(parsed_url.spotify_id)
                    if playlist_data:
                        title = playlist_data.get('name') or title
                        artist_name = "Various Artists"  # Playlists typically have various artists
                        tracks = playlist_data.get('tracks', [])
                        total_tracks = len(tracks) if tracks else None
                        logger.info(f"Fetched playlist metadata: {title} ({total_tracks} tracks)")
                    
            except Exception as e:
                logger.warning(f"Could not fetch Spotify metadata: {e}")
        
        # Fallback titles if still not available
        if not title:
            title = f"Manual {url_type.title()} Download"
        if not artist_name:
            artist_name = "Unknown Artist"
        if not total_tracks and url_type == 'track':
            total_tracks = 1
        
        # Use base download path - SpotiFLAC handles artist/album folders based on config
        download_dir = settings.download_path
        
        # Create download record without album association
        download = Download(
            album_id=None,  # No album record
            spotify_url=spotify_url,
            url_type=url_type,
            title=title,
            artist_name=artist_name,
            total_tracks=total_tracks,
            status=DownloadStatus.PENDING,
            service=",".join(settings.spotiflac_services),
            output_path=download_dir,
        )
        download.image_url = fetch_cover_url(spotify_url)
        db.add(download)
        db.flush()
        
        # Create task
        task = DownloadTask(
            download_id=download.id,
            album_id=None,
            spotify_url=spotify_url,
            output_path=download_dir,
            services=settings.spotiflac_services,
            retry_minutes=settings.spotiflac_retry_minutes,
            title=title,
            artist_name=artist_name,
            album_name=title,
            total_tracks=total_tracks,
        )
        
        # Add to queue
        self._queue.put(task)
        download.status = DownloadStatus.QUEUED
        
        logger.info(f"Added URL to download queue: {spotify_url} - {title}")
        
        return download
    
    def prioritize(self, download_id: int) -> bool:
        """"Download next": move a queued download to the front. If another job
        is running or waiting, it steps aside (interrupted, then re-queued right
        behind this one; a re-run skips everything already filed)."""
        with get_db_session() as db:
            download = db.query(Download).get(download_id)
            if not download or download.status not in (DownloadStatus.QUEUED, DownloadStatus.PENDING):
                return False
            top = db.query(func.max(Download.priority)).scalar() or 0
            download.priority = top + 1

        current = self._current_download
        if current and current.download_id == download_id:
            return True  # already the one being worked on

        with self._queue.mutex:
            pending = self._queue.queue
            task = next((t for t in pending if t and t.download_id == download_id), None)
            if task is None:
                return False
            pending.remove(task)
            pending.appendleft(task)

        if current is not None:
            logger.info(f"Download {download_id} prioritised; download {current.download_id} steps aside")
            self._preempt_for = download_id
            process = self._current_process
            if process is not None and process.poll() is None:
                process.terminate()
        else:
            logger.info(f"Download {download_id} moved to the front of the queue")
        return True

    def _preempted(self, task: DownloadTask) -> bool:
        return self._preempt_for is not None and self._preempt_for != task.download_id

    def _yield_to_priority(self, task: DownloadTask) -> None:
        """Re-queue an interrupted task right behind the prioritised one."""
        with self._queue.mutex:
            self._queue.queue.insert(min(1, len(self._queue.queue)), task)
            self._queue.unfinished_tasks += 1
            self._queue.not_empty.notify()
        self._preempt_for = None
        self._current_download = None
        self._current_process = None
        with get_db_session() as db:
            download = db.query(Download).get(task.download_id)
            if download and download.status != DownloadStatus.CANCELLED:
                download.status = DownloadStatus.QUEUED
                download.error_message = None
                download.current_track_name = "Queued – paused for a prioritised download"
        logger.info(f"Download {task.download_id} re-queued behind the prioritised download")

    def _nap(self, task: DownloadTask, seconds: float) -> None:
        """Sleep in small steps so stop and "Download next" act quickly."""
        end = time.time() + seconds
        while self._running and time.time() < end and not self._preempted(task):
            time.sleep(1)

    def cancel_download(self, download_id: int) -> bool:
        """
        Cancel a download.
        
        Args:
            download_id: ID of the download to cancel
            
        Returns:
            True if cancelled
        """
        with get_db_session() as db:
            download = db.query(Download).get(download_id)
            
            if not download:
                return False
            
            if download.status == DownloadStatus.DOWNLOADING:
                # Cancel current process
                if self._current_process and self._current_download and \
                   self._current_download.download_id == download_id:
                    self._current_process.terminate()
            
            download.status = DownloadStatus.CANCELLED
            download.completed_at = datetime.utcnow()
            
            logger.info(f"Cancelled download: {download_id}")
            
        return True
    
    def retry_download(self, download_id: int) -> bool:
        """
        Retry a failed download.
        
        Args:
            download_id: ID of the download to retry
            
        Returns:
            True if queued for retry
        """
        with get_db_session() as db:
            download = db.query(Download).get(download_id)
            
            if not download:
                return False
            
            if download.status not in [DownloadStatus.FAILED, DownloadStatus.CANCELLED]:
                logger.warning(f"Cannot retry download {download_id} with status {download.status}")
                return False
            
            settings = get_settings()
            
            # Reset download status
            download.status = DownloadStatus.QUEUED
            download.error_message = None
            download.progress = 0
            download.started_at = None
            download.completed_at = None
            
            # Use base download path - SpotiFLAC handles artist/album folders based on config
            download_dir = settings.download_path
            download.output_path = download_dir
            
            # Extract metadata
            album_id = download.album.id if download.album else None
            spotify_url = download.album.spotify_url if download.album else download.spotify_url
            
            artist_name = "Unknown Artist"
            if download.artist_name:
                artist_name = download.artist_name
            elif download.album and download.album.artist:
                artist_name = download.album.artist.name
                
            album_name = "Unknown Album"
            if download.title:
                album_name = download.title
            elif download.album:
                album_name = download.album.name
            
            download.service = ",".join(settings.spotiflac_services)
            # Retry gives the job's skipped tracks another try
            cleared = track_failures.clear_download(db, download.id)
            if cleared:
                logger.info(f"Download {download_id}: cleared {cleared} repeated-failure record(s)")

            # Create new task
            task = DownloadTask(
                download_id=download.id,
                album_id=album_id,
                spotify_url=spotify_url,
                output_path=download_dir,
                services=settings.spotiflac_services,
                retry_minutes=settings.spotiflac_retry_minutes,
                artist_name=artist_name,
                album_name=album_name,
                total_tracks=download.total_tracks if download.total_tracks else (download.album.total_tracks if download.album else None),
            )
            
            self._queue.put(task)
            logger.info(f"Queued download for retry: {download_id}")
            
        return True
    
    def get_queue(self) -> List[Dict[str, Any]]:
        """Get all downloads in queue or in progress."""
        with get_db_session() as db:
            downloads = db.query(Download).filter(
                Download.status.in_([
                    DownloadStatus.PENDING,
                    DownloadStatus.QUEUED,
                    DownloadStatus.DOWNLOADING,
                ])
            ).order_by(Download.created_at).all()
            
            return [d.to_dict() for d in downloads]
    
    def get_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get download history."""
        with get_db_session() as db:
            downloads = db.query(Download).filter(
                Download.status.in_([
                    DownloadStatus.COMPLETED,
                    DownloadStatus.FAILED,
                    DownloadStatus.CANCELLED,
                ])
            ).order_by(Download.completed_at.desc()).limit(limit).all()
            
            return [d.to_dict() for d in downloads]
    
    def _worker_loop(self):
        """Main worker loop for processing downloads."""
        logger.info("Download worker loop started")
        while self._running:
            try:
                # Get next task (blocks until available or timeout)
                task = self._queue.get(timeout=1)
                
                if task is None:
                    continue

                # Current while it waits too, so "Download next" can take over
                self._current_download = task

                # Cancelled while still queued: cancel_download() only marked it
                if self._is_cancelled(task.download_id):
                    logger.info(f"Skipping cancelled download {task.download_id}")
                    self._current_download = None
                    self._queue.task_done()
                    continue

                outcome = self._wait_for_window(task)
                if outcome == "preempted":
                    self._yield_to_priority(task)
                    self._queue.task_done()
                    continue
                if outcome != "ready":
                    self._current_download = None
                    self._queue.task_done()
                    continue

                logger.info(f"Processing download task: {task.download_id}")
                self._process_download(task)
                logger.info(f"Finished processing download task: {task.download_id}")
                
                # Mark task as done in the queue
                self._queue.task_done()
                
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error in download worker loop: {e}", exc_info=True)
                # Don't crash the worker, continue processing
                continue
        
        logger.info("Download worker loop stopped")
    
    def _wait_for_window(self, task: DownloadTask) -> str:
        """Pause a task while every tracked provider in its chain is on a
        scheduled break or down, until they have been back for
        BREAK_RESUME_STABLE_SECONDS, and while the next break is too close
        to start a run (BreakMonitor.ready), keeping it QUEUED (a task that
        was already downloading goes back to QUEUED too).

        Returns "ready" once it may run, "cancelled" if it was cancelled while
        paused, or "stopped" if the manager is shutting down (the task stays
        QUEUED so startup recovery picks it up again).
        """
        if not get_settings().break_gate_downloads:
            return "ready"

        monitor = get_break_monitor()
        monitor.ensure_polled()
        held = False
        while self._running:
            ok, message = monitor.ready(task.services)
            if ok:
                break
            if not held:
                logger.info(f"Download {task.download_id} paused: {message}")
                held = True
            with get_db_session() as db:
                download = db.query(Download).get(task.download_id)
                if not download or download.status == DownloadStatus.CANCELLED:
                    logger.info(f"Download {task.download_id} cancelled while paused")
                    return "cancelled"
                if download.status == DownloadStatus.DOWNLOADING:
                    download.status = DownloadStatus.QUEUED
                download.error_message = None  # an earlier run's error is stale while waiting
                if download.current_track_name != message:
                    download.current_track_name = message
            self._nap(task, 15)
            if self._preempted(task):
                return "preempted"

        if not self._running:
            return "stopped"
        if held:
            logger.info(f"Download servers available, resuming download {task.download_id}")
            # Turnstile failures caused by the break would otherwise keep the
            # runner skipping tidal-web for up to an hour after it ends;
            # qobuz's rate-limit skip stays (the break doesn't lift it)
            try:
                from app.services.service_status import clear_turnstile_cooldowns
                cleared = clear_turnstile_cooldowns()
                if cleared:
                    logger.info(f"Cleared provider circuit breaker after break: {', '.join(cleared)}")
            except OSError as e:
                logger.warning(f"Could not clear circuit breaker: {e}")
        return "ready"

    def _stop_reason(self, task: DownloadTask) -> Optional[str]:
        """Why a running job should stop now: "disk", "break" (a scheduled
        break is due within BREAK_STOP_LEAD_SECONDS or on), "down" (servers
        down for BREAK_DOWN_STOP_MINUTES) or None."""
        if low_disk():
            return "disk"
        settings = get_settings()
        if not (settings.break_gate_downloads and settings.break_pause_running):
            return None
        monitor = get_break_monitor()
        stop_at = monitor.stop_at(task.services)
        if (stop_at and time.time() >= stop_at) or monitor.on_break(task.services):
            return "break"
        if monitor.blocked_for(task.services) >= settings.break_down_stop_minutes * 60:
            return "down"
        return None

    def _watch_run(self, task: DownloadTask, process: subprocess.Popen,
                   result: "DownloadResult", done: threading.Event, last_output: List[float]) -> None:
        """Runs beside the runner: stops it for a break (on the countdown, to
        the second), a lasting outage or low disk. Filed tracks stay; the job
        waits and re-runs. Also kills a hung runner (no output for
        RUNNER_IDLE_MINUTES, or exited while a child still holds its output)."""
        settings = get_settings()
        idle_limit = settings.runner_idle_minutes * 60
        if settings.break_gate_downloads and settings.break_pause_running:
            from app.services.break_monitor import format_clock, format_seconds
            break_at = get_break_monitor().break_at(task.services)
            stop_at = get_break_monitor().stop_at(task.services)
            if break_at:
                logger.info(f"Download {task.download_id}: next break at {format_clock(break_at)}; "
                            f"stopping at {format_clock(stop_at)} (in {format_seconds(stop_at - time.time())})")
        ticks = 0
        exited_at = stop_sent = None
        while not done.wait(1):
            idle = time.monotonic() - last_output[0]
            exited = process.poll() is not None
            if exited and exited_at is None:
                exited_at = time.monotonic()
            if exited and time.monotonic() - exited_at > 60:
                # The runner is gone but a child keeps its output open, so
                # the reader never sees the end; the run's result stands
                logger.error(f"Download {task.download_id}: runner exited but a child process "
                             "still holds its output; killing its child processes")
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
                return
            if not exited and idle_limit > 0 and idle > idle_limit:
                logger.error(f"Download {task.download_id}: runner printed nothing for "
                             f"{idle / 60:.0f} min; killing it and its child processes")
                result.hung = True
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
                return
            if exited:
                continue
            if result.stopped_for is not None:
                if time.monotonic() - stop_sent > 60:
                    logger.error(f"Download {task.download_id}: runner ignored the stop for 60 s; killing it")
                    with contextlib.suppress(OSError):
                        os.killpg(process.pid, signal.SIGKILL)
                    return
                continue
            ticks += 1
            reason = self._stop_reason(task)
            if not reason and ticks % 5 == 0 and daily_cap_state()["reached"]:
                reason = "cap"
            if not reason:
                continue
            result.stopped_for = reason
            if reason == "break":
                at = get_break_monitor().break_at(task.services)
                why = (f"scheduled break at {format_utc(at)} in {int(at - time.time())}s" if at
                       else "scheduled break started")
            elif reason == "down":
                why = f"download servers down for {settings.break_down_stop_minutes:g} min"
            elif reason == "cap":
                why = f"{cap_text(daily_cap_state())} reached"
            else:
                why = f"low disk space ({free_gb():.1f} GB free)"
            logger.warning(f"Stopping download {task.download_id}: {why}")
            process.terminate()
            stop_sent = time.monotonic()

    def _wait_for_disk(self, task: DownloadTask) -> str:
        """Hold a task (QUEUED, "Waiting – low disk space …") until the
        downloads disk has MIN_FREE_GB free again. Same outcomes as
        _wait_for_window."""
        minimum = get_settings().min_free_gb
        logger.warning(f"Download {task.download_id} waiting: low disk space "
                       f"({free_gb():.1f} GB free, needs {minimum:g} GB)")
        record_status_event("disk", "limited",
                            f"Low disk space: {free_gb():.1f} GB free (needs {minimum:g} GB); downloads paused")
        while self._running and low_disk():
            message = f"Waiting – low disk space ({free_gb():.1f} GB free, needs {minimum:g} GB)"
            with get_db_session() as db:
                download = db.query(Download).get(task.download_id)
                if not download or download.status == DownloadStatus.CANCELLED:
                    return "cancelled"
                download.status = DownloadStatus.QUEUED
                download.error_message = None
                download.current_track_name = message
            self._nap(task, 60)
            if self._preempted(task):
                return "preempted"
        if not self._running:
            return "stopped"
        logger.info(f"Disk space available again ({free_gb():.1f} GB free), resuming download {task.download_id}")
        record_status_event("disk", "ok", f"Disk space OK: {free_gb():.1f} GB free; downloads resume")
        return "ready"

    def _wait_for_cap(self, task: DownloadTask) -> str:
        """Hold a task (QUEUED, "Waiting – daily cap …") until the daily
        and hourly downloads are back down to CAP_RESUME_RATIO of their caps.
        Same outcomes as _wait_for_window."""
        state = daily_cap_state()
        if state["reached"]:
            detail = (f"{cap_text(state)[0].upper()}{cap_text(state)[1:]} reached; "
                      f"resumes at {format_utc(state['resume_at'])}")
            logger.warning(f"Download {task.download_id} waiting: {detail}")
            record_status_event("cap", "limited", detail)
        while self._running:
            state = daily_cap_state()
            if not state["reached"]:
                break
            message = f"Waiting – {cap_text(state)}, resumes at {format_utc(state['resume_at'])}"
            with get_db_session() as db:
                download = db.query(Download).get(task.download_id)
                if not download or download.status == DownloadStatus.CANCELLED:
                    return "cancelled"
                download.status = DownloadStatus.QUEUED
                download.error_message = None
                download.current_track_name = message
            self._nap(task, 60)
            if self._preempted(task):
                return "preempted"
        if not self._running:
            return "stopped"
        if state["count"] or state["cap"]:
            record_status_event("cap", "ok", f"Under the caps: {state['count']}/{state['cap']} in 24 h, "
                                             f"{state.get('hour_count', 0)}/{state.get('hour_cap') or '–'} in 1 h; downloads resume")
        return "ready"

    def _wait_for_rate_limit(self, task: DownloadTask, until: float) -> str:
        """Hold a task (QUEUED, "Waiting – rate limited ...") until the shared
        download service accepts requests again. Same outcomes as
        _wait_for_window: "ready", "cancelled" or "stopped"."""
        resume_at = format_utc(until)
        logger.warning(f"Download {task.download_id} waiting: download service rate limited until {resume_at}")
        while self._running and time.time() < until:
            minutes = max(1, int((until - time.time()) // 60))
            message = f"Waiting – download service rate limited, resumes in {format_minutes(minutes)} ({resume_at})"
            with get_db_session() as db:
                download = db.query(Download).get(task.download_id)
                if not download or download.status == DownloadStatus.CANCELLED:
                    return "cancelled"
                download.status = DownloadStatus.QUEUED
                download.error_message = None
                download.current_track_name = message
            self._nap(task, 30)
            if self._preempted(task):
                return "preempted"
        return "ready" if self._running else "stopped"

    def _resume_downloading(self, task: DownloadTask) -> None:
        with get_db_session() as db:
            download = db.query(Download).get(task.download_id)
            if download:
                download.status = DownloadStatus.DOWNLOADING
                download.error_message = None
                download.current_track_name = "Resuming..."

    def _store_missing(self, download_id: int, missing: List[Dict[str, str]]) -> None:
        """Replace the job's failed-track list with the runner's report of the
        collection's missing tracks (earlier runs' entries are stale)."""
        try:
            with get_db_session() as db:
                download = db.query(Download).get(download_id)
                if download:
                    download.failed_tracks = missing
            if missing:
                logger.info(f"Download {download_id}: {len(missing)} track(s) missing after this run")
        except Exception as e:
            logger.error(f"Unable to store missing tracks: {e}")

    def _is_cancelled(self, download_id: int) -> bool:
        with get_db_session() as db:
            download = db.query(Download).get(download_id)
            return not download or download.status == DownloadStatus.CANCELLED

    def _process_download(self, task: DownloadTask):
        """Process a single download task with retry mechanism."""
        self._current_download = task
        
        try:
            with get_db_session() as db:
                download = db.query(Download).get(task.download_id)
                
                if not download:
                    logger.error(f"Download record not found: {task.download_id}")
                    return
                
                # Update status
                download.status = DownloadStatus.DOWNLOADING
                download.started_at = datetime.utcnow()
                # A previous (interrupted) run's error and last track are stale now
                download.error_message = None
                download.current_track_name = "Starting..."
                db.commit()
                
                self._notify_progress(task.download_id, "downloading", 0)
        except Exception as e:
            logger.error(f"Error updating download status to DOWNLOADING: {e}")
        
        # Retry loop with exponential backoff
        max_retries = 3
        retry_count = 0
        success = False
        cancelled = False
        last_error = None

        while retry_count <= max_retries and not success:
            try:
                if retry_count > 0:
                    # Calculate exponential backoff: 2^retry_count seconds
                    backoff_seconds = 2 ** retry_count
                    logger.info(f"Retry attempt {retry_count}/{max_retries} for download {task.download_id}")
                    logger.info(f"Waiting {backoff_seconds} seconds before retry (exponential backoff)...")
                    
                    # Update download record with retry info
                    with get_db_session() as db:
                        download = db.query(Download).get(task.download_id)
                        if download:
                            download.retry_count = retry_count
                            download.current_track_name = f"Retrying... (attempt {retry_count}/{max_retries})"
                            db.commit()
                    
                    time.sleep(backoff_seconds)
                
                # "Download next" on another job: step aside (re-queued behind it)
                if self._preempted(task):
                    self._yield_to_priority(task)
                    return

                # A known download-service penalty (SpotiFLAC's backoff or one the
                # runner recorded): don't start the runner (its planning alone
                # can take minutes); wait as if it had stopped on the limit
                from app.services.service_status import download_backoff
                penalty_until = download_backoff()["until"]
                if penalty_until > time.time() + 30:
                    result = DownloadResult(success=False, rate_limited_until=penalty_until,
                                            error_message="Download service rate limited until "
                                                          + format_utc(penalty_until))
                elif low_disk():
                    result = DownloadResult(success=False, stopped_for="disk",
                                            error_message="Low disk space")
                elif daily_cap_state()["reached"]:
                    result = DownloadResult(success=False, stopped_for="cap",
                                            error_message="Download cap reached")
                else:
                    # Execute SpotiFLAC
                    result = self._execute_spotiflac(task)
                if result.missing_reported:
                    task.missing_reported = True
                    self._store_missing(task.download_id, result.tracks_failed)

                # Interrupted for a prioritised download (its runner was
                # terminated): filed tracks are kept, the re-run skips them
                if self._preempted(task):
                    self._yield_to_priority(task)
                    return

                # Stopped on purpose (scheduled break / low disk): wait, then
                # re-run without spending a retry (filed tracks are skipped)
                if result.stopped_for:
                    if result.stopped_for == "cap":
                        outcome = self._wait_for_cap(task)
                        if outcome == "ready":
                            outcome = self._wait_for_window(task)
                    elif result.stopped_for in ("break", "down"):
                        outcome = self._wait_for_window(task)
                    else:
                        outcome = self._wait_for_disk(task)
                    if outcome == "preempted":
                        self._yield_to_priority(task)
                        return
                    if outcome == "cancelled":
                        cancelled = True
                        break
                    if outcome == "stopped":
                        self._current_download = None
                        self._current_process = None
                        return
                    self._resume_downloading(task)
                    continue

                # The runner stopped early: the shared download service is rate
                # limited. Hold the whole job (filed tracks are kept and skipped
                # on the re-run) and resume afterwards, without spending a retry.
                if result.rate_limited_until and result.rate_limited_until > time.time():
                    outcome = self._wait_for_rate_limit(task, result.rate_limited_until)
                    if outcome == "preempted":
                        self._yield_to_priority(task)
                        return
                    if outcome == "cancelled":
                        cancelled = True
                        break
                    if outcome == "stopped":
                        self._current_download = None
                        self._current_process = None
                        return
                    outcome = self._wait_for_window(task)
                    if outcome == "preempted":
                        self._yield_to_priority(task)
                        return
                    if outcome != "ready":
                        cancelled = self._is_cancelled(task.download_id)
                        if not cancelled:
                            self._current_download = None
                            self._current_process = None
                            return
                        break
                    self._resume_downloading(task)
                    continue

                if result.success:
                    success = True
                    last_error = None
                    break
                else:
                    last_error = result.error_message or "Download failed"

                    if self._is_cancelled(task.download_id):
                        cancelled = True
                        break

                    # A break that started mid-download: back to the paused
                    # queue without spending a retry, then try again
                    if get_settings().break_gate_downloads:
                        monitor = get_break_monitor()
                        monitor.poll()
                        if monitor.paused(task.services):
                            logger.warning(
                                f"Download servers paused during download {task.download_id}; "
                                "returning it to the queue until they are available"
                            )
                            outcome = self._wait_for_window(task)
                            if outcome == "preempted":
                                self._yield_to_priority(task)
                                return
                            if outcome == "cancelled":
                                cancelled = True
                                break
                            if outcome == "stopped":
                                self._current_download = None
                                self._current_process = None
                                return
                            with get_db_session() as db:
                                download = db.query(Download).get(task.download_id)
                                if download:
                                    download.status = DownloadStatus.DOWNLOADING
                                    download.current_track_name = "Resuming after break..."
                            continue

                    retry_count += 1
                    logger.warning(f"Download attempt {retry_count} failed: {last_error}")
                    if result.verification_blocked:
                        logger.warning(
                            "Turnstile verification failed or is paused; skipping further "
                            "retries since they would only extend the pause"
                        )
                        break
                    
            except Exception as e:
                last_error = str(e)
                retry_count += 1
                logger.error(f"Download attempt {retry_count} raised exception: {e}", exc_info=True)

        # Cancelled: cancel_download() already set the final status
        if cancelled:
            logger.info(f"Download {task.download_id} cancelled, stopping retries")
            self._current_download = None
            self._current_process = None
            return

        # Update final status
        try:
            with get_db_session() as db:
                download = db.query(Download).get(task.download_id)
                
                if not download:
                    logger.error(f"Download record disappeared: {task.download_id}")
                    return
                
                download.retry_count = retry_count
                
                if success:
                    download.status = DownloadStatus.COMPLETED
                    download.progress = 100
                    download.error_message = None
                    download.current_track_name = "Download complete"
                    
                    # Verify completeness and mark album if incomplete
                    incomplete_info = self._verify_download_completeness(task, download)
                    if incomplete_info and incomplete_info.get('is_incomplete'):
                        if incomplete_info.get('found', -1) == 0:
                            logger.error("Download returned success but 0 files were found. Re-classifying as FAILED.")
                            download.status = DownloadStatus.FAILED
                            download.error_message = "Download failed: 0 files produced"
                        else:
                            logger.warning(f"Download completed but album is incomplete: {incomplete_info}")
                            download.error_message = f"Incomplete: {len(incomplete_info.get('missing_tracks', []))} track(s) missing"
                    
                    # Mark album as owned (or potentially incomplete)
                    if download.album_id and download.status != DownloadStatus.FAILED:
                        album = db.query(Album).get(download.album_id)
                        if album:
                            album.is_owned = True
                            if incomplete_info and incomplete_info.get('is_incomplete'):
                                album.is_incomplete = True
                                album.missing_tracks = incomplete_info.get('missing_tracks', [])
                    
                    if download.status != DownloadStatus.FAILED:
                        logger.info(f"Download completed: {task.download_id}")
                        self._notify_progress(task.download_id, "completed", 100)
                        
                        # Trigger library scan after successful download
                        try:
                            settings = get_settings()
                            if settings.auto_scan_on_startup:
                                from app.services.library_scanner import get_library_scanner
                                scanner = get_library_scanner()
                                logger.info("Triggering library scan after download completion")
                                scanner.scan_library()
                            else:
                                logger.info("Skipping library scan (auto_scan disabled)")
                        except Exception as scan_err:
                            logger.error(f"Failed to trigger library scan: {scan_err}")
                    else:
                        logger.warning(f"Download {task.download_id} was re-classified as FAILED")
                else:
                    download.status = DownloadStatus.FAILED
                    download.error_message = last_error or f"Download failed after {retry_count} attempts"
                    
                    logger.error(f"Download failed after {retry_count} attempts: {task.download_id}")
                    self._notify_progress(task.download_id, "failed", 0, download.error_message)
                
                download.completed_at = datetime.utcnow()
                db.commit()
                logger.debug(f"Download status committed: {task.download_id} -> {download.status}")
                
        except Exception as e:
            logger.error(f"Error processing download {task.download_id}: {e}", exc_info=True)
            
            try:
                with get_db_session() as db:
                    download = db.query(Download).get(task.download_id)
                    if download:
                        download.status = DownloadStatus.FAILED
                        download.error_message = str(e)
                        download.completed_at = datetime.utcnow()
                        db.commit()
            except Exception as db_err:
                logger.error(f"Failed to update download status in DB: {db_err}")
            
            self._notify_progress(task.download_id, "failed", 0, str(e))
        
        finally:
            self._current_download = None
            self._current_process = None
    
    def _verify_download_completeness(self, task: DownloadTask, download: Download) -> Optional[Dict[str, Any]]:
        """
        Verify if the download is complete by checking expected vs actual tracks.
        
        Args:
            task: Download task containing album info
            download: Download record with failure information
            
        Returns:
            Dict with completeness info, or None if verification not possible
        """
        try:
            # Get expected track count
            expected_tracks = task.total_tracks or download.total_tracks
            if not expected_tracks:
                logger.warning("Cannot verify completeness: no expected track count")
                return None
            
            # Get failed tracks from download record
            failed_tracks = download.failed_tracks or []
            if isinstance(failed_tracks, str):
                failed_tracks = json.loads(failed_tracks) if failed_tracks else []

            # The runner planned the collection: its missing list is exact
            if task.missing_reported:
                found = max(0, expected_tracks - len(failed_tracks))
                logger.info(f"Completeness check: {found} of {expected_tracks} tracks filed or "
                            f"downloaded before, {len(failed_tracks)} missing")
                if failed_tracks:
                    logger.warning(f"Album incomplete: {len(failed_tracks)} track(s) missing")
                return {
                    'is_incomplete': bool(failed_tracks),
                    'expected': expected_tracks,
                    'found': found,
                    'missing_tracks': [f['name'] for f in failed_tracks]
                }

            # Files the runner reported placing for this download
            if task.downloaded_files:
                actual_track_count = sum(1 for f in task.downloaded_files if os.path.exists(f))
                download_dir = None
            else:
                download_dir = self._get_expected_download_dir(task)
                actual_track_count = None

            if actual_track_count is None and (not download_dir or not os.path.exists(download_dir)):
                logger.warning(f"Cannot verify completeness: download directory not found: {download_dir}")
                # If we have failed tracks but no directory, definitely incomplete
                if failed_tracks:
                    return {
                        'is_incomplete': True,
                        'expected': expected_tracks,
                        'found': 0,
                        'missing_tracks': [f['name'] for f in failed_tracks]
                    }
                return None
            
            if actual_track_count is None:
                actual_track_count = len(self._get_audio_files(download_dir))
            
            logger.info(f"Completeness check: Expected {expected_tracks} tracks, found {actual_track_count} files, {len(failed_tracks)} failures recorded")
            
            # Determine if incomplete
            is_incomplete = False
            missing_tracks = []
            
            # If we have explicit failures, mark as incomplete
            if failed_tracks:
                is_incomplete = True
                missing_tracks = [f['name'] for f in failed_tracks]
            # If file count doesn't match expected (with some tolerance for bonus tracks)
            elif actual_track_count < expected_tracks:
                is_incomplete = True
                missing_count = expected_tracks - actual_track_count
                missing_tracks = [f"Unknown track #{i+1}" for i in range(missing_count)]
            
            if is_incomplete:
                logger.warning(f"Album incomplete: {len(missing_tracks)} track(s) missing")
                return {
                    'is_incomplete': True,
                    'expected': expected_tracks,
                    'found': actual_track_count,
                    'missing_tracks': missing_tracks
                }
            else:
                logger.info("Album download is complete!")
                return {
                    'is_incomplete': False,
                    'expected': expected_tracks,
                    'found': actual_track_count,
                    'missing_tracks': []
                }
                
        except Exception as e:
            logger.error(f"Error verifying download completeness: {e}", exc_info=True)
            return None
    
    def _get_expected_download_dir(self, task: DownloadTask) -> Optional[str]:
        """Get the expected download directory for a task by interpreting the format structure."""
        base_path = task.output_path
        if not base_path or not os.path.exists(base_path):
            return None
            
        from app.config import get_settings
        settings = get_settings()
        
        is_track = "track" in task.spotify_url
        is_playlist = "playlist" in task.spotify_url
        
        if is_track:
            return base_path
            
        elif is_playlist:
            structure = getattr(settings, 'spotiflac_playlist_folder_structure', 'Playlists/{playlist_name}')
            playlist_name = task.album_name or task.title or "Unknown Playlist"
            playlist_name = self._sanitize_filename(playlist_name)
            
            parts = structure.split('/')
            current_path = base_path
            for part in parts:
                if "{playlist_name}" in part:
                    part = part.replace("{playlist_name}", playlist_name)
                current_path = os.path.join(current_path, part)
            return current_path
            
        else:
            # Album
            structure = settings.spotiflac_folder_structure
            album_name = task.album_name or "Unknown Album"
            artist_name = task.artist_name or "Unknown Artist"
            
            parts = structure.split('/')
            current_path = base_path
            
            for part in parts:
                if not os.path.exists(current_path):
                    break
                    
                if '{' not in part:
                    current_path = os.path.join(current_path, part)
                    continue
                    
                found = False
                
                # Determine what we are looking for in this directory level
                if "{album_artist}" in part or "{artist}" in part:
                    search_str = self._sanitize_filename(artist_name).lower()
                elif "{album}" in part:
                    search_str = self._sanitize_filename(album_name).lower()
                else:
                    search_str = ""
                
                if search_str:
                    try:
                        for item in os.listdir(current_path):
                            if os.path.isdir(os.path.join(current_path, item)) and search_str in item.lower():
                                current_path = os.path.join(current_path, item)
                                found = True
                                break
                    except OSError:
                        pass
                
                if not found:
                    break
                    
            return current_path
    
    def _sanitize_filename(self, name: str) -> str:
        """Sanitize a string for use in filenames."""
        invalid_chars = '<>:"\\/|?*'
        for char in invalid_chars:
            name = name.replace(char, '_')
        
        name = name.strip('. ')
        
        return name
    
    def _write_history_file(self, task: DownloadTask) -> Optional[str]:
        """Write {spotify_id: {"path", "title", "artist"}} of every track in
        the download history for the runner, which skips those it can't find
        on disk, also under another Spotify ID with the same artist + title."""
        try:
            with get_db_session() as db:
                rows = db.query(TrackHistory.spotify_id, TrackHistory.file_path,
                                TrackHistory.title, TrackHistory.artists).filter(
                    TrackHistory.file_path.isnot(None)
                ).all()
            path = os.path.join(tempfile.gettempdir(), f"spotiflac-history-{task.download_id}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({sid: {"path": fp, "title": title,
                                 "artist": (artists or "").split(";")[0].strip()}
                           for sid, fp, title, artists in rows}, f)
            return path
        except Exception as e:
            logger.warning(f"Could not write download history for the runner: {e}")
            return None

    def _write_skip_file(self, task: DownloadTask) -> Optional[str]:
        """Write the tracks that failed repeatedly ({spotify_id: {provider:
        reason}}) for the runner, which doesn't hand them to those providers."""
        try:
            with get_db_session() as db:
                skips = track_failures.skip_list(db)
            if not skips:
                return None
            path = os.path.join(tempfile.gettempdir(), f"spotiflac-skip-{task.download_id}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(skips, f)
            return path
        except Exception as e:
            logger.warning(f"Could not write the skip list for the runner: {e}")
            return None

    def _record_track(self, data: dict, download_id: int) -> None:
        """Upsert a ___TRACK___ event into the download history."""
        spotify_id = (data.get("spotify_id") or "").strip()
        if not spotify_id:
            return
        fields = ("isrc", "title", "artists", "album", "album_artist", "year",
                  "track_number", "file_path", "size_bytes", "playlist", "source")
        with get_db_session() as db:
            row = db.query(TrackHistory).filter(TrackHistory.spotify_id == spotify_id).first()
            if row is None:
                row = TrackHistory(spotify_id=spotify_id)
                db.add(row)
            for name in fields:
                value = data.get(name)
                # Keep a known value rather than overwrite it with a blank
                if value not in (None, ""):
                    if name == "source" and row.source == "downloaded":
                        continue  # "downloaded" is the more useful fact to keep
                    setattr(row, name, value)
            row.download_id = download_id
            row.last_seen_at = datetime.utcnow()
            track_failures.clear(db, [spotify_id])

    def _apply_runner_event(self, line: str, download_id: int) -> None:
        """Update the download record from a runner ___PROGRESS___ or
        ___PLAYLIST___ line, which drive the progress shown on the dashboard."""
        tag, _, payload = line.partition("=")
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.debug(f"Unparseable runner event: {line[:120]}")
            return
        try:
            with get_db_session() as db:
                download = db.query(Download).get(download_id)
                if not download or download.status != DownloadStatus.DOWNLOADING:
                    return
                if tag == "___PLAYLIST___":
                    name = (data.get("name") or "").strip()
                    if name:
                        download.title = name
                        if self._current_download and self._current_download.download_id == download_id:
                            self._current_download.album_name = name
                    return
                done, total = int(data.get("done") or 0), int(data.get("total") or 0)
                download.current_track_number = done
                progress = download.progress or 0
                if total:
                    download.total_tracks = total
                    # 100 is set when the whole download is verified complete
                    progress = download.progress = min(99, int(done * 100 / total))
                state = data.get("state")
                # A failure here is one provider missing one track; the job
                # carries on (next track, then the next provider)
                provider = (data.get("provider") or "").split(":", 1)[-1]
                prefix = {"existing": "Already have", "previous": "Downloaded before",
                          "failed": f"Couldn't get from {provider}" if provider else "Couldn't get"}.get(state, "Saved")
                missing = int(data.get("failed") or 0)
                suffix = f" ({missing} not found yet)" if state == "failed" and missing else ""
                download.current_track_name = f"{prefix}: {data.get('track', '')}{suffix}"[:255]
            self._notify_progress(download_id, "downloading", progress)
        except Exception as e:
            logger.error(f"Unable to apply runner progress: {e}")

    def _parse_spotiflac_output(self, line: str, download_id: int) -> None:
        """
        Parse spotiflac output line and update progress.
        Extracts track progress from lines like: [1/17] Starting download: Damned - Kevin Sherwood
        Also detects track failures from error messages.
        
        Args:
            line: Output line from spotiflac
            download_id: ID of the download to update
        """
        # Log every line we're trying to parse
        logger.debug(f"[PARSER] Parsing line for download {download_id}: {line[:100]}")

        # Structured lines from scripts/spotiflac_runner.py (one per filed track)
        if line.startswith("___TRACK___="):
            try:
                self._record_track(json.loads(line.partition("=")[2]), download_id)
            except Exception as e:
                logger.error(f"Unable to record track history: {e}")
            return
        if line.startswith("___FAIL___="):
            try:
                with get_db_session() as db:
                    track_failures.record(db, json.loads(line.partition("=")[2]), download_id)
            except Exception as e:
                logger.error(f"Unable to record track failure: {e}")
            return
        if line.startswith("___PURGE___="):
            try:
                data = json.loads(line.partition("=")[2])
                from app.services.library_check import forget_file
                forget_file(data.get("file_path", ""))
                logger.warning(f"Removed non-FLAC file {data.get('file_path')} "
                               f"({data.get('format')}, {data.get('action')}); it will be downloaded again")
            except Exception as e:
                logger.error(f"Unable to handle purged file: {e}")
            return
        if line.startswith(("___PROGRESS___=", "___PLAYLIST___=")):
            self._apply_runner_event(line, download_id)
            return

        # Pattern: [X/Y] Starting download: TrackName - Artist
        match = re.match(r'\[(\d+)/(\d+)\]\s+Starting download:\s+(.+)', line)
        if match:
            current_track = int(match.group(1))
            total_tracks = int(match.group(2))
            track_name = match.group(3).strip()
            
            logger.info(f"✓ MATCHED track progress: [{current_track}/{total_tracks}] {track_name}")
            
            # Calculate progress percentage
            progress = int((current_track / total_tracks) * 100) if total_tracks > 0 else 0
            
            try:
                with get_db_session() as db:
                    download = db.query(Download).get(download_id)
                    if download:
                        logger.info(f"  Current status: {download.status}")
                        if download.status == DownloadStatus.DOWNLOADING:
                            download.current_track_number = current_track
                            download.total_tracks = total_tracks
                            download.current_track_name = track_name
                            download.progress = progress
                            db.commit()
                            self._notify_progress(download_id, "downloading", progress)
                            
                            if current_track == total_tracks:
                                download.status = DownloadStatus.COMPLETED
                                download.progress = 100
                                download.current_track_name = "Download complete"
                                download.completed_at = datetime.utcnow()
                                
                                if download.album_id:
                                    album = db.query(Album).get(download.album_id)
                                    if album:
                                        album.is_owned = True
                                
                                db.commit()
                                self._notify_progress(download_id, "completed", 100)
                                
                                try:
                                    from app.services.library_scanner import get_library_scanner
                                    scanner = get_library_scanner()
                                    scanner.scan_library()
                                except Exception as scan_err:
                                    logger.error(f"Failed to trigger library scan: {scan_err}")
                        else:
                            logger.warning(f"  Download status is {download.status}, not updating progress")
                    else:
                        logger.error(f"  Download {download_id} not found in database!")
            except Exception as e:
                logger.error(f"Unable to update track progress: {e}", exc_info=True)
    
    def _execute_spotiflac(self, task: DownloadTask) -> DownloadResult:
        """
        Execute SpotiFLAC to download an album.
        
        Args:
            task: Download task to execute
            
        Returns:
            DownloadResult with success status and error information
        """
        result = self._execute_spotiflac_cli_with_capture(task)

        # Primary success signal is the subprocess return code
        if result.rate_limited_until:
            logger.info(result.error_message)
        elif not result.success:
            logger.warning(f"Download failed: {result.error_message}")
        else:
            if "download completed" in result.logs.lower() or "status: download completed" in result.logs.lower():
                logger.info("Download completed successfully (verified from return code + SpotiFLAC output)")
            else:
                logger.info("SpotiFLAC execution completed (return code 0, missing explicit stdout marker)")
        
        # Create m3u playlist if this is a playlist download and it succeeded
        if result.success:
            parsed_url = parse_spotify_url(task.spotify_url)

            if parsed_url.url_type != SpotifyUrlType.PLAYLIST:
                self._normalize_album_artist_tags(task)

            if parsed_url.url_type == SpotifyUrlType.PLAYLIST:
                # The runner writes the .m3u8 itself, in playlist order
                if result.m3u:
                    logger.info(f"✓ Playlist file: {result.m3u}")
                else:
                    logger.warning("Playlist download finished without a playlist file (no files placed)")
        
        # Update download record with result
        try:
            with get_db_session() as db:
                download = db.query(Download).get(task.download_id)
                if download and result.error_message:
                    download.error_message = result.error_message
                    db.commit()
        except Exception as e:
            logger.error(f"Failed to update download error: {e}")
        
        return result

    def _normalize_album_artist_tags(self, task: DownloadTask) -> None:
        """Ensure all downloaded tracks share a consistent album artist tag."""
        album_artist = (task.artist_name or "").strip()
        if not album_artist:
            logger.debug(f"Skipping album artist normalization for {task.download_id}: no artist name")
            return

        # Only the files this download produced. (Walking a directory here
        # used to retag every file under the downloads root for track URLs.)
        audio_files = [f for f in task.downloaded_files if os.path.exists(f)]
        if not audio_files:
            logger.warning(f"No downloaded files to normalize album artist for {task.download_id}")
            return

        updated_count = 0
        failed_count = 0

        for file_path in sorted(audio_files):
            try:
                if self._set_album_artist_tag(file_path, album_artist):
                    updated_count += 1
            except Exception as e:
                failed_count += 1
                logger.warning(f"Failed to normalize album artist for {file_path}: {e}")

        logger.info(
            f"Album artist normalization complete for download {task.download_id}: "
            f"updated={updated_count}, failed={failed_count}, album_artist='{album_artist}'"
        )

    def _set_album_artist_tag(self, file_path: str, album_artist: str) -> bool:
        """Set ALBUMARTIST (and BAND for Vorbis-compatible tags) on one audio file."""
        changed = False

        audio_easy = MutagenFile(file_path, easy=True)
        if audio_easy is None:
            return False

        existing_album_artist = audio_easy.get("albumartist") or []
        desired_value = [str(album_artist)]

        if existing_album_artist != desired_value:
            audio_easy["albumartist"] = desired_value
            audio_easy.save()
            changed = True

        audio_full = MutagenFile(file_path)
        if audio_full is not None and getattr(audio_full, "tags", None) is not None:
            format_name = type(audio_full).__name__.lower()
            if "flac" in format_name or "ogg" in format_name or "opus" in format_name:
                existing_band = audio_full.tags.get("BAND") or audio_full.tags.get("band") or []
                if existing_band != desired_value:
                    audio_full.tags["BAND"] = desired_value
                    audio_full.save()
                    changed = True

        return changed
    

    def _execute_spotiflac_cli_with_capture(self, task: DownloadTask) -> DownloadResult:
        """Execute SpotiFLAC via CLI and capture output."""
        result = DownloadResult(success=False)
        from app.config import get_settings
        settings = get_settings()
        
        # Find the executable
        # Since SpotiFLAC 5.0.0 removed the headless CLI in favor of interactive GUI/TUI,
        # we must invoke the python module programmatically using the isolated interpreter.
        python_executable = "/opt/spotiflac-venv/bin/python"
        
        if not os.path.exists(python_executable):
            logger.error(f"Isolated python environment not found at {python_executable}")
            result.error_message = "SpotiFLAC Python environment not found."
            return result
            
        # Build command using the helper script to avoid injection risks
        runner_script = "/app/scripts/spotiflac_runner.py"
        if not os.path.exists(runner_script):
            # Fallback for local development
            runner_script = "scripts/spotiflac_runner.py"
            
        import json
        services_json = json.dumps(task.services)
        registries_json = json.dumps(settings.spotiflac_registry_urls)
        # The runner files each download into this layout itself
        layout_json = json.dumps({
            "folder": settings.spotiflac_folder_structure,
            "playlist_folder": settings.spotiflac_playlist_folder_structure,
            "track_format": settings.spotiflac_album_track_format,
            "single_track_format": settings.spotiflac_single_track_format,
        })

        cmd = [
            python_executable,
            runner_script,
            task.spotify_url,
            task.output_path,
            services_json,
            registries_json,
            layout_json,
        ]
        history_file = self._write_history_file(task) if settings.history_skip_redownload else None
        skip_file = self._write_skip_file(task)
        if history_file or skip_file:
            cmd.append(history_file or "")
        if skip_file:
            cmd.append(skip_file)
        
        
        
        # Do not pass --loop to spotiflac CLI, otherwise it will never exit and block our worker thread.        
        logger.info(f"Executing SpotiFLAC CLI: {' '.join(cmd)}")
        
        try:
            self._current_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # Line buffered
                universal_newlines=True,
                cwd=task.output_path,
                start_new_session=True,  # own process group: a hung run is killed with its children
            )
            
            # Capture output and parse in real-time
            stdout_lines = []
            
            
            # Stops the runner for a break / outage / low disk, also while
            # it prints nothing (e.g. planning a big playlist)
            run_done = threading.Event()
            last_output = [time.monotonic()]
            threading.Thread(target=self._watch_run, name=f"watch-{task.download_id}", daemon=True,
                             args=(task, self._current_process, result, run_done, last_output)).start()

            # Read stdout in real-time
            line_count = 0
            while True:
                line = self._current_process.stdout.readline()
                if not line:
                    break
                last_output[0] = time.monotonic()
                line_count += 1
                stdout_lines.append(line)
                # Forward the runner's output live, minus the noise
                if runner_line_wanted(line):
                    logger.info(f"[runner] {line.rstrip()[:600]}")
                # Parse the line for progress
                self._parse_spotiflac_output(line.strip(), task.download_id)
            
            
            # Output closed: the runner has exited (or was killed by _watch_run)
            self._current_process.wait()
            run_done.set()
            stdout = ''.join(stdout_lines)
            stderr = ""
            result.logs = stdout
            
            stdout = ''.join(stdout_lines)
            result.logs = f"{stdout}\n{stderr}"
            
            # The useful lines were forwarded live (runner_line_wanted)
            logger.info(f"Runner exited with code {self._current_process.returncode} "
                        f"after {line_count} output line(s)")
            
            runner_result = {}
            for out_line in stdout_lines:
                if out_line.startswith("___RESULT___="):
                    try:
                        runner_result = json.loads(out_line.split("=", 1)[1])
                    except json.JSONDecodeError:
                        logger.warning(f"Unparseable runner result line: {out_line.strip()}")
            result.verification_blocked = bool(
                runner_result.get("turnstile_failed") or runner_result.get("verification_paused")
            )
            result.files = [f for f in runner_result.get("files") or [] if isinstance(f, str)]
            result.m3u = runner_result.get("m3u")
            if runner_result.get("rate_limited_until"):
                result.rate_limited_until = float(runner_result["rate_limited_until"])
            stats = runner_result.get("service_stats")
            if isinstance(stats, dict) and any(stats.values()):
                # Pacing data: the download service's refusals in this run
                logger.info(f"Download {task.download_id}: download service refusals this run: "
                            + ", ".join(f"{k} ×{v}" for k, v in stats.items() if v))
            if isinstance(runner_result.get("missing"), list):
                result.missing_reported = True
                result.tracks_failed = [
                    {"name": str(m.get("name") or "Unknown track"), "error": str(m.get("error") or "")[:600],
                     "timestamp": datetime.utcnow().isoformat()}
                    for m in runner_result["missing"] if isinstance(m, dict)
                ]
            # Kept on the task (across retries) for the completeness check
            task.downloaded_files += [f for f in result.files if f not in task.downloaded_files]

            if result.hung and not result.stopped_for:
                result.success = False
                result.error_message = f"Runner hung (no output for {get_settings().runner_idle_minutes:g} min); killed"
            elif result.stopped_for:
                result.success = False
                result.error_message = {"break": "Stopped for a scheduled break",
                                        "down": "Stopped: download servers down",
                                        "cap": "Stopped: download cap reached"}.get(
                                            result.stopped_for, "Stopped: low disk space")
            elif self._current_process.returncode == 0:
                result.success = True
            else:
                result.error_message = f"SpotiFLAC exited with code {self._current_process.returncode}"
                if result.rate_limited_until:
                    # Exit 75: stopped on purpose; the job waits and re-runs
                    result.error_message = "Stopped early: download service rate limited until " + \
                        format_utc(result.rate_limited_until)
                elif result.verification_blocked:
                    result.error_message += " (Turnstile verification failed or paused)"
                
        except Exception as e:
            logger.error(f"Error executing SpotiFLAC CLI: {e}")
            result.error_message = str(e)
        
        return result
    
    def _get_audio_files(self, directory: str) -> set:
        """Get all audio files in a directory (recursively)."""
        audio_extensions = {'.flac', '.mp3', '.m4a', '.wav', '.ogg', '.opus', '.aac'}
        files = set()
        
        if not os.path.exists(directory):
            return files
        
        for root, _, filenames in os.walk(directory):
            for filename in filenames:
                ext = os.path.splitext(filename)[1].lower()
                if ext in audio_extensions:
                    files.add(os.path.join(root, filename))
        
        return files
    
    def _create_m3u_playlist(self, task: DownloadTask, download_dir: str) -> Optional[str]:
        """Create m3u playlist file for a playlist download.
        
        Args:
            task: Download task with playlist information
            download_dir: Directory where files were downloaded
            
        Returns:
            Path to created m3u file, or None if failed
        """
        try:
            logger.info(f"Starting m3u creation for playlist in {download_dir}")
            
            # Parse the URL to get playlist ID
            parsed_url = parse_spotify_url(task.spotify_url)
            if parsed_url.url_type != SpotifyUrlType.PLAYLIST:
                logger.warning(f"Not a playlist URL: {task.spotify_url}")
                return None
            
            logger.info(f"Fetching playlist metadata for ID: {parsed_url.spotify_id}")
            
            # Fetch playlist metadata from Spotify
            playlist_name = None
            tracks = []
            
            try:
                spotify = get_spotify_service()
                playlist_data = spotify.get_playlist(parsed_url.spotify_id)
                
                if playlist_data:
                    playlist_name = playlist_data.get("name", "playlist")
                    tracks = playlist_data.get("tracks", [])
                    logger.info(f"Playlist '{playlist_name}' has {len(tracks)} tracks from Spotify API")
                else:
                    logger.warning(f"Could not fetch playlist data from Spotify API for {parsed_url.spotify_id}")
            except Exception as spotify_err:
                logger.warning(f"Spotify API error (will use fallback): {spotify_err}")
            
            # Fallback: Use task info if Spotify API failed
            if not playlist_name:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    playlist_name = f"playlist_{parsed_url.spotify_id}_{timestamp}"
                    logger.info(f"Using unique fallback playlist name: {playlist_name}")
            
            # The download directory should already be the full path including artist/playlist
            # since we construct it in the task creation
            audio_files = self._get_audio_files(download_dir)
            
            logger.info(f"Found {len(audio_files)} audio files in {download_dir}")
            
            if not audio_files:
                logger.warning(f"No audio files found in {download_dir}")
                return None
            
            # Build m3u content
            m3u_lines = ["#EXTM3U"]
            matched_files = []
            
            # If we have track metadata from Spotify, try to match files to tracks
            if tracks:
                logger.info("Attempting to match files to Spotify track metadata")
                for track in tracks:
                    track_name = track.get("name", "")
                    artist_name = track.get("artist_name", "")
                    duration_ms = track.get("duration_ms", 0)
                    duration_sec = duration_ms // 1000 if duration_ms else -1
                    
                    matched_file = None
                    search_patterns = [
                        f"{artist_name} - {track_name}",
                        f"{track_name} - {artist_name}",
                        track_name,
                    ]
                    
                    for file_path in audio_files:
                        file_name = os.path.basename(file_path)
                        file_name_noext = os.path.splitext(file_name)[0]
                        
                        for pattern in search_patterns:
                            if pattern.lower() in file_name_noext.lower():
                                matched_file = file_path
                                break
                        
                        if matched_file:
                            break
                    
                    if matched_file:
                        rel_path = os.path.relpath(matched_file, download_dir)
                        m3u_lines.append(f"#EXTINF:{duration_sec},{artist_name} - {track_name}")
                        m3u_lines.append(rel_path)
                        matched_files.append(matched_file)
                    else:
                        logger.debug(f"Could not match file for: {artist_name} - {track_name}")
            
            # Fallback: If no tracks matched or no Spotify metadata, just add all audio files
            if not matched_files:
                logger.info("Using fallback: adding all audio files found in directory")
                for file_path in sorted(audio_files):
                    rel_path = os.path.relpath(file_path, download_dir)
                    file_name = os.path.basename(file_path)
                    file_name_noext = os.path.splitext(file_name)[0]
                    
                    # Try to extract duration from file if possible (defaults to -1)
                    m3u_lines.append(f"#EXTINF:-1,{file_name_noext}")
                    m3u_lines.append(rel_path)
                    matched_files.append(file_path)
            
            if not matched_files:
                logger.warning("No tracks could be added to m3u (this shouldn't happen)")
                return None
            
            m3u_filename = self._sanitize_filename(playlist_name) + ".m3u"
            m3u_path = os.path.join(download_dir, m3u_filename)
            
            with open(m3u_path, "w", encoding="utf-8") as f:
                f.write("\n".join(m3u_lines))
            
            logger.info(f"Created m3u playlist: {m3u_path} with {len(matched_files)} tracks")
            return m3u_path
            
        except Exception as e:
            logger.error(f"Error creating m3u playlist: {e}", exc_info=True)
            return None


_download_manager: Optional[DownloadManager] = None


def get_download_manager() -> DownloadManager:
    """Get the global download manager instance."""
    global _download_manager
    if _download_manager is None:
        _download_manager = DownloadManager()
    return _download_manager
