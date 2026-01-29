"""
Spotiflac web - API Routes Package
"""

from app.routes.api import router as api_router
from app.routes.pages import router as pages_router

__all__ = ["api_router", "pages_router"]
