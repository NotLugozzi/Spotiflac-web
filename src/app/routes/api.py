"""
Spotiflac web - API Routes
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Form, Request
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session
from typing import Optional, List
from pydantic import BaseModel, model_validator
import asyncio
import json
import logging
import os

from app.database import get_db
from app.models import Artist, Album, Track, Download, DownloadStatus, LibraryScan, TrackHistory
from app.services.spotify_service import get_spotify_service
from app.services.library_scanner import get_library_scanner
from app.services.download_manager import get_download_manager
from app.services.break_monitor import get_break_monitor
from app.services import service_status
from app.services.metadata_service import get_metadata_service
from app.services.url_parser import (
    parse_spotify_url, 
    parse_multiple_urls, 
    is_valid_spotify_url,
    SpotifyUrlType
)
from app.config import get_settings
from app.services.discovery_defaults import DEFAULT_CATEGORIES

router = APIRouter(prefix="/api", tags=["API"])
logger = logging.getLogger(__name__)


# ============================================================================
# Authentication Endpoints
# ============================================================================

@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    """Login endpoint."""
    settings = get_settings()
    
    # Check credentials
    import hmac
    valid_auth = False
    if settings.auth_username and settings.auth_password:
        try:
            safe_user = str(username) if username is not None else ""
            safe_pass = str(password) if password is not None else ""
            u_match = hmac.compare_digest(safe_user.encode('utf-8'), settings.auth_username.encode('utf-8'))
            p_match = hmac.compare_digest(safe_pass.encode('utf-8'), settings.auth_password.encode('utf-8'))
            valid_auth = bool(u_match & p_match)
        except Exception:
            valid_auth = False
    
    if not valid_auth:
        # Failed login - redirect back to login with error
        return RedirectResponse(url="/login?error=Invalid+credentials", status_code=303)
        
    # Set session
    request.session["user"] = username
    return RedirectResponse(url="/", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    """Logout endpoint."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@router.get("/logout")
async def logout_get(request: Request):
    """Logout endpoint (GET for convenience)."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# ============================================================================
# Pydantic Models for Request/Response
# ============================================================================

class ArtistResponse(BaseModel):
    id: int
    name: str
    spotify_id: Optional[str]
    spotify_url: Optional[str]
    image_url: Optional[str]
    genres: List[str]
    is_monitored: bool
    album_count: int

    class Config:
        from_attributes = True


class AlbumResponse(BaseModel):
    id: int
    name: str
    spotify_id: Optional[str]
    spotify_url: Optional[str]
    image_url: Optional[str]
    album_type: Optional[str]
    release_date: Optional[str]
    total_tracks: Optional[int]
    is_owned: bool
    is_wanted: bool
    artist_id: int
    artist_name: Optional[str]

    class Config:
        from_attributes = True


class DownloadRequest(BaseModel):
    album_id: Optional[int] = None
    url: Optional[str] = None
    
    @model_validator(mode='after')
    def check_at_least_one(self):
        if not self.album_id and not self.url:
            raise ValueError('Either album_id or url must be provided')
        return self


class UrlDownloadRequest(BaseModel):
    """Request model for downloading from a direct Spotify URL."""
    url: str
    title: Optional[str] = None
    artist_name: Optional[str] = None


class BatchUrlDownloadRequest(BaseModel):
    """Request model for downloading multiple Spotify URLs."""
    urls: List[str]


class SearchRequest(BaseModel):
    query: str
    limit: int = 10


class SettingsUpdate(BaseModel):
    key: str
    value: str


class AlbumMetadataFixRequest(BaseModel):
    album_artist: str
    album_name: Optional[str] = None


class TrackMetadataFixRequest(BaseModel):
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    albumartist: Optional[str] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = None


# ============================================================================
# Library Endpoints
# ============================================================================

@router.get("/library/stats")
async def get_library_stats(db: Session = Depends(get_db)):
    """Get library statistics."""
    artist_count = db.query(Artist).count()
    album_count = db.query(Album).count()
    owned_album_count = db.query(Album).filter(Album.is_owned == True).count()
    track_count = db.query(Track).count()
    wanted_count = db.query(Album).filter(Album.is_wanted == True).count()
    
    # Get recent additions
    recent_albums = db.query(Album).filter(
        Album.is_owned == True
    ).order_by(Album.created_at.desc()).limit(5).all()
    
    return {
        "artists": artist_count,
        "albums": album_count,
        "owned_albums": owned_album_count,
        "tracks": track_count,
        "wanted": wanted_count,
        "recent_albums": [a.to_dict() for a in recent_albums],
    }


@router.post("/library/scan")
async def start_library_scan(background_tasks: BackgroundTasks):
    """Start a library scan."""
    scanner = get_library_scanner()
    
    if scanner.is_scanning:
        raise HTTPException(status_code=409, detail="Scan already in progress")
    
    background_tasks.add_task(scanner.scan_library)
    
    return {"status": "started", "message": "Library scan started"}


@router.get("/library/scan/status")
async def get_scan_status():
    """Get current scan status."""
    scanner = get_library_scanner()
    return scanner.scan_progress


@router.get("/library/scan/history")
async def get_scan_history(
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db)
):
    """Get scan history."""
    scans = db.query(LibraryScan).order_by(
        LibraryScan.started_at.desc()
    ).limit(limit).all()
    
    return [s.to_dict() for s in scans]


# ============================================================================
# Artist Endpoints
# ============================================================================

@router.get("/artists")
async def list_artists(
    search: Optional[str] = None,
    monitored_only: bool = False,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db)
):
    """List all artists with optional filtering."""
    query = db.query(Artist)
    
    if search:
        query = query.filter(Artist.name.ilike(f"%{search}%"))
    
    if monitored_only:
        query = query.filter(Artist.is_monitored == True)
    
    total = query.count()
    artists = query.order_by(Artist.name).offset(
        (page - 1) * per_page
    ).limit(per_page).all()
    
    return {
        "items": [a.to_dict() for a in artists],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    }


@router.get("/artists/{artist_id}")
async def get_artist(artist_id: int, db: Session = Depends(get_db)):
    """Get artist details."""
    artist = db.query(Artist).filter(Artist.id == artist_id).first()
    
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    
    return artist.to_dict()


@router.get("/artists/{artist_id}/albums")
async def get_artist_albums(
    artist_id: int,
    include_unowned: bool = True,
    db: Session = Depends(get_db)
):
    """Get all albums for an artist."""
    artist = db.query(Artist).filter(Artist.id == artist_id).first()
    
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    
    query = db.query(Album).filter(Album.artist_id == artist_id)
    
    if not include_unowned:
        query = query.filter(Album.is_owned == True)
    
    albums = query.order_by(Album.release_date.desc()).all()
    
    return [a.to_dict() for a in albums]


# Plain `def`: blocking Spotify call, run in FastAPI's thread pool (not the event loop)
@router.post("/artists/{artist_id}/sync")
def sync_artist(
    artist_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """Sync artist data with Spotify."""
    artist = db.query(Artist).filter(Artist.id == artist_id).first()
    
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    
    scanner = get_library_scanner()
    
    def sync_task():
        from app.database import get_db_session
        with get_db_session() as session:
            a = session.query(Artist).get(artist_id)
            if a is None:
                return
            scanner.sync_artist_with_spotify(session, a)
    
    background_tasks.add_task(sync_task)
    
    return {"status": "syncing", "message": f"Syncing artist: {artist.name}"}


@router.patch("/artists/{artist_id}")
async def update_artist(
    artist_id: int,
    is_monitored: Optional[bool] = None,
    db: Session = Depends(get_db)
):
    """Update artist settings."""
    artist = db.query(Artist).filter(Artist.id == artist_id).first()
    
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    
    if is_monitored is not None:
        artist.is_monitored = is_monitored
    
    db.commit()
    
    return artist.to_dict()


@router.post("/artists/{artist_id}/unlink")
async def unlink_artist_from_spotify(
    artist_id: int,
    db: Session = Depends(get_db)
):
    """Remove Spotify link from an artist (unlink remote artist from local)."""
    artist = db.query(Artist).filter(Artist.id == artist_id).first()
    
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    
    # Clear Spotify metadata
    artist.spotify_id = None
    artist.spotify_url = None
    artist.image_url = None
    artist.genres = []
    artist.popularity = None
    artist.followers = None
    artist.last_synced = None
    
    db.commit()
    
    logger.info(f"Unlinked artist '{artist.name}' (ID: {artist_id}) from Spotify")
    
    return {"status": "success", "message": f"Artist '{artist.name}' has been unlinked from Spotify"}


# ============================================================================
# Album Endpoints
# ============================================================================

@router.get("/albums")
async def list_albums(
    search: Optional[str] = None,
    owned_only: bool = False,
    wanted_only: bool = False,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db)
):
    """List all albums with optional filtering."""
    query = db.query(Album)
    
    # Exclude compilations
    query = query.filter(Album.album_type != "compilation")
    
    if search:
        query = query.filter(Album.name.ilike(f"%{search}%"))
    
    if owned_only:
        query = query.filter(Album.is_owned == True)
    
    if wanted_only:
        query = query.filter(Album.is_wanted == True)
    
    total = query.count()
    albums = query.order_by(Album.release_date.desc()).offset(
        (page - 1) * per_page
    ).limit(per_page).all()
    
    return {
        "items": [a.to_dict() for a in albums],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    }


@router.get("/albums/{album_id}")
async def get_album(album_id: int, db: Session = Depends(get_db)):
    """Get album details with tracks."""
    album = db.query(Album).filter(Album.id == album_id).first()
    
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    
    result = album.to_dict()
    result["tracks"] = [t.to_dict() for t in album.tracks]
    result["artist"] = album.artist.to_dict() if album.artist else None
    
    return result


@router.patch("/albums/{album_id}")
async def update_album(
    album_id: int,
    is_wanted: Optional[bool] = None,
    db: Session = Depends(get_db)
):
    """Update album settings."""
    album = db.query(Album).filter(Album.id == album_id).first()
    
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    
    if is_wanted is not None:
        album.is_wanted = is_wanted
    
    db.commit()
    
    return album.to_dict()


# Plain `def`: blocking Spotify call, run in FastAPI's thread pool (not the event loop)
@router.post("/albums/{album_id}/refresh")
def refresh_album_from_spotify(
    album_id: int,
    db: Session = Depends(get_db)
):
    """Refresh album data from Spotify and check completeness."""
    album = db.query(Album).filter(Album.id == album_id).first()
    
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    
    if not album.spotify_id:
        raise HTTPException(status_code=400, detail="Album has no Spotify ID")
    
    spotify = get_spotify_service()
    spotify_data = spotify.get_album(album.spotify_id)
    
    if not spotify_data:
        raise HTTPException(status_code=404, detail="Album not found on Spotify")
    
    # Update album
    album.image_url = spotify_data.get("image_url")
    album.album_type = spotify_data.get("album_type")
    album.release_date = spotify_data.get("release_date")
    album.total_tracks = spotify_data.get("total_tracks")
    album.label = spotify_data.get("label")
    
    # Update/create tracks from Spotify
    for track_data in spotify_data.get("tracks", []):
        track = db.query(Track).filter(
            Track.spotify_id == track_data.get("spotify_id")
        ).first()
        
        if not track:
            track = Track(
                album_id=album.id,
                spotify_id=track_data.get("spotify_id"),
            )
            db.add(track)
        
        track.name = track_data.get("name")
        track.spotify_url = track_data.get("spotify_url")
        track.track_number = track_data.get("track_number")
        track.disc_number = track_data.get("disc_number", 1)
        track.duration_ms = track_data.get("duration_ms")
        track.explicit = track_data.get("explicit", False)
        track.isrc = track_data.get("isrc")
    
    db.commit()
    
    # Check completeness if album is owned
    if album.is_owned:
        scanner = get_library_scanner()
        completeness = scanner.check_album_completeness(db, album)
        
        if completeness.get('is_complete') is not None:
            is_complete = completeness['is_complete']
            album.is_incomplete = not is_complete
            
            if not is_complete:
                album.missing_tracks = completeness.get('missing_tracks', [])
                logger.info(
                    f"Album '{album.name}' marked as incomplete: "
                    f"{len(album.missing_tracks)} missing tracks"
                )
            else:
                album.missing_tracks = []
                logger.info(f"Album '{album.name}' is complete!")
            
            db.commit()
    
    return album.to_dict()


@router.post("/albums/{album_id}/check-completeness")
async def check_album_completeness(
    album_id: int,
    db: Session = Depends(get_db)
):
    """Check if an album is complete by comparing local files against Spotify."""
    album = db.query(Album).filter(Album.id == album_id).first()
    
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    
    if not album.spotify_id:
        raise HTTPException(status_code=400, detail="Album has no Spotify ID")
    
    if not album.is_owned:
        raise HTTPException(status_code=400, detail="Album is not in your library")
    
    scanner = get_library_scanner()
    completeness = scanner.check_album_completeness(db, album)
    
    if 'error' in completeness:
        raise HTTPException(status_code=500, detail=completeness['error'])
    
    # Update album status
    is_complete = completeness['is_complete']
    album.is_incomplete = not is_complete
    
    if not is_complete:
        album.missing_tracks = completeness.get('missing_tracks', [])
    else:
        album.missing_tracks = []
    
    db.commit()
    
    return {
        "album": album.to_dict(),
        "completeness": completeness
    }


@router.post("/albums/check-all-completeness")
async def check_all_albums_completeness(
    only_incomplete: bool = Query(False, description="Only check albums already marked as incomplete"),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db)
):
    """
    Check completeness of all owned albums in the library.
    This can be a long-running operation for large libraries.
    """
    def run_check():
        scanner = get_library_scanner()
        return scanner.check_all_albums_completeness(only_incomplete=only_incomplete)
    
    # Run in background for large libraries
    if background_tasks:
        background_tasks.add_task(run_check)
        return {
            "message": "Completeness check started in background",
            "status": "running"
        }
    else:
        results = run_check()
        return results


# ============================================================================
# Metadata Fix Endpoints
# ============================================================================

@router.get("/albums/{album_id}/metadata-preview")
async def preview_album_metadata(
    album_id: int,
    db: Session = Depends(get_db)
):
    """Preview current metadata for local tracks in an album."""
    album = db.query(Album).filter(Album.id == album_id).first()
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    metadata_service = get_metadata_service()
    preview_tracks = []
    unique_album_artists = set()

    for track in sorted(album.tracks, key=lambda t: (t.disc_number or 1, t.track_number or 0, t.id)):
        if not track.local_path:
            continue

        metadata = metadata_service.read_metadata(track.local_path)
        if not metadata:
            continue

        album_artist_value = metadata.get("albumartist")
        if album_artist_value:
            unique_album_artists.add(album_artist_value)

        preview_tracks.append({
            "track_id": track.id,
            "track_name": track.name,
            "local_path": track.local_path,
            "metadata": {
                "title": metadata.get("title"),
                "artist": metadata.get("artist"),
                "album": metadata.get("album"),
                "albumartist": album_artist_value,
                "track_number": metadata.get("track_number"),
                "disc_number": metadata.get("disc_number"),
            }
        })

    suggested_album_artist = (
        album.artist.name if album.artist and album.artist.name
        else (next(iter(unique_album_artists)) if unique_album_artists else None)
    )

    return {
        "album_id": album.id,
        "album_name": album.name,
        "track_count": len(preview_tracks),
        "unique_album_artists": sorted(unique_album_artists),
        "suggested_album_artist": suggested_album_artist,
        "tracks": preview_tracks,
    }


@router.post("/albums/{album_id}/metadata/fix")
async def fix_album_metadata(
    album_id: int,
    request: AlbumMetadataFixRequest,
    db: Session = Depends(get_db)
):
    """Apply album-level metadata fixes to all local tracks in an album."""
    album = db.query(Album).filter(Album.id == album_id).first()
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")

    album_artist = (request.album_artist or "").strip()
    album_name = (request.album_name or "").strip() if request.album_name is not None else None

    if not album_artist:
        raise HTTPException(status_code=400, detail="album_artist is required")

    metadata_service = get_metadata_service()
    updated = 0
    failed = []

    for track in album.tracks:
        if not track.local_path:
            continue

        write_payload = {"albumartist": album_artist}
        if album_name:
            write_payload["album"] = album_name

        success = metadata_service.write_metadata(track.local_path, write_payload)
        if success:
            updated += 1
        else:
            failed.append({"track_id": track.id, "track_name": track.name})

    if album_name:
        album.name = album_name

    db.commit()

    return {
        "status": "ok",
        "updated_tracks": updated,
        "failed_tracks": failed,
        "album_artist": album_artist,
        "album_name": album_name or album.name,
    }


@router.get("/tracks/{track_id}/metadata-preview")
async def preview_track_metadata(
    track_id: int,
    db: Session = Depends(get_db)
):
    """Preview current metadata for one local track."""
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    if not track.local_path:
        raise HTTPException(status_code=400, detail="Track has no local file")

    metadata_service = get_metadata_service()
    metadata = metadata_service.read_metadata(track.local_path)
    if not metadata:
        raise HTTPException(status_code=404, detail="Track metadata could not be read")

    return {
        "track_id": track.id,
        "track_name": track.name,
        "album_id": track.album_id,
        "local_path": track.local_path,
        "metadata": {
            "title": metadata.get("title"),
            "artist": metadata.get("artist"),
            "album": metadata.get("album"),
            "albumartist": metadata.get("albumartist"),
            "track_number": metadata.get("track_number"),
            "disc_number": metadata.get("disc_number"),
        }
    }


@router.post("/tracks/{track_id}/metadata/fix")
async def fix_track_metadata(
    track_id: int,
    request: TrackMetadataFixRequest,
    db: Session = Depends(get_db)
):
    """Apply metadata fixes to one local track."""
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    if not track.local_path:
        raise HTTPException(status_code=400, detail="Track has no local file")

    write_payload = {}

    if request.title is not None and request.title.strip():
        write_payload["title"] = request.title.strip()
    if request.artist is not None and request.artist.strip():
        write_payload["artist"] = request.artist.strip()
    if request.album is not None and request.album.strip():
        write_payload["album"] = request.album.strip()
    if request.albumartist is not None and request.albumartist.strip():
        write_payload["albumartist"] = request.albumartist.strip()
    if request.track_number is not None and request.track_number > 0:
        write_payload["track_number"] = request.track_number
    if request.disc_number is not None and request.disc_number > 0:
        write_payload["disc_number"] = request.disc_number

    if not write_payload:
        raise HTTPException(status_code=400, detail="No metadata fields to update")

    metadata_service = get_metadata_service()
    success = metadata_service.write_metadata(track.local_path, write_payload)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to write metadata")

    if "title" in write_payload:
        track.name = write_payload["title"]
    if "track_number" in write_payload:
        track.track_number = write_payload["track_number"]
    if "disc_number" in write_payload:
        track.disc_number = write_payload["disc_number"]

    db.commit()

    return {
        "status": "ok",
        "track_id": track.id,
        "updated_fields": list(write_payload.keys()),
        "track": track.to_dict(),
    }


# ============================================================================
# Download Endpoints
# ============================================================================

@router.get("/downloads/queue")
async def get_download_queue():
    """Get current download queue."""
    manager = get_download_manager()
    return {
        "queue": manager.get_queue(),
        "current": manager.current_download,
        "queue_size": manager.queue_size,
    }


@router.get("/downloads/history")
async def get_download_history(
    limit: int = Query(50, ge=1, le=200)
):
    """Get download history."""
    manager = get_download_manager()
    return manager.get_history(limit)


# Plain `def`: blocking Spotify call, run in FastAPI's thread pool (not the event loop)
@router.post("/downloads")
def create_download(
    request: DownloadRequest,
    db: Session = Depends(get_db)
):
    """Add an album to the download queue."""
    manager = get_download_manager()
    
    if not manager.is_available:
        raise HTTPException(status_code=503, detail="SpotiFLAC is not available")
    
    # Handle URL-based download - start immediately in parallel
    if request.url:
        parsed = parse_spotify_url(request.url)
        
        if not parsed.is_valid:
            raise HTTPException(status_code=400, detail="Invalid Spotify URL")
        
        # Fetch metadata
        title, artist_name, _ = _fetch_spotify_metadata(parsed.spotify_id, parsed.url_type.value)
        
        download = manager.start_download(
            db=db,
            spotify_url=parsed.normalized_url,
            url_type=parsed.url_type.value,
            title=title,
            artist_name=artist_name,
        )
        db.commit()
        return download.to_dict()
    
    # Handle album_id-based download (legacy) - also start immediately
    if request.album_id:
        album = db.query(Album).filter(Album.id == request.album_id).first()
        
        if not album:
            raise HTTPException(status_code=404, detail="Album not found")
        
        if not album.spotify_url:
            raise HTTPException(status_code=400, detail="Album has no Spotify URL")
        
        # Start download immediately instead of queuing
        parsed = parse_spotify_url(album.spotify_url)
        if parsed.is_valid:
            download = manager.start_download(
                db=db,
                spotify_url=parsed.normalized_url,
                url_type=parsed.url_type.value,
                title=album.name,
                artist_name=album.artist.name if album.artist else None,
            )
            # Link to album
            download.album_id = album.id
            db.commit()
            return download.to_dict()
        else:
            raise HTTPException(status_code=400, detail="Invalid album Spotify URL")
    
    raise HTTPException(status_code=400, detail="Either album_id or url must be provided")


@router.delete("/downloads/{download_id}")
async def cancel_download(download_id: int):
    """Cancel a download."""
    manager = get_download_manager()
    
    if not manager.cancel_download(download_id):
        raise HTTPException(status_code=404, detail="Download not found")
    
    return {"status": "cancelled"}


# Plain `def`: DB work plus a possible process interrupt, run in the thread pool
@router.post("/downloads/{download_id}/prioritize")
def prioritize_download(download_id: int):
    """"Download next": move a queued download to the front; a running or
    waiting job steps aside and continues right after it."""
    if not get_download_manager().prioritize(download_id):
        raise HTTPException(status_code=409, detail="Only queued downloads can be prioritised")
    return {"status": "prioritized"}


@router.post("/downloads/{download_id}/retry")
async def retry_download(download_id: int):
    """Retry a failed download."""
    manager = get_download_manager()
    
    if not manager.retry_download(download_id):
        raise HTTPException(status_code=400, detail="Cannot retry download")
    
    return {"status": "queued"}


def _fetch_spotify_metadata(spotify_id: str, url_type: str) -> tuple[str, str, str]:
    """
    Fetch title and artist from Spotify API based on URL type.
    
    Returns:
        (title, artist_name, album_name) tuple
    """
    spotify = get_spotify_service()
    
    title = None
    artist_name = None
    album_name = None
    
    if not spotify.is_configured:
        return title, artist_name, album_name
    
    try:
        if url_type == "track":
            track = spotify.get_track(spotify_id)
            if track:
                title = track.get("name")
                artist_name = track.get("artist_name")
                album_name = track.get("album_name")
        elif url_type == "album":
            album = spotify.get_album(spotify_id)
            if album:
                title = album.get("name")
                artist_name = album.get("artist_name")
                album_name = album.get("name")
        elif url_type == "artist":
            artist = spotify.search_artists(spotify_id, limit=1)
            if artist:
                artist_name = artist[0].get("name")
                title = f"All albums by {artist_name}"
        elif url_type == "playlist":
            # Playlists don't have a single artist
            title = f"Playlist"
    except Exception as e:
        logger.warning(f"Could not fetch Spotify metadata for {url_type} {spotify_id}: {e}")
    
    return title, artist_name, album_name


# ============================================================================
# Direct URL Download Endpoints
# ============================================================================

# Plain `def` (not async) on purpose: these make blocking Spotify API calls,
# which FastAPI then runs in its thread pool instead of on the event loop.
@router.post("/downloads/url")
def download_from_url(
    request: UrlDownloadRequest,
    db: Session = Depends(get_db)
):
    """
    Add a Spotify URL directly to the download queue.
    
    Supports:
    - Album URLs: https://open.spotify.com/album/xxxxx
    - Track URLs: https://open.spotify.com/track/xxxxx
    - Artist URLs: https://open.spotify.com/artist/xxxxx (downloads all albums)
    - Playlist URLs: https://open.spotify.com/playlist/xxxxx
    - Spotify URIs: spotify:album:xxxxx, spotify:track:xxxxx, etc.
    """
    url = request.url
    title = request.title
    artist_name = request.artist_name
    # Parse the URL
    parsed = parse_spotify_url(url)
    
    if not parsed.is_valid:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid Spotify URL. Supported formats: album, track, artist, or playlist URLs/URIs"
        )
    
    manager = get_download_manager()
    
    if not manager.is_available:
        raise HTTPException(status_code=503, detail="SpotiFLAC is not available")
    
    # Fetch metadata from Spotify if not provided
    if not title or not artist_name:
        fetched_title, fetched_artist, _ = _fetch_spotify_metadata(parsed.spotify_id, parsed.url_type.value)
        title = title or fetched_title
        artist_name = artist_name or fetched_artist
    
    # Start download immediately (parallel execution)
    download = manager.start_download(
        db=db,
        spotify_url=parsed.normalized_url,
        url_type=parsed.url_type.value,
        title=title,
        artist_name=artist_name,
    )
    db.commit()
    
    return download.to_dict()


@router.post("/downloads/urls")
def download_from_urls(
    request: BatchUrlDownloadRequest,
    db: Session = Depends(get_db)
):
    """
    Add multiple Spotify URLs to the download queue.
    
    Accepts a list of URLs (album, track, artist, or playlist URLs/URIs).
    Invalid URLs will be skipped with a warning.
    Downloads will start immediately in parallel.
    """
    manager = get_download_manager()
    
    if not manager.is_available:
        raise HTTPException(status_code=503, detail="SpotiFLAC is not available")
    
    results = []
    errors = []
    
    for url in request.urls:
        parsed = parse_spotify_url(url)
        
        if not parsed.is_valid:
            errors.append({"url": url, "error": "Invalid Spotify URL"})
            continue
        
        try:
            # Fetch metadata from Spotify
            title, artist_name, _ = _fetch_spotify_metadata(parsed.spotify_id, parsed.url_type.value)
            
            # Start download immediately (parallel execution)
            download = manager.start_download(
                db=db,
                spotify_url=parsed.normalized_url,
                url_type=parsed.url_type.value,
                title=title,
                artist_name=artist_name,
            )
            results.append(download.to_dict())
        except Exception as e:
            errors.append({"url": url, "error": str(e)})
    
    db.commit()
    
    return {
        "queued": results,
        "errors": errors,
        "total_queued": len(results),
        "total_errors": len(errors),
    }


@router.post("/downloads/url/parse")
async def parse_url(url: str = Query(..., description="Spotify URL to parse")):
    """
    Parse a Spotify URL and return extracted information.
    Useful for validation before submitting a download.
    """
    parsed = parse_spotify_url(url)
    
    return {
        "is_valid": parsed.is_valid,
        "url_type": parsed.url_type.value,
        "spotify_id": parsed.spotify_id,
        "normalized_url": parsed.normalized_url,
        "uri": parsed.uri,
        "original_url": parsed.original_url,
    }


# ============================================================================
# Search Endpoints
# ============================================================================

# Plain `def`: blocking Spotify call, run in FastAPI's thread pool (not the event loop)
@router.get("/search/artists")
def search_artists_spotify(
    query: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50)
):
    """Search for artists on Spotify."""
    spotify = get_spotify_service()
    
    if not spotify.is_configured:
        raise HTTPException(status_code=503, detail="Spotify API not configured")
    
    results = spotify.search_artist(query, limit)
    return {"results": results}


# Plain `def`: blocking Spotify call, run in FastAPI's thread pool (not the event loop)
@router.get("/search/albums")
def search_albums_spotify(
    query: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50)
):
    """Search for albums on Spotify."""
    spotify = get_spotify_service()
    
    if not spotify.is_configured:
        raise HTTPException(status_code=503, detail="Spotify API not configured")
    
    results = spotify.search_album(query, limit)
    return {"results": results}


@router.get("/search/library")
async def search_library(
    query: str = Query(..., min_length=1),
    db: Session = Depends(get_db)
):
    """Search the local library."""
    artists = db.query(Artist).filter(
        Artist.name.ilike(f"%{query}%")
    ).limit(10).all()
    
    albums = db.query(Album).filter(
        Album.name.ilike(f"%{query}%")
    ).limit(10).all()
    
    return {
        "artists": [a.to_dict() for a in artists],
        "albums": [a.to_dict() for a in albums],
    }


# ============================================================================
# Track History
# ============================================================================

# Plain `def`: synchronous DB work, run in the thread pool
@router.get("/track-history")
def track_history(
    q: str = Query("", description="Search title, artists, album or playlist"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Every track the downloader has filed, kept after the files leave downloads/."""
    query = db.query(TrackHistory)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(TrackHistory.title.ilike(like), TrackHistory.artists.ilike(like),
                                 TrackHistory.album.ilike(like), TrackHistory.playlist.ilike(like)))
    total = query.count()
    rows = query.order_by(TrackHistory.last_seen_at.desc()).offset(offset).limit(limit).all()
    root = get_settings().download_path
    return {
        "total": total,
        "tracks": [{
            "spotify_id": r.spotify_id, "isrc": r.isrc, "title": r.title, "artists": r.artists,
            "album": r.album, "album_artist": r.album_artist, "year": r.year,
            "track_number": r.track_number, "playlist": r.playlist, "file_path": r.file_path,
            "size_bytes": r.size_bytes, "source": r.source, "download_id": r.download_id,
            "on_disk": bool(r.file_path) and os.path.exists(os.path.join(root, r.file_path)),
            "first_seen_at": r.first_seen_at.isoformat() + "Z" if r.first_seen_at else None,
            "last_seen_at": r.last_seen_at.isoformat() + "Z" if r.last_seen_at else None,
        } for r in rows],
    }


@router.get("/track-failures")
def skipped_tracks(db: Session = Depends(get_db)):
    """Tracks skipped after failing repeatedly with every provider in the chain."""
    from app.services import track_failures
    tracks = track_failures.skipped_tracks(db)
    for t in tracks:
        t["last_at"] = t["last_at"].isoformat() + "Z" if t["last_at"] else None
    return {"total": len(tracks), "tracks": tracks}


@router.delete("/track-failures")
def clear_all_track_failures(db: Session = Depends(get_db)):
    """Forget every recorded failure: skipped tracks are tried again on the next run."""
    from app.models import TrackFailure
    cleared = db.query(TrackFailure).delete(synchronize_session=False)
    db.commit()
    logger.info(f"Cleared all {cleared} track failure record(s)")
    return {"cleared": cleared}


class TrackHistoryDelete(BaseModel):
    ids: List[int] = []
    # Delete every row matching q instead of ids ("select all matching")
    all_matching: bool = False
    q: str = ""


@router.post("/track-history/delete")
def delete_track_history(body: TrackHistoryDelete, db: Session = Depends(get_db)):
    """Remove rows from the download history. A removed track whose file is no
    longer in downloads/ is downloaded again by the next job that has it."""
    query = db.query(TrackHistory)
    if body.all_matching:
        if body.q.strip():
            like = f"%{body.q.strip()}%"
            query = query.filter(or_(TrackHistory.title.ilike(like), TrackHistory.artists.ilike(like),
                                     TrackHistory.album.ilike(like), TrackHistory.playlist.ilike(like)))
    elif body.ids:
        query = query.filter(TrackHistory.id.in_(body.ids))
    else:
        return {"deleted": 0}
    deleted = query.delete(synchronize_session=False)
    db.commit()
    logger.info(f"Deleted {deleted} track history row(s)"
                + (f" matching '{body.q}'" if body.all_matching and body.q.strip() else
                   " (all)" if body.all_matching else ""))
    return {"deleted": deleted}


@router.post("/track-failures/{spotify_id}/reset")
def reset_track_failures(spotify_id: str, db: Session = Depends(get_db)):
    """Give a skipped track another try on the next run."""
    from app.services import track_failures
    cleared = track_failures.clear(db, [spotify_id])
    db.commit()
    return {"cleared": cleared}


# ============================================================================
# Break Monitor
# ============================================================================

@router.get("/break-status")
async def break_status():
    """Upstream download server break schedule (from the spotbye status page)."""
    return get_break_monitor().snapshot()


@router.get("/status")
async def service_status_api():
    """Current state of the download servers, the Spotify API and our own
    download-service rate limit."""
    return service_status.current()


@router.get("/provider-cooldowns")
def list_provider_cooldowns():
    """Providers the runner skips for now (e.g. qobuz after a rate-limited search)."""
    return {"cooldowns": service_status.provider_cooldowns()}


@router.delete("/provider-cooldowns")
def clear_all_provider_cooldowns():
    """Use every provider again from the next run."""
    try:
        cleared = service_status.clear_provider_cooldowns()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not update the cooldown file: {e}")
    logger.info(f"Cleared provider cooldowns: {', '.join(cleared) or 'none'}")
    return {"cleared": cleared}


@router.delete("/provider-cooldowns/{provider}")
def clear_provider_cooldown(provider: str):
    try:
        cleared = service_status.clear_provider_cooldowns(provider)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not update the cooldown file: {e}")
    if not cleared:
        raise HTTPException(status_code=404, detail=f"No cooldown for {provider}")
    logger.info(f"Cleared provider cooldown: {provider}")
    return {"cleared": cleared}


@router.get("/quota")
def quota_state():
    """The dynamic daily cap (QUOTA_MODE=dynamic): cap, ceiling, recorded
    penalties and the last changes."""
    from app.services import quota
    if get_settings().quota_mode != "dynamic":
        raise HTTPException(status_code=404, detail="QUOTA_MODE is not dynamic")
    return quota.summary()


@router.delete("/quota")
def quota_reset():
    """Forget the learned cap; it's seeded again from the penalty history."""
    from app.services import quota
    if get_settings().quota_mode != "dynamic":
        raise HTTPException(status_code=404, detail="QUOTA_MODE is not dynamic")
    quota.reset()
    state = quota.summary()
    logger.info(f"Dynamic quota reset: cap {state['cap']}")
    return state


@router.post("/library-check")
def library_check_now():
    """Run the library check now: remove .flac files that aren't FLAC
    (deleted, or moved to downloads/.rejected/ with lossy allowed)."""
    from app.services.library_check import run_check
    return run_check()


@router.get("/status/history")
async def service_status_history(hours: int = 24):
    """State changes in the last ``hours`` (max 30 days) plus each
    component's state at the start of the window."""
    return service_status.history(max(1, min(hours, 24 * service_status.KEEP_DAYS)))


# ============================================================================
# Settings Endpoints
# ============================================================================

@router.get("/settings")
async def get_settings_api():
    """Get application settings."""
    settings = get_settings()
    
    return {
        "spotify_configured": settings.spotify_client_id != "",
        "music_library_paths": settings.music_library_paths,
        "download_path": settings.download_path,
        "spotiflac_services": settings.spotiflac_services,
        "scan_interval_minutes": settings.scan_interval_minutes,
        "auto_scan_on_startup": settings.auto_scan_on_startup,
    }


@router.get("/settings/status")
async def get_system_status():
    """Get system status."""
    settings = get_settings()
    spotify = get_spotify_service()
    manager = get_download_manager()
    scanner = get_library_scanner()
    
    return {
        "spotify": {
            "configured": spotify.is_configured,
            "connected": spotify.is_connected,
        },
        "spotiflac": {
            "available": manager.is_available,
            "running": manager.is_running,
        },
        "scanner": {
            "scanning": scanner.is_scanning,
        },
        "paths": {
            "music_library": settings.music_library_paths,
            "download": settings.download_path,
        },
    }


# ============================================================================
# Server-Sent Events for Real-time Updates
# ============================================================================

@router.get("/events")
async def event_stream():
    """Server-Sent Events stream for real-time updates."""
    async def generate():
        scanner = get_library_scanner()
        manager = get_download_manager()
        
        # Add callbacks
        scan_queue = asyncio.Queue()
        download_queue = asyncio.Queue()
        
        def on_scan_progress(data):
            asyncio.get_event_loop().call_soon_threadsafe(
                scan_queue.put_nowait, data
            )
        
        def on_download_progress(data):
            asyncio.get_event_loop().call_soon_threadsafe(
                download_queue.put_nowait, data
            )
        
        scanner.add_progress_callback(on_scan_progress)
        manager.add_progress_callback(on_download_progress)
        
        try:
            while True:
                # Check scan queue
                try:
                    data = scan_queue.get_nowait()
                    yield f"event: scan\ndata: {json.dumps(data)}\n\n"
                except asyncio.QueueEmpty:
                    pass
                
                # Check download queue
                try:
                    data = download_queue.get_nowait()
                    yield f"event: download\ndata: {json.dumps(data)}\n\n"
                except asyncio.QueueEmpty:
                    pass
                
                # Heartbeat
                yield f"event: heartbeat\ndata: {{}}\n\n"
                
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
    
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )

# ============================================================================
# Discovery Settings & Actions
# ============================================================================

from typing import Dict, Any

class DiscoveryConfigRequest(BaseModel):
    categories: Dict[str, str]

class DiscoveryBlockRequest(BaseModel):
    playlist_id: str
    category: Optional[str] = None

@router.get("/discovery/config")
async def get_discovery_config():
    import os
    from pathlib import Path
    import json
    
    config_path = Path(os.environ.get("CURATED_PLAYLISTS_PATH", "/app/data/curated_playlists.json")).parent / "discovery_config.json"
    
    if not config_path.exists():
        return {"categories": DEFAULT_CATEGORIES.copy(), "blocklist": []}
        
    try:
        with open(config_path, "r") as f:
            return json.load(f)
    except Exception:
        return {"error": "Failed to read config"}

@router.post("/discovery/config")
async def update_discovery_config(request: DiscoveryConfigRequest, background_tasks: BackgroundTasks):
    import os
    import json
    import subprocess
    import sys
    from pathlib import Path
    
    # 1. Validation
    max_cats = int(os.environ.get("SPOTIFY_DISCOVERY_MAX_CATEGORIES", 15))
    if len(request.categories) > max_cats:
        raise HTTPException(status_code=400, detail=f"Maximum {max_cats} categories allowed.")
        
    cleaned_categories = {}
    for k, v in request.categories.items():
        k_clean = k.strip()
        v_clean = v.strip()
        if not k_clean or not v_clean:
            raise HTTPException(status_code=400, detail="Category names and search terms cannot be empty.")
        if len(k_clean) > 30 or len(v_clean) > 50:
            raise HTTPException(status_code=400, detail="Category names (max 30) or search terms (max 50) too long.")
        cleaned_categories[k_clean] = v_clean
        
    if not cleaned_categories:
        raise HTTPException(status_code=400, detail="At least one category is required.")
        
    # 2. Read existing blocklist
    config_path = Path(os.environ.get("CURATED_PLAYLISTS_PATH", "/app/data/curated_playlists.json")).parent / "discovery_config.json"
    blocklist = []
    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                blocklist = json.load(f).get("blocklist", [])
        except Exception:
            pass
            
    # 3. Save new config
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump({"categories": cleaned_categories, "blocklist": blocklist}, f, indent=2)
        
    # 4. Trigger background run
    script_path = Path(__file__).resolve().parent.parent / "services" / "update_discovery.py"
    if script_path.exists():
        async def run_script():
            try:
                # --force: changed categories need filling even inside the
                # minimum interval (cooldown and call budget still apply)
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(script_path), "--force",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await proc.communicate()
            except Exception as e:
                logger.error(f"Failed to spawn discovery config update: {e}")
                
        background_tasks.add_task(run_script)
        
    return {"status": "success", "categories": cleaned_categories}

# Global dict to store debounce timers for discovery updates
_discovery_debounce_timers = {}

@router.post("/discovery/block")
async def block_discovery_playlist(request: DiscoveryBlockRequest, background_tasks: BackgroundTasks):
    import os
    import json
    import subprocess
    import sys
    from pathlib import Path
    import asyncio
    
    json_path = Path(os.environ.get("CURATED_PLAYLISTS_PATH", "/app/data/curated_playlists.json"))
    config_path = json_path.parent / "discovery_config.json"
    
    # 1. Update config blocklist
    config = {"categories": {}, "blocklist": []}
    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                config = json.load(f)
        except Exception:
            pass
            
    if request.playlist_id not in config.get("blocklist", []):
        config.setdefault("blocklist", []).append(request.playlist_id)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
            
    # 2. Purge from current JSON cache immediately so UI reload doesn't show it
    if json_path.exists():
        try:
            with open(json_path, "r") as f:
                curated_data = json.load(f)
            
            modified = False
            for cat, entries in curated_data.items():
                new_entries = [e for e in entries if (e if isinstance(e, str) else e.get("id")) != request.playlist_id]
                if len(new_entries) != len(entries):
                    curated_data[cat] = new_entries
                    modified = True
            
            if modified:
                with open(json_path, "w") as f:
                    json.dump(curated_data, f, indent=2)
        except Exception as e:
            logger.error(f"Error purging blocked playlist from cache: {e}")
            
    # 3. Trigger scoped background run (Debounced by 3 seconds)
    script_path = Path(__file__).resolve().parent.parent / "services" / "update_discovery.py"
    if script_path.exists() and request.category:
        
        async def delayed_run(cat_name: str):
            try:
                await asyncio.sleep(3.0)
            except asyncio.CancelledError:
                return  # Cancelled during the initial sleep, completely safe
                
            retries = 10
            proc = None
            for attempt in range(retries):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable, str(script_path), "--category", cat_name,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    
                    stdout, stderr = await proc.communicate()
                    returncode = proc.returncode
                    proc = None  # Cleanly finished
                    
                    if returncode == 42:
                        logger.info(f"Discovery update for '{cat_name}' locked. Retrying in 5s (attempt {attempt+1}/{retries})")
                        await asyncio.sleep(5.0)
                        continue
                        
                    if returncode == 43:
                        logger.warning(f"Discovery update for '{cat_name}' skipped: Spotify API rate limit cooldown")
                    elif returncode != 0:
                        logger.error(f"Discovery update for '{cat_name}' failed with code {returncode}:\n{stderr.decode()}")
                    else:
                        logger.info(f"Discovery update for '{cat_name}' succeeded.")
                        
                    break # Success or non-lock failure, stop retrying
                    
                except asyncio.CancelledError:
                    if proc is not None and proc.returncode is None:
                        try:
                            proc.kill()
                            await proc.wait()
                        except Exception:
                            pass
                    raise
                except Exception as e:
                    logger.error(f"Failed to spawn discovery update for '{cat_name}': {e}")
                    break
            else:
                logger.error(f"Gave up refilling category '{cat_name}' after {retries} lock contention retries.")
                
            # Clean up the finished task reference
            _discovery_debounce_timers.pop(cat_name, None)
            
        # Cancel existing timer for this category if the user clicks rapidly
        if request.category in _discovery_debounce_timers:
            _discovery_debounce_timers[request.category].cancel()
            
        # Create a new debounced task
        task = asyncio.create_task(delayed_run(request.category))
        _discovery_debounce_timers[request.category] = task
        
    return {"status": "success", "blocked": request.playlist_id}
