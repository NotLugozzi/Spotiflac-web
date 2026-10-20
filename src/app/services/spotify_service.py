import spotipy
from typing import Optional, List, Dict, Any, Tuple
import logging
import time
import json
import os
from pathlib import Path
from functools import wraps

from app.config import get_settings
from app.services.discovery_defaults import DEFAULT_CATEGORIES
from app.services.spotify_limits import CooldownSpotify, SpotifyRateLimited, credential_slots

logger = logging.getLogger(__name__)


def rate_limit_handler(max_retries: int = 3, base_delay: float = 1.0):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except SpotifyRateLimited:
                    return None
                except spotipy.SpotifyException as e:
                    if e.http_status == 429:
                        # Don't sleep: Retry-After can be 11+ hours, and this may be
                        # running on the request thread
                        return None
                    elif e.http_status in (500, 502, 503, 504):
                        delay = base_delay * (2 ** attempt)
                        logger.warning(f"Server error {e.http_status}. Retrying in {delay}s...")
                        time.sleep(delay)
                    else:
                        raise
                except Exception as e:
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(f"Error: {e}. Retrying in {delay}s...")
                        time.sleep(delay)
                    else:
                        raise
            return None
        return wrapper
    return decorator


class SpotifyService:
    """Service for interacting with the Spotify API."""
    
    def __init__(self):
        self._client: Optional[spotipy.Spotify] = None
        self._initialized = False
        self._playlist_cache: Dict[str, Tuple[float, dict]] = {}
    
    def _ensure_client(self) -> bool:
        # Rate limits are handled per endpoint by CooldownSpotify: calls to a
        # cooled-down endpoint raise SpotifyRateLimited at once, which the
        # methods below turn into their usual empty result
        if self._client is not None:
            return True

        settings = get_settings()
        
        if not settings.spotify_client_id or not settings.spotify_client_secret:
            logger.warning("Spotify API credentials not configured")
            return False
        
        try:
            self._client = CooldownSpotify(credential_slots(
                settings.spotify_client_id, settings.spotify_client_secret,
                settings.spotify_client_id_backup, settings.spotify_client_secret_backup,
            ))
            self._initialized = True
            backup = " (with backup app)" if len(self._client.slots) > 1 else ""
            logger.info(f"Spotify client initialized successfully{backup}")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Spotify client: {e}")
            return False
    
    @property
    def is_configured(self) -> bool:
        settings = get_settings()
        return bool(settings.spotify_client_id and settings.spotify_client_secret)
    
    @property
    def is_connected(self) -> bool:
        # The client is created lazily (no API call), so build it here; otherwise
        # the dashboard says "Not Configured" after every restart until something
        # happens to call Spotify
        return self._ensure_client()
    
    @rate_limit_handler()
    def search_artist(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search for artists on Spotify.
        
        Args:
            query: Search query string
            limit: Maximum number of results
            
        Returns:
            List of artist dictionaries
        """
        if not self._ensure_client():
            return []
        
        try:
            results = self._client.search(q=query, type="artist", limit=limit)
            artists = results.get("artists", {}).get("items", [])
            
            return [self._parse_artist(artist) for artist in artists]
        except Exception as e:
            logger.error(f"Error searching for artist '{query}': {e}")
            return []
    
    @rate_limit_handler()
    def get_artist(self, artist_id: str) -> Optional[Dict[str, Any]]:
        if not self._ensure_client():
            return None
        
        try:
            artist = self._client.artist(artist_id)
            return self._parse_artist(artist)
        except Exception as e:
            logger.error(f"Error getting artist '{artist_id}': {e}")
            return None
    
    @rate_limit_handler()
    def get_artist_albums(
        self, 
        artist_id: str, 
        include_groups: str = "album,single",
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Get all albums for an artist.
        """
        if not self._ensure_client():
            return []
        
        try:
            albums = []
            offset = 0
            
            while True:
                results = self._client.artist_albums(
                    artist_id,
                    album_type=include_groups,
                    limit=limit,
                    offset=offset,
                )
                
                items = results.get("items", [])
                if not items:
                    break
                
                albums.extend([self._parse_album(album) for album in items])
                
                if len(items) < limit:
                    break
                    
                offset += limit
            
            # Sort by release date (newest first)
            albums.sort(key=lambda x: x.get("release_date", ""), reverse=True)
            
            return albums
        except Exception as e:
            logger.error(f"Error getting albums for artist '{artist_id}': {e}")
            return []
    
    @rate_limit_handler()
    def get_album(self, album_id: str) -> Optional[Dict[str, Any]]:
        if not self._ensure_client():
            return None
        
        try:
            album = self._client.album(album_id)
            parsed = self._parse_album(album)
            
            # Add tracks
            tracks = album.get("tracks", {}).get("items", [])
            parsed["tracks"] = [self._parse_track(track, album) for track in tracks]
            
            return parsed
        except Exception as e:
            logger.error(f"Error getting album '{album_id}': {e}")
            return None
    
    @rate_limit_handler()
    def get_album_tracks(self, album_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        if not self._ensure_client():
            return []
        
        try:
            # First get album info for additional track metadata
            album = self._client.album(album_id)
            
            tracks = []
            offset = 0
            
            while True:
                results = self._client.album_tracks(album_id, limit=limit, offset=offset)
                items = results.get("items", [])
                
                if not items:
                    break
                
                tracks.extend([self._parse_track(track, album) for track in items])
                
                if len(items) < limit:
                    break
                    
                offset += limit
            
            return tracks
        except Exception as e:
            logger.error(f"Error getting tracks for album '{album_id}': {e}")
            return []
    
    @rate_limit_handler()
    def get_track(self, track_id: str) -> Optional[Dict[str, Any]]:
        if not self._ensure_client():
            return None
        
        try:
            track = self._client.track(track_id)
            album = track.get("album", {})
            return self._parse_track(track, album)
        except Exception as e:
            logger.error(f"Error getting track '{track_id}': {e}")
            return None
    
    @rate_limit_handler()
    def search_album(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        if not self._ensure_client():
            return []
        
        try:
            results = self._client.search(q=query, type="album", limit=limit)
            albums = results.get("albums", {}).get("items", [])
            
            return [self._parse_album(album) for album in albums]
        except Exception as e:
            logger.error(f"Error searching for album '{query}': {e}")
            return []
    
    @rate_limit_handler()
    def get_playlist(self, playlist_id: str) -> Optional[Dict[str, Any]]:
        """Get playlist information including tracks."""
        if not self._ensure_client():
            return None
        
        try:
            playlist = self._client.playlist(playlist_id)
            parsed = self._parse_playlist(playlist)
            
            tracks = []
            offset = 0
            limit = 100
            
            while True:
                results = self._client.playlist_tracks(
                    playlist_id, 
                    limit=limit, 
                    offset=offset
                )
                
                items = results.get("items", [])
                if not items:
                    break
                
                for item in items:
                    track = item.get("track")
                    if track:
                        album = track.get("album", {})
                        tracks.append(self._parse_track(track, album))
                
                if len(items) < limit:
                    break
                    
                offset += limit
            
            parsed["tracks"] = tracks
            return parsed
            
        except Exception as e:
            logger.error(f"Error getting playlist '{playlist_id}': {e}")
            return None
    
    def match_artist_name(self, name: str) -> Optional[Dict[str, Any]]:
        results = self.search_artist(name, limit=5)
        
        if not results:
            return None
        
        name_lower = name.lower().strip()
        for artist in results:
            if artist["name"].lower().strip() == name_lower:
                return artist
        
        return results[0]
    
    def _parse_artist(self, artist: Dict[str, Any]) -> Dict[str, Any]:
        images = artist.get("images", [])
        image_url = images[0]["url"] if images else None
        
        return {
            "spotify_id": artist.get("id"),
            "name": artist.get("name"),
            "spotify_url": artist.get("external_urls", {}).get("spotify"),
            "image_url": image_url,
            "genres": artist.get("genres", []),
            "popularity": artist.get("popularity"),
            "followers": artist.get("followers", {}).get("total"),
        }
    
    def _parse_album(self, album: Dict[str, Any]) -> Dict[str, Any]:
        images = album.get("images", [])
        image_url = images[0]["url"] if images else None
        
        # Get primary artist
        artists = album.get("artists", [])
        primary_artist = artists[0] if artists else {}
        
        return {
            "spotify_id": album.get("id"),
            "name": album.get("name"),
            "spotify_url": album.get("external_urls", {}).get("spotify"),
            "image_url": image_url,
            "album_type": album.get("album_type"),
            "release_date": album.get("release_date"),
            "release_date_precision": album.get("release_date_precision"),
            "total_tracks": album.get("total_tracks"),
            "label": album.get("label"),
            "artist_id": primary_artist.get("id"),
            "artist_name": primary_artist.get("name"),
        }
    
    def _parse_track(self, track: Dict[str, Any], album: Dict[str, Any] = None) -> Dict[str, Any]:
        # Get primary artist
        artists = track.get("artists", [])
        primary_artist = artists[0] if artists else {}
        
        return {
            "spotify_id": track.get("id"),
            "name": track.get("name"),
            "spotify_url": track.get("external_urls", {}).get("spotify"),
            "track_number": track.get("track_number"),
            "disc_number": track.get("disc_number", 1),
            "duration_ms": track.get("duration_ms"),
            "explicit": track.get("explicit", False),
            "isrc": track.get("external_ids", {}).get("isrc") if "external_ids" in track else None,
            "preview_url": track.get("preview_url"),
            "artist_id": primary_artist.get("id"),
            "artist_name": primary_artist.get("name"),
            "album_id": album.get("id") if album else None,
            "album_name": album.get("name") if album else None,
        }
    
    def _parse_playlist(self, playlist: Dict[str, Any]) -> Dict[str, Any]:
        """Parse playlist data from Spotify API."""
        images = playlist.get("images", [])
        image_url = images[0]["url"] if images else None
        
        owner = playlist.get("owner", {})
        
        return {
            "spotify_id": playlist.get("id"),
            "name": playlist.get("name"),
            "description": playlist.get("description"),
            "spotify_url": playlist.get("external_urls", {}).get("spotify"),
            "image_url": image_url,
            "owner_name": owner.get("display_name"),
            "owner_id": owner.get("id"),
            "total_tracks": playlist.get("tracks", {}).get("total"),
            "public": playlist.get("public"),
        }


    @rate_limit_handler()
    def get_curated_playlists(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch curated community playlists from local JSON, bypassing deprecated endpoints."""
        if not self._ensure_client():
            return []
            
        json_path = os.environ.get("CURATED_PLAYLISTS_PATH", "/app/data/curated_playlists.json")
            
        if not os.path.exists(json_path):
            logger.warning(f"Curated playlists JSON not found at {json_path}")
            return []
            
        try:
            with open(json_path, 'r') as f:
                curated_data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to read curated playlists JSON: {e}")
            return []
            
        # Get active categories from config
        config = self._get_discovery_config()
        active_categories = config.get("categories", {})
        
        # Get requested category, or default to the first active one
        if not category or category not in active_categories:
            category = list(active_categories.keys())[0] if active_categories else None
            
        if not category:
            return []
            
        playlist_ids = curated_data.get(category, [])
        results = []
        
        ttl_seconds = 24 * 3600 # 24 hours
        current_time = time.time()
        
        for item in playlist_ids:
            if isinstance(item, dict):
                # Script-populated entry (already has metadata)
                pid = item.get("id")
                if not pid:
                    continue
                    
                formatted_pl = {
                    "id": pid,
                    "name": item.get("name") or "",
                    "description": item.get("description") or "",
                    "owner": item.get("owner") or "Unknown",
                    "tracks_total": item.get("track_count") or 0,
                    "spotify_url": f"https://open.spotify.com/playlist/{pid}",
                    "image_url": item.get("cover_url") or ""
                }
                results.append(formatted_pl)
                continue
                
            # Hand-added entry (just a string ID)
            pid = item
            
            # Check cache
            if pid in self._playlist_cache:
                cached_time, cached_data = self._playlist_cache[pid]
                if current_time - cached_time < ttl_seconds:
                    results.append(cached_data)
                    continue
                    
            try:
                # Fetch fresh from Spotify API
                pl = self._client.playlist(pid)
                
                # Format to match the expected dict structure
                image_url = ""
                if pl.get("images") and len(pl["images"]) > 0:
                    image_url = pl["images"][0].get("url", "")
                    
                formatted_pl = {
                    "id": pl["id"],
                    "name": pl["name"],
                    "description": pl.get("description", ""),
                    "owner": pl.get("owner", {}).get("display_name", "Unknown"),
                    "tracks_total": pl.get("tracks", {}).get("total", 0),
                    "spotify_url": pl.get("external_urls", {}).get("spotify", ""),
                    "image_url": image_url
                }
                
                # Update cache
                self._playlist_cache[pid] = (current_time, formatted_pl)
                results.append(formatted_pl)
                
            except spotipy.SpotifyException as e:
                # Expected churn (e.g., 404 deleted, 403 private)
                logger.warning(f"Skipping playlist {pid}: Spotify API returned {e.http_status} - {e.msg}")
                continue
            except Exception as e:
                # Unexpected errors (network timeouts, JSON parse errors, etc)
                logger.exception(f"Unexpected error fetching playlist {pid}. Error type: {type(e).__name__}")
                continue
                
        return results

    def _get_discovery_config(self) -> Dict[str, Any]:
        """Load the user's discovery configuration."""
        json_path = Path(os.environ.get("CURATED_PLAYLISTS_PATH", "/app/data/curated_playlists.json"))
        config_path = json_path.parent / "discovery_config.json"
        
        if not config_path.exists():
            return {"categories": DEFAULT_CATEGORIES.copy(), "blocklist": []}
            
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception:
            return {"categories": DEFAULT_CATEGORIES.copy(), "blocklist": []}

    def get_curated_categories(self) -> List[str]:
        """Get the list of active categories from the discovery config."""
        config = self._get_discovery_config()
        return list(config.get("categories", {}).keys())


# Global service instance
_spotify_service: Optional[SpotifyService] = None


def get_spotify_service() -> SpotifyService:
    global _spotify_service
    if _spotify_service is None:
        _spotify_service = SpotifyService()
    return _spotify_service

