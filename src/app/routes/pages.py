"""
Spotiflac web - Page Routes (HTML Templates)
"""

from fastapi import APIRouter, Depends, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session
from typing import Optional
import os
from datetime import datetime

from app.database import get_db
from app.models import Artist, Album, Download, DownloadStatus, TrackHistory
from app.services.spotify_service import get_spotify_service
from app.services.library_scanner import get_library_scanner
from app.services.download_manager import get_download_manager
from app.services.break_monitor import get_break_monitor
from app.services import service_status
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


def _active_downloads(db: Session, limit: int = 5):
    """Queued/running jobs for the dashboard card, current one first."""
    return _queue(db)[0][:limit]


def _dashboard_stats(db: Session) -> dict:
    return {
        "stats": {
            "artists": db.query(Artist).count(),
            "albums": db.query(Album).count(),
            "owned": db.query(Album).filter(Album.is_owned == True).count(),
            "wanted": db.query(Album).filter(Album.is_wanted == True).count(),
        },
        "status": {
            "spotify_connected": get_spotify_service().is_connected,
            "spotiflac_available": get_download_manager().is_available,
            "scanning": get_library_scanner().is_scanning,
        },
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    """Home page / Dashboard."""

    # Get recent albums
    recent_albums = db.query(Album).filter(
        Album.is_owned == True
    ).order_by(Album.created_at.desc()).limit(8).all()
    
    return get_templates().TemplateResponse(
        "pages/dashboard.html",
        {
            "request": request,
            **_dashboard_stats(db),
            "recent_albums": recent_albums,
            "active_downloads": _active_downloads(db),
        }
    )


@router.get("/partials/dashboard-stats", response_class=HTMLResponse)
async def dashboard_stats_partial(request: Request, db: Session = Depends(get_db)):
    """Dashboard stats grid, plus the header badges out of band."""
    return get_templates().TemplateResponse(
        "partials/dashboard_stats.html",
        {"request": request, **_dashboard_stats(db), "oob": True},
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
    """Downloads queue and history page (both refresh themselves)."""
    active, current_id = _queue(db)
    return get_templates().TemplateResponse(
        "pages/downloads.html",
        {
            "request": request,
            "active_downloads": active,
            "current_id": current_id,
            "history": _history(db),
            "spotiflac_available": get_download_manager().is_available,
        }
    )


def _queue(db: Session):
    """Active downloads in the order they run: the current job (running or
    waiting) first, then by "Download next" priority, then oldest first."""
    downloads = db.query(Download).filter(
        Download.status.in_([DownloadStatus.PENDING, DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING])
    ).order_by(Download.priority.desc(), Download.created_at.asc()).all()
    current = get_download_manager()._current_download
    current_id = current.download_id if current else None
    downloads.sort(key=lambda d: d.id != current_id)  # stable: keeps the rest in order
    return downloads, current_id


def _history(db: Session):
    return db.query(Download).filter(
        Download.status.in_([DownloadStatus.COMPLETED, DownloadStatus.FAILED, DownloadStatus.CANCELLED])
    ).order_by(Download.completed_at.desc()).limit(50).all()


@router.get("/partials/download-history", response_class=HTMLResponse)
async def download_history_partial(request: Request, db: Session = Depends(get_db)):
    """Downloads page history card (finished jobs)."""
    return get_templates().TemplateResponse(
        "partials/download_history.html", {"request": request, "history": _history(db)}
    )


# Plain `def`: blocking Spotify call, run in FastAPI's thread pool (not the event loop)
@router.get("/search", response_class=HTMLResponse)
def search_page(
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


# Plain `def`: synchronous DB work, run in the thread pool
@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    """Settings page."""
    from app.models import TrackFailure
    from app.services import track_failures
    settings = get_settings()
    spotify = get_spotify_service()
    manager = get_download_manager()
    failure_rows = db.query(TrackFailure).count()
    failure_tracks = db.query(TrackFailure.spotify_id).distinct().count()
    skipped = len(track_failures.skipped_tracks(db))
    history_total = db.query(TrackHistory).count()
    
    return get_templates().TemplateResponse(
        "pages/settings.html",
        {
            "request": request,
            "settings": settings,
            "spotify_configured": spotify.is_configured,
            "spotify_connected": spotify.is_connected,
            "spotiflac_available": manager.is_available,
            "failure_rows": failure_rows,
            "failure_tracks": failure_tracks,
            "skipped_total": skipped,
            "history_total": history_total,
            "cooldowns": service_status.provider_cooldowns(),
            "quota": quota_summary(),
        }
    )


def quota_summary():
    """Dynamic cap for the Maintenance card, or None when QUOTA_MODE is fixed."""
    if get_settings().quota_mode != "dynamic":
        return None
    from app.services import quota
    from app.services.break_monitor import format_utc
    state = quota.summary()
    state["next_step"] = format_utc(state["next_step_at"])
    return state


@router.get("/partials/track-history-editor", response_class=HTMLResponse)
def track_history_editor(
    request: Request,
    q: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    """Settings: track history rows with checkboxes, for deleting entries."""
    per_page = 100
    page = max(1, page)
    q = q.strip()
    query = db.query(TrackHistory)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(TrackHistory.title.ilike(like), TrackHistory.artists.ilike(like),
                                 TrackHistory.album.ilike(like), TrackHistory.playlist.ilike(like)))
    total = query.count()
    rows = query.order_by(TrackHistory.last_seen_at.desc(), TrackHistory.id.desc()) \
        .offset((page - 1) * per_page).limit(per_page).all()
    root = get_settings().download_path
    return get_templates().TemplateResponse(
        "partials/track_history_editor.html",
        {
            "request": request, "q": q, "page": page, "total": total,
            "pages": max(1, -(-total // per_page)),
            "rows": [{"row": r, "on_disk": bool(r.file_path) and os.path.exists(os.path.join(root, r.file_path))}
                     for r in rows],
        },
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


# Plain `def`: synchronous DB and filesystem work, run in the thread pool
@router.get("/partials/track-history", response_class=HTMLResponse)
def track_history_partial(
    request: Request,
    q: str = "",
    page: int = 1,
    show: str = "all",
    db: Session = Depends(get_db),
):
    """Per-song download history, kept even after files leave downloads/,
    plus tracks skipped after failing repeatedly with every provider.
    show: all | downloaded | skipped."""
    from app.services import track_failures
    per_page = 50
    page = max(1, page)
    show = show if show in ("all", "downloaded", "skipped") else "all"
    q = q.strip()

    skipped = track_failures.skipped_tracks(db)
    skipped_total = len(skipped)
    if q:
        needle = q.lower()
        skipped = [t for t in skipped if any(needle in (t[k] or "").lower()
                                             for k in ("title", "artists", "album", "playlist"))]

    query = db.query(TrackHistory)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            TrackHistory.title.ilike(like),
            TrackHistory.artists.ilike(like),
            TrackHistory.album.ilike(like),
            TrackHistory.playlist.ilike(like),
        ))
    root = get_settings().download_path

    def downloaded_items(rows):
        return [{"row": row, "when": row.last_seen_at or datetime.min,
                 "on_disk": bool(row.file_path) and os.path.exists(os.path.join(root, row.file_path))}
                for row in rows]

    def skipped_items(tracks):
        return [{"skipped": t, "when": t["last_at"]} for t in tracks]

    ordered = query.order_by(TrackHistory.last_seen_at.desc(), TrackHistory.id.desc())
    offset = (page - 1) * per_page
    if show == "skipped":
        total = len(skipped)
        tracks = skipped_items(skipped[offset:offset + per_page])
    elif show == "downloaded":
        total = query.count()
        tracks = downloaded_items(ordered.offset(offset).limit(per_page).all())
    else:
        # Both, newest first: the first offset+per_page of each, merged
        total = query.count() + len(skipped)
        merged = downloaded_items(ordered.limit(offset + per_page).all()) + \
            skipped_items(skipped[:offset + per_page])
        merged.sort(key=lambda item: item["when"], reverse=True)
        tracks = merged[offset:offset + per_page]
    return get_templates().TemplateResponse(
        "partials/track_history.html",
        {
            "request": request, "tracks": tracks, "q": q, "page": page, "show": show,
            "total": total, "pages": max(1, -(-total // per_page)),
            "all_total": db.query(TrackHistory).count(),
            "skipped_total": skipped_total,
        },
    )


@router.get("/partials/break-status", response_class=HTMLResponse)
async def break_status_partial(request: Request, db: Session = Depends(get_db)):
    """Top-bar pill for the upstream break schedule."""
    queued = db.query(Download).filter(Download.status == DownloadStatus.QUEUED).count()
    return get_templates().TemplateResponse(
        "partials/break_status.html",
        {"request": request, "brk": get_break_monitor().snapshot(), "queued": queued,
         "ours": service_status.current_ours(), "cooldowns": service_status.provider_cooldowns()}
    )


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    """Status: download servers, Spotify API and our own rate limit."""
    return get_templates().TemplateResponse(
        "pages/status.html", {"request": request}
    )


@router.get("/partials/status-summary", response_class=HTMLResponse)
async def status_summary_partial(request: Request):
    """Status page cards: current state and 24 h summary per component."""
    return get_templates().TemplateResponse(
        "partials/status_summary.html",
        {"request": request, "status": service_status.current(),
         "summary": service_status.summary(24)},
    )


@router.get("/partials/download-queue", response_class=HTMLResponse)
async def download_queue_partial(
    request: Request,
    db: Session = Depends(get_db)
):
    """Partial for the download queue (see _queue), plus the header's item
    count out of band."""
    downloads, current_id = _queue(db)
    return get_templates().TemplateResponse(
        "partials/download_queue.html",
        {"request": request, "downloads": downloads, "current_id": current_id, "oob": True}
    )


@router.get("/partials/active-downloads", response_class=HTMLResponse)
async def active_downloads_partial(
    request: Request,
    db: Session = Depends(get_db)
):
    """Dashboard "Active Downloads" card (hidden when nothing is queued)."""
    return get_templates().TemplateResponse(
        "partials/active_downloads_card.html",
        {"request": request, "active_downloads": _active_downloads(db)}
    )


@router.get("/discovery", response_class=HTMLResponse)
async def discovery_page(
    request: Request,
    db: Session = Depends(get_db)
):
    """Discovery main page."""
    spotify = get_spotify_service()
    categories = spotify.get_curated_categories()
    
    # Get playlists for the first category by default
    active_category = categories[0] if categories else None
    playlists = spotify.get_curated_playlists(active_category) if active_category else []
    
    return get_templates().TemplateResponse(
        "pages/discovery.html",
        {
            "request": request,
            "categories": categories,
            "active_category": active_category,
            "playlists": playlists,
            "spotify_configured": spotify.is_configured,
            "max_categories": int(os.environ.get("SPOTIFY_DISCOVERY_MAX_CATEGORIES", 15))
        }
    )

@router.get("/partials/discovery/category/{category_name}", response_class=HTMLResponse)
async def discovery_category_partial(
    request: Request,
    category_name: str
):
    """Partial for a specific discovery category grid."""
    spotify = get_spotify_service()
    
    import urllib.parse
    category_name = urllib.parse.unquote(category_name)
    playlists = spotify.get_curated_playlists(category_name)
    
    return get_templates().TemplateResponse(
        "partials/discovery_grid.html",
        {
            "request": request,
            "playlists": playlists,
            "active_category": category_name
        }
    )
