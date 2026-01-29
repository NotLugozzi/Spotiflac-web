"""
Spotiflac web - Library Scanner Service
"""

import os
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileDeletedEvent
import threading
import time

from sqlalchemy.orm import Session

from app.models import Artist, Album, Track, LibraryScan, ScanStatus
from app.database import get_db_session
from app.services.metadata_service import get_metadata_service, SUPPORTED_EXTENSIONS
from app.services.spotify_service import get_spotify_service
from app.config import get_settings

logger = logging.getLogger(__name__)


class LibraryEventHandler(FileSystemEventHandler):
    """Handler for file system events in the music library."""
    
    def __init__(self, scanner: "LibraryScanner"):
        self.scanner = scanner
        self._debounce_timer: Optional[threading.Timer] = None
        self._pending_paths: set = set()
        self._lock = threading.Lock()
    
    def on_created(self, event: FileCreatedEvent):
        """Handle file creation events."""
        if event.is_directory:
            return
        
        if self._is_audio_file(event.src_path):
            self._debounce_scan(event.src_path)
    
    def on_deleted(self, event: FileDeletedEvent):
        """Handle file deletion events."""
        if event.is_directory:
            return
        
        if self._is_audio_file(event.src_path):
            logger.info(f"File deleted: {event.src_path}")
            self.scanner.handle_file_deletion(event.src_path)
    
    def _is_audio_file(self, path: str) -> bool:
        """Check if the path is an audio file."""
        return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS
    
    def _debounce_scan(self, path: str, delay: float = 2.0):
        """Debounce scan requests to avoid scanning during bulk operations."""
        with self._lock:
            self._pending_paths.add(path)
            
            if self._debounce_timer:
                self._debounce_timer.cancel()
            
            self._debounce_timer = threading.Timer(delay, self._execute_scan)
            self._debounce_timer.start()
    
    def _execute_scan(self):
        """Execute the debounced scan."""
        with self._lock:
            paths = list(self._pending_paths)
            self._pending_paths.clear()
        
        if paths:
            logger.info(f"Processing {len(paths)} new files")
            self.scanner.process_new_files(paths)


class LibraryScanner:
    """Service for scanning and monitoring the music library."""
    
    def __init__(self):
        self.metadata_service = get_metadata_service()
        self.spotify_service = get_spotify_service()
        self._observer: Optional[Observer] = None
        self._is_scanning = False
        self._scan_progress: Dict[str, Any] = {
            "status": "idle",
            "current_path": None,
            "progress": 0,
            "total": 0,
            "artists_found": 0,
            "albums_found": 0,
            "tracks_found": 0,
        }
        self._callbacks: List[Callable] = []
    
    @property
    def is_scanning(self) -> bool:
        """Check if a scan is in progress."""
        return self._is_scanning
    
    @property
    def scan_progress(self) -> Dict[str, Any]:
        """Get current scan progress."""
        return self._scan_progress.copy()
    
    def add_progress_callback(self, callback: Callable):
        """Add a callback to be called on progress updates."""
        self._callbacks.append(callback)
    
    def _notify_progress(self):
        """Notify all callbacks of progress update."""
        for callback in self._callbacks:
            try:
                callback(self._scan_progress.copy())
            except Exception as e:
                logger.error(f"Error in progress callback: {e}")
    
    def start_watching(self, paths: Optional[List[str]] = None):
        """
        Start watching the music library for changes.
        
        Args:
            paths: List of paths to watch (uses config if not provided)
        """
        if self._observer and self._observer.is_alive():
            logger.warning("File watcher is already running")
            return
        
        settings = get_settings()
        watch_paths = paths or settings.music_library_paths
        
        self._observer = Observer()
        event_handler = LibraryEventHandler(self)
        
        for path in watch_paths:
            if os.path.exists(path):
                self._observer.schedule(event_handler, path, recursive=True)
                logger.info(f"Watching directory: {path}")
            else:
                logger.warning(f"Watch path does not exist: {path}")
        
        self._observer.start()
        logger.info("File watcher started")
    
    def stop_watching(self):
        """Stop watching the music library."""
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
            logger.info("File watcher stopped")
    
    def scan_library(self, paths: Optional[List[str]] = None) -> LibraryScan:
        """
        Perform a full library scan.
        
        Args:
            paths: List of paths to scan (uses config if not provided)
            
        Returns:
            LibraryScan record with scan results
        """
        if self._is_scanning:
            logger.warning("Scan already in progress")
            raise RuntimeError("A scan is already in progress")
        
        settings = get_settings()
        scan_paths = paths or settings.music_library_paths
        
        self._is_scanning = True
        self._scan_progress = {
            "status": "scanning",
            "current_path": None,
            "progress": 0,
            "total": 0,
            "artists_found": 0,
            "albums_found": 0,
            "tracks_found": 0,
        }
        
        with get_db_session(auto_commit=False) as db:
            scan = LibraryScan(
                status=ScanStatus.SCANNING,
                scan_path=",".join(scan_paths),
            )
            db.add(scan)
            db.flush()
            db.commit()
            
            try:
                all_files = []
                for path in scan_paths:
                    if os.path.exists(path):
                        files = list(self._discover_files(path))
                        all_files.extend(files)
                        logger.info(f"Found {len(files)} audio files in {path}")
                
                self._scan_progress["total"] = len(all_files)
                self._notify_progress()
                
                artists_seen = set()
                albums_seen = set()
                
                for i, file_path in enumerate(all_files):
                    self._scan_progress["current_path"] = file_path
                    self._scan_progress["progress"] = i + 1
                    self._notify_progress()
                    
                    try:
                        artist_id, album_id = self._process_file(db, file_path)
                        
                        # Commit after each successful file processing
                        db.commit()
                        
                        if artist_id:
                            artists_seen.add(artist_id)
                        if album_id:
                            albums_seen.add(album_id)
                        
                        self._scan_progress["tracks_found"] = i + 1
                        self._scan_progress["artists_found"] = len(artists_seen)
                        self._scan_progress["albums_found"] = len(albums_seen)
                    except Exception as e:
                        logger.error(f"Error processing file {file_path}: {e}")
                        db.rollback()
            
                scan.status = ScanStatus.COMPLETED
                scan.artists_found = len(artists_seen)
                scan.albums_found = len(albums_seen)
                scan.tracks_found = len(all_files)
                scan.completed_at = datetime.utcnow()
                db.commit()
                
                self._scan_progress["status"] = "completed"
                self._notify_progress()
                
                logger.info(
                    f"Scan completed: {scan.artists_found} artists, "
                    f"{scan.albums_found} albums, {scan.tracks_found} tracks"
                )
                
            except Exception as e:
                scan.status = ScanStatus.ERROR
                scan.error_message = str(e)
                scan.completed_at = datetime.utcnow()
                db.commit()
                
                self._scan_progress["status"] = "error"
                self._notify_progress()
                
                logger.error(f"Scan failed: {e}")
                raise
            finally:
                self._is_scanning = False
            
            return scan
    
    def _discover_files(self, directory: str):
        """Generator that yields audio file paths in a directory."""
        for root, _, files in os.walk(directory):
            for file in files:
                file_path = os.path.join(root, file)
                if self.metadata_service.is_audio_file(file_path):
                    yield file_path
    
    def _process_file(self, db: Session, file_path: str) -> tuple:
        """
        Process a single audio file.
        
        Returns:
            Tuple of (artist_id, album_id) or (None, None)
        """
        metadata = self.metadata_service.read_metadata(file_path)
        
        if not metadata:
            return None, None
        
        artist_name = metadata.get("albumartist") or metadata.get("artist")
        album_name = metadata.get("album")
        
        if not artist_name:
            # Try to infer from directory structure
            artist_name = self._infer_artist_from_path(file_path)
        
        if not artist_name:
            logger.debug(f"Could not determine artist for {file_path}")
            return None, None
        
        # Get or create artist
        artist = self._get_or_create_artist(db, artist_name, file_path)
        
        if not artist:
            return None, None
        
        # Get or create album if present
        album = None
        if album_name:
            album = self._get_or_create_album(db, artist, album_name, metadata, file_path)
        
        # Create or update track
        if album:
            self._get_or_create_track(db, album, metadata, file_path)
        
        return artist.id if artist else None, album.id if album else None
    
    def _infer_artist_from_path(self, file_path: str) -> Optional[str]:
        """Try to infer artist name from directory structure."""
        # Common patterns: /Artist/Album/Track.flac or /Artist/Track.flac
        parts = Path(file_path).parts
        
        if len(parts) >= 3:
            # Assume grandparent is artist
            return parts[-3]
        elif len(parts) >= 2:
            return parts[-2]
        
        return None
    
    def _get_or_create_artist(
        self, 
        db: Session, 
        name: str, 
        file_path: str
    ) -> Optional[Artist]:
        """Get or create an artist record."""
        # Check if artist exists
        artist = db.query(Artist).filter(
            Artist.name.ilike(name)
        ).first()
        
        if artist:
            return artist
        
        # Try to match with Spotify
        spotify_data = self.spotify_service.match_artist_name(name)
        
        artist = Artist(
            name=name,
            local_path=str(Path(file_path).parent.parent),
        )
        
        if spotify_data:
            artist.spotify_id = spotify_data.get("spotify_id")
            artist.spotify_url = spotify_data.get("spotify_url")
            artist.image_url = spotify_data.get("image_url")
            artist.genres = spotify_data.get("genres", [])
            artist.popularity = spotify_data.get("popularity")
            artist.followers = spotify_data.get("followers")
            artist.last_synced = datetime.utcnow()
        
        db.add(artist)
        db.flush()
        
        logger.info(f"Created artist: {name} (Spotify: {bool(spotify_data)})")
        
        return artist
    
    def _get_or_create_album(
        self,
        db: Session,
        artist: Artist,
        name: str,
        metadata: Dict[str, Any],
        file_path: str,
    ) -> Optional[Album]:
        """Get or create an album record."""
        # Check if album exists
        album = db.query(Album).filter(
            Album.artist_id == artist.id,
            Album.name.ilike(name)
        ).first()
        
        if album:
            return album
        
        # Create new album
        album = Album(
            name=name,
            artist_id=artist.id,
            local_path=str(Path(file_path).parent),
            is_owned=True,
        )
        
        # Try to get Spotify data
        if artist.spotify_id:
            spotify_albums = self.spotify_service.get_artist_albums(artist.spotify_id)
            
            # Find matching album
            name_lower = name.lower()
            for spotify_album in spotify_albums:
                if spotify_album.get("name", "").lower() == name_lower:
                    album.spotify_id = spotify_album.get("spotify_id")
                    album.spotify_url = spotify_album.get("spotify_url")
                    album.image_url = spotify_album.get("image_url")
                    album.album_type = spotify_album.get("album_type")
                    album.release_date = spotify_album.get("release_date")
                    album.release_date_precision = spotify_album.get("release_date_precision")
                    album.total_tracks = spotify_album.get("total_tracks")
                    album.label = spotify_album.get("label")
                    break
        
        # Fallback to file metadata
        if metadata.get("year") and not album.release_date:
            album.release_date = str(metadata["year"])
        
        db.add(album)
        db.flush()
        
        logger.debug(f"Created album: {name} for artist {artist.name}")
        
        return album
    
    def _get_or_create_track(
        self,
        db: Session,
        album: Album,
        metadata: Dict[str, Any],
        file_path: str,
    ) -> Track:
        """Get or create a track record."""
        # Check if track exists by file path
        track = db.query(Track).filter(
            Track.local_path == file_path
        ).first()
        
        if track:
            return track
        
        track = Track(
            name=metadata.get("title") or Path(file_path).stem,
            album_id=album.id,
            local_path=file_path,
            track_number=metadata.get("track_number"),
            disc_number=metadata.get("disc_number", 1),
            duration_ms=metadata.get("duration_ms"),
            isrc=metadata.get("isrc"),
            file_format=metadata.get("file_format"),
            bitrate=metadata.get("bitrate"),
            sample_rate=metadata.get("sample_rate"),
        )
        
        db.add(track)
        db.flush()
        
        return track
    
    def process_new_files(self, file_paths: List[str]):
        """Process newly added files."""
        with get_db_session(auto_commit=False) as db:
            for file_path in file_paths:
                try:
                    self._process_file(db, file_path)
                    db.commit()
                except Exception as e:
                    logger.error(f"Error processing new file {file_path}: {e}")
                    db.rollback()
    
    def handle_file_deletion(self, file_path: str):
        """
        Handle file deletion by updating the database.
        Marks tracks as deleted and updates album ownership status.
        """
        with get_db_session() as db:
            # Find track by path
            track = db.query(Track).filter(Track.local_path == file_path).first()
            
            if not track:
                logger.debug(f"No track found for deleted file: {file_path}")
                return
            
            album = track.album
            logger.info(f"Track deleted: {track.name} from {album.name if album else 'Unknown'}")
            
            # Delete the track
            db.delete(track)
            db.flush()
            
            # Check if album has any remaining tracks
            if album:
                remaining_tracks = db.query(Track).filter(Track.album_id == album.id).count()
                
                if remaining_tracks == 0:
                    # No more tracks, mark album as not owned
                    logger.info(f"Album has no more tracks, marking as not owned: {album.name}")
                    album.is_owned = False
                    album.local_path = None
                    
                    # Check if artist has any owned albums
                    artist = album.artist
                    if artist:
                        owned_albums = db.query(Album).filter(
                            Album.artist_id == artist.id,
                            Album.is_owned == True
                        ).count()
                        
                        if owned_albums == 0:
                            logger.info(f"Artist has no more owned albums: {artist.name}")
            
            db.commit()
            logger.info(f"Database updated after file deletion: {file_path}")
    
    def sync_artist_with_spotify(self, db: Session, artist: Artist) -> bool:
        """
        Sync an artist's data with Spotify.
        
        Args:
            db: Database session
            artist: Artist to sync
            
        Returns:
            True if successful
        """
        if not artist.spotify_id:
            # Try to find the artist
            spotify_data = self.spotify_service.match_artist_name(artist.name)
            if not spotify_data:
                logger.warning(f"Could not find artist on Spotify: {artist.name}")
                return False
            
            artist.spotify_id = spotify_data.get("spotify_id")
        
        # Get full artist data
        spotify_artist = self.spotify_service.get_artist(artist.spotify_id)
        
        if not spotify_artist:
            return False
        
        # Update artist
        artist.spotify_url = spotify_artist.get("spotify_url")
        artist.image_url = spotify_artist.get("image_url")
        artist.genres = spotify_artist.get("genres", [])
        artist.popularity = spotify_artist.get("popularity")
        artist.followers = spotify_artist.get("followers")
        artist.last_synced = datetime.utcnow()
        
        # Sync albums
        spotify_albums = self.spotify_service.get_artist_albums(artist.spotify_id)
        
        for spotify_album in spotify_albums:
            # Check if album exists
            album = db.query(Album).filter(
                Album.spotify_id == spotify_album.get("spotify_id")
            ).first()
            
            if not album:
                # Check by name
                album = db.query(Album).filter(
                    Album.artist_id == artist.id,
                    Album.name.ilike(spotify_album.get("name"))
                ).first()
            
            if album:
                # Update existing album
                album.spotify_id = spotify_album.get("spotify_id")
                album.spotify_url = spotify_album.get("spotify_url")
                album.image_url = spotify_album.get("image_url")
                album.album_type = spotify_album.get("album_type")
                album.release_date = spotify_album.get("release_date")
                album.total_tracks = spotify_album.get("total_tracks")
            else:
                # Create new album (not owned)
                album = Album(
                    name=spotify_album.get("name"),
                    artist_id=artist.id,
                    spotify_id=spotify_album.get("spotify_id"),
                    spotify_url=spotify_album.get("spotify_url"),
                    image_url=spotify_album.get("image_url"),
                    album_type=spotify_album.get("album_type"),
                    release_date=spotify_album.get("release_date"),
                    total_tracks=spotify_album.get("total_tracks"),
                    is_owned=False,
                )
                db.add(album)
        
        db.flush()
        logger.info(f"Synced artist with Spotify: {artist.name}")
        
        return True


# Global scanner instance
_library_scanner: Optional[LibraryScanner] = None


def get_library_scanner() -> LibraryScanner:
    """Get the global library scanner instance."""
    global _library_scanner
    if _library_scanner is None:
        _library_scanner = LibraryScanner()
    return _library_scanner
