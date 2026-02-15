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
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
import subprocess
import shutil
import sys
from io import StringIO
from contextlib import redirect_stdout, redirect_stderr
from mutagen import File as MutagenFile

from sqlalchemy.orm import Session

from app.models import Download, Album, DownloadStatus
from app.database import get_db_session
from app.config import get_settings
from app.services.url_parser import parse_spotify_url, SpotifyUrlType
from app.services.spotify_service import get_spotify_service
from app.services.spotify_service import get_spotify_service

logger = logging.getLogger(__name__)


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


@dataclass
class DownloadResult:
    """Result of a SpotiFLAC download operation."""
    success: bool
    tracks_expected: List[str] = field(default_factory=list)
    tracks_found: List[str] = field(default_factory=list)
    tracks_failed: List[Dict[str, str]] = field(default_factory=list)  # {name, error}
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
            service=settings.spotiflac_service,
            output_path=settings.download_path,
        )
        db.add(download)
        db.flush()
        
        # Create task with simple path construction
        download_dir = settings.download_path
        if artist_name and title:
            artist_clean = self._sanitize_filename(artist_name)
            download_dir = os.path.join(settings.download_path, artist_clean)
        
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
        """Start the download worker."""
        if self._running:
            logger.warning("Download manager is already running")
            return
        
        self._running = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        logger.info("Download manager started")
    
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
        
        # Construct download path - only artist folder, SpotiFLAC creates album folder
        artist_clean = self._sanitize_filename(album.artist.name if album.artist else "Unknown Artist")
        download_dir = os.path.join(settings.download_path, artist_clean)
        
        # Create download record
        download = Download(
            album_id=album.id,
            status=DownloadStatus.PENDING,
            service=settings.spotiflac_service,
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
                    
            except Exception as e:
                logger.warning(f"Could not fetch Spotify metadata: {e}")
        
        # Fallback titles if still not available
        if not title:
            title = f"Manual {url_type.title()} Download"
        if not artist_name:
            artist_name = "Unknown Artist"
        if not total_tracks and url_type == 'track':
            total_tracks = 1
        
        # Construct download path - only artist folder, SpotiFLAC creates album folder
        artist_clean = self._sanitize_filename(artist_name)
        download_dir = os.path.join(settings.download_path, artist_clean)
        
        # Create download record without album association
        download = Download(
            album_id=None,  # No album record
            spotify_url=spotify_url,
            url_type=url_type,
            title=title,
            artist_name=artist_name,
            total_tracks=total_tracks,
            status=DownloadStatus.PENDING,
            service=settings.spotiflac_service,
            output_path=download_dir,
        )
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
            
            # Construct download path - only artist folder, SpotiFLAC creates album folder
            artist_clean = self._sanitize_filename(album.artist.name if album.artist else "Unknown Artist")
            download_dir = os.path.join(settings.download_path, artist_clean)
            download.output_path = download_dir
            
            # Create new task
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
                db.commit()
                
                self._notify_progress(task.download_id, "downloading", 0)
        except Exception as e:
            logger.error(f"Error updating download status to DOWNLOADING: {e}")
        
        # Retry loop with exponential backoff
        max_retries = 3
        retry_count = 0
        success = False
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
                
                # Execute SpotiFLAC
                result = self._execute_spotiflac(task)
                
                if result.success:
                    success = True
                    last_error = None
                    break
                else:
                    last_error = result.error_message or "Download failed"
                    retry_count += 1
                    logger.warning(f"Download attempt {retry_count} failed: {last_error}")
                    
            except Exception as e:
                last_error = str(e)
                retry_count += 1
                logger.error(f"Download attempt {retry_count} raised exception: {e}", exc_info=True)
        
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
                        logger.warning(f"Download completed but album is incomplete: {incomplete_info}")
                        download.error_message = f"Incomplete: {len(incomplete_info.get('missing_tracks', []))} track(s) failed"
                    
                    # Mark album as owned (or potentially incomplete)
                    if download.album_id:
                        album = db.query(Album).get(download.album_id)
                        if album:
                            album.is_owned = True
                            if incomplete_info and incomplete_info.get('is_incomplete'):
                                album.is_incomplete = True
                                album.missing_tracks = incomplete_info.get('missing_tracks', [])
                    
                    logger.info(f"Download completed: {task.download_id}")
                    self._notify_progress(task.download_id, "completed", 100)
                    
                    # Trigger library scan after successful download
                    try:
                        from app.services.library_scanner import get_library_scanner
                        scanner = get_library_scanner()
                        logger.info("Triggering library scan after download completion")
                        scanner.scan_library()
                    except Exception as scan_err:
                        logger.error(f"Failed to trigger library scan: {scan_err}")
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
            
            # Count downloaded files in the directory
            download_dir = self._get_expected_download_dir(task)
            if not download_dir or not os.path.exists(download_dir):
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
            
            # Count audio files
            audio_files = self._get_audio_files(download_dir)
            actual_track_count = len(audio_files)
            
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
        """Get the expected download directory for a task.
        
        Since we pass {base}/{artist} to SpotiFLAC and it creates the album folder,
        we need to look for the album folder inside the artist folder.
        """
        artist_path = task.output_path
        
        if not artist_path or not os.path.exists(artist_path):
            return None
        
        # If no album name, just return the artist path
        if not task.album_name:
            return artist_path
        
        # Look for the album folder that SpotiFLAC created
        album_clean = self._sanitize_filename(task.album_name)
        expected_album_path = os.path.join(artist_path, album_clean)
        
        # Check if exact match exists
        if os.path.exists(expected_album_path):
            return expected_album_path
        
        # SpotiFLAC might clean the name differently, search for similar folders
        album_lower = task.album_name.lower()
        try:
            for item in os.listdir(artist_path):
                item_path = os.path.join(artist_path, item)
                if os.path.isdir(item_path) and album_lower in item.lower():
                    return item_path
        except OSError:
            pass
        
        return expected_album_path
    
    def _sanitize_filename(self, name: str) -> str:
        """Sanitize a string for use in filenames."""
        invalid_chars = '<>:"\\/|?*'
        for char in invalid_chars:
            name = name.replace(char, '_')
        
        name = name.strip('. ')
        
        return name
    
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
        
        # Detect track failures - look for error patterns
        # Common patterns: "Failed to download", "Error:", "Could not find", "Track not available"
        error_patterns = [
            r'(?:Failed|Error|Unable) to (?:download|find).*?[:\-]\s*(.+)',
            r'Track not (?:available|found)[:\-]\s*(.+)',
            r'Could not (?:download|find)[:\-]\s*(.+)',
            r'\[ERROR\]\s*(.+)',
            r'Exception.*?[:\-]\s*(.+)',
        ]
        
        for pattern in error_patterns:
            error_match = re.search(pattern, line, re.IGNORECASE)
            if error_match:
                error_detail = error_match.group(1).strip() if error_match.lastindex else line.strip()
                logger.warning(f"✗ DETECTED track failure: {error_detail[:100]}")
                
                # Try to extract track name from the line if possible
                track_name = "Unknown track"
                track_name_match = re.search(r'[:\-]\s*([^:\-]+?)(?:\s*-\s*[^:\-]+)?$', error_detail)
                if track_name_match:
                    track_name = track_name_match.group(1).strip()
                
                # Store failed track in download record
                try:
                    with get_db_session() as db:
                        download = db.query(Download).get(download_id)
                        if download:
                            failed_tracks = download.failed_tracks or []
                            if isinstance(failed_tracks, str):
                                failed_tracks = json.loads(failed_tracks) if failed_tracks else []
                            
                            # Add to failed tracks if not already there
                            failed_entry = {
                                "name": track_name,
                                "error": error_detail[:200],  # Limit error message length
                                "timestamp": datetime.utcnow().isoformat()
                            }
                            
                            # Avoid duplicates
                            if not any(f.get('name') == track_name for f in failed_tracks):
                                failed_tracks.append(failed_entry)
                                download.failed_tracks = failed_tracks
                                db.commit()
                                logger.info(f"Recorded failed track: {track_name}")
                except Exception as e:
                    logger.error(f"Unable to record failed track: {e}", exc_info=True)
                break
        
        # Log other relevant lines
        if 'completed' in line.lower() or 'finished' in line.lower() or 'done' in line.lower():
            logger.info(f"[PARSER] Possible completion line: {line[:100]}")
        elif 'downloading' in line.lower() or 'searching' in line.lower():
            logger.debug(f"[PARSER] Activity line (no match): {line[:100]}")
    
    def _execute_spotiflac(self, task: DownloadTask) -> DownloadResult:
        """
        Execute SpotiFLAC to download an album.
        
        Args:
            task: Download task to execute
            
        Returns:
            DownloadResult with success status and error information
        """
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
        
        if "download completed" in result.logs.lower() or "status: download completed" in result.logs.lower():
            logger.info("Download completed successfully (verified from SpotiFLAC output)")
            result.success = True
        elif result.error_message:
            logger.warning(f"Download failed: {result.error_message}")
            result.success = False
        else:
            # Default to success if SpotiFLAC ran without errors
            logger.info("SpotiFLAC execution completed")
            result.success = True
        
        # Create m3u playlist if this is a playlist download and it succeeded
        if result.success:
            parsed_url = parse_spotify_url(task.spotify_url)

            if parsed_url.url_type != SpotifyUrlType.PLAYLIST:
                self._normalize_album_artist_tags(task)

            if parsed_url.url_type == SpotifyUrlType.PLAYLIST:
                logger.info("Detected playlist download, attempting to create m3u file")
                # For playlists, use the output_path directly (no album subfolders)
                download_dir = task.output_path
                
                if download_dir and os.path.exists(download_dir):
                    logger.info(f"Creating m3u playlist in: {download_dir}")
                    m3u_path = self._create_m3u_playlist(task, download_dir)
                    if m3u_path:
                        logger.info(f"✓ Created playlist file: {m3u_path}")
                    else:
                        logger.warning("Failed to create m3u playlist file")
                else:
                    logger.error(f"Download directory does not exist: {download_dir}")
        
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

        target_dir = self._get_expected_download_dir(task)
        if not target_dir or not os.path.exists(target_dir):
            target_dir = task.output_path

        if not target_dir or not os.path.exists(target_dir):
            logger.warning(
                f"Skipping album artist normalization for {task.download_id}: directory not found ({target_dir})"
            )
            return

        audio_files = self._get_audio_files(target_dir)
        if not audio_files:
            logger.warning(f"No audio files found for album artist normalization in {target_dir}")
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
            
            # Capture stdout/stderr by replacing sys.stdout directly
            # Use TeeOutput to capture AND log in real-time
            stdout_capture = TeeOutput(task.download_id, self._parse_spotiflac_output)
            stderr_capture = TeeOutput(task.download_id, self._parse_spotiflac_output)
            
            # Save original stdout/stderr
            original_stdout = sys.stdout
            original_stderr = sys.stderr
            
            logger.info(f"   URL: {task.spotify_url}")
            logger.info(f"   Output: {task.output_path}")
            logger.info(f"   Services: {task.services}")
            
            try:
                # Replace sys.stdout and sys.stderr directly
                sys.stdout = stdout_capture
                sys.stderr = stderr_capture
                
                SpotiFLAC(
                    url=task.spotify_url,
                    output_dir=task.output_path,
                    services=task.services if task.services else ["qobuz", "tidal", "deezer"],
                    loop=None,
                )
            finally:
                sys.stdout = original_stdout
                sys.stderr = original_stderr
                        
            stdout_text = stdout_capture.getvalue()
            stderr_text = stderr_capture.getvalue()
            
            result.logs = f"{stdout_text}\n{stderr_text}"            
            lines_parsed = 0
            lines_with_content = 0
            all_lines = result.logs.split('\n')
            
            for i, line in enumerate(all_lines, 1):
                if line.strip():
                    lines_with_content += 1
                    lines_parsed += 1
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
        ]
        
        if task.retry_minutes > 0:
            cmd.extend(["--loop", str(task.retry_minutes)])
        
        logger.info(f"Executing SpotiFLAC CLI: {' '.join(cmd)}")
        
        try:
            self._current_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
                universal_newlines=True,
            )
            
            # Capture output and parse in real-time
            stdout_lines = []
            stderr_lines = []
            
            
            # Read stdout in real-time
            line_count = 0
            while True:
                line = self._current_process.stdout.readline()
                if not line:
                    break
                line_count += 1
                stdout_lines.append(line)
                logger.debug(f"[CLI OUTPUT #{line_count}] {line.strip()[:100]}")
                # Parse the line for progress
                self._parse_spotiflac_output(line.strip(), task.download_id)
            
            
            # Wait for process to complete and get stderr
            self._current_process.wait()
            stderr = self._current_process.stderr.read()
            stderr_lines = stderr.split('\n') if stderr else []
            
            # Parse stderr as well
            for line in stderr_lines:
                self._parse_spotiflac_output(line.strip(), task.download_id)
            
            stdout = ''.join(stdout_lines)
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
