"""
Spotiflac web - Configuration Module
"""

from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List, Optional
from functools import lru_cache
import os


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Application
    app_name: str = Field(default="Spotiflac web")
    app_env: str = Field(default="production")
    debug: bool = Field(default=False)
    secret_key: str = Field(default="change-me-in-production")
    
    # Server
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8080)
    
    # Database
    database_url: str = Field(default="sqlite:///./data/spotiflac.db")
    
    # Spotify API (Optional - only needed for automatic matching)
    spotify_client_id: str = Field(default="")
    spotify_client_secret: str = Field(default="")
    
    @property
    def spotify_configured(self) -> bool:
        """Check if Spotify API credentials are configured."""
        return bool(self.spotify_client_id and self.spotify_client_secret)
    
    # Paths
    music_library_path: str = Field(default="/music")
    download_path: str = Field(default="/downloads")
    
    # SpotiFLAC
    spotiflac_service: str = Field(default="tidal,qobuz,deezer")
    spotiflac_retry_minutes: int = Field(default=30)
    
    # Authentication
    auth_username: Optional[str] = Field(default=None)
    auth_password: Optional[str] = Field(default=None)
    
    # Scanning
    scan_interval_minutes: int = Field(default=60)
    auto_scan_on_startup: bool = Field(default=True)
    
    # Logging
    log_level: str = Field(default="INFO")
    
    @property
    def music_library_paths(self) -> List[str]:
        """Parse comma-separated music library paths."""
        return [p.strip() for p in self.music_library_path.split(",") if p.strip()]
    
    @property
    def spotiflac_services(self) -> List[str]:
        """Parse comma-separated services."""
        return [s.strip() for s in self.spotiflac_service.split(",") if s.strip()]
    
    @property
    def auth_enabled(self) -> bool:
        """Check if authentication is enabled."""
        return bool(self.auth_username and self.auth_password)
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
