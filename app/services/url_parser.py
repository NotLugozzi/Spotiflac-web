import re
from typing import Optional, Tuple
from dataclasses import dataclass
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class SpotifyUrlType(str, Enum):
    """Types of Spotify URLs/URIs."""
    ARTIST = "artist"
    ALBUM = "album"
    TRACK = "track"
    PLAYLIST = "playlist"
    UNKNOWN = "unknown"


@dataclass
class ParsedSpotifyUrl:
    """Parsed Spotify URL data."""
    url_type: SpotifyUrlType
    spotify_id: str
    original_url: str
    normalized_url: str
    uri: str
    
    @property
    def is_valid(self) -> bool:
        """Check if the parsed URL is valid."""
        return self.url_type != SpotifyUrlType.UNKNOWN and bool(self.spotify_id)


SPOTIFY_URL_PATTERN = re.compile(
    r'https?://open\.spotify\.com/(album|track|artist|playlist)/([a-zA-Z0-9]+)(?:\?.*)?$'
)

SPOTIFY_URI_PATTERN = re.compile(
    r'spotify:(album|track|artist|playlist):([a-zA-Z0-9]+)$'
)

SPOTIFY_SHARE_PATTERN = re.compile(
    r'https?://(?:open\.)?spotify\.com(?:/intl-[a-z]+)?/(album|track|artist|playlist)/([a-zA-Z0-9]+)(?:\?.*)?$'
)


def parse_spotify_url(url: str) -> ParsedSpotifyUrl:
    url = url.strip()
    
    # Try standard URL pattern
    match = SPOTIFY_URL_PATTERN.match(url)
    if match:
        url_type_str, spotify_id = match.groups()
        url_type = SpotifyUrlType(url_type_str)
        return ParsedSpotifyUrl(
            url_type=url_type,
            spotify_id=spotify_id,
            original_url=url,
            normalized_url=f"https://open.spotify.com/{url_type_str}/{spotify_id}",
            uri=f"spotify:{url_type_str}:{spotify_id}",
        )
    
    # Try share link pattern (includes intl- paths)
    match = SPOTIFY_SHARE_PATTERN.match(url)
    if match:
        url_type_str, spotify_id = match.groups()
        url_type = SpotifyUrlType(url_type_str)
        return ParsedSpotifyUrl(
            url_type=url_type,
            spotify_id=spotify_id,
            original_url=url,
            normalized_url=f"https://open.spotify.com/{url_type_str}/{spotify_id}",
            uri=f"spotify:{url_type_str}:{spotify_id}",
        )
    
    # Try URI pattern
    match = SPOTIFY_URI_PATTERN.match(url)
    if match:
        url_type_str, spotify_id = match.groups()
        url_type = SpotifyUrlType(url_type_str)
        return ParsedSpotifyUrl(
            url_type=url_type,
            spotify_id=spotify_id,
            original_url=url,
            normalized_url=f"https://open.spotify.com/{url_type_str}/{spotify_id}",
            uri=f"spotify:{url_type_str}:{spotify_id}",
        )
    
    # Invalid URL
    logger.warning(f"Could not parse Spotify URL: {url}")
    return ParsedSpotifyUrl(
        url_type=SpotifyUrlType.UNKNOWN,
        spotify_id="",
        original_url=url,
        normalized_url="",
        uri="",
    )


def is_valid_spotify_url(url: str) -> bool:
    parsed = parse_spotify_url(url)
    return parsed.is_valid


def extract_spotify_id(url: str) -> Optional[str]:
    parsed = parse_spotify_url(url)
    return parsed.spotify_id if parsed.is_valid else None


def get_spotify_url_type(url: str) -> SpotifyUrlType:
    parsed = parse_spotify_url(url)
    return parsed.url_type


def normalize_spotify_url(url: str) -> str:
    parsed = parse_spotify_url(url)
    return parsed.normalized_url


def parse_multiple_urls(text: str) -> list[ParsedSpotifyUrl]:
    # Split by newlines and spaces
    potential_urls = re.split(r'[\s\n]+', text)
    
    results = []
    for url in potential_urls:
        url = url.strip()
        if not url:
            continue
            
        parsed = parse_spotify_url(url)
        if parsed.is_valid:
            results.append(parsed)
    
    return results
