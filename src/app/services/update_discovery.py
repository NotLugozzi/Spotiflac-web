#!/usr/bin/env python3
"""
update_discovery.py

Automates maintenance of SpotiFlac-web's data/curated_playlists.json.

Two jobs, sharing one validation function:
  1. DISCOVER  - search Spotify for new candidate playlists per category,
                 vet each one, and add any that pass.
  2. REVALIDATE - re-check every ID already in the JSON file and drop any
                 that now 404/403 (deleted, gone private, ownership changed).

A playlist is only ever kept if:
  - sp.playlist(id) resolves without error, AND
  - owner.id is not "spotify" (and not in a small denylist of known
    Spotify-owned regional/editorial accounts).

Run manually:
    python update_discovery.py            # discover + revalidate (default)
    python update_discovery.py --discover-only
    python update_discovery.py --revalidate-only

Or via cron, e.g. weekly:
    0 4 * * 0 cd /path/to/spotiflac-web && python update_discovery.py >> logs/discovery_update.log 2>&1

NOTE: This script talks to the Spotify Web API. Spotify's February 2026
Web API revision renamed playlist sub-resources (tracks -> items) and
capped search results per call at 10. This script only reads top-level
playlist metadata (name, owner, images, track count) via sp.playlist(),
not track listings, so it should be unaffected by the items/tracks
rename -- but re-confirm sp.playlist() still returns what you expect
against your actual app credentials before relying on this unattended.
"""

import argparse
import fcntl
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import spotipy
from dotenv import load_dotenv

# Shared with the web app's spotify_service (same credentials, same quota)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from discovery_defaults import DEFAULT_CATEGORIES  # noqa: E402
from spotify_limits import CooldownSpotify, SpotifyRateLimited, cooldown_until, credential_slots, format_until  # noqa: E402

JSON_PATH = Path(os.environ.get("CURATED_PLAYLISTS_PATH", "/app/data/curated_playlists.json"))

MAX_PER_CATEGORY = int(os.environ.get("SPOTIFY_DISCOVERY_MAX_PLAYLISTS", 20))  # how many validated playlists to keep per category
SEARCH_LIMIT = 10             # Spotify's current per-call cap (Feb 2026 revision)

# Quota protection: discovery shares the app's Spotify quota, so keep it small.
REQUEST_DELAY_SECONDS = 1.0   # pacing between API calls
MAX_API_CALLS = int(os.environ.get("DISCOVERY_MAX_API_CALLS", 40))  # hard cap per run
MAX_SEARCH_PAGES = 2          # search pages per category (10 results each)
REVALIDATE_PER_RUN = 20       # existing playlists re-checked per run (one /playlists call each)
MIN_INTERVAL_HOURS = float(os.environ.get("DISCOVERY_MIN_INTERVAL_HOURS", 24))  # between full runs
STATE_PATH = JSON_PATH.parent / ".discovery_state.json"

EXIT_LOCKED = 42              # another run holds the lock; callers treat this as "skipped"
EXIT_RATE_LIMITED = 43        # Spotify rate limited; progress saved, retry after the cooldown


class BudgetExhausted(Exception):
    """MAX_API_CALLS reached; the run stops and the next one continues."""


# Stop the run (saving progress) instead of treating playlists as dead
HALT = (SpotifyRateLimited, BudgetExhausted)


class BudgetSpotify(CooldownSpotify):
    """Counts API calls against MAX_API_CALLS. Rate limits surface as
    SpotifyRateLimited from CooldownSpotify (shared, per-endpoint cooldowns)."""

    calls = 0

    def _internal_call(self, method, url, payload, params):
        if self.calls >= MAX_API_CALLS:
            raise BudgetExhausted()
        self.calls += 1
        return super()._internal_call(method, url, payload, params)


def load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_state(**updates: Any) -> None:
    state = {**load_state(), **updates}
    try:
        STATE_PATH.write_text(json.dumps(state))
    except OSError as e:
        log.warning("Could not write %s: %s", STATE_PATH, e)

# Known Spotify-owned account IDs beyond the obvious "spotify" -- extend
# this list if you spot other official/regional editorial accounts slipping
# through (e.g. localized charts accounts). owner.id is the reliable field;
# display names can be spoofed/duplicated by other users.
SPOTIFY_OWNED_DENYLIST: Set[str] = {
    "spotify",
    "spotifycharts",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("update_discovery")


# ---------------------------------------------------------------------------
# Spotify client & Lockfile
# ---------------------------------------------------------------------------

def build_client() -> spotipy.Spotify:
    load_dotenv()
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        log.error("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set in .env — aborting.")
        sys.exit(1)
    # Never sleeps on Retry-After (Spotify sends 11+ hours on quota limits)
    return BudgetSpotify(credential_slots(
        client_id, client_secret,
        os.environ.get("SPOTIFY_CLIENT_ID_BACKUP", ""),
        os.environ.get("SPOTIFY_CLIENT_SECRET_BACKUP", ""),
    ))

class ProcessLock:
    """Single-run guard using flock(). The kernel drops the lock when the
    process exits for any reason, so a run killed mid-way (e.g. by a
    container recreate) can't leave a stale lock behind. The lock file
    itself is left in place on purpose."""

    def __init__(self):
        self.lock_path = JSON_PATH.parent / ".discovery_update.lock"
        self.fd = None

    def __enter__(self):
        try:
            self.fd = os.open(self.lock_path, os.O_CREAT | os.O_WRONLY, 0o644)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("Another discovery update is running, skipping this one.")
            sys.exit(EXIT_LOCKED)
        except OSError as e:
            log.error("Could not acquire lock %s: %s", self.lock_path, e)
            sys.exit(1)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Config Management
# ---------------------------------------------------------------------------

CONFIG_PATH = JSON_PATH.parent / "discovery_config.json"


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {"categories": DEFAULT_CATEGORIES.copy(), "blocklist": []}
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        log.error("Could not read config %s (%s). Using defaults.", CONFIG_PATH, e)
        return {"categories": DEFAULT_CATEGORIES.copy(), "blocklist": []}

def get_blocklist() -> Set[str]:
    cfg = load_config()
    return set(cfg.get("blocklist") or [])

def get_categories() -> Dict[str, str]:
    cfg = load_config()
    return cfg.get("categories") or DEFAULT_CATEGORIES.copy()


# ---------------------------------------------------------------------------
# Shared validation — used by BOTH discovery and revalidation
# ---------------------------------------------------------------------------

def resolve_and_validate(sp: spotipy.Spotify, playlist_id: str, blocklist: Set[str] = None) -> Optional[Dict[str, Any]]:
    """
    Attempt to resolve a playlist ID via sp.playlist(). Returns a small
    metadata dict if it's a live, non-Spotify-owned, public playlist.
    Returns None (and logs why) if it should be excluded.
    """
    if blocklist is None:
        blocklist = set()
        
    if playlist_id in blocklist:
        log.info("Skipping %s: present in user blocklist", playlist_id)
        return None

    try:
        pl = sp.playlist(playlist_id, fields="id,name,description,owner,images,tracks(total),public")
    except HALT:
        raise
    except spotipy.SpotifyException as e:
        log.warning("Skipping %s: Spotify API returned %s (%s)", playlist_id, e.http_status, e.msg)
        return None
    except Exception as e:
        log.exception("Unexpected error resolving %s (%s)", playlist_id, type(e).__name__)
        return None

    if pl is None:
        log.warning("Skipping %s: empty response", playlist_id)
        return None

    return validate_playlist(pl)


def validate_playlist(pl: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Apply the keep rules to a playlist object, either from sp.playlist()
    or straight from search results (which carry the same fields, so
    discovery needs no extra lookup per candidate)."""
    playlist_id = pl.get("id")
    owner_id = (pl.get("owner") or {}).get("id", "")
    if not playlist_id or not owner_id:
        return None
    if owner_id.lower() in SPOTIFY_OWNED_DENYLIST:
        log.info("Skipping %s ('%s'): owned by Spotify (%s), not a community playlist",
                  playlist_id, pl.get("name"), owner_id)
        return None

    if pl.get("public") is False:
        log.info("Skipping %s ('%s'): playlist is not public", playlist_id, pl.get("name"))
        return None

    images = pl.get("images") or []
    # Feb 2026 API revision renamed the playlist "tracks" object to "items"
    tracks_obj = pl.get("tracks") or pl.get("items")

    return {
        "id": pl["id"],
        "name": pl.get("name", ""),
        "description": pl.get("description", ""),
        "owner": owner_id,
        "cover_url": images[0]["url"] if images else None,
        "track_count": tracks_obj.get("total", 0) if isinstance(tracks_obj, dict) else 0,
        # Revalidation rotates through entries oldest-checked first
        "checked_at": int(time.time()),
    }


# ---------------------------------------------------------------------------
# Job 1: discover new candidates via search
# ---------------------------------------------------------------------------

def discover_candidates(sp: spotipy.Spotify, existing_ids: Set[str], blocklist: Set[str], target_category: Optional[str] = None,
                        found: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                        counts: Optional[Dict[str, int]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """``counts``: playlists already stored per category. Only the open slots
    (MAX_PER_CATEGORY - count) are searched for, and full categories cost no
    API calls at all."""
    # Filled in place so a caller still has partial results if the run halts
    if found is None:
        found = {}
    categories = get_categories()

    for category, query in categories.items():
        if target_category and category != target_category:
            continue

        wanted = MAX_PER_CATEGORY - (counts or {}).get(category, 0)
        if wanted <= 0:
            log.info("Category '%s' is full (%d), skipping search", category, MAX_PER_CATEGORY)
            continue

        accepted: List[Dict[str, Any]] = []
        found[category] = accepted
        offset = 0
        pages = 0

        while len(accepted) < wanted and pages < MAX_SEARCH_PAGES:
            log.info("Searching for candidates in category '%s' (query: %r, offset: %d)...", category, query, offset)
            pages += 1
            time.sleep(REQUEST_DELAY_SECONDS)
            try:
                # Max limit per call is strictly 10 for Spotify search API (Feb 2026 revision)
                results = sp.search(q=query, type="playlist", limit=SEARCH_LIMIT, offset=offset)
            except HALT:
                raise
            except spotipy.SpotifyException as e:
                log.warning("Search failed for '%s': %s %s — skipping this category", category, e.http_status, e.msg)
                break
            except Exception as e:
                log.exception("Unexpected error searching '%s' (%s)", category, type(e).__name__)
                break

            raw_items = (results.get("playlists") or {}).get("items") or []
            if not raw_items:
                break # no more results from spotify

            for raw in raw_items:
                if raw is None:
                    continue
                pid = raw.get("id")
                if not pid or pid in existing_ids or pid in blocklist:
                    continue

                # Search results carry owner/public/images/track count already;
                # only look the playlist up if the owner is missing
                meta = validate_playlist(raw) if (raw.get("owner") or {}).get("id") else None
                if meta is None and not (raw.get("owner") or {}).get("id"):
                    time.sleep(REQUEST_DELAY_SECONDS)
                    meta = resolve_and_validate(sp, pid, blocklist)
                if meta is not None:
                    accepted.append(meta)
                    existing_ids.add(pid)

                if len(accepted) >= wanted:
                    break

            offset += len(raw_items)

        log.info("Category '%s': %d new playlist(s) accepted", category, len(accepted))
        if not accepted:
            del found[category]

    return found


# ---------------------------------------------------------------------------
# Job 2: revalidate everything already on file
# ---------------------------------------------------------------------------

def revalidate_existing(sp: spotipy.Spotify, data: Dict[str, List[Dict[str, Any]]], blocklist: Set[str], target_category: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    cleaned: Dict[str, List[Dict[str, Any]]] = {}
    categories = list(data.items())

    # Each check is one /playlists call, so only re-check the
    # REVALIDATE_PER_RUN entries checked longest ago; the rest rotate in later
    def checked_at(entry: Any) -> float:
        return entry.get("checked_at", 0) if isinstance(entry, dict) else 0

    candidates = [
        entry for category, entries in categories
        if not target_category or category == target_category
        for entry in entries
    ]
    to_check = {id(e) for e in sorted(candidates, key=checked_at)[:REVALIDATE_PER_RUN]}

    for cat_index, (category, entries) in enumerate(categories):
        if target_category and category != target_category:
            cleaned[category] = entries
            continue

        kept: List[Dict[str, Any]] = []
        for index, entry in enumerate(entries):
            pid = entry if isinstance(entry, str) else entry.get("id")
            if not pid:
                continue
            if id(entry) not in to_check:
                kept.append(entry)
                continue
            time.sleep(REQUEST_DELAY_SECONDS)
            try:
                meta = resolve_and_validate(sp, pid, blocklist)
            except HALT as e:
                # Keep everything not yet checked as-is rather than dropping it
                cleaned[category] = kept + list(entries[index:])
                for rest_category, rest_entries in categories[cat_index + 1:]:
                    cleaned[rest_category] = rest_entries
                e.partial = cleaned
                raise
            if meta is not None:
                kept.append(meta)
            else:
                log.info("Removing %s from category '%s' during revalidation", pid, category)

        if kept:
            cleaned[category] = kept
        else:
            log.info("Category '%s' has no valid playlists left after revalidation", category)

    return cleaned


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_existing() -> Dict[str, List[Dict[str, Any]]]:
    if not JSON_PATH.exists():
        return {}
    try:
        with open(JSON_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error("Could not read existing %s (%s) — treating as empty rather than overwriting blindly.",
                   JSON_PATH, e)
        backup = JSON_PATH.with_suffix(".json.bak")
        try:
            JSON_PATH.rename(backup)
            log.info("Backed up unreadable file to %s", backup)
        except OSError:
            pass
        return {}


def save(data: Dict[str, List[Dict[str, Any]]]) -> None:
    # Union with config: guarantee every active category is present, even if empty,
    # and strictly drop any orphaned categories not in the config.
    categories = get_categories()
    enforced_data = {k: data.get(k, []) for k in categories}
    
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = JSON_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(enforced_data, f, indent=2)
    tmp_path.replace(JSON_PATH)
    total = sum(len(v) for v in enforced_data.values())
    log.info("Wrote %s: %d categories, %d playlists total", JSON_PATH, len(enforced_data), total)


def merge(existing: Dict[str, List[Dict[str, Any]]],
          discovered: Dict[str, List[Dict[str, Any]]],
          target_category: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    merged: Dict[str, List[Dict[str, Any]]] = {k: list(v) for k, v in existing.items()}
    for category, new_entries in discovered.items():
        if target_category and category != target_category:
            continue
        merged.setdefault(category, [])
        seen_ids = {e["id"] if isinstance(e, dict) else e for e in merged[category]}
        
        # Calculate how many slots are open
        slots_open = MAX_PER_CATEGORY - len(merged[category])
        for entry in new_entries:
            if slots_open <= 0:
                break
            if entry["id"] not in seen_ids:
                merged[category].append(entry)
                seen_ids.add(entry["id"])
                slots_open -= 1
    return merged


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discover-only", action="store_true", help="Only search for new playlists.")
    parser.add_argument("--revalidate-only", action="store_true", help="Only re-check existing playlists.")
    parser.add_argument("--category", type=str, help="Scope the operation to a single category name.")
    parser.add_argument("--force", action="store_true",
                        help="Run a full update even if one ran within DISCOVERY_MIN_INTERVAL_HOURS.")
    args = parser.parse_args()

    with ProcessLock():
        # Discovery is search-driven; a /playlists cooldown only pauses revalidation
        slots = [slot for slot, _, _ in credential_slots(
            os.environ.get("SPOTIFY_CLIENT_ID", ""), os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
            os.environ.get("SPOTIFY_CLIENT_ID_BACKUP", ""), os.environ.get("SPOTIFY_CLIENT_SECRET_BACKUP", ""),
        )] or ["primary"]
        until = cooldown_until("search", slots)
        if until:
            log.info("Spotify search is rate limited until %s, skipping discovery update.", format_until(until))
            sys.exit(EXIT_RATE_LIMITED)

        # Full runs are the expensive ones; scoped (--category) runs are small
        full_run = not args.category
        last_full = float(load_state().get("last_full_run", 0))
        hours_since = (time.time() - last_full) / 3600
        if full_run and not args.force and hours_since < MIN_INTERVAL_HOURS:
            log.info("Last full discovery update was %.1f h ago (minimum %g h), skipping.",
                     hours_since, MIN_INTERVAL_HOURS)
            return

        sp = build_client()
        existing = load_existing()
        blocklist = get_blocklist()
        base = existing
        discovered: Dict[str, List[Dict[str, Any]]] = {}

        try:
            if not args.discover_only:
                # Revalidate first (prune dead entries), then discover
                try:
                    base = revalidate_existing(sp, existing, blocklist, args.category)
                except SpotifyRateLimited as e:
                    # Unchecked playlists are kept as-is; search may still work
                    base = getattr(e, "partial", base)
                    log.warning("Revalidation paused (%s); keeping unchecked playlists, continuing with search.", e)
                if args.revalidate_only:
                    save(base)
                    return

            existing_ids = {e["id"] if isinstance(e, dict) else e
                            for category, entries in base.items()
                            if not args.category or category == args.category
                            for e in entries}
            discover_candidates(sp, existing_ids, blocklist, args.category, found=discovered,
                                counts={k: len(v) for k, v in base.items()})
            save(merge(base, discovered, args.category))
        except HALT as e:
            # Save what was confirmed so far; unchecked entries are kept as-is
            base = getattr(e, "partial", base)
            save(merge(base, discovered, args.category))
            if isinstance(e, SpotifyRateLimited):
                log.warning("Stopped: %s. Progress saved.", e)
                sys.exit(EXIT_RATE_LIMITED)
            log.info("Stopped after %d API calls (DISCOVERY_MAX_API_CALLS). Progress saved; "
                     "the next run continues.", MAX_API_CALLS)
        finally:
            log.info("Discovery used %d Spotify API call(s).", sp.calls)

        if full_run:
            save_state(last_full_run=time.time())


if __name__ == "__main__":
    main()
