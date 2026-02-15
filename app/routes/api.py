"""
Spotiflac web - API Routes
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Form, Request
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse
from sqlalchemy.orm import Session
from typing import Optional, List
from pydantic import BaseModel, model_validator
import asyncio
import json
import logging

from app.database import get_db
from app.models import Artist, Album, Track, Download, DownloadStatus, LibraryScan
from app.services.spotify_service import get_spotify_service
from app.services.library_scanner import get_library_scanner
from app.services.download_manager import get_download_manager
from app.services.metadata_service import get_metadata_service
from app.services.url_parser import (
    parse_spotify_url, 
    parse_multiple_urls, 
    is_valid_spotify_url,
    SpotifyUrlType
)
from app.config import get_settings

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
    if username == settings.auth_username and password == settings.auth_password:
        # Set session
        request.session["user"] = username
        return RedirectResponse(url="/", status_code=303)
    
    # Failed login - redirect back to login with error
    return RedirectResponse(url="/login?error=Invalid+credentials", status_code=303)


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


@router.post("/artists/{artist_id}/sync")
async def sync_artist(
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


@router.post("/albums/{album_id}/refresh")
async def refresh_album_from_spotify(
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


@router.post("/downloads")
async def create_download(
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

@router.post("/downloads/url")
async def download_from_url(
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
async def download_from_urls(
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

@router.get("/search/artists")
async def search_artists_spotify(
    query: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50)
):
    """Search for artists on Spotify."""
    spotify = get_spotify_service()
    
    if not spotify.is_configured:
        raise HTTPException(status_code=503, detail="Spotify API not configured")
    
    results = spotify.search_artist(query, limit)
    return {"results": results}


@router.get("/search/albums")
async def search_albums_spotify(
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
