"""
Lasting per-track failures (wrong match, not found, wrong length), counted
per provider from the runner's ___FAIL___ events.

After TRACK_FAIL_LIMIT failures a provider is skipped for that track for
TRACK_FAIL_RETRY_DAYS, so the same few hundred unmatched tracks aren't looked
up again in every download window. A track skipped by every provider in the
chain shows in the track history as "Skipped". Filing the track, or retrying
its job, clears its rows.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import TrackFailure

logger = logging.getLogger(__name__)


def isrc_tolerance() -> float:
    """The ISRC length tolerance in effect (0 when different ISRCs are rejected)."""
    settings = get_settings()
    return settings.isrc_mismatch_max_seconds if settings.accept_isrc_mismatch else 0.0


def record(db: Session, data: Dict[str, Any], download_id: int) -> None:
    """Count one ___FAIL___ event."""
    spotify_id = (data.get("spotify_id") or "").strip()
    provider = (data.get("provider") or "").strip()
    if not spotify_id or not provider:
        return
    row = db.query(TrackFailure).filter(
        TrackFailure.spotify_id == spotify_id, TrackFailure.provider == provider).first()
    if row is None:
        row = TrackFailure(spotify_id=spotify_id, provider=provider, count=0)
        db.add(row)
    for name in ("title", "artists", "album", "playlist", "reason", "kind"):
        value = data.get(name)
        if value not in (None, ""):
            setattr(row, name, str(value)[:600])
    tolerance = data.get("isrc_tolerance")
    row.isrc_tolerance = float(tolerance) if tolerance is not None else None
    row.count = (row.count or 0) + 1
    row.download_id = download_id
    row.last_at = datetime.utcnow()
    db.flush()  # sessions here don't autoflush; a second event must find this row


def clear(db: Session, spotify_ids: Iterable[str]) -> int:
    ids = [i for i in spotify_ids if i]
    if not ids:
        return 0
    return db.query(TrackFailure).filter(TrackFailure.spotify_id.in_(ids)) \
        .delete(synchronize_session=False)


def clear_download(db: Session, download_id: int) -> int:
    """Give a job's skipped tracks another try (its Retry button)."""
    ids = [r.spotify_id for r in db.query(TrackFailure.spotify_id)
           .filter(TrackFailure.download_id == download_id).distinct()]
    return clear(db, ids)


def _counts(row: TrackFailure, limit: int, since: datetime, tolerance: float) -> bool:
    """Whether a row has reached the limit and still applies."""
    if (row.count or 0) < limit or not row.last_at or row.last_at < since:
        return False
    # A looser ISRC tolerance than when it failed: worth another try
    if row.kind == "isrc" and (row.isrc_tolerance or 0) < tolerance:
        return False
    return True


def active(db: Session) -> Dict[str, Dict[str, TrackFailure]]:
    """spotify_id -> {provider: row} for every provider a track is skipped for."""
    settings = get_settings()
    limit = settings.track_fail_limit
    if limit <= 0:
        return {}
    since = datetime.utcnow() - timedelta(days=settings.track_fail_retry_days)
    tolerance = isrc_tolerance()
    skips: Dict[str, Dict[str, TrackFailure]] = {}
    for row in db.query(TrackFailure).filter(TrackFailure.count >= limit,
                                             TrackFailure.last_at >= since):
        if _counts(row, limit, since, tolerance):
            skips.setdefault(row.spotify_id, {})[row.provider] = row
    return skips


def skip_list(db: Session) -> Dict[str, Dict[str, str]]:
    """The runner's skip file: {spotify_id: {provider: reason}}."""
    return {sid: {p: (r.reason or "") for p, r in rows.items()}
            for sid, rows in active(db).items()}


def skipped_tracks(db: Session, services: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Tracks skipped by every provider in the chain, newest first, for the
    track history: title/artists/album/playlist, per-provider reasons, when."""
    services = services if services is not None else get_settings().spotiflac_services
    tracks = []
    for sid, rows in active(db).items():
        if not services or not all(p in rows for p in services):
            continue
        latest = max(rows.values(), key=lambda r: r.last_at)
        tracks.append({
            "spotify_id": sid,
            "title": latest.title,
            "artists": latest.artists,
            "album": latest.album,
            "playlist": latest.playlist,
            "download_id": latest.download_id,
            "attempts": max(r.count or 0 for r in rows.values()),
            "reasons": {p.split(":", 1)[-1]: r.reason for p, r in rows.items()},
            "last_at": latest.last_at,
        })
    tracks.sort(key=lambda t: t["last_at"], reverse=True)
    return tracks
