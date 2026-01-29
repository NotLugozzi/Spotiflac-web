"""
Spotiflac web - Download Manager Service
"""

import os
import re
import glob
import logging
import threading
import queue
from typing import Optional, List, Dict, Any, Callable, Tuple
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
import subprocess
import shutil
import sys
from io import StringIO
from contextlib import redirect_stdout, redirect_stderr

from sqlalchemy.orm import Session

from app.models import Download, Album, DownloadStatus
from app.database import get_db_session
from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class DownloadTask:
    """Represents a download task to be processed."""
    download_id: int
    album_id: Optional[int]  # Can be None for manual URL downloads
    spotify_url: str
    output_path: str
    services: List[str]
    filename_format: str
    use_artist_subfolders: bool
    use_album_subfolders: bool
    retry_minutes: int
    # Optional metadata for manual downloads
    title: Optional[str] = None
    artist_name: Optional[str] = None
    album_name: Optional[str] = None
    total_tracks: Optional[int] = None


@dataclass
class DownloadResult:
    """Result of a SpotiFLAC download operation."""
    success: bool
    tracks_expected: List[str] = field(default_factory=list)
    tracks_found: List[str] = field(default_factory=list)
    error_message: Optional[str] = None
    logs: str = ""


class DownloadManager:
    """Service for managing SpotiFLAC downloads."""
    
    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._running = False
        self._current_download: Optional[DownloadTask] = None
        self._current_process: Optional[subprocess.Popen] = None
        self._callbacks: List[Callable] = []
        self._spotiflac_available = self._check_spotiflac()
        self._progress_monitor_thread: Optional[threading.Thread] = None
        self._monitor_running = False
    
    def _check_spotiflac(self) -> bool:
        """Check if SpotiFLAC is available."""
        # First try importing as Python module
        try:
            from SpotiFLAC import SpotiFLAC
            logger.info("SpotiFLAC Python module is available")
            return True
        except ImportError:
            pass
        
        # Try finding the CLI executable
        if shutil.which("spotiflac") or shutil.which("SpotiFLAC"):
            logger.info("SpotiFLAC CLI is available")
            return True
        
        # Check for local launcher.py
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
    
    def start_download_immediately(
        self, 
        db: Session, 
        spotify_url: str,
        url_type: str,
        title: Optional[str] = None,
        artist_name: Optional[str] = None
    ) -> Download:
        """
        Start a download immediately in a background thread without using the queue.
        This allows parallel downloads since SpotiFLAC runs as separate processes.
        
        Args:
            db: Database session
            spotify_url: Spotify URL or URI
            url_type: Type of URL (album, track, artist, playlist)
            title: Optional title for display
            artist_name: Optional artist name for display
            
        Returns:
            Download record
        """
        settings = get_settings()
        
        # Create download record
        download = Download(
            album_id=None,
            spotify_url=spotify_url,
            url_type=url_type,
            title=title or f"Manual {url_type.title()} Download",
            artist_name=artist_name,
            status=DownloadStatus.DOWNLOADING,
            service=settings.spotiflac_service,
            output_path=settings.download_path,
            started_at=datetime.utcnow(),
        )
        db.add(download)
        db.flush()
        
        # Create task
        task = DownloadTask(
            download_id=download.id,
            album_id=None,
            spotify_url=spotify_url,
            output_path=settings.download_path,
            services=settings.spotiflac_services,
            filename_format=settings.spotiflac_filename_format,
            use_artist_subfolders=settings.spotiflac_use_artist_subfolders,
            use_album_subfolders=settings.spotiflac_use_album_subfolders,
            retry_minutes=settings.spotiflac_retry_minutes,
            title=title,
            artist_name=artist_name,
            album_name=title,
            total_tracks=None,
        )
        
        # Start download in a background thread immediately
        download_thread = threading.Thread(
            target=self._process_download,
            args=(task,),
            daemon=True
        )
        download_thread.start()
        
        logger.info(f"Started immediate download in background: {spotify_url}")
        
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
        """Start the download worker."""
        if self._running:
            logger.warning("Download manager is already running")
            return
        
        self._running = True
        self._monitor_running = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        self._progress_monitor_thread = threading.Thread(target=self._progress_monitor_loop, daemon=True)
        self._progress_monitor_thread.start()
        logger.info("Download manager started")
    
    def stop(self):
        """Stop the download worker."""
        self._running = False
        self._monitor_running = False
        
        # Cancel current download if any
        if self._current_process:
            self._current_process.terminate()
        
        # Wait for worker to finish
        if self._worker_thread and self._worker_thread.is_alive():
            self._queue.put(None)  # Sentinel to wake up the worker
            self._worker_thread.join(timeout=5)
        
        # Wait for progress monitor to finish
        if self._progress_monitor_thread and self._progress_monitor_thread.is_alive():
            self._progress_monitor_thread.join(timeout=2)
        
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
        
        settings = get_settings()
        
        # Check if already in queue or downloading
        existing = db.query(Download).filter(
            Download.album_id == album.id,
            Download.status.in_([DownloadStatus.PENDING, DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING])
        ).first()
        
        if existing:
            logger.warning(f"Album {album.name} is already in the download queue")
            return existing
        
        # Create download record
        download = Download(
            album_id=album.id,
            status=DownloadStatus.PENDING,
            service=settings.spotiflac_service,
            output_path=settings.download_path,
        )
        db.add(download)
        db.flush()
        
        # Create task
        task = DownloadTask(
            download_id=download.id,
            album_id=album.id,
            spotify_url=album.spotify_url,
            output_path=settings.download_path,
            services=settings.spotiflac_services,
            filename_format=settings.spotiflac_filename_format,
            use_artist_subfolders=settings.spotiflac_use_artist_subfolders,
            use_album_subfolders=settings.spotiflac_use_album_subfolders,
            retry_minutes=settings.spotiflac_retry_minutes,
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
        
        Args:
            db: Database session
            spotify_url: Spotify URL or URI
            url_type: Type of URL (album, track, artist, playlist)
            title: Optional title for display
            artist_name: Optional artist name for display
            
        Returns:
            Download record
        """
        settings = get_settings()
        
        # Check if same URL already in queue
        existing = db.query(Download).filter(
            Download.spotify_url == spotify_url,
            Download.status.in_([DownloadStatus.PENDING, DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING])
        ).first()
        
        if existing:
            logger.warning(f"URL {spotify_url} is already in the download queue")
            return existing
        
        # Create download record without album association
        download = Download(
            album_id=None,  # No album record
            spotify_url=spotify_url,
            url_type=url_type,
            title=title or f"Manual {url_type.title()} Download",
            artist_name=artist_name,
            status=DownloadStatus.PENDING,
            service=settings.spotiflac_service,
            output_path=settings.download_path,
        )
        db.add(download)
        db.flush()
        
        # Create task
        task = DownloadTask(
            download_id=download.id,
            album_id=None,
            spotify_url=spotify_url,
            output_path=settings.download_path,
            services=settings.spotiflac_services,
            filename_format=settings.spotiflac_filename_format,
            use_artist_subfolders=settings.spotiflac_use_artist_subfolders,
            use_album_subfolders=settings.spotiflac_use_album_subfolders,
            retry_minutes=settings.spotiflac_retry_minutes,
            title=title,
            artist_name=artist_name,
            album_name=title,
            total_tracks=None,
        )
        
        # Add to queue
        self._queue.put(task)
        download.status = DownloadStatus.QUEUED
        
        logger.info(f"Added URL to download queue: {spotify_url}")
        
        return download
    
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
            
            album = download.album
            if not album:
                return False
            
            settings = get_settings()
            
            # Reset download status
            download.status = DownloadStatus.QUEUED
            download.error_message = None
            download.progress = 0
            download.started_at = None
            download.completed_at = None
            
            # Create new task
            task = DownloadTask(
                download_id=download.id,
                album_id=album.id,
                spotify_url=album.spotify_url,
                output_path=settings.download_path,
                services=settings.spotiflac_services,
                filename_format=settings.spotiflac_filename_format,
                use_artist_subfolders=settings.spotiflac_use_artist_subfolders,
                use_album_subfolders=settings.spotiflac_use_album_subfolders,
                retry_minutes=settings.spotiflac_retry_minutes,
                album_name=album.name,
                total_tracks=album.total_tracks,
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
    
    def _process_download(self, task: DownloadTask):
        """Process a single download task."""
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
                db.commit()
                
                self._notify_progress(task.download_id, "downloading", 0)
        except Exception as e:
            logger.error(f"Error updating download status to DOWNLOADING: {e}")
        
        try:
            # Execute SpotiFLAC
            success = self._execute_spotiflac(task)
            
            with get_db_session() as db:
                download = db.query(Download).get(task.download_id)
                
                if not download:
                    logger.error(f"Download record disappeared: {task.download_id}")
                    return
                
                if success:
                    download.status = DownloadStatus.COMPLETED
                    download.progress = 100
                    download.error_message = None
                    
                    # Mark album as owned if linked
                    if download.album_id:
                        album = db.query(Album).get(download.album_id)
                        if album:
                            album.is_owned = True
                    
                    logger.info(f"Download completed: {task.download_id}")
                    self._notify_progress(task.download_id, "completed", 100)
                else:
                    download.status = DownloadStatus.FAILED
                    if not download.error_message:
                        download.error_message = "Download failed - check logs for details"
                    
                    logger.error(f"Download failed: {task.download_id}")
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
    
    def _progress_monitor_loop(self):
        """Monitor download progress by checking filesystem."""
        logger.info("Progress monitor loop started")
        while self._monitor_running:
            try:
                if self._current_download:
                    self._update_download_progress(self._current_download)
                
                # Check every 2 seconds
                threading.Event().wait(2)
                
            except Exception as e:
                logger.error(f"Error in progress monitor loop: {e}", exc_info=True)
                threading.Event().wait(2)
        
        logger.info("Progress monitor loop stopped")
    
    def _update_download_progress(self, task: DownloadTask):
        """Update progress for current download based on filesystem."""
        try:
            # Determine the expected download directory
            download_dir = self._get_expected_download_dir(task)
            
            if not download_dir or not os.path.exists(download_dir):
                return
            
            # Count audio files in the directory
            audio_files = self._get_audio_files(download_dir)
            tracks_found = len(audio_files)
            
            # Calculate progress
            if task.total_tracks and task.total_tracks > 0:
                progress = min(int((tracks_found / task.total_tracks) * 100), 99)
            else:
                # No track count available, just show that something is happening
                progress = min(tracks_found * 5, 95) if tracks_found > 0 else 0
            
            # Update database
            with get_db_session() as db:
                download = db.query(Download).get(task.download_id)
                if download and download.status == DownloadStatus.DOWNLOADING:
                    if download.progress != progress:
                        download.progress = progress
                        db.commit()
                        logger.debug(f"Download {task.download_id}: {tracks_found} tracks found, progress: {progress}%")
                        self._notify_progress(task.download_id, "downloading", progress)
        
        except Exception as e:
            logger.error(f"Error updating download progress: {e}")
    
    def _get_expected_download_dir(self, task: DownloadTask) -> Optional[str]:
        """Get the expected download directory for a task."""
        base_path = task.output_path
        
        if not base_path or not os.path.exists(base_path):
            return None
        
        # Build path based on settings
        path_parts = [base_path]
        
        if task.use_artist_subfolders and task.artist_name:
            # Clean artist name for filesystem
            artist_clean = self._sanitize_filename(task.artist_name)
            path_parts.append(artist_clean)
        
        if task.use_album_subfolders and task.album_name:
            # Clean album name for filesystem
            album_clean = self._sanitize_filename(task.album_name)
            path_parts.append(album_clean)
        
        expected_path = os.path.join(*path_parts)
        
        # Check if path exists
        if os.path.exists(expected_path):
            return expected_path
        
        # Try to find a directory that matches (SpotiFLAC might clean names differently)
        if task.album_name:
            album_lower = task.album_name.lower()
            
            # Check in base path
            search_base = os.path.join(*path_parts[:-1]) if len(path_parts) > 1 else base_path
            
            if os.path.exists(search_base):
                for item in os.listdir(search_base):
                    item_path = os.path.join(search_base, item)
                    if os.path.isdir(item_path) and album_lower in item.lower():
                        return item_path
        
        return expected_path
    
    def _sanitize_filename(self, name: str) -> str:
        """Sanitize a string for use in filenames."""
        # Remove or replace invalid characters
        invalid_chars = '<>:"\\/|?*'
        for char in invalid_chars:
            name = name.replace(char, '_')
        
        # Remove leading/trailing spaces and dots
        name = name.strip('. ')
        
        return name
    
    def _execute_spotiflac(self, task: DownloadTask) -> bool:
        """
        Execute SpotiFLAC to download an album.
        
        Args:
            task: Download task to execute
            
        Returns:
            True if successful
        """
        # Get files before download to compare later
        files_before = self._get_audio_files(task.output_path)
        logger.info(f"Files in output directory before download: {len(files_before)}")
        
        # Try Python module first
        spotiflac_available = False
        try:
            from SpotiFLAC import SpotiFLAC
            spotiflac_available = True
        except ImportError:
            logger.warning("SpotiFLAC module not available, will try CLI")
        
        result = DownloadResult(success=False)
        
        if spotiflac_available:
            result = self._execute_spotiflac_module(task)
        else:
            result = self._execute_spotiflac_cli_with_capture(task)
        
        # Parse logs to find expected tracks
        expected_tracks = self._parse_expected_tracks(result.logs)
        result.tracks_expected = expected_tracks
        logger.info(f"Expected tracks from logs: {expected_tracks}")
        
        # Get files after download
        files_after = self._get_audio_files(task.output_path)
        new_files = files_after - files_before
        result.tracks_found = list(new_files)
        logger.info(f"New files found after download: {len(new_files)}")
        
        # Verify download success
        if new_files:
            logger.info(f"Download verified: {len(new_files)} new audio files found")
            for f in new_files:
                logger.info(f"  - {os.path.basename(f)}")
            result.success = True
        elif expected_tracks:
            # Check if any expected tracks match existing files
            matched = self._match_tracks_to_files(expected_tracks, files_after)
            if matched:
                logger.info(f"Download verified: {len(matched)} tracks matched to existing files")
                result.success = True
                result.tracks_found = matched
            else:
                logger.warning("No new files and no matches found - download may have failed")
                result.success = False
                result.error_message = "No audio files downloaded"
        else:
            # No expected tracks parsed - check if any files exist
            if files_after:
                logger.info(f"Cannot verify specific tracks, but {len(files_after)} audio files exist in output")
                result.success = True
            else:
                logger.warning("No audio files found in output directory")
                result.success = False
                result.error_message = "No audio files in output directory"
        
        # Update download record with result
        try:
            with get_db_session() as db:
                download = db.query(Download).get(task.download_id)
                if download and result.error_message:
                    download.error_message = result.error_message
                    db.commit()
        except Exception as e:
            logger.error(f"Failed to update download error: {e}")
        
        return result.success
    
    def _execute_spotiflac_module(self, task: DownloadTask) -> DownloadResult:
        """Execute SpotiFLAC as Python module and capture output."""
        result = DownloadResult(success=False)
        
        try:
            from SpotiFLAC import SpotiFLAC
            
            logger.info(f"Starting SpotiFLAC download: {task.spotify_url}")
            logger.info(f"Output directory: {task.output_path}")
            logger.info(f"Services: {task.services}")
            
            # Ensure output directory exists
            os.makedirs(task.output_path, exist_ok=True)
            
            # Capture stdout/stderr
            stdout_capture = StringIO()
            stderr_capture = StringIO()
            
            # Also set up logging capture
            log_capture = StringIO()
            log_handler = logging.StreamHandler(log_capture)
            log_handler.setLevel(logging.DEBUG)
            
            # Get SpotiFLAC's logger if it exists
            spotiflac_logger = logging.getLogger('SpotiFLAC')
            original_handlers = spotiflac_logger.handlers.copy()
            spotiflac_logger.addHandler(log_handler)
            
            try:
                with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                    SpotiFLAC(
                        url=task.spotify_url,
                        output_dir=task.output_path,
                        services=task.services if task.services else ["tidal", "deezer", "qobuz"],
                        filename_format=task.filename_format or "{title} - {artist}",
                        use_track_numbers=True,
                        use_artist_subfolders=task.use_artist_subfolders,
                        use_album_subfolders=task.use_album_subfolders,
                        loop=task.retry_minutes if task.retry_minutes > 0 else None,
                    )
            finally:
                # Restore original handlers
                spotiflac_logger.handlers = original_handlers
            
            # Combine all captured output
            stdout_text = stdout_capture.getvalue()
            stderr_text = stderr_capture.getvalue()
            log_text = log_capture.getvalue()
            
            result.logs = f"{stdout_text}\n{stderr_text}\n{log_text}"
            
            logger.info(f"SpotiFLAC stdout: {stdout_text[:500] if stdout_text else '(empty)'}")
            logger.info(f"SpotiFLAC stderr: {stderr_text[:500] if stderr_text else '(empty)'}")
            
            # SpotiFLAC completed without exception
            result.success = True
            
        except Exception as e:
            logger.error(f"SpotiFLAC module error: {e}", exc_info=True)
            result.error_message = f"SpotiFLAC error: {str(e)}"
            result.success = False
        
        return result
    
    def _execute_spotiflac_cli_with_capture(self, task: DownloadTask) -> DownloadResult:
        """Execute SpotiFLAC via CLI and capture output."""
        result = DownloadResult(success=False)
        
        # Find the executable
        executable = shutil.which("spotiflac") or shutil.which("SpotiFLAC")
        
        if not executable:
            if os.path.exists("launcher.py"):
                executable = "python3"
                args = ["launcher.py"]
            else:
                logger.error("SpotiFLAC executable not found")
                result.error_message = "SpotiFLAC not found"
                return result
        else:
            args = []
        
        # Build command
        cmd = [executable] + args + [
            task.spotify_url,
            task.output_path,
            "--service", *task.services,
            "--filename-format", task.filename_format,
            "--use-track-numbers",
        ]
        
        if task.use_artist_subfolders:
            cmd.append("--use-artist-subfolders")
        
        if task.use_album_subfolders:
            cmd.append("--use-album-subfolders")
        
        if task.retry_minutes > 0:
            cmd.extend(["--loop", str(task.retry_minutes)])
        
        logger.info(f"Executing SpotiFLAC CLI: {' '.join(cmd)}")
        
        try:
            self._current_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            
            stdout, stderr = self._current_process.communicate()
            result.logs = f"{stdout}\n{stderr}"
            
            logger.info(f"SpotiFLAC CLI stdout: {stdout[:500] if stdout else '(empty)'}")
            logger.info(f"SpotiFLAC CLI stderr: {stderr[:500] if stderr else '(empty)'}")
            
            if self._current_process.returncode == 0:
                result.success = True
            else:
                result.error_message = f"SpotiFLAC exited with code {self._current_process.returncode}"
                
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
    
    def _parse_expected_tracks(self, logs: str) -> List[str]:
        """Parse SpotiFLAC logs to extract expected track names."""
        tracks = []
        
        # Pattern: "[1/10] Starting download: Track Name - Artist Name"
        pattern = r'\[\d+/\d+\]\s*Starting download:\s*(.+?)(?:\n|$)'
        matches = re.findall(pattern, logs, re.MULTILINE)
        tracks.extend(matches)
        
        # Pattern: "Found track: Artist - Title"
        pattern2 = r'Found track:\s*(.+?)(?:\n|$)'
        matches2 = re.findall(pattern2, logs, re.MULTILINE)
        tracks.extend(matches2)
        
        # Pattern: "Downloading: Title - Artist"
        pattern3 = r'Downloading:\s*(.+?)(?:\n|$)'
        matches3 = re.findall(pattern3, logs, re.MULTILINE)
        tracks.extend(matches3)
        
        # Clean up track names
        cleaned = []
        for track in tracks:
            track = track.strip()
            if track and len(track) > 2:
                cleaned.append(track)
        
        return list(set(cleaned))  # Remove duplicates
    
    def _match_tracks_to_files(self, tracks: List[str], files: set) -> List[str]:
        """Check if any expected tracks match files in the directory."""
        matched = []
        
        for track in tracks:
            # Normalize track name for comparison
            track_lower = track.lower()
            track_words = set(re.findall(r'\w+', track_lower))
            
            for filepath in files:
                filename = os.path.basename(filepath).lower()
                filename_no_ext = os.path.splitext(filename)[0]
                filename_words = set(re.findall(r'\w+', filename_no_ext))
                
                # Check if most words from track name appear in filename
                if track_words and len(track_words & filename_words) >= len(track_words) * 0.5:
                    matched.append(filepath)
                    break
        
        return matched


# Global download manager instance
_download_manager: Optional[DownloadManager] = None


def get_download_manager() -> DownloadManager:
    """Get the global download manager instance."""
    global _download_manager
    if _download_manager is None:
        _download_manager = DownloadManager()
    return _download_manager
