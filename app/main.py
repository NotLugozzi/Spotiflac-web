"""
Spotiflac web - Main Application Entry Point
"""

import logging
import sys
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

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    settings = get_settings()
    
    logger.info(f"Starting {settings.app_name}...")
    
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
    
    logger.info(f"{settings.app_name} is ready!")
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    
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
        
        # Allow health check without auth
        if request.url.path == "/health":
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

# Make templates available to pages module
pages_module.templates = templates

# Include routers
app.include_router(api_router)
app.include_router(pages_module.router)

# Static files (if we add any later)
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# Error handlers
@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    """Handle 404 errors."""
    if request.url.path.startswith("/api/"):
        return {"detail": "Not found"}
    
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
        return {"detail": "Internal server error"}
    
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
