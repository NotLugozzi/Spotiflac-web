"""
Spotiflac web - Page Routes (HTML Templates)
"""

from fastapi import APIRouter, Depends, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.models import Artist, Album, Download, DownloadStatus
from app.services.spotify_service import get_spotify_service
from app.services.library_scanner import get_library_scanner
from app.services.download_manager import get_download_manager
from app.config import get_settings

router = APIRouter(tags=["Pages"])

# Templates will be configured in main.py
templates: Optional[Jinja2Templates] = None


def get_templates() -> Jinja2Templates:
    """Get the templates instance."""
    if templates is None:
        raise RuntimeError("Templates not configured")
    return templates


# ============================================================================
# Page Routes
# ============================================================================

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None):
    """Login page."""
    settings = get_settings()
    
    # Redirect to home if auth is not enabled
    if not settings.auth_enabled:
        return RedirectResponse(url="/", status_code=303)
    
    # Redirect to home if already logged in
    if request.session.get("user"):
        return RedirectResponse(url="/", status_code=303)
    
    return get_templates().TemplateResponse(
        "pages/login.html",
        {"request": request, "error": error}
    )


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    """Home page / Dashboard."""
    # Get stats
    artist_count = db.query(Artist).count()
    album_count = db.query(Album).count()
    owned_count = db.query(Album).filter(Album.is_owned == True).count()
    wanted_count = db.query(Album).filter(Album.is_wanted == True).count()
    
    # Get recent albums
    recent_albums = db.query(Album).filter(
        Album.is_owned == True
    ).order_by(Album.created_at.desc()).limit(8).all()
    
    # Get active downloads
    active_downloads = db.query(Download).filter(
        Download.status.in_([DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING])
    ).limit(5).all()
    
    # Get system status
    spotify = get_spotify_service()
    manager = get_download_manager()
    scanner = get_library_scanner()
    
    return get_templates().TemplateResponse(
        "pages/dashboard.html",
        {
            "request": request,
            "stats": {
                "artists": artist_count,
                "albums": album_count,
                "owned": owned_count,
                "wanted": wanted_count,
            },
            "recent_albums": recent_albums,
            "active_downloads": active_downloads,
            "status": {
                "spotify_connected": spotify.is_connected,
                "spotiflac_available": manager.is_available,
                "scanning": scanner.is_scanning,
            },
        }
    )


@router.get("/artists", response_class=HTMLResponse)
async def artists_page(
    request: Request,
    search: Optional[str] = None,
    page: int = 1,
    db: Session = Depends(get_db)
):
    """Artists listing page."""
    per_page = 24
    
    query = db.query(Artist)
    
    if search:
        query = query.filter(Artist.name.ilike(f"%{search}%"))
    
    total = query.count()
    artists = query.order_by(Artist.name).offset(
        (page - 1) * per_page
    ).limit(per_page).all()
    
    return get_templates().TemplateResponse(
        "pages/artists.html",
        {
            "request": request,
            "artists": artists,
            "search": search,
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page,
        }
    )


@router.get("/artists/{artist_id}", response_class=HTMLResponse)
async def artist_detail_page(
    request: Request,
    artist_id: int,
    db: Session = Depends(get_db)
):
    """Artist detail page with discography."""
    artist = db.query(Artist).filter(Artist.id == artist_id).first()
    
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    
    # Get albums grouped by type (excluding compilations)
    albums = db.query(Album).filter(
        Album.artist_id == artist_id,
        Album.album_type != "compilation"
    ).order_by(Album.release_date.desc()).all()
    
    albums_by_type = {
        "album": [],
        "single": [],
        "compilation": [],
        "other": [],
    }
    
    for album in albums:
        album_type = album.album_type or "other"
        if album_type in albums_by_type:
            albums_by_type[album_type].append(album)
        else:
            albums_by_type["other"].append(album)
    
    return get_templates().TemplateResponse(
        "pages/artist_detail.html",
        {
            "request": request,
            "artist": artist,
            "albums_by_type": albums_by_type,
            "total_albums": len(albums),
            "owned_albums": sum(1 for a in albums if a.is_owned),
        }
    )


@router.get("/albums", response_class=HTMLResponse)
async def albums_page(
    request: Request,
    search: Optional[str] = None,
    filter: Optional[str] = None,  # owned, wanted, missing
    page: int = 1,
    db: Session = Depends(get_db)
):
    """Albums listing page."""
    per_page = 24
    
    query = db.query(Album)
    
    # Exclude compilations
    query = query.filter(Album.album_type != "compilation")
    
    if search:
        query = query.filter(Album.name.ilike(f"%{search}%"))
    
    if filter == "owned":
        query = query.filter(Album.is_owned == True)
    elif filter == "wanted":
        query = query.filter(Album.is_wanted == True)
    elif filter == "missing":
        query = query.filter(Album.is_owned == False)
    
    total = query.count()
    albums = query.order_by(Album.release_date.desc()).offset(
        (page - 1) * per_page
    ).limit(per_page).all()
    
    return get_templates().TemplateResponse(
        "pages/albums.html",
        {
            "request": request,
            "albums": albums,
            "search": search,
            "filter": filter,
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page,
        }
    )


@router.get("/albums/{album_id}", response_class=HTMLResponse)
async def album_detail_page(
    request: Request,
    album_id: int,
    db: Session = Depends(get_db)
):
    """Album detail page with tracks."""
    album = db.query(Album).filter(Album.id == album_id).first()
    
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    
    # Get download status
    download = db.query(Download).filter(
        Download.album_id == album_id
    ).order_by(Download.created_at.desc()).first()
    
    return get_templates().TemplateResponse(
        "pages/album_detail.html",
        {
            "request": request,
            "album": album,
            "artist": album.artist,
            "tracks": sorted(album.tracks, key=lambda t: (t.disc_number or 1, t.track_number or 0)),
            "download": download,
        }
    )


@router.get("/downloads", response_class=HTMLResponse)
async def downloads_page(request: Request, db: Session = Depends(get_db)):
    """Downloads queue and history page."""
    # Get active downloads
    active = db.query(Download).filter(
        Download.status.in_([
            DownloadStatus.PENDING,
            DownloadStatus.QUEUED,
            DownloadStatus.DOWNLOADING
        ])
    ).order_by(Download.created_at).all()
    
    # Get history
    history = db.query(Download).filter(
        Download.status.in_([
            DownloadStatus.COMPLETED,
            DownloadStatus.FAILED,
            DownloadStatus.CANCELLED
        ])
    ).order_by(Download.completed_at.desc()).limit(50).all()
    
    manager = get_download_manager()
    
    return get_templates().TemplateResponse(
        "pages/downloads.html",
        {
            "request": request,
            "active_downloads": active,
            "history": history,
            "spotiflac_available": manager.is_available,
        }
    )


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Search page."""
    results = {
        "artists": [],
        "albums": [],
        "spotify_artists": [],
        "spotify_albums": [],
    }
    
    spotify = get_spotify_service()
    spotify_configured = spotify.is_configured
    
    if q:
        # Search local library
        results["artists"] = db.query(Artist).filter(
            Artist.name.ilike(f"%{q}%")
        ).limit(10).all()
        
        results["albums"] = db.query(Album).filter(
            Album.name.ilike(f"%{q}%")
        ).limit(10).all()
        
        # Search Spotify (only if configured)
        if spotify_configured:
            results["spotify_artists"] = spotify.search_artist(q, limit=5)
            results["spotify_albums"] = spotify.search_album(q, limit=5)
    
    return get_templates().TemplateResponse(
        "pages/search.html",
        {
            "request": request,
            "query": q,
            "results": results,
            "spotify_configured": spotify_configured,
        }
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page."""
    settings = get_settings()
    spotify = get_spotify_service()
    manager = get_download_manager()
    
    return get_templates().TemplateResponse(
        "pages/settings.html",
        {
            "request": request,
            "settings": settings,
            "spotify_configured": spotify.is_configured,
            "spotify_connected": spotify.is_connected,
            "spotiflac_available": manager.is_available,
        }
    )


# ============================================================================
# HTMX Partial Routes
# ============================================================================

@router.get("/partials/artist-card/{artist_id}", response_class=HTMLResponse)
async def artist_card_partial(
    request: Request,
    artist_id: int,
    db: Session = Depends(get_db)
):
    """Partial for artist card."""
    artist = db.query(Artist).filter(Artist.id == artist_id).first()
    
    if not artist:
        raise HTTPException(status_code=404, detail="Artist not found")
    
    return get_templates().TemplateResponse(
        "partials/artist_card.html",
        {"request": request, "artist": artist}
    )


@router.get("/partials/album-card/{album_id}", response_class=HTMLResponse)
async def album_card_partial(
    request: Request,
    album_id: int,
    db: Session = Depends(get_db)
):
    """Partial for album card."""
    album = db.query(Album).filter(Album.id == album_id).first()
    
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    
    return get_templates().TemplateResponse(
        "partials/album_card.html",
        {"request": request, "album": album}
    )


@router.get("/partials/download-item/{download_id}", response_class=HTMLResponse)
async def download_item_partial(
    request: Request,
    download_id: int,
    db: Session = Depends(get_db)
):
    """Partial for download queue item."""
    download = db.query(Download).filter(Download.id == download_id).first()
    
    if not download:
        raise HTTPException(status_code=404, detail="Download not found")
    
    return get_templates().TemplateResponse(
        "partials/download_item.html",
        {"request": request, "download": download}
    )


@router.get("/partials/scan-status", response_class=HTMLResponse)
async def scan_status_partial(request: Request):
    """Partial for scan status."""
    scanner = get_library_scanner()
    
    return get_templates().TemplateResponse(
        "partials/scan_status.html",
        {"request": request, "progress": scanner.scan_progress}
    )


@router.get("/partials/download-queue", response_class=HTMLResponse)
async def download_queue_partial(
    request: Request,
    db: Session = Depends(get_db)
):
    """Partial for download queue - returns all active downloads."""
    downloads = db.query(Download).filter(
        Download.status.in_([
            DownloadStatus.PENDING,
            DownloadStatus.QUEUED,
            DownloadStatus.DOWNLOADING
        ])
    ).order_by(Download.created_at.desc()).all()
    
    return get_templates().TemplateResponse(
        "partials/download_queue.html",
        {"request": request, "downloads": downloads}
    )


@router.get("/partials/active-downloads", response_class=HTMLResponse)
async def active_downloads_partial(
    request: Request,
    db: Session = Depends(get_db)
):
    """Partial for dashboard active downloads."""
    downloads = db.query(Download).filter(
        Download.status.in_([
            DownloadStatus.PENDING,
            DownloadStatus.QUEUED,
            DownloadStatus.DOWNLOADING
        ])
    ).order_by(Download.created_at.desc()).limit(5).all()
    
    return get_templates().TemplateResponse(
        "partials/active_downloads.html",
        {"request": request, "downloads": downloads}
    )
