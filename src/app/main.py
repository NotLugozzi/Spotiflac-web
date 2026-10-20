"""
Spotiflac web - Main Application Entry Point
"""

import logging
import time
import sys
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.database import init_db, close_db
from app.routes.api import router as api_router
from app.routes import pages as pages_module
from app.services.library_scanner import get_library_scanner
from app.services.download_manager import get_download_manager
from app.services.break_monitor import get_break_monitor
from app.services import service_status
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
import subprocess
import json

# Configure logging
def setup_logging():
    settings = get_settings()
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ]
    )
    
    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("watchdog").setLevel(logging.WARNING)
    # "Running job … / executed successfully" for every minutely poll
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    if settings.access_log_hide_polling:
        logging.getLogger("uvicorn.access").addFilter(PollingAccessFilter())


class PollingAccessFilter(logging.Filter):
    """Drops uvicorn access-log lines for successful requests the UI makes on
    a timer (HTMX partials every few seconds, the Docker health check, static
    files). Errors on those paths, and every other request, are still logged."""

    QUIET_PREFIXES = ("/partials/", "/health", "/static/", "/favicon.ico")

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access args: (client_addr, method, full_path, http_version, status_code)
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path, status = str(args[2]), args[4]
        try:
            ok = int(status) < 400
        except (TypeError, ValueError):
            return True
        return not (ok and path.startswith(self.QUIET_PREFIXES))


setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    settings = get_settings()
    
    logger.info(f"Starting {settings.app_name}...")
    
    # Run database migrations first
    try:
        from migrations.runner import run_migrations
        run_migrations()
    except Exception as e:
        logger.warning(f"Could not run migrations: {e}")
    
    # Initialize database
    init_db()
    logger.info("Database initialized")
    
    # Initialize services
    scanner = get_library_scanner()
    download_manager = get_download_manager()
    
    # Start download manager
    download_manager.start()
    logger.info("Download manager started")
    
    # Start file watcher
    scanner.start_watching()
    logger.info("File watcher started")
    
    # Auto-scan on startup if enabled
    if settings.auto_scan_on_startup:
        logger.info("Auto-scan on startup is enabled, starting initial scan...")
        try:
            # Run scan in background
            import threading
            scan_thread = threading.Thread(target=scanner.scan_library, daemon=True)
            scan_thread.start()
        except Exception as e:
            logger.error(f"Failed to start initial scan: {e}")
            
    # Setup background scheduler
    scheduler = BackgroundScheduler()
    
    def run_discovery_update(category: str = None):
        if not settings.spotify_client_id or not settings.spotify_client_secret:
            logger.info("Spotify API credentials not set, skipping discovery update.")
            return
            
        try:
            logger.info(f"Running background discovery update{f' for category {category}' if category else ''}...")
            
            # Run the discovery script
            script_path = Path(__file__).resolve().parent / "services" / "update_discovery.py"
                
            if script_path.exists():
                cmd = [sys.executable, str(script_path)]
                if category:
                    cmd.extend(["--category", category])
                    
                subprocess.run(cmd, check=True, cwd=str(script_path.parent))
                logger.info("Background discovery update completed successfully.")
            else:
                logger.warning(f"Discovery script not found at {script_path}")
        except subprocess.CalledProcessError as e:
            # Exit codes defined in update_discovery.py
            if e.returncode == 42:
                logger.info("Discovery update skipped: another run is in progress")
            elif e.returncode == 43:
                logger.warning("Discovery update skipped: Spotify API rate limit cooldown (see update_discovery log above)")
            else:
                logger.error(f"Discovery update failed with exit code {e.returncode}")
        except Exception as e:
            logger.error(f"Discovery update failed: {e}")
            
    # Daily job; update_discovery.py also enforces DISCOVERY_MIN_INTERVAL_HOURS
    scheduler.add_job(run_discovery_update, 'interval', days=1)

    # Poll the upstream break schedule; first poll runs right away
    break_monitor = get_break_monitor()
    scheduler.add_job(
        break_monitor.poll, 'interval',
        seconds=max(15, settings.break_poll_seconds),
        next_run_time=datetime.now(),
    )
    # Around a predicted break start: poll every 10 s to confirm it
    scheduler.add_job(break_monitor.poll_near_break, 'interval', seconds=10)
    # Record state changes for the Status page (after the first break poll)
    scheduler.add_job(
        service_status.record, 'interval', seconds=60,
        next_run_time=datetime.now() + timedelta(seconds=20),
    )
    # Library check (non-FLAC .flac files); first run 5 min after start
    if settings.library_check_hours > 0:
        from app.services.library_check import run_check
        scheduler.add_job(
            run_check, 'interval', hours=settings.library_check_hours,
            next_run_time=datetime.now() + timedelta(minutes=5),
        )
    scheduler.start()
    logger.info("Background scheduler started")
    
    # Run once on startup if the file is missing or has the placeholder
    try:
        json_path = Path(os.environ.get("CURATED_PLAYLISTS_PATH", "/app/data/curated_playlists.json"))
        
        needs_update = False
        if not json_path.exists():
            logger.info("Curated playlists JSON missing. Will run initial discovery update...")
            needs_update = True
        else:
            try:
                import json
                with open(json_path, 'r') as f:
                    content = f.read()
                    
                is_empty = False
                try:
                    parsed = json.loads(content) if content.strip() else {}
                    is_empty = not parsed or not any(v for v in parsed.values() if isinstance(v, list) and v)
                except json.JSONDecodeError:
                    is_empty = True  # Malformed JSON should be rebuilt
                    
                if is_empty:
                    logger.info("Curated playlists JSON is empty or malformed. Will run initial discovery update...")
                    needs_update = True
                elif "YOUR_USER_OWNED_PLAYLIST_ID_HERE" in content:
                    logger.info("Placeholder playlists detected. Will run initial discovery update...")
                    needs_update = True
            except Exception as e:
                logger.warning(f"Curated playlists JSON is unreadable ({e}). Will run initial discovery update...")
                needs_update = True
                
        if needs_update and settings.spotify_client_id:
            import threading
            threading.Thread(target=run_discovery_update, daemon=True).start()
    except Exception as e:
        logger.error(f"Failed to check curated playlists: {e}")
    
    logger.info(f"{settings.app_name} is ready!")
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    
    scheduler.shutdown()
    scanner.stop_watching()
    download_manager.stop()
    close_db()
    
    logger.info("Shutdown complete")


# Create FastAPI application
app = FastAPI(
    title="Spotiflac web",
    description="Music library manager with SpotiFLAC integration",
    version="1.0.0",
    lifespan=lifespan,
)

# Authentication middleware class
class AuthMiddleware(BaseHTTPMiddleware):
    """Require authentication if enabled."""
    
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        
        # Skip authentication if not configured
        if not settings.auth_enabled:
            return await call_next(request)
        
        # Allow health check and static assets (favicon on the login page) without auth
        if request.url.path in ("/health", "/favicon.ico") or request.url.path.startswith("/static/"):
            return await call_next(request)
        
        # Allow login page and auth endpoints
        if request.url.path in ["/login", "/api/login", "/api/logout"]:
            return await call_next(request)
        
        # Check if user is authenticated
        user = request.session.get("user")
        if not user:
            # Redirect to login for page requests
            if not request.url.path.startswith("/api/"):
                return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
            # Return 401 for API requests
            else:
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "Not authenticated"}
                )
        
        return await call_next(request)

# Add middleware in reverse order of execution
# Middleware added LAST runs FIRST in the request processing chain

# CORS middleware (runs first)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Authentication middleware (runs second)
app.add_middleware(AuthMiddleware)

# Session middleware (runs last, but must be available to auth middleware)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

# Setup templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))


def break_countdown():
    """For the queue: when the running job stops for the next break, or None."""
    from app.services.break_monitor import format_clock, format_seconds
    monitor = get_break_monitor()
    services = settings.spotiflac_services
    stop_at = monitor.stop_at(services)
    if not (stop_at and settings.break_gate_downloads and settings.break_pause_running):
        return None
    return {"stop_at": format_clock(stop_at), "break_at": format_clock(monitor.break_at(services)),
            "in": format_seconds(stop_at - time.time())}


templates.env.globals["break_countdown"] = break_countdown


def daily_cap():
    """For the queue: downloads in the last 24 h / hour against the caps, or None."""
    from app.services.download_manager import daily_cap_state
    state = daily_cap_state()
    return state if state["cap"] or state["hour_cap"] else None


templates.env.globals["daily_cap"] = daily_cap

# Make templates available to pages module
pages_module.templates = templates

# Include routers
app.include_router(api_router)
app.include_router(pages_module.router)

# Static files (favicon)
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Browsers request this path on their own; the icon itself is an SVG
    return RedirectResponse(url="/static/favicon.svg", status_code=status.HTTP_301_MOVED_PERMANENTLY)


# Error handlers
@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    """Handle 404 errors."""
    if request.url.path.startswith("/api/"):
        # A plain dict isn't a response here: every API 404 used to become a 500
        return JSONResponse({"detail": getattr(exc, "detail", None) or "Not found"}, status_code=404)
    
    return templates.TemplateResponse(
        "pages/404.html",
        {"request": request},
        status_code=404
    )


@app.exception_handler(500)
async def server_error_handler(request: Request, exc: Exception):
    """Handle 500 errors."""
    logger.error(f"Server error: {exc}")
    
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Internal server error"}, status_code=500)
    
    return templates.TemplateResponse(
        "pages/500.html",
        {"request": request},
        status_code=500
    )


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint for Docker/Kubernetes."""
    return {"status": "healthy", "version": "1.0.0"}


def main():
    """Run the application."""
    import uvicorn
    
    settings = get_settings()
    
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
