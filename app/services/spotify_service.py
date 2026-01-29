import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from typing import Optional, List, Dict, Any
import logging
import time
from functools import wraps

from app.config import get_settings

logger = logging.getLogger(__name__)


def rate_limit_handler(max_retries: int = 3, base_delay: float = 1.0):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except spotipy.SpotifyException as e:
                    if e.http_status == 429:
                        retry_after = int(e.headers.get("Retry-After", base_delay * (2 ** attempt)))
                        logger.warning(f"Rate limited. Retrying after {retry_after} seconds...")
                        time.sleep(retry_after)
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
    
    def _ensure_client(self) -> bool:
        if self._client is not None:
            return True
        
        settings = get_settings()
        
        if not settings.spotify_client_id or not settings.spotify_client_secret:
            logger.warning("Spotify API credentials not configured")
            return False
        
        try:
            auth_manager = SpotifyClientCredentials(
                client_id=settings.spotify_client_id,
                client_secret=settings.spotify_client_secret,
            )
            self._client = spotipy.Spotify(auth_manager=auth_manager)
            self._initialized = True
            logger.info("Spotify client initialized successfully")
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
        return self._client is not None
    
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
    
    def match_artist_name(self, name: str) -> Optional[Dict[str, Any]]:
        results = self.search_artist(name, limit=5)
        
        if not results:
            return None
        
        # Try to find exact match first
        name_lower = name.lower().strip()
        for artist in results:
            if artist["name"].lower().strip() == name_lower:
                return artist
        
        # Return best result if no exact match
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


# Global service instance
_spotify_service: Optional[SpotifyService] = None


def get_spotify_service() -> SpotifyService:
    global _spotify_service
    if _spotify_service is None:
        _spotify_service = SpotifyService()
    return _spotify_service
