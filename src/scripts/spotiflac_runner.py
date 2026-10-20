#!/usr/bin/env python3
"""
SpotiFLAC Runner — isolated subprocess wrapper.

Invoked by download_manager.py via the isolated venv's Python interpreter.
All parameters are passed as sys.argv to avoid injection risks.

Usage:
    /opt/spotiflac-venv/bin/python spotiflac_runner.py \
        <url> <output_dir> <services_json> <registries_json> [<layout_json>]

File layout: SpotiFLAC can't build folders from its filename template (it
strips "/"), so it downloads into <output_dir>/.staging/<run>/ and the runner
moves each file into the layout from <layout_json> (see DEFAULT_LAYOUT), e.g.
    <Playlist>/<Album Artist>/[<Year>] <Album>/<NN> - <Artist; Artist> - <Title>.flac
Playlists also get <Playlist>/<Playlist>.m3u8 in playlist order. Final paths
are reported in the ___RESULT___ line.

Exit codes:
    0  — download completed successfully
    1  — bad arguments
    2  — SpotiFLAC module not importable
    3  — download raised an exception
"""
import sys
import json
import logging
import time
import os
import re
import asyncio
import contextlib
import glob
import shutil
import types
import collections
import urllib.parse
import threading
from io import StringIO

class ResultCaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.successful = 0
        self.failed = 0
        self.turnstile_failed = False
        self.verification_paused = False
        self.total = 0
        # Unix time the shared download service (zarz-v2: tidal/qobuz/amazon/
        # deezer) told us to come back; 0 if not rate limited
        self.rate_limited_until = 0.0
        # Recent per-request HTTP 429s from the download service, (time,
        # decoded request path): the qobuz extension swallows a rate-limited
        # search and reports "No verified Qobuz track match found", so a "not
        # found" for a track whose lookups were rate limited proves nothing
        self.recent_429 = collections.deque(maxlen=200)
        # A qobuz request (/qbz/…) was rate limited: qobuz searches go through
        # the shared download service, and repeated short qobuz 429s escalated
        # to a 24 h block (2026-10-08), so qobuz is dropped instead of retried
        self.qobuz_limited = False
        # Per-run counts of the download service's refusals, for pacing data
        # and the 428 early stop: 429s by request group, and VERIFY_REQUIRED
        # (HTTP 428) ticket refusals
        self.stats = {"429_qobuz": 0, "429_tickets": 0, "429_other": 0, "428": 0}

    def emit(self, record):
        if getattr(record, "echo", False):
            return  # quoted text (Node's stderr), not something that happened here
        msg = self.format(record)
        self.count(msg)
        self.scan(msg)

    def count(self, msg):
        """Logged lines only (not the stdout copy scan() also sees), so a
        line is never counted twice."""
        hit = re.search(r"HTTP 429 for \w+ (\S+)", msg)
        if hit:
            path = hit.group(1)
            group = "qobuz" if path.startswith("/qbz/") else "tickets" if path.startswith("/tickets") else "other"
            self.stats[f"429_{group}"] += 1
        if "VERIFY_REQUIRED" in msg:
            self.stats["428"] += 1

    def scan(self, msg):
        # Look for success/fail counts; SpotiFLAC's session summary pads the
        # label ("Successful    : 1"), so allow spaces before the colon
        succ_match = re.search(r'Successful\s*:\s*(\d+)', msg)
        fail_match = re.search(r'Failed\s*:\s*(\d+)', msg)
        
        # "[RUN] 306 track(s) · ext:tidal-web · ..." announces the batch size
        run_match = re.search(r'\[RUN\]\s+(\d+)\s+track', msg)
        if run_match:
            self.total = int(run_match.group(1))

        if succ_match:
            self.successful = int(succ_match.group(1))
        if fail_match:
            self.failed = int(fail_match.group(1))
            
        if "Turnstile" in msg and ("timeout" in msg.lower() or "failed" in msg.lower()):
            self.turnstile_failed = True
        if "verification paused" in msg.lower():
            self.verification_paused = True

        # "HTTP 429 for POST /tickets — retry after 670" /
        # "verification paused after a failed attempt — next try in 15 min"
        hit = re.search(r"HTTP 429 for \w+ (\S+)", msg)
        if hit:
            self.recent_429.append((time.time(), urllib.parse.unquote_plus(hit.group(1)).lower()))
            if hit.group(1).startswith("/qbz/"):
                self.qobuz_limited = True
        retry = re.search(r"HTTP 429\b.*?retry after (\d+)", msg)
        pause = re.search(r"verification paused.*?next try in (\d+)\s*min", msg)
        until = 0.0
        if retry and hit and hit.group(1).startswith("/qbz/") and int(retry.group(1)) <= LONG_LIMIT_SECONDS:
            pass  # a short qobuz limit only cools qobuz down, the other providers carry on
        elif retry:
            until = time.time() + int(retry.group(1))
        elif pause:
            until = time.time() + int(pause.group(1)) * 60
        if until > self.rate_limited_until:
            self.rate_limited_until = until


# SpotiFLAC only persists its verification pause; a plain 429 with a long
# Retry-After (seen: "POST /tickets — retry after 86338s") lives in memory
# only. Keep it in a file of the same shape, next to SpotiFLAC's, so the
# pre-start check (verification_backoff_until), the app's Status page and
# the top-bar pill all see it, also after a restart. Not named .zarz-v2-*,
# so SpotiFLAC itself leaves it alone.
APP_BACKOFF_FILE = ".app-ratelimit.auth-backoff.json"


def remember_rate_limit(until, home=os.path.expanduser("~/.spotiflac")):
    path = os.path.join(home, "signed_sessions", APP_BACKOFF_FILE)
    try:
        previous = {}
        with contextlib.suppress(OSError, ValueError):
            with open(path, encoding="utf-8") as f:
                previous = json.load(f) or {}
        if float(previous.get("not_before") or 0) >= until:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"not_before": until, "failures": int(previous.get("failures") or 0) + 1,
                       "source": "spotiflac_runner",
                       "recorded_at": time.time()}, f)
        os.replace(tmp, path)
    except OSError as e:
        logger.error(f"Could not record the rate limit in {path}: {e}")


# A 429 asking us to wait longer than this is a block (seen: 24 h): stop at
# once, every further request may extend it
LONG_LIMIT_SECONDS = 3600

# qobuz-web sends every search and lookup through the shared download
# service; amazon only its downloads (it searches Amazon/song.link itself).
# So qobuz always goes last, and its first 429 ends the qobuz phase and
# skips qobuz for QOBUZ_COOLDOWN_HOURS instead of waiting and retrying.
QOBUZ = "ext:qobuz-web"
try:
    QOBUZ_COOLDOWN_HOURS = float(os.environ.get("QOBUZ_COOLDOWN_HOURS", "24"))
except ValueError:
    QOBUZ_COOLDOWN_HOURS = 24.0


# VERIFY_REQUIRED (HTTP 428) ticket refusals came before penalty 3 (13 in the
# two windows before it): after VERIFY_STOP_COUNT in one run (0 = off), stop
# and pause VERIFY_STOP_MINUTES like a rate limit
def _env_number(name, default, kind=float):
    try:
        return kind(os.environ.get(name, default))
    except ValueError:
        return kind(default)


VERIFY_STOP_COUNT = _env_number("VERIFY_STOP_COUNT", "5", int)
VERIFY_STOP_MINUTES = _env_number("VERIFY_STOP_MINUTES", "30")
# Tracks SpotiFLAC downloads at once (its default 2; 1 is gentler)
MAX_CONCURRENT_DOWNLOADS = max(1, _env_number("MAX_CONCURRENT_DOWNLOADS", "2", int))
# Restarts of a crashed JS extension process per run (install_runtime_revival)
EXT_RUNTIME_RESTARTS = max(0, _env_number("EXT_RUNTIME_RESTARTS", "5", int))


def order_providers(services):
    """qobuz-web last, the rest in the configured order."""
    return [s for s in services if s != QOBUZ] + [s for s in services if s == QOBUZ]


def install_enrichment_filter():
    """Leave qobuz-web out of SpotiFLAC's metadata enrichment: it runs every
    enriching extension for every downloaded track, whatever the provider,
    so qobuz-web made two download-service searches per track (and timed
    out while qobuz was rate limited). The built-in qobuz lookup stays."""
    from SpotiFLAC.core import extension_enrichment as ee

    every, for_service = ee.enrichment_extensions, ee.extensions_for_service
    ee.enrichment_extensions = lambda *a, **k: [e for e in every(*a, **k) if e != "qobuz-web"]
    ee.extensions_for_service = lambda *a, **k: [e for e in for_service(*a, **k) if e != "qobuz-web"]


_revive_lock = threading.Lock()
runtime_restarts = 0


def revive_runtime(provider, rt):
    """Restart a JS extension's dead Node process in place, so the pool keeps
    the same object. Logs what Node said before it died: SpotiFLAC keeps its
    stderr at DEBUG only."""
    global runtime_restarts
    proc = rt._proc
    if proc is not None and proc.poll() is None:
        return
    with _revive_lock:
        if runtime_restarts >= EXT_RUNTIME_RESTARTS:
            return  # the call fails as before ("JSRuntime not started …")
        runtime_restarts += 1
        n = runtime_restarts
    code = proc.returncode if proc is not None else None
    logger.warning(f"{provider.name}: extension process exited (code {code}); "
                   f"restarting it ({n}/{EXT_RUNTIME_RESTARTS} this run)")
    for line in list(rt._stderr_tail)[-8:]:
        logger.warning(f"{provider.name}: Node said: {line}", extra={"echo": True})
    rt.stop()
    rt._stderr_tail.clear()
    rt._ready_event.clear()
    rt._pending.clear()
    rt._progress_cbs.clear()
    try:
        rt.start()
    except Exception as e:
        logger.warning(f"{provider.name}: could not restart the extension process: {e}")


def install_runtime_revival():
    """SpotiFLAC 5.0.1 puts a JS extension's Node process back in its pool
    even after it died and never replaces it, so after one crash every later
    call to that extension failed at once ("JSRuntime not started or already
    terminated") for the rest of the run (amazon, 2026-10-11). Check each one
    as it's handed out and restart it if it's dead."""
    from SpotiFLAC.extensions.provider import JSExtensionProvider

    original = JSExtensionProvider._acquire_runtime

    @contextlib.contextmanager
    def acquire(self):
        with original(self) as rt:
            revive_runtime(self, rt)
            yield rt

    JSExtensionProvider._acquire_runtime = acquire


def verification_backoff_until(home=os.path.expanduser("~/.spotiflac")):
    """When SpotiFLAC's own verification backoff for the shared download
    service ends (its .<namespace>-*.auth-backoff.json files), or 0."""
    latest = 0.0
    for path in glob.glob(os.path.join(home, "signed_sessions", ".*auth-backoff.json")):
        try:
            with open(path, encoding="utf-8") as f:
                latest = max(latest, float(json.load(f).get("not_before") or 0))
        except (OSError, ValueError, AttributeError):
            pass
    return latest

# SPOTIFLAC_DEBUG=1 turns on DEBUG logs for SpotiFLAC, its Turnstile solver and pydoll
DEBUG = os.environ.get("SPOTIFLAC_DEBUG", "").lower() in ("1", "true", "yes")

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True
)

capture_handler = ResultCaptureHandler()
logging.getLogger().addHandler(capture_handler)


class StdoutScanner:
    """Passes stdout through while scanning it, since SpotiFLAC prints its
    session summary (the Successful/Failed counts) instead of logging it."""

    def __init__(self, stream, handler):
        self._stream = stream
        self._handler = handler

    def write(self, text):
        self._handler.scan(text)
        return self._stream.write(text)

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


sys.stdout = StdoutScanner(sys.stdout, capture_handler)

# Unless debugging, drop per-request chatter: httpx/httpcore/hpack log every
# HTTP call, SpotiFLAC's signed session repeats each request at INFO (its 429
# warnings stay), and pydoll/websockets narrate the browser
if not DEBUG:
    for name in ("pydoll", "httpx", "httpcore", "hpack", "websockets",
                 "SpotiFLAC.core.signed_session_mobile"):
        logging.getLogger(name).setLevel(logging.WARNING)

logger = logging.getLogger("spotiflac_runner")


# ── File layout ──────────────────────────────────────────────────────
# Placeholders: {playlist_name} {album_artist} {artist} (first) {artists}
# ("; "-joined) {album} {year} {disc} {title} {track} (album track number,
# 2-digit, also for playlist downloads; playlist order lives in the .m3u8)
DEFAULT_LAYOUT = {
    "folder": "{album_artist}/[{year}] {album}",
    "playlist_folder": "{playlist_name}",
    "track_format": "{track} - {artists} - {title}",
    "single_track_format": "{track} - {artists} - {title}",
}
AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac"}
STAGING_DIR = ".staging"
_UNSAFE = re.compile(r'[\\/*?:"<>|]')


def safe(value, fallback=""):
    value = re.sub(r"\s+", " ", _UNSAFE.sub("", str(value or ""))).strip().rstrip(".").strip()
    return value or fallback


def render(template, fields):
    """Fill a template; returns its non-empty path parts. Empty brackets left
    by a missing value (e.g. "[] Album" without a year) are dropped."""
    parts = []
    for part in template.split("/"):
        for key, value in fields.items():
            part = part.replace("{" + key + "}", value)
        part = re.sub(r"\[\s*\]|\(\s*\)", "", part)
        part = safe(part)
        if part:
            parts.append(part)
    return parts


def emit(tag, payload):
    """Structured line for the app (download_manager parses ___TAG___=json)."""
    print(f"___{tag}___={json.dumps(payload)}", flush=True)


class Layout:
    """Files each track into the layout the moment SpotiFLAC finishes it.

    SpotiFLAC downloads into a staging dir under a unique stem (see
    filename()), and calls on_track() per finished track, after the file is
    in its final location. Downloaded tracks are moved from staging; tracks
    SpotiFLAC skipped because they already exist (matched by ISRC or tags
    against the downloads tree) are moved into the layout too, or hard-linked
    when they belong to another playlist or album tree. A killed run therefore
    loses nothing, and re-queuing a playlist only downloads what's missing.
    """

    def __init__(self, options, url, root, staging):
        self.opts = {**DEFAULT_LAYOUT, **{k: v for k, v in (options or {}).items() if v}}
        self.is_playlist = "/playlist/" in url or ":playlist:" in url
        self.is_track = "/track/" in url or ":track:" in url
        self.root = root
        self.staging = staging
        self.positions = {}       # track id -> playlist/album position
        self.placed = {}          # track id -> (position, final_path)
        self.failed = 0
        self.total = 0
        self.downloaded = 0
        self.existing = 0
        self.playlist_name = None
        # Tracks in the app's download history whose files are no longer under
        # downloads/ (moved to Plex etc.): track id -> (position, recorded path)
        self.previous = {}
        self.fail_streak = 0
        self.stop_for_rate_limit = None  # set by main(): ends the run early
        self.stop_for_qobuz = None  # set by main(): ends the qobuz phase
        # track id -> "Artists - Title" for the whole collection, and the last
        # provider error per track, for the missing-tracks report
        self.names = {}
        self.errors = {}
        # Spotify local files (no artist): never handed to a provider
        self.local = set()
        # Tracks whose existing file turned out not to be FLAC (deleted or
        # moved to .rejected/): download them again, ignore their history
        self.purged = set()
        # Lasting failures from earlier runs, from the app:
        # track id -> {provider: reason}; a track is not handed to a provider
        # listed for it. current_provider is the one SpotiFLAC is running.
        self.skip = {}
        self.current_provider = None
        self._fail_reported = set()
        self._lock = threading.Lock()

    def capture_collection(self, name, tracks):
        """Called by plan_collection() with the resolved collection before any
        track downloads; gives the real playlist name and the total."""
        self.total = len(tracks or [])
        for track in tracks or []:
            if getattr(track, "id", None):
                self.names.setdefault(track.id, f"{track.artists} - {track.title}")
        if self.is_playlist and name and not self.playlist_name:
            self.playlist_name = name
            emit("PLAYLIST", {"name": name})

    def _fields(self, metadata, tags=None):
        tags = tags or {}
        artists = list(metadata.artist_names or []) or [
            a.strip() for a in (metadata.artists or "").split(",") if a.strip()]
        album_artists = list(metadata.album_artist_names or []) or [metadata.album_artist or tags.get("album_artist") or ""]
        # Always the album track number; the playlist position only orders the .m3u8
        number = metadata.track_number or tags.get("track") or 0
        year = (metadata.release_date or "")[:4] or tags.get("year", "")
        return {
            "playlist_name": safe(self.playlist_name, "Playlist"),
            "album_artist": safe(album_artists[0], safe(artists[0] if artists else "", "Unknown Artist")),
            "artist": safe(artists[0] if artists else "", "Unknown Artist"),
            "artists": safe("; ".join(artists), "Unknown Artist"),
            "album": safe(metadata.album, "Unknown Album"),
            "year": year,
            "disc": str(metadata.disc_number or 1),
            "title": safe(metadata.title, "Unknown Title"),
            "track": f"{number:02d}" if number > 0 else "",
        }

    def playlist_dir(self):
        if not self.is_playlist:
            return None
        return os.path.join(self.root, *render(self.opts["playlist_folder"], self._fields_name_only()))

    def _fields_name_only(self):
        return {"playlist_name": safe(self.playlist_name, "Playlist")}

    def destination(self, metadata, ext, with_playlist=True, tags=None):
        fields = self._fields(metadata, tags)
        parts = []
        if self.is_playlist and with_playlist:
            parts += render(self.opts["playlist_folder"], fields)
        parts += render(self.opts["folder"], fields)
        fmt = self.opts["single_track_format" if self.is_track else "track_format"]
        name = " - ".join(render(fmt, fields))
        # A missing value (e.g. no track number) leaves a dangling " - "
        name = re.sub(r"(\s*-\s*){2,}", " - ", name).strip(" -") or fields["title"]
        return os.path.join(self.root, *parts, name + ext)

    def _detect_playlist_name(self):
        # Fallback if capture_collection() didn't run: a staging/<playlist name>/
        # folder, when SpotiFLAC creates one
        if self.playlist_name is None and self.is_playlist:
            try:
                dirs = [d for d in os.listdir(self.staging) if os.path.isdir(os.path.join(self.staging, d))]
            except OSError:
                dirs = []
            if dirs:
                self.playlist_name = dirs[0]
                emit("PLAYLIST", {"name": self.playlist_name})

    def _belongs_elsewhere(self, path, metadata, tags=None):
        """True if an existing file is part of another managed tree: its own
        album-layout location, or a folder (other than this playlist's) that
        holds a .m3u8. Those get hard-linked, not moved, so they keep working."""
        path = os.path.abspath(path)
        ext = os.path.splitext(path)[1]
        if path == os.path.abspath(self.destination(metadata, ext, with_playlist=False, tags=tags)):
            return True
        own = os.path.abspath(self.playlist_dir()) if self.is_playlist else None
        root = os.path.abspath(self.root)
        parent = os.path.dirname(path)
        while parent.startswith(root) and parent != root:
            if parent == own:
                return False
            try:
                if any(n.endswith(".m3u8") for n in os.listdir(parent)):
                    return True
            except OSError:
                pass
            parent = os.path.dirname(parent)
        return False

    def _prune_empty_dirs(self, start):
        """Remove folders emptied by moving files out (e.g. copied desktop-app folders)."""
        root = os.path.abspath(self.root)
        parent = os.path.abspath(start)
        while parent.startswith(root) and parent != root:
            try:
                os.rmdir(parent)
            except OSError:
                return
            parent = os.path.dirname(parent)

    def on_track(self, result, metadata):
        """SpotiFLAC post-download hook, called once per finished track."""
        with self._lock:
            self._detect_playlist_name()
            path = getattr(result, "file_path", None)
            position = self.positions.get(metadata.id, 0)

            # A previously downloaded track (stand-in skipped by SpotiFLAC, or
            # re-downloaded anyway): it's recorded in history, drop the file
            if metadata.id in self.previous and path and \
                    os.path.abspath(path).startswith(os.path.abspath(self.staging)):
                with contextlib.suppress(OSError):
                    os.remove(path)
                return

            # A pre-linked track SpotiFLAC skipped: already filed and counted
            # during planning, so just drop the staging link
            already = self.placed.get(metadata.id)
            if (already and os.path.exists(already[1]) and path
                    and os.path.abspath(path).startswith(os.path.abspath(self.staging))):
                with contextlib.suppress(OSError):
                    os.remove(path)  # the staging link (or a redundant re-download)
                return
            # An existing file this run would count as "already present": a
            # fake FLAC (e.g. MP3 data from before the download check) is
            # removed and the track downloaded again
            if (getattr(result, "success", False) and path and os.path.exists(path)
                    and not os.path.abspath(path).startswith(os.path.abspath(self.staging))
                    and path.lower().endswith(".flac")):
                actual = audio_format(path)
                if actual not in ("flac", "unreadable"):
                    action = dispose_bad_file(path, self.root, actual)
                    self._prune_empty_dirs(os.path.dirname(path))
                    self.purged.add(metadata.id)
                    logger.warning(f"Existing file for '{metadata.artists} - {metadata.title}' is "
                                   f"{actual or 'unknown data'}, not FLAC: {action}; downloading it again")
                    return
            if not getattr(result, "success", False) or not path or not os.path.exists(path):
                self.note_error(metadata, result, getattr(result, "error", "") or "no file produced")
                self.failed += 1
                self.fail_streak += 1
                self._progress(metadata, "failed")
                # The shared download service is rate limited / has paused
                # verification: every remaining track would fail the same
                # way, so stop and let the app wait instead
                until = capture_handler.rate_limited_until
                if self.current_provider == QOBUZ and capture_handler.qobuz_limited and self.stop_for_qobuz:
                    self.stop_for_qobuz()
                if (until > time.time() + LONG_LIMIT_SECONDS
                        or self.fail_streak >= 3 and until > time.time() + 30) and self.stop_for_rate_limit:
                    self.stop_for_rate_limit(until)
                return
            # A fresh download must really be what its extension says: a
            # provider has delivered MP3 data saved as .flac. Reject it, so the
            # track stays missing and the next provider gets a go.
            fresh = not getattr(result, "skipped", False) and \
                os.path.abspath(path).startswith(os.path.abspath(self.staging))
            actual = audio_format(path) if fresh else None
            if fresh and os.path.splitext(path)[1].lower() == ".flac" and actual != "flac":
                provider = self.note_error(metadata, result, f"delivered {actual or 'unknown data'}, not FLAC")
                logger.warning(f"Rejected '{metadata.artists} - {metadata.title}': "
                               f"{provider or 'provider'} delivered {actual or 'unknown data'}, not FLAC")
                with contextlib.suppress(OSError):
                    os.remove(path)
                self.failed += 1
                self._progress(metadata, "failed")
                return
            if fresh:
                with _isrc_lock:
                    waived = isrc_waived.pop((metadata.title, metadata.artists), None)
                if waived and not self._isrc_length_ok(metadata, result, path, waived):
                    return
            self.fail_streak = 0

            ext = os.path.splitext(path)[1]
            tags = read_tags(path)
            dest = self.destination(metadata, ext, tags=tags)
            try:
                if os.path.abspath(path) == os.path.abspath(dest):
                    pass
                elif os.path.exists(dest):
                    if os.path.abspath(path).startswith(os.path.abspath(self.staging)):
                        os.remove(path)  # fresh download of a file already in place
                    logger.info(f"Already in place: {dest}")
                elif getattr(result, "skipped", False):
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    if self._belongs_elsewhere(path, metadata, tags):
                        try:
                            os.link(path, dest)
                        except OSError:
                            shutil.copy2(path, dest)
                        logger.info(f"Linked existing: {path} -> {dest}")
                    else:
                        shutil.move(path, dest)
                        self._prune_empty_dirs(os.path.dirname(path))
                        logger.info(f"Moved existing: {path} -> {dest}")
                else:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.move(path, dest)
                    logger.info(f"Saved: {dest}")
            except OSError as e:
                # shutil.move copies when it can't rename, then deletes the
                # source; if only the delete failed (e.g. a folder the container
                # user can't write to), the file IS in place, so count it.
                complete = os.path.exists(dest) and (
                    not os.path.exists(path) or os.path.getsize(dest) == os.path.getsize(path))
                if complete:
                    logger.warning(f"Placed '{metadata.title}' at {dest}, but could not remove {path}: {e}")
                else:
                    logger.error(f"Could not place '{metadata.title}' at {dest}: {e}")
                    self.errors.setdefault(metadata.id, {})["filing"] = f"could not file: {e}"
                    self.failed += 1
                    self._progress(metadata, "failed")
                    return

            self.placed[metadata.id] = (position, dest)
            state = "existing" if getattr(result, "skipped", False) else "downloaded"
            if state == "existing":
                self.existing += 1
            else:
                self.downloaded += 1
            self._record(metadata, tags, dest, state)
            self._progress(metadata, state)
            if self.is_playlist:
                self.write_m3u()

    def _record(self, metadata, tags, dest, state):
        """___TRACK___ event: the app stores it in its download history."""
        fields = self._fields(metadata, tags)
        try:
            size = os.path.getsize(dest)
        except OSError:
            size = None
        emit("TRACK", {
            "spotify_id": metadata.id,
            "isrc": (getattr(metadata, "isrc", "") or tags.get("isrc") or "").upper() or None,
            "title": fields["title"],
            "artists": fields["artists"],
            "album": fields["album"],
            "album_artist": fields["album_artist"],
            "year": fields["year"] or None,
            "track_number": int(fields["track"]) if fields["track"] else None,
            "file_path": os.path.relpath(dest, self.root),
            "size_bytes": size,
            "playlist": self.playlist_name if self.is_playlist else None,
            "source": state,
        })

    def _isrc_length_ok(self, metadata, result, path, waived):
        """A file let through despite a different ISRC: keep it only if its
        length is within ISRC_MISMATCH_MAX_SECONDS of Spotify's."""
        want, got = waived
        expected = (getattr(metadata, "duration_ms", 0) or 0) / 1000
        actual = audio_seconds(path)
        if expected and actual is not None and abs(actual - expected) <= ISRC_MISMATCH_MAX_SECONDS:
            logger.info(f"Accepted different ISRC for '{metadata.artists} - {metadata.title}': "
                        f"asked for {want}, got {got}; length {actual:.1f}s vs {expected:.1f}s")
            return True
        length = (f"length {actual:.1f}s vs {expected:.1f}s" if expected and actual is not None
                  else "length unknown")
        provider = self.note_error(metadata, result,
                                   f"different recording (ISRC {want} vs {got}), {length}")
        logger.warning(f"Rejected '{metadata.artists} - {metadata.title}' from {provider or 'provider'}: "
                       f"different ISRC and {length}")
        with contextlib.suppress(OSError):
            os.remove(path)
        self.failed += 1
        self._progress(metadata, "failed")
        return False

    _CHAIN_PREFIX = re.compile(r"^All providers failed after \d+ attempt\(s\) — ")
    _PROVIDER_PREFIX = re.compile(r"^(ext:[\w-]+): (.*)$", re.S)

    def note_error(self, metadata, result, error):
        """Record why a provider couldn't deliver a track, one reason per
        provider (tidal, then amazon, then qobuz), and return the provider.
        SpotiFLAC reports provider "none" and puts the real one in the text:
        "All providers failed after 1 attempt(s) — ext:amazon: Track not …"."""
        error = self._CHAIN_PREFIX.sub("", str(error or "").strip())
        provider = getattr(result, "provider", "") or ""
        provider = "" if provider == "none" else provider
        m = self._PROVIDER_PREFIX.match(error)
        if m:
            provider, error = m.group(1), m.group(2)
        provider = provider or self.current_provider or ""
        self.errors.setdefault(metadata.id, {})[provider or "?"] = error
        self._report_failure(metadata, provider, error)
        return provider

    def _report_failure(self, metadata, provider, error):
        """___FAIL___ event for a lasting failure (once per track and provider
        per run): the app counts them and, past TRACK_FAIL_LIMIT, has the
        next runs skip the track for that provider."""
        kind = failure_kind(error)
        if kind is None or not provider or (metadata.id, provider) in self._fail_reported:
            return
        if kind == "match" and NOT_FOUND.search(error) and rate_limited_lookup(metadata.title):
            logger.debug(f"Not counting '{metadata.title}' as not found on {provider}: its lookups were rate limited")
            return
        self._fail_reported.add((metadata.id, provider))
        emit("FAIL", {
            "spotify_id": metadata.id,
            "provider": provider,
            "kind": kind,
            "reason": error[:600],
            "title": getattr(metadata, "title", None),
            "artists": getattr(metadata, "artists", None),
            "album": getattr(metadata, "album", None),
            "playlist": self.playlist_name if self.is_playlist else None,
            "isrc_tolerance": ISRC_MISMATCH_MAX_SECONDS if ACCEPT_ISRC_MISMATCH else 0,
        })

    def error_text(self, track_id):
        """"tidal-web: Wrong track: … · qobuz-web: … · amazon: …"."""
        reasons = self.errors.get(track_id) or {}
        return " · ".join(f"{p.split(':', 1)[-1]}: {e}" for p, e in reasons.items())

    def missing(self):
        """Tracks of the collection that are neither filed nor downloaded
        before, in collection order, with every provider's reason."""
        done = set(self.placed) | set(self.previous)
        ids = sorted((tid for tid in self.positions if tid not in done), key=self.positions.get)
        return [{"id": tid, "name": self.names.get(tid, tid),
                 "error": (self.error_text(tid) or self.skip_text(tid) or "not attempted")[:600]}
                for tid in ids]

    def skip_text(self, track_id):
        """"skipped after repeated failures (tidal-web: …; qobuz-web: …)"."""
        reasons = self.skip.get(track_id)
        if not reasons:
            return ""
        return "skipped after repeated failures (" + "; ".join(
            f"{p.split(':', 1)[-1]}: {r}" for p, r in reasons.items()) + ")"

    def skipped_for(self, track_id, provider):
        return provider in (self.skip.get(track_id) or {})

    def fully_skipped(self, track_id, services):
        reasons = self.skip.get(track_id) or {}
        return bool(services) and all(p in reasons for p in services)

    def _progress(self, metadata, state):
        # "done" counts tracks that are actually filed (or downloaded before).
        # A failure doesn't: the track goes on to the next provider, and it
        # is only missing once every provider has had it (see missing()).
        failed_ids = set(self.errors) - set(self.placed) - set(self.previous)
        reasons = self.errors.get(metadata.id) or {} if state == "failed" else {}
        last = next(reversed(reasons), "") if reasons else ""
        emit("PROGRESS", {
            "done": len(self.placed) + len(self.previous),
            "previous": len(self.previous),
            "total": self.total or capture_handler.total,
            "downloaded": self.downloaded,
            "existing": self.existing,
            "failed": len(failed_ids),
            "state": state,
            "provider": last if last.startswith("ext:") else "",
            "track": f"{metadata.artists} - {metadata.title}",
        })

    def write_m3u(self):
        """<playlist dir>/<Playlist>.m3u8 in playlist order, keeping entries from
        earlier runs that still exist."""
        playlist_dir = self.playlist_dir()
        if not playlist_dir or not (self.placed or self.previous):
            return None
        os.makedirs(playlist_dir, exist_ok=True)
        m3u = os.path.join(playlist_dir, os.path.basename(playlist_dir) + ".m3u8")
        # Previously downloaded tracks keep their recorded path even though the
        # file has moved on (e.g. the whole folder was copied to Plex)
        everything = sorted(list(self.placed.values()) + list(self.previous.values()))
        entries = [os.path.relpath(dest, playlist_dir) for _, dest in everything]
        try:
            if os.path.exists(m3u):
                with open(m3u, encoding="utf-8") as f:
                    old = [l.strip() for l in f if l.strip() and not l.startswith("#")]
                entries += [e for e in old if e not in entries and os.path.exists(os.path.join(playlist_dir, e))]
            tmp = m3u + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n" + "\n".join(entries) + "\n")
            os.replace(tmp, m3u)
        except OSError as e:
            logger.error(f"Failed to write playlist file {m3u}: {e}")
            return None
        return m3u

    def sweep_staging(self):
        """Safety net after a run: anything left in staging that the hook didn't
        file (hook error, odd provider output) is reported, not silently lost."""
        leftovers = []
        for dirpath, _, names in os.walk(self.staging):
            leftovers += [os.path.join(dirpath, n) for n in names
                          if os.path.splitext(n)[1].lower() in AUDIO_EXTS]
        if leftovers:
            logger.warning(f"{len(leftovers)} downloaded file(s) were not filed; kept in {self.staging}")
        return leftovers


def track_url(track):
    return f"https://open.spotify.com/track/{track.id}"


# "(feat. X)" / "[with X]" in a title: releases of one song differ in it
_FEATURING = re.compile(r"\s*[(\[](?:feat\.?|ft\.?|featuring|with)\s[^)\]]*[)\]]", re.IGNORECASE)


def song_key(title, artist):
    """(artist, title), normalised, for matching one song across Spotify IDs
    (single vs album vs compilation); None if either is missing."""
    def norm(text):
        return re.sub(r"[\W_]+", " ", str(text or "").casefold()).strip()
    title, artist = norm(_FEATURING.sub("", str(title or ""))), norm(artist)
    return (artist, title) if artist and title else None


def first_artist(track):
    names = list(getattr(track, "artist_names", None) or []) or \
        [a.strip() for a in str(getattr(track, "artists", "") or "").split(",")]
    return names[0] if names else ""


LOCAL_FILE_REASON = "Spotify local file (no artist on Spotify), can't be searched for"


def is_local_file(track):
    """SpotiFLAC fills in "Unknown Artist" when Spotify lists no artist at
    all, which only happens for local files added to a playlist."""
    return str(getattr(track, "artists", "") or "").strip() in ("", "Unknown Artist")


async def plan_collection(url, layout, root, staging, services, registries, history=None):
    """Resolve the URL's tracks, file those already on disk, return the rest.

    Matching uses SpotiFLAC's own matcher over the whole downloads tree (it
    skips dot-dirs, so not .staging): ISRC when known, then title/artist/album
    tags. Playlist listings carry no ISRC, so tags do the work there.
    """
    from SpotiFLAC import AsyncSpotiFLAC
    from SpotiFLAC.core.playlist_sync import find_existing_track, index_audio_files

    history = history or {}
    # Same song downloaded before under another Spotify ID (another release):
    # artist + title -> its recorded path
    by_song = {}
    for entry in history.values():
        key = song_key(entry.get("title"), entry.get("artist"))
        if key and entry.get("path"):
            by_song.setdefault(key, entry["path"])
    by_song_matches = 0

    kwargs = {"output_dir": staging, "services": services, "sync_extensions": False}
    if registries:
        kwargs["registries"] = registries
    async with AsyncSpotiFLAC(**kwargs) as client:
        info, tracks = await client.get_playlist(url)
    tracks = [t for t in (tracks or []) if getattr(t, "id", None)]
    if not tracks:
        raise ValueError("no tracks resolved")

    layout.capture_collection(info.get("name"), tracks)
    for position, track in enumerate(tracks, 1):
        layout.positions.setdefault(track.id, position)

    index = await asyncio.to_thread(index_audio_files, root)
    pending = []
    for track in tracks:
        existing = find_existing_track(index, track, "")
        if existing is not None and os.path.exists(existing) and track.id not in layout.placed:
            await asyncio.to_thread(
                layout.on_track, types.SimpleNamespace(success=True, skipped=True, file_path=str(existing)), track)
            if track.id in layout.placed:
                continue
        # Not on disk, but downloaded before (in the app's history): the file
        # was moved on (e.g. to Plex), so don't fetch it again
        recorded = history.get(track.id) if track.id not in layout.purged else None
        if not recorded and track.id not in layout.purged:
            other = by_song.get(song_key(track.title, first_artist(track)))
            if other:
                recorded = {"path": other}
                by_song_matches += 1
        if recorded and recorded.get("path"):
            recorded_path = os.path.join(root, recorded["path"])
            if os.path.exists(recorded_path):
                # Still on disk (tag matching just missed it, e.g. "feat."
                # credits): it's an on-disk track, not a moved-on one
                await asyncio.to_thread(
                    layout.on_track, types.SimpleNamespace(success=True, skipped=True, file_path=recorded_path), track)
                if track.id in layout.placed:
                    continue
            else:
                layout.previous[track.id] = (layout.positions.get(track.id, 0), recorded_path)
                layout._progress(track, "previous")
                continue
        pending.append(track)

    # A duplicate playlist entry (same song, another Spotify ID) whose recorded
    # file is gone while its twin is on disk: file it as on disk, not moved on
    if layout.previous:
        def key(t):
            return (str(t.title).casefold().strip(), str(t.artists).casefold().strip())
        on_disk = {key(t): layout.placed[t.id][1] for t in tracks if t.id in layout.placed}
        for track in tracks:
            twin = on_disk.get(key(track)) if track.id in layout.previous else None
            if twin and os.path.exists(twin):
                # Shares the twin's file (on_track could move it away)
                tags = await asyncio.to_thread(read_tags, twin)
                with layout._lock:
                    del layout.previous[track.id]
                    layout.placed[track.id] = (layout.positions.get(track.id, 0), twin)
                    layout.existing += 1
                    layout._record(track, tags, twin, "existing")
                    layout._progress(track, "existing")

    if layout.previous and layout.is_playlist:
        layout.write_m3u()
    logger.info(f"Planned {len(tracks)} track(s): {len(layout.placed)} already on disk, "
                f"{len(layout.previous)} downloaded before (moved on), {len(pending)} to download"
                + (f"; {by_song_matches} matched by artist + title under another Spotify ID" if by_song_matches else ""))
    return tracks, pending


# Opt-in (ACCEPT_ISRC_MISMATCH=true): accept a provider's file whose ISRC
# differs from Spotify's when everything else agrees. SpotiFLAC rejects any
# conflicting ISRC outright, yet the same audio is often released under
# several (another label or region, a compilation). Its title, artist and
# version checks ("(Live)", "(Remix)", karaoke…) still apply, and on_track()
# keeps the file only if its length is within ISRC_MISMATCH_MAX_SECONDS of
# Spotify's; a remaster or cover usually differs by more.
ACCEPT_ISRC_MISMATCH = os.environ.get("ACCEPT_ISRC_MISMATCH", "").lower() in ("1", "true", "yes")
try:
    ISRC_MISMATCH_MAX_SECONDS = float(os.environ.get("ISRC_MISMATCH_MAX_SECONDS", "2"))
except ValueError:
    ISRC_MISMATCH_MAX_SECONDS = 2.0
# (requested title, requested artists) -> (wanted ISRC, delivered ISRC) for
# files let through despite a different ISRC; on_track() checks their length
isrc_waived = {}
_isrc_lock = threading.Lock()


# Lasting failures (the same track fails the same way next time): a wrong
# match, no match at all, a wrong length, a different recording. Anything
# that may be the service's state (rate limits, 5xx, timeouts, Turnstile,
# no payload) is never counted.
TRANSIENT = re.compile(
    r"HTTP (408|425|429|5\d\d)\b|\b429\b|time(d)? ?out|rate.?limit|turnstile|verification|"
    r"ticket|connection|network|temporar|\bunavailable\b|usable payload|cancel|no file produced|"
    r"could not file|exception", re.I)
NOT_FOUND = re.compile(r"Track not available|No verified|not_found|not found", re.I)
LASTING = re.compile(r"Wrong track|Validation failed|Wrong duration|not FLAC", re.I)
# A "not found" may be a swallowed rate limit: a rate-limited search for the
# title in the last 5 min, or any rate-limited ID lookup in the last 20 s
# (those can't be tied to a track; 2 tracks download in parallel)
SEARCH_429_WINDOW = 300
LOOKUP_429_WINDOW = 20


def rate_limited_lookup(title):
    now = time.time()
    title = str(title or "").strip().lower()
    for at, path in list(capture_handler.recent_429):
        if "search?" in path and title and title in path and now - at < SEARCH_429_WINDOW:
            return True
        if "/get?" in path and now - at < LOOKUP_429_WINDOW:
            return True
    return False


def failure_kind(error):
    """"isrc", "match" (lasting) or None (may be temporary: don't count)."""
    error = str(error or "")
    if TRANSIENT.search(error):
        return None
    if "different recording" in error:
        return "isrc"
    if NOT_FOUND.search(error) or LASTING.search(error):
        return "match"
    return None


def install_isrc_leniency():
    import SpotiFLAC.extensions.provider as provider_module

    original = provider_module.track_identity_mismatch

    def lenient(**kwargs):
        reason = original(**kwargs)
        if not reason.startswith("different recording: asked for ISRC"):
            return reason
        # Same checks without the ISRCs: title, artist, version, album
        rest = original(**{**kwargs, "expected_isrc": "", "found_isrc": ""})
        if rest:
            return rest
        with _isrc_lock:
            isrc_waived[(kwargs.get("expected_title", ""), kwargs.get("expected_artist", ""))] = (
                kwargs.get("expected_isrc", ""), kwargs.get("found_isrc", ""))
        return ""

    provider_module.track_identity_mismatch = lenient
    logger.info(f"Accepting a different ISRC when title, artist and version match and the "
                f"length is within {ISRC_MISMATCH_MAX_SECONDS:g}s (ACCEPT_ISRC_MISMATCH)")


def audio_seconds(path):
    try:
        import mutagen
        audio = mutagen.File(path)
        return float(audio.info.length) if audio and audio.info else None
    except Exception:
        return None


def install_worker_filter(layout, root):
    """Keep tracks that are already filed (or downloaded before) away from
    SpotiFLAC's download worker entirely.

    Every SpotiFLAC path hands the resolved track list, ISRCs filled in, to
    SpotiflacDownloader._run_worker_async before any provider is contacted.
    Dropping tracks there costs nothing; letting SpotiFLAC "skip" them costs a
    provider availability lookup each (~60 s per track on tidal-web). The ISRCs
    also allow a second, exact matching pass against the downloads tree for
    tracks the planner's tag matching missed (e.g. "feat." artist credits).
    """
    import SpotiFLAC.application.download_engine as engine
    from SpotiFLAC.core.playlist_sync import find_existing_track, index_audio_files

    original = engine.SpotiflacDownloader._run_worker_async

    async def filtered(self, tracks, *args, **kwargs):
        index = None
        keep = []
        skipped = 0
        for track in tracks:
            if track.id in layout.placed or track.id in layout.previous or track.id in layout.local:
                continue
            if layout.skipped_for(track.id, layout.current_provider):
                skipped += 1
                continue
            if track.isrc:
                if index is None:
                    index = await asyncio.to_thread(index_audio_files, root)
                existing = find_existing_track(index, track, "")
                if existing is not None and os.path.exists(existing):
                    await asyncio.to_thread(
                        layout.on_track,
                        types.SimpleNamespace(success=True, skipped=True, file_path=str(existing)), track)
                    if track.id in layout.placed:
                        continue
            keep.append(track)
        logger.info(f"Handing {len(keep)} of {len(tracks)} track(s) to SpotiFLAC; "
                    f"the rest are already filed or were downloaded before"
                    + (f"; {skipped} skipped for {layout.current_provider} after repeated failures"
                       if skipped else ""))
        if not keep:
            return []
        return await original(self, keep, *args, **kwargs)

    engine.SpotiflacDownloader._run_worker_async = filtered


def audio_format(path):
    """The container a file's bytes actually hold ("flac", "mp3", "mp4",
    "ogg", "wav"), from its magic bytes; None if unrecognised."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:3] == b"ID3" and len(head) >= 10:
                # ID3v2 tag in front (MP3, occasionally FLAC): skip it
                size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
                f.seek(10 + size)
                head = f.read(12)
    except OSError:
        return "unreadable"
    if head[:4] == b"fLaC":
        return "flac"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"RIFF":
        return "wav"
    return None


# Lossy files aren't wanted unless ALLOW_LOSSY_FALLBACK is on: then a fake
# FLAC is deleted. With lossy allowed it's kept aside in downloads/.rejected/
# (a dot-folder, so SpotiFLAC's matcher doesn't count it as present).
ALLOW_LOSSY = os.environ.get("ALLOW_LOSSY_FALLBACK", "").lower() in ("1", "true", "yes")
REJECTED_DIR = ".rejected"


def dispose_bad_file(path, root, actual):
    """Delete (or, with lossy allowed, move to .rejected/) a .flac that isn't
    FLAC, and tell the app to drop its history entry. Returns what happened."""
    rel = os.path.relpath(path, root)
    if ALLOW_LOSSY:
        dest = os.path.join(root, REJECTED_DIR, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(path, dest)
        action = f"moved to {REJECTED_DIR}/"
    else:
        os.remove(path)
        action = "deleted"
    emit("PURGE", {"file_path": rel, "format": actual, "action": action})
    return action


def read_tags(path):
    """Year, album track number and album artist from a file's own tags, to
    fill gaps in listing metadata (playlist listings have no release date)."""
    try:
        import mutagen
        tags = mutagen.File(path, easy=True) or {}
    except Exception:
        return {}

    def first(key):
        values = tags.get(key) or []
        return str(values[0]).strip() if values else ""

    number = first("tracknumber").split("/")[0]
    return {
        "year": first("date")[:4] if first("date")[:4].isdigit() else "",
        "track": int(number) if number.isdigit() else 0,
        "album_artist": first("albumartist"),
        "isrc": first("isrc"),
    }


def clean_stale_staging(root, max_age_hours=12):
    """Remove staging dirs left behind by killed runs."""
    base = os.path.join(root, STAGING_DIR)
    if not os.path.isdir(base):
        return
    cutoff = time.time() - max_age_hours * 3600
    for name in os.listdir(base):
        path = os.path.join(base, name)
        try:
            if os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                logger.info(f"Removed stale staging dir {path}")
        except OSError:
            pass

def main():
    if len(sys.argv) < 5:
        logger.error(
            "Usage: spotiflac_runner.py <url> <output_dir> "
            "<services_json> <registries_json>"
        )
        sys.exit(1)

    url = sys.argv[1]
    output_dir = sys.argv[2]

    try:
        services = json.loads(sys.argv[3])
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON for services: {e}")
        sys.exit(1)
    if order_providers(services) != services:
        services = order_providers(services)
        logger.info(f"qobuz-web moved to the end of the provider order: {services}")

    try:
        registries = json.loads(sys.argv[4])
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON for registries: {e}")
        sys.exit(1)

    try:
        layout_options = json.loads(sys.argv[5]) if len(sys.argv) > 5 else {}
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON for layout: {e}")
        sys.exit(1)

    # Optional: JSON file of previously downloaded tracks from the app's
    # history, {"<spotify id>": {"path": "<relative to downloads>", "title",
    # "artist" (first artist)}}
    history = {}
    if len(sys.argv) > 6 and sys.argv[6]:
        try:
            with open(sys.argv[6], encoding="utf-8") as f:
                history = json.load(f) or {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not read download history ({e}); continuing without it")

    # Optional: JSON file of lasting failures from earlier runs,
    # {"<spotify id>": {"<provider>": "<reason>"}}: skip the track there
    skip = {}
    if len(sys.argv) > 7 and sys.argv[7]:
        try:
            with open(sys.argv[7], encoding="utf-8") as f:
                skip = json.load(f) or {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not read the skip list ({e}); trying every track")

    # ── Import SpotiFLAC from the isolated venv ──────────────────────
    try:
        from SpotiFLAC import SpotiFLAC
    except ImportError as e:
        logger.error(f"Failed to import SpotiFLAC: {e}")
        sys.exit(2)

    # ── Log what we're about to do ───────────────────────────────────
    logger.info(f"URL:        {url}")
    logger.info(f"Output dir: {output_dir}")
    logger.info(f"Services:   {services}")
    logger.info(f"Registries: {registries}")
    logger.info(f"Debug:      {DEBUG}")

    clean_stale_staging(output_dir)
    staging = os.path.join(output_dir, STAGING_DIR, f"{os.getpid()}-{int(time.time())}")
    os.makedirs(staging, exist_ok=True)
    layout = Layout(layout_options, url, output_dir, staging)
    layout.skip = skip
    logger.info(f"Layout:     {layout.opts}")

    # ── Plan: resolve the collection, file what's already on disk ────
    # SpotiFLAC's own playlist paths ignore filename_format, playlist
    # subfolders and its existing-track index, so the runner plans the run
    # itself: resolve the tracks, match them against the downloads tree with
    # SpotiFLAC's matcher (ISRC, then title/artist/album), file the matches,
    # and download only what's missing as explicit track links. Downloaded
    # tracks are filed by Layout.on_track (a post_download_hooks callable,
    # which every path honours) from their metadata, not their file name.
    pending = None  # None: planning failed, download the URL as-is
    plan_tracks = []
    try:
        plan_tracks, pending = asyncio.run(plan_collection(url, layout, output_dir, staging, services, registries, history))
    except Exception as e:
        logger.warning(f"Could not plan the download ({e}); downloading the URL directly")
    if pending:
        given_up = [t for t in pending if layout.fully_skipped(t.id, services)]
        if given_up:
            pending = [t for t in pending if not layout.fully_skipped(t.id, services)]
            logger.info(f"Skipping {len(given_up)} track(s) that failed repeatedly with every provider")
    if pending:
        # Spotify local files (the playlist owner's own uploads) come without
        # artist or album, so no provider can match them, and amazon's
        # artist-less search for them crashes its Node process
        local = [t for t in pending if is_local_file(t)]
        if local:
            pending = [t for t in pending if not is_local_file(t)]
            for t in local:
                layout.local.add(t.id)
                layout.errors.setdefault(t.id, {})["spotify"] = LOCAL_FILE_REASON
            logger.info(f"Skipping {len(local)} Spotify local file(s): no artist on Spotify, nothing to search for")
    if ACCEPT_ISRC_MISMATCH:
        try:
            install_isrc_leniency()
        except (ImportError, AttributeError) as e:
            logger.warning(f"Could not relax SpotiFLAC's ISRC check ({e}); a different ISRC still rejects")
    try:
        install_worker_filter(layout, output_dir)
    except (ImportError, AttributeError) as e:
        logger.warning(f"Could not filter already-filed tracks from SpotiFLAC ({e}); "
                       "they'll be checked with the provider and skipped instead")
    try:
        install_enrichment_filter()
    except (ImportError, AttributeError) as e:
        logger.warning(f"Could not leave qobuz-web out of metadata enrichment ({e})")
    try:
        install_runtime_revival()
    except (ImportError, AttributeError) as e:
        logger.warning(f"Could not watch for crashed extension processes ({e})")

    # Rate limits on the shared download service hold the whole job
    layout.stop_for_rate_limit = lambda until: finish(
        layout, staging, verification_paused=True, rate_limited_until=until, hard=True)
    if pending is None or pending:
        backoff = verification_backoff_until()
        if backoff > time.time() + 60:
            logger.warning("Download service verification is in backoff; not starting downloads")
            finish(layout, staging, verification_paused=True, rate_limited_until=backoff)

    # ── Circuit Breaker Setup ────────────────────────────────────────
    circuit_breaker_file = os.path.expanduser("~/.spotiflac/circuit_breaker.json")
    breaker_state = {}
    if os.path.exists(circuit_breaker_file):
        try:
            with open(circuit_breaker_file, "r") as f:
                breaker_state = json.load(f)
        except:
            pass

    def save_breaker_state():
        try:
            os.makedirs(os.path.dirname(circuit_breaker_file), exist_ok=True)
            with open(circuit_breaker_file, "w") as f:
                json.dump(breaker_state, f)
        except Exception as e:
            logger.error(f"Failed to save circuit breaker: {e}")

    def cool_down_qobuz():
        until = time.time() + QOBUZ_COOLDOWN_HOURS * 3600
        breaker_state[QOBUZ] = {"penalty_level": 0, "penalty_until": until, "reason": "rate limited"}
        save_breaker_state()
        logger.warning(f"{QOBUZ} rate limited: skipping it until "
                       f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(until))} instead of retrying")

    def stop_qobuz():
        # qobuz is the last provider, so the run is simply over: the job
        # finishes with what's missing instead of waiting for the service
        cool_down_qobuz()
        finish(layout, staging, hard=True)

    layout.stop_for_qobuz = stop_qobuz

    # The hook only sees finished tracks; a 24 h block mid-track, or a qobuz
    # search 429 with downloads still running, must stop the run at once
    def watchdog():
        while True:
            time.sleep(1)
            until = capture_handler.rate_limited_until
            blocked = until > time.time() + LONG_LIMIT_SECONDS
            qobuz = layout.current_provider == QOBUZ and capture_handler.qobuz_limited
            refused = 0 < VERIFY_STOP_COUNT <= capture_handler.stats["428"]
            if (blocked or qobuz or refused) and not finishing.is_set():
                with layout._lock:  # not while a track is being filed
                    if blocked:
                        layout.stop_for_rate_limit(until)
                    elif qobuz:
                        stop_qobuz()
                    else:
                        logger.warning(f"Stopped: {capture_handler.stats['428']} verification refusals "
                                       f"(HTTP 428) this run; pausing {VERIFY_STOP_MINUTES:g} min")
                        layout.stop_for_rate_limit(time.time() + VERIFY_STOP_MINUTES * 60)

    threading.Thread(target=watchdog, name="rate-limit-watchdog", daemon=True).start()

    # ── Fallback Execution Loop ──────────────────────────────────────
    # Per track: each provider only gets the tracks still missing.
    start_time = time.time()
    turnstile_failed = False
    verification_paused = False

    for provider in services:
        if pending is not None and not pending:
            break
        now = time.time()
        # Check circuit breaker
        if provider in breaker_state:
            penalty_until = breaker_state[provider].get("penalty_until", 0)
            if now < penalty_until:
                logger.warning(f"Skipping {provider} due to active circuit breaker (cool-down).")
                continue
        if provider == QOBUZ and capture_handler.qobuz_limited:
            cool_down_qobuz()  # a qobuz request was rate limited earlier in the run
            continue

        layout.current_provider = provider
        if pending is not None and all(layout.skipped_for(t.id, provider) for t in pending):
            logger.info(f"Skipping {provider}: every remaining track failed there repeatedly")
            continue
        left = "all" if pending is None else len(pending)
        logger.info(f"Attempting download with provider: {provider} ({left} track(s))")

        # Reset capture handler for this run
        capture_handler.successful = 0
        capture_handler.failed = 0
        capture_handler.turnstile_failed = False
        capture_handler.verification_paused = False

        kwargs = {
            "output_dir": staging,
            "post_download_hooks": [layout.on_track],
            "services": [provider],  # Run one at a time for fallback
            # SpotiFLAC defaults to WARNING, which hides the solver's progress logs
            "log_level": logging.DEBUG if DEBUG else logging.INFO,
            "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
        }
        # Always the collection URL: SpotiFLAC then resolves ISRCs in bulk and
        # downloads in parallel. (Passing track links instead ran one slow,
        # ISRC-less run per track: "No verified TIDAL track match found".)
        kwargs["url"] = url
        if registries:
            kwargs["registries"] = registries

        try:
            placed_before = len(layout.placed)
            # We must catch SystemExit because SpotiFLAC might call sys.exit() on failure
            try:
                SpotiFLAC(**kwargs)
            except SystemExit as se:
                logger.info(f"SpotiFLAC sys.exit({se.code}) caught.")

            placed_now = len(layout.placed) - placed_before
            if pending is not None:
                pending = [t for t in pending if t.id not in layout.placed]

            # qobuz was rate limited as its phase ended (the watchdog stops
            # it otherwise): cool it down, don't wait for the service
            if provider == QOBUZ and capture_handler.qobuz_limited and \
                    capture_handler.rate_limited_until <= time.time() + LONG_LIMIT_SECONDS:
                cool_down_qobuz()
                continue

            if placed_now > 0:
                logger.info(f"{provider}: filed {placed_now} track(s)"
                            + (f", {len(pending)} still missing" if pending else ""))
                # Reset circuit breaker for this provider on success
                if provider in breaker_state:
                    breaker_state.pop(provider)
                    save_breaker_state()
                if pending is None or not pending:
                    break
                continue

            if capture_handler.verification_paused:
                verification_paused = True

            # tidal/qobuz/amazon/deezer share one download service: if it's
            # rate limited, the next provider would fail the same way
            if capture_handler.rate_limited_until > time.time() + 30:
                finish(layout, staging, turnstile_failed=turnstile_failed, verification_paused=True,
                       rate_limited_until=capture_handler.rate_limited_until)

            # If failed, penalize if it was a Turnstile failure
            if capture_handler.turnstile_failed:
                turnstile_failed = True
                logger.warning(f"Turnstile challenge failed for {provider}.")

                state = breaker_state.get(provider, {"penalty_level": 0})
                # Escalate penalty (mins): 30, 60, 120, 240, 480, 1440 max
                level = min(state["penalty_level"] + 1, 6)
                minutes = 30 * (2 ** (level - 1))
                if level == 6: minutes = 1440

                state["penalty_level"] = level
                state["penalty_until"] = now + (minutes * 60)
                breaker_state[provider] = state
                save_breaker_state()
                logger.warning(f"Circuit breaker triggered for {provider}: {minutes}m cooldown.")

        except Exception as e:
            logger.error(f"Download exception using {provider}: {e}", exc_info=True)
            # Continue to next provider

    elapsed = time.time() - start_time
    logger.info(f"Fallback chain completed in {elapsed:.1f}s")
    finish(layout, staging, turnstile_failed=turnstile_failed, verification_paused=verification_paused)


finishing = threading.Event()


def finish(layout, staging, *, turnstile_failed=False, verification_paused=False,
           rate_limited_until=0.0, hard=False):
    """Write the playlist file, report ___RESULT___ and exit.

    rate_limited_until > 0 means the run stopped early because the shared
    download service is rate limited / paused verification until then: the
    app keeps the job waiting and re-runs it afterwards (filed tracks are
    skipped). hard=True exits immediately (called from a SpotiFLAC hook thread,
    with downloads still in flight that should not continue)."""
    finishing.set()  # the watchdog leaves a run that is already ending alone
    total_successful = len(layout.placed) + len(layout.previous)
    m3u = layout.write_m3u() if layout.is_playlist else None
    # Keep staging only if something in it wasn't filed (cleaned after 12 h)
    if hard or not layout.sweep_staging():
        shutil.rmtree(staging, ignore_errors=True)
    missing = layout.missing() if layout.positions else []
    logger.info(f"Filed {len(layout.placed)} track(s): {layout.downloaded} downloaded, "
                f"{layout.existing} already present"
                + (f"; {len(layout.previous)} downloaded before" if layout.previous else "")
                + (f"; {len(missing)} missing" if layout.positions else "")
                + f" ({layout.failed} failed attempt(s) across providers)")
    for item in missing:
        logger.info(f"Missing: {item['name']} ({item['error']})")
    st = capture_handler.stats
    logger.info(f"Download service this run: {st['429_qobuz'] + st['429_tickets'] + st['429_other']}×429 "
                f"(qobuz {st['429_qobuz']}, tickets {st['429_tickets']}, other {st['429_other']}), "
                f"{st['428']}×428")

    result = {
        "successful": total_successful,
        "failed": 0 if total_successful > 0 else 1,
        "turnstile_failed": turnstile_failed,
        "verification_paused": verification_paused,
        "files": [dest for _, dest in sorted(layout.placed.values())],
        "m3u": m3u,
        "downloaded": layout.downloaded,
        "existing": layout.existing,
        "track_failures": layout.failed,
        "previous": len(layout.previous),
        "rate_limited_until": rate_limited_until or None,
        "service_stats": dict(capture_handler.stats),
        # Only for collections the runner planned (positions known)
        "missing": missing if layout.positions else None,
    }
    print(f"\n___RESULT___={json.dumps(result)}\n", flush=True)

    if rate_limited_until:
        logger.warning("Stopped: download service rate limited until "
                       f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(rate_limited_until))}")
        remember_rate_limit(rate_limited_until)
        code = 75  # EX_TEMPFAIL: try again later
    elif total_successful == 0:
        logger.error("Download failed across all available providers.")
        code = 1
    else:
        logger.info("Status: Download completed!")
        code = 0
    if hard:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    sys.exit(code)


if __name__ == "__main__":
    main()
