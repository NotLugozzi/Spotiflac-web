"""
Spotify Web API rate-limit handling shared by the web app (spotify_service)
and the standalone discovery script (update_discovery.py).

Both use the same client credentials, so they share one quota. When Spotify
answers 429 it sends a Retry-After that can be 11+ hours on quota limits.
spotipy's default urllib3 retry policy honours that header and sleeps for the
whole time, which hangs whatever made the call. Instead:

  * ``build_session()`` gives spotipy a session that retries only 5xx and
    never sleeps on Retry-After, so a 429 comes back immediately;
  * ``CooldownSpotify`` records a cooldown on any 429 and refuses calls to
    that endpoint until it passes.

Cooldowns are per endpoint family (the first path segment: ``playlists``,
``search``, ``tracks``…) because Spotify's quota blocks are: a 429 on
``/playlists`` while ``/search`` and ``/tracks`` keep answering normally is
a real case, and a global cooldown would needlessly block those too.

Quotas are per Spotify app, so an optional backup app
(SPOTIFY_CLIENT_ID_BACKUP / SPOTIFY_CLIENT_SECRET_BACKUP) gets its own
cooldowns: a call goes to the first app whose endpoint isn't cooling down.
Primary cooldowns keep the plain ``family`` key; the backup's are stored as
``backup:family``.

No ``app`` imports here: update_discovery.py runs as a plain script.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import requests
import requests.adapters
import spotipy
import urllib3
from spotipy.oauth2 import SpotifyClientCredentials

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("CURATED_PLAYLISTS_PATH", "/app/data/curated_playlists.json")).parent
COOLDOWN_PATH = DATA_DIR / ".spotify_ratelimit.json"

# Used when a 429 carries no Retry-After header
DEFAULT_RETRY_AFTER = 3600

PRIMARY = "primary"
BACKUP = "backup"


def credential_slots(client_id: str, client_secret: str,
                     backup_id: str = "", backup_secret: str = "") -> List[Tuple[str, str, str]]:
    """(slot, client_id, client_secret) for each configured Spotify app, primary first."""
    slots = []
    if client_id and client_secret:
        slots.append((PRIMARY, client_id, client_secret))
    if backup_id and backup_secret and backup_id != client_id:
        slots.append((BACKUP, backup_id, backup_secret))
    return slots


def cooldown_key(slot: str, family: str) -> str:
    return family if slot == PRIMARY else f"{slot}:{family}"


def build_session() -> requests.Session:
    """A requests session for spotipy: retries transient 5xx, never sleeps on 429."""
    retry = urllib3.Retry(
        total=3,
        connect=None,
        read=False,
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]),
        status=3,
        backoff_factor=0.3,
        status_forcelist=(500, 502, 503, 504),
        respect_retry_after_header=False,
    )
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def endpoint_family(url: str) -> str:
    """``https://api.spotify.com/v1/playlists/xyz`` or ``playlists/xyz`` -> ``playlists``."""
    path = url.split("?", 1)[0]
    if "/v1/" in path:
        path = path.split("/v1/", 1)[1]
    return path.strip("/").split("/", 1)[0] or "unknown"


def _read() -> Dict[str, float]:
    try:
        families = json.loads(COOLDOWN_PATH.read_text()).get("families", {})
        return {k: float(v) for k, v in families.items()}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def active_cooldowns() -> Dict[str, float]:
    """Endpoint family -> unix time its cooldown ends, for active cooldowns only."""
    now = time.time()
    return {k: v for k, v in _read().items() if v > now}


def cooldown_until(family: str, slots: Sequence[str] = (PRIMARY,)) -> Optional[float]:
    """When ``family`` is usable again: None if any of ``slots`` isn't cooling
    down, else the unix time the earliest of their cooldowns ends."""
    active = active_cooldowns()
    ends = [active.get(cooldown_key(slot, family)) for slot in slots]
    if not ends or not all(ends):
        return None
    return min(ends)


def format_until(until: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(until))


def record_cooldown(family: str, retry_after: int, slot: str = PRIMARY) -> float:
    until = time.time() + retry_after
    families = active_cooldowns()
    families[cooldown_key(slot, family)] = until
    try:
        COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        COOLDOWN_PATH.write_text(json.dumps({"families": families}))
    except OSError as e:
        logger.error(f"Could not record Spotify rate-limit cooldown: {e}")
    app = "" if slot == PRIMARY else f" ({slot} app)"
    logger.warning(f"Spotify API rate limited on /{family}{app} (Retry-After {retry_after}s); "
                   f"skipping /{family} calls{app} until {format_until(until)}")
    return until


def retry_after_of(e: spotipy.SpotifyException) -> int:
    try:
        return int((e.headers or {}).get("Retry-After", DEFAULT_RETRY_AFTER))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER


class SpotifyRateLimited(Exception):
    """Raised instead of calling a Spotify endpoint while its cooldown is active."""

    def __init__(self, family: str, until: float):
        super().__init__(f"Spotify API /{family} rate limited until {format_until(until)}")
        self.family = family
        self.until = until


class CooldownSpotify(spotipy.Spotify):
    """spotipy client that never sleeps on 429. A 429 records a cooldown for
    that endpoint family (per app) and the call moves on to the backup app if
    one is configured; once every app is cooling down for that family, calls
    raise SpotifyRateLimited immediately until the earliest cooldown passes.

    ``credentials`` is the output of ``credential_slots()``."""

    def __init__(self, credentials: Sequence[Tuple[str, str, str]], **kwargs):
        if not credentials:
            raise ValueError("No Spotify credentials configured")
        kwargs.setdefault("requests_session", build_session())
        kwargs.setdefault("requests_timeout", 15)
        self._managers = [
            (slot, SpotifyClientCredentials(client_id=cid, client_secret=secret))
            for slot, cid, secret in credentials
        ]
        # Shared across the web app's threadpool: the app used by a call is per thread
        self._local = threading.local()
        super().__init__(auth_manager=self._managers[0][1], **kwargs)

    @property
    def slots(self) -> List[str]:
        return [slot for slot, _ in self._managers]

    def _auth_headers(self):
        manager = getattr(self._local, "manager", None) or self.auth_manager
        return {"Authorization": f"Bearer {manager.get_access_token(as_dict=False)}"}

    def _internal_call(self, method, url, payload, params):
        family = endpoint_family(url)
        active = active_cooldowns()
        for slot, manager in self._managers:
            if active.get(cooldown_key(slot, family)):
                continue
            self._local.manager = manager
            try:
                return super()._internal_call(method, url, payload, params)
            except spotipy.SpotifyException as e:
                if e.http_status != 429:
                    raise
                record_cooldown(family, retry_after_of(e), slot)
            finally:
                self._local.manager = None
        raise SpotifyRateLimited(family, cooldown_until(family, self.slots))
