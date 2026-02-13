"""
Spotiflac web - Database Models
"""

from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, 
    ForeignKey, Enum, Float, JSON, create_engine
)
from sqlalchemy.orm import relationship, declarative_base, sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from datetime import datetime
import enum
from typing import Optional


Base = declarative_base()


class DownloadStatus(str, enum.Enum):
    """Status of a download task."""
    PENDING = "pending"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScanStatus(str, enum.Enum):
    """Status of a library scan."""
    IDLE = "idle"
    SCANNING = "scanning"
    COMPLETED = "completed"
    ERROR = "error"


class Artist(Base):
    """Artist model representing a music artist."""
    __tablename__ = "artists"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, index=True)
    spotify_id = Column(String(50), unique=True, nullable=True, index=True)
    spotify_url = Column(String(500), nullable=True)
    image_url = Column(String(500), nullable=True)
    genres = Column(JSON, default=list)
    popularity = Column(Integer, nullable=True)
    followers = Column(Integer, nullable=True)
    
    # Local library info
    local_path = Column(String(1000), nullable=True)
    is_monitored = Column(Boolean, default=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_synced = Column(DateTime, nullable=True)
    
    # Relationships
    albums = relationship("Album", back_populates="artist", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Artist(id={self.id}, name='{self.name}')>"
    
    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "spotify_id": self.spotify_id,
            "spotify_url": self.spotify_url,
            "image_url": self.image_url,
            "genres": self.genres or [],
            "popularity": self.popularity,
            "followers": self.followers,
            "local_path": self.local_path,
            "is_monitored": self.is_monitored,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_synced": self.last_synced.isoformat() if self.last_synced else None,
            "album_count": len(self.albums) if self.albums else 0,
        }


class Album(Base):
    """Album model representing a music album."""
    __tablename__ = "albums"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, index=True)
    spotify_id = Column(String(50), unique=True, nullable=True, index=True)
    spotify_url = Column(String(500), nullable=True)
    image_url = Column(String(500), nullable=True)
    
    # Album metadata
    album_type = Column(String(50), nullable=True)  # album, single, compilation
    release_date = Column(String(20), nullable=True)
    release_date_precision = Column(String(10), nullable=True)  # year, month, day
    total_tracks = Column(Integer, nullable=True)
    label = Column(String(255), nullable=True)
    
    # Local library info
    local_path = Column(String(1000), nullable=True)
    is_owned = Column(Boolean, default=False)
    is_wanted = Column(Boolean, default=False)
    is_incomplete = Column(Boolean, default=False)
    missing_tracks = Column(JSON, default=list)  # List of missing track names/numbers
    
    # Foreign keys
    artist_id = Column(Integer, ForeignKey("artists.id"), nullable=False)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    artist = relationship("Artist", back_populates="albums")
    tracks = relationship("Track", back_populates="album", cascade="all, delete-orphan")
    downloads = relationship("Download", back_populates="album", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Album(id={self.id}, name='{self.name}')>"
    
    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "spotify_id": self.spotify_id,
            "spotify_url": self.spotify_url,
            "image_url": self.image_url,
            "album_type": self.album_type,
            "release_date": self.release_date,
            "total_tracks": self.total_tracks,
            "label": self.label,
            "local_path": self.local_path,
            "is_owned": self.is_owned,
            "is_wanted": self.is_wanted,
            "is_incomplete": self.is_incomplete,
            "missing_tracks": self.missing_tracks or [],
            "artist_id": self.artist_id,
            "artist_name": self.artist.name if self.artist else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "track_count": len(self.tracks) if self.tracks else 0,
        }


class Track(Base):
    """Track model representing a single track."""
    __tablename__ = "tracks"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, index=True)
    spotify_id = Column(String(50), unique=True, nullable=True, index=True)
    spotify_url = Column(String(500), nullable=True)
    
    # Track metadata
    track_number = Column(Integer, nullable=True)
    disc_number = Column(Integer, default=1)
    duration_ms = Column(Integer, nullable=True)
    explicit = Column(Boolean, default=False)
    isrc = Column(String(20), nullable=True)
    preview_url = Column(String(500), nullable=True)
    
    # Local file info
    local_path = Column(String(1000), nullable=True)
    file_format = Column(String(10), nullable=True)  # flac, mp3, etc.
    bitrate = Column(Integer, nullable=True)
    sample_rate = Column(Integer, nullable=True)
    
    # Foreign keys
    album_id = Column(Integer, ForeignKey("albums.id"), nullable=False)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    album = relationship("Album", back_populates="tracks")
    
    def __repr__(self):
        return f"<Track(id={self.id}, name='{self.name}')>"
    
    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "spotify_id": self.spotify_id,
            "spotify_url": self.spotify_url,
            "track_number": self.track_number,
            "disc_number": self.disc_number,
            "duration_ms": self.duration_ms,
            "duration_formatted": self._format_duration(),
            "explicit": self.explicit,
            "isrc": self.isrc,
            "local_path": self.local_path,
            "file_format": self.file_format,
            "album_id": self.album_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
    
    def _format_duration(self) -> str:
        """Format duration in MM:SS format."""
        if not self.duration_ms:
            return "0:00"
        total_seconds = self.duration_ms // 1000
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        return f"{minutes}:{seconds:02d}"


class Download(Base):
    """Download model representing a download task."""
    __tablename__ = "downloads"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    status = Column(Enum(DownloadStatus), default=DownloadStatus.PENDING)
    progress = Column(Float, default=0.0)
    error_message = Column(Text, nullable=True)
    
    service = Column(String(50), nullable=True)
    output_path = Column(String(1000), nullable=True)

    spotify_url = Column(String(500), nullable=True)
    url_type = Column(String(20), nullable=True)
    title = Column(String(255), nullable=True)
    artist_name = Column(String(255), nullable=True) 


    current_track_name = Column(String(500), nullable=True)
    current_track_number = Column(Integer, nullable=True) 
    total_tracks = Column(Integer, nullable=True)
    
    # Retry and failure tracking
    retry_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)
    failed_tracks = Column(JSON, default=list)  # List of failed track info {name, error, attempts}
    
    album_id = Column(Integer, ForeignKey("albums.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    album = relationship("Album", back_populates="downloads")
    
    def __repr__(self):
        return f"<Download(id={self.id}, status={self.status})>"
    
    def to_dict(self):
        if self.album:
            album_name = self.album.name
            artist_name = self.album.artist.name if self.album.artist else None
            image_url = self.album.image_url
            spotify_url = self.album.spotify_url
        else:
            album_name = self.title
            artist_name = self.artist_name
            image_url = None
            spotify_url = self.spotify_url
        
        return {
            "id": self.id,
            "status": self.status.value,
            "progress": self.progress,
            "error_message": self.error_message,
            "service": self.service,
            "output_path": self.output_path,
            "album_id": self.album_id,
            "album_name": album_name,
            "artist_name": artist_name,
            "image_url": image_url,
            "spotify_url": spotify_url,
            "url_type": self.url_type,
            "title": self.title,
            "is_manual": self.album_id is None,
            "current_track_name": self.current_track_name,
            "current_track_number": self.current_track_number,
            "total_tracks": self.total_tracks,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "failed_tracks": self.failed_tracks or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class LibraryScan(Base):
    """Library scan history model."""
    __tablename__ = "library_scans"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    status = Column(Enum(ScanStatus), default=ScanStatus.IDLE)
    scan_path = Column(String(1000), nullable=True)
    
    # Statistics
    artists_found = Column(Integer, default=0)
    albums_found = Column(Integer, default=0)
    tracks_found = Column(Integer, default=0)
    new_artists = Column(Integer, default=0)
    new_albums = Column(Integer, default=0)
    
    # Error tracking
    error_message = Column(Text, nullable=True)
    
    # Timestamps
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    
    def __repr__(self):
        return f"<LibraryScan(id={self.id}, status={self.status})>"
    
    def to_dict(self):
        return {
            "id": self.id,
            "status": self.status.value,
            "scan_path": self.scan_path,
            "artists_found": self.artists_found,
            "albums_found": self.albums_found,
            "tracks_found": self.tracks_found,
            "new_artists": self.new_artists,
            "new_albums": self.new_albums,
            "error_message": self.error_message,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class Setting(Base):
    """Application settings stored in database."""
    __tablename__ = "settings"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=True)
    value_type = Column(String(20), default="string")  # string, int, bool, json
    description = Column(String(500), nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f"<Setting(key='{self.key}')>"
    
    def get_typed_value(self):
        """Return the value with proper type conversion."""
        if self.value is None:
            return None
        if self.value_type == "int":
            return int(self.value)
        if self.value_type == "bool":
            return self.value.lower() in ("true", "1", "yes")
        if self.value_type == "json":
            import json
            return json.loads(self.value)
        return self.value
