"""
Spotiflac web - Configuration Module
"""

from pydantic_settings import BaseSettings
from pydantic import Field
from typing import List, Optional
from functools import lru_cache
import os


# Providers that can't deliver FLAC (apple-music is metadata only)
LOSSY_SERVICES = {"ext:ytmusic-spotiflac", "ext:soundcloud", "ext:apple-music"}


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
    # Optional second Spotify app, used for an endpoint while the primary is rate limited
    spotify_client_id_backup: str = Field(default="")
    spotify_client_secret_backup: str = Field(default="")
    
    @property
    def spotify_configured(self) -> bool:
        """Check if Spotify API credentials are configured."""
        return bool(self.spotify_client_id and self.spotify_client_secret)
    
    # Paths
    music_library_path: str = Field(default="/music")
    download_path: str = Field(default="/app/downloads")
    
    # SpotiFLAC
    # FLAC providers, tried in order (Tidal first). They share one download
    # service (zarz-v2), so a rate limit there pauses the whole job.
    # qobuz-web always goes last: it searches through that service too.
    spotiflac_service: str = Field(default="ext:tidal-web,ext:amazon,ext:qobuz-web")
    # After a qobuz request is rate limited, skip qobuz for N hours instead of
    # waiting and retrying (read by scripts/spotiflac_runner.py)
    qobuz_cooldown_hours: float = Field(default=24)
    # After N VERIFY_REQUIRED (HTTP 428) refusals in one run, stop and pause
    # N minutes like a rate limit; 0 = off (read by the runner)
    verify_stop_count: int = Field(default=5)
    verify_stop_minutes: float = Field(default=30)
    # Tracks SpotiFLAC downloads at once (its default 2; 1 is gentler)
    max_concurrent_downloads: int = Field(default=2)
    # Opt-in: allow lossy providers (ytmusic, soundcloud) as a last resort
    allow_lossy_fallback: bool = Field(default=False)
    # Opt-in: accept a provider's file whose ISRC differs from Spotify's when
    # title, artist and version match and its length is within N seconds
    # (read by scripts/spotiflac_runner.py from the environment)
    accept_isrc_mismatch: bool = Field(default=False)
    isrc_mismatch_max_seconds: float = Field(default=2.0)
    # Library check: every N hours, remove .flac files in downloads/ that
    # aren't FLAC (deleted, or moved to .rejected/ with lossy allowed); 0 = off
    library_check_hours: float = Field(default=6)
    spotiflac_retry_minutes: int = Field(default=30)
    spotiflac_binary_path: str = Field(default="/opt/spotiflac-venv/bin/spotiflac")
    spotiflac_registries: str = Field(default="")

    # Skip tracks already in the download history, even if their files have
    # left downloads/ (copied to Plex on another host, etc.)
    history_skip_redownload: bool = Field(default=True)

    # Upstream break monitor (download servers pause on a schedule)
    break_status_url: str = Field(default="https://spotbye.qzz.io/api/status")
    break_poll_seconds: int = Field(default=60)
    break_gate_downloads: bool = Field(default=True)
    # Also stop a job that is already running when a scheduled break starts
    # (filed tracks are kept; it resumes after the break)
    break_pause_running: bool = Field(default=True)
    # Stop a running job this many seconds before the next scheduled break
    # (the break time is worked out to the second from the status page)
    break_stop_lead_seconds: int = Field(default=60)
    # After a break or outage, resume only once the servers have been
    # available this long (the status page flaps for a few minutes)
    break_resume_stable_seconds: int = Field(default=180)
    # Stop a running job only if the servers stay down this long (outside a
    # scheduled break); shorter blips are ridden out
    break_down_stop_minutes: float = Field(default=5)
    # Don't start a run this close to the next break (warm-up takes minutes)
    break_min_window_minutes: float = Field(default=8)
    # Kill a runner (and its browser/Node children) that printed nothing for
    # this long: hung; the attempt counts as a failed try (0 = off)
    runner_idle_minutes: float = Field(default=30)

    # Skip a track for a provider after it failed there this many times for
    # a lasting reason (wrong match, not found, wrong length), for
    # TRACK_FAIL_RETRY_DAYS; 0 = never skip
    track_fail_limit: int = Field(default=2)
    track_fail_retry_days: float = Field(default=7)
    # Daily quota guard: at most this many tracks downloaded per rolling
    # 24 h (from the track history; files already on disk don't count). The
    # download service has given a 24 h penalty after ~850-1,150 downloads.
    # A job stops when it's reached and resumes as older downloads age out.
    # 0 = no cap
    daily_track_cap: int = Field(default=700)
    # Dynamic quota (app/services/quota.py): "dynamic" learns the cap from
    # our own penalties (x QUOTA_BACKOFF of the downloads before one; +
    # QUOTA_STEP after each clean day at the cap, up to QUOTA_CEILING x the
    # lowest trigger of the last QUOTA_MEMORY_DAYS); DAILY_TRACK_CAP is then
    # only the start value when there's no penalty history. "fixed" = off
    quota_mode: str = Field(default="fixed")
    quota_min: int = Field(default=200)
    quota_max: int = Field(default=1000)
    quota_step: int = Field(default=25)
    quota_backoff: float = Field(default=0.8)
    quota_ceiling: float = Field(default=0.9)
    quota_memory_days: float = Field(default=7)
    # Burst guard: at most this many tracks downloaded per rolling hour
    # (every penalty so far followed a day with a 236-309 download hour);
    # 0 = off
    hourly_track_cap: int = Field(default=120)
    # Once a cap (daily or hourly) is hit, resume only when the count is
    # back down to this share of it, not as soon as one download ages out
    cap_resume_ratio: float = Field(default=0.85)
    # Disk guard: don't start, and stop a running job, while the downloads
    # disk has less than this much free space (GB); 0 = off
    min_free_gb: float = Field(default=10)

    # Custom Formatting
    # Applied by scripts/spotiflac_runner.py. Placeholders: {playlist_name}
    # {album_artist} {artist} (first) {artists} ("; "-joined) {album} {year}
    # {disc} {title} {track} (album track number, 2-digit)
    spotiflac_single_track_format: str = Field(default="{track} - {artists} - {title}")
    spotiflac_album_track_format: str = Field(default="{track} - {artists} - {title}")
    spotiflac_folder_structure: str = Field(default="{album_artist}/[{year}] {album}")
    spotiflac_playlist_folder_structure: str = Field(default="{playlist_name}")
    
    # Authentication
    auth_username: Optional[str] = Field(default=None)
    auth_password: Optional[str] = Field(default=None)
    
    # Scanning
    scan_interval_minutes: int = Field(default=60)
    auto_scan_on_startup: bool = Field(default=True)
    
    # Logging
    log_level: str = Field(default="INFO")
    # Hide access-log lines for successful UI polling (/partials/*, /health, /static/*)
    access_log_hide_polling: bool = Field(default=True)
    
    @property
    def music_library_paths(self) -> List[str]:
        """Parse comma-separated music library paths."""
        return [p.strip() for p in self.music_library_path.split(",") if p.strip()]
    
    @property
    def spotiflac_services(self) -> List[str]:
        """Parse comma-separated services. Providers that can't deliver FLAC
        (ytmusic/soundcloud are lossy; apple-music is metadata only) are left
        out unless ALLOW_LOSSY_FALLBACK=true, and then only as a last resort.
        qobuz-web goes after the other lossless ones (it searches through the
        shared download service; amazon doesn't)."""
        services = [s.strip() for s in self.spotiflac_service.split(",") if s.strip()]
        lossless = [s for s in services if s not in LOSSY_SERVICES]
        lossless = [s for s in lossless if s != "ext:qobuz-web"] + [s for s in lossless if s == "ext:qobuz-web"]
        if not self.allow_lossy_fallback:
            return lossless
        return lossless + [s for s in services if s in LOSSY_SERVICES and s != "ext:apple-music"]
    
    @property
    def spotiflac_registry_urls(self) -> List[str]:
        """Parse comma-separated registry URLs."""
        return [r.strip() for r in self.spotiflac_registries.split(",") if r.strip()]
    
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
