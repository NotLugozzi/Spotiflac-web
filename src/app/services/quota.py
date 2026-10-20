"""Dynamic daily quota (QUOTA_MODE=dynamic): the daily download cap learns
from our own download-service penalties instead of staying at a fixed
DAILY_TRACK_CAP.

The service has blocked us for 24 h after 1,166, 874, 526 and 578 downloads
in a day, so a fixed number goes stale. AIMD:

* a penalty (a block longer than LONG_LIMIT_SECONDS) sets the cap to
  QUOTA_BACKOFF x the downloads in the 24 h before it;
* after a clean day (24 h since the last change, no penalty, and the cap
  was reached at least once, so it's what limited us) the cap goes up by
  QUOTA_STEP, but never above QUOTA_CEILING x the lowest trigger of the
  last QUOTA_MEMORY_DAYS days (QUOTA_MAX once those have aged out).

State lives in ~/.spotiflac/quota_state.json (data/spotiflac_home on the
host). Without it, the first call seeds the penalty list from the Status
page's "ours" events and the track history; deleting it re-seeds.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.config import get_settings
from app.database import get_db_session

logger = logging.getLogger(__name__)

DAY = 86400
# Same threshold as the runner: a 429 asking to wait longer is a block
LONG_LIMIT_SECONDS = 3600
APP_BACKOFF_FILE = ".app-ratelimit.auth-backoff.json"
KEEP_CHANGES = 50

_lock = threading.Lock()


def state_path() -> str:
    return os.path.join(os.path.expanduser("~/.spotiflac"), "quota_state.json")


def _load() -> Optional[Dict[str, Any]]:
    try:
        with open(state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and "cap" in data else None
    except (OSError, ValueError):
        return None


def _save(state: Dict[str, Any]) -> None:
    path = state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        logger.error(f"Could not save the quota state in {path}: {e}")


def _utc_ts(dt: datetime) -> float:
    return (dt - datetime(1970, 1, 1)).total_seconds()


def _utc_dt(ts: float) -> datetime:
    return datetime(1970, 1, 1) + timedelta(seconds=ts)


def downloads_before(at: float, hours: float = 24) -> int:
    """Tracks we downloaded in the ``hours`` before unix time ``at``."""
    from app.models import TrackHistory
    end = _utc_dt(at)
    with get_db_session() as db:
        return db.query(TrackHistory).filter(
            TrackHistory.source == "downloaded",
            TrackHistory.first_seen_at >= end - timedelta(hours=hours),
            TrackHistory.first_seen_at <= end,
        ).count()


def _clamp(value: float) -> int:
    s = get_settings()
    return int(max(s.quota_min, min(s.quota_max, round(value))))


def ceiling(state: Dict[str, Any], now: float) -> int:
    """Highest cap a clean day may raise to."""
    s = get_settings()
    since = now - s.quota_memory_days * DAY
    recent = [p["count"] for p in state.get("penalties", []) if p["at"] >= since]
    if not recent:
        return int(s.quota_max)
    return _clamp(int(s.quota_ceiling * min(recent)))


def _change(state: Dict[str, Any], now: float, new: int, why: str) -> Dict[str, Any]:
    change = {"at": now, "from": state.get("cap"), "to": new, "why": why}
    state["cap"] = new
    state["changes"] = (state.get("changes", []) + [change])[-KEEP_CHANGES:]
    return change


def _seed_penalties() -> List[Dict[str, Any]]:
    """Past penalties from the Status page's "ours" events: a "limited"
    run of events that lasted longer than LONG_LIMIT_SECONDS. Penalties
    with fewer downloads before them than QUOTA_MIN carry no information
    about the quota (e.g. before the track history started)."""
    from app.models import StatusEvent
    with get_db_session() as db:
        rows = [(e.state, e.created_at) for e in db.query(StatusEvent)
                .filter(StatusEvent.component == "ours")
                .order_by(StatusEvent.created_at, StatusEvent.id).all()]
    spans, start = [], None
    for state, at in rows:
        if state == "limited" and start is None:
            start = at
        elif state != "limited" and start is not None:
            spans.append((start, at))
            start = None
    if start is not None:
        spans.append((start, None))
    penalties = []
    now = time.time()
    for begin, end in spans:
        at = _utc_ts(begin)
        lifted = _utc_ts(end) if end else None
        if (lifted or now) - at <= LONG_LIMIT_SECONDS:
            continue
        count = downloads_before(at)
        if count < get_settings().quota_min:
            continue
        penalties.append({"at": at, "until": lifted, "count": count, "failures": None, "source": "seed"})
    return penalties


def _seed(now: float) -> Dict[str, Any]:
    s = get_settings()
    state = {"version": 1, "cap": None, "changed_at": now, "reached": False,
             "penalties": _seed_penalties(), "changes": []}
    if state["penalties"]:
        last = state["penalties"][-1]
        _change(state, now, min(_clamp(s.quota_backoff * last["count"]), ceiling(state, now)),
                f"seeded from {len(state['penalties'])} past penalties "
                f"(last at {last['count']} downloads in 24 h)")
        # The clean-day clock starts when that penalty lifted
        state["changed_at"] = last["until"] or (last["at"] + DAY)
    else:
        _change(state, now, _clamp(s.daily_track_cap or s.quota_max), "no past penalties: starting at DAILY_TRACK_CAP")
    return state


def _active_block(now: float) -> Optional[Dict[str, Any]]:
    """The current long block, if any: {"until", "failures", "hit_at"}."""
    from app.services.service_status import download_backoff
    backoff = download_backoff()
    until = backoff["until"]
    if until - now <= LONG_LIMIT_SECONDS:
        return None
    hit_at = now
    # The runner writes when it saw the block; trust that over "now" (the
    # app may have been down when it happened)
    path = os.path.join(os.path.expanduser("~/.spotiflac"), "signed_sessions", APP_BACKOFF_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        recorded = float(data.get("recorded_at") or 0)
        if abs(float(data.get("not_before") or 0) - until) < LONG_LIMIT_SECONDS and 0 < recorded <= now:
            hit_at = recorded
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {"until": until, "failures": backoff["failures"], "hit_at": hit_at}


def _known(state: Dict[str, Any], block: Dict[str, Any]) -> bool:
    for p in state["penalties"]:
        if p.get("until") and abs(p["until"] - block["until"]) < LONG_LIMIT_SECONDS:
            return True
        if abs(p["at"] - block["hit_at"]) < 2 * LONG_LIMIT_SECONDS:
            return True
    return False


def tick(count_24h: int, now: Optional[float] = None) -> Dict[str, Any]:
    """The current dynamic cap, after applying any new penalty or clean
    day. ``count_24h``: tracks downloaded in the last 24 h. Cheap when
    nothing changes (one small file read and a glob)."""
    from app.services.download_manager import record_status_event
    s = get_settings()
    now = time.time() if now is None else now
    events = []
    with _lock:
        state = _load()
        dirty = False
        if state is None:
            state = _seed(now)
            dirty = True
            events.append(("ok", f"Dynamic cap {state['cap']}: {state['changes'][-1]['why']}"))
        block = _active_block(now)
        if block and not _known(state, block):
            count = downloads_before(block["hit_at"])
            state["penalties"].append({"at": block["hit_at"], "until": block["until"], "count": count,
                                       "failures": block["failures"], "source": "live"})
            old = state["cap"]
            change = _change(state, now, _clamp(s.quota_backoff * count),
                             f"penalty after {count} downloads in 24 h")
            state["changed_at"] = block["until"]
            state["reached"] = False
            dirty = True
            events.append(("limited", f"Penalty after {count} downloads in 24 h: dynamic cap {old} → {change['to']}"))
        if not block:
            if count_24h >= state["cap"] and not state["reached"]:
                state["reached"] = True
                dirty = True
            if state["reached"] and now - state["changed_at"] >= DAY:
                new = min(state["cap"] + s.quota_step, ceiling(state, now))
                if new > state["cap"]:
                    old = state["cap"]
                    _change(state, now, new, "clean day at the cap")
                    state["changed_at"] = now
                    state["reached"] = count_24h >= new
                    dirty = True
                    events.append(("ok", f"Clean day at the cap: dynamic cap {old} → {new}"))
        if dirty:
            _save(state)
    for level, detail in events:
        logger.info(detail)
        record_status_event("cap", level, detail)
    return state


def summary(count_24h: Optional[int] = None) -> Dict[str, Any]:
    """For the API and Settings: cap, ceiling, last change, penalties."""
    from app.services.download_manager import daily_downloads
    if count_24h is None:
        count_24h = len(daily_downloads())
    now = time.time()
    state = tick(count_24h, now)
    last = state["changes"][-1] if state.get("changes") else None
    return {"mode": get_settings().quota_mode, "cap": state["cap"], "count": count_24h,
            "ceiling": ceiling(state, now), "reached": state["reached"],
            "next_step_at": state["changed_at"] + DAY, "last_change": last,
            "penalties": state["penalties"], "changes": state.get("changes", [])}


def reset() -> None:
    """Forget the learned state; the next call re-seeds from history."""
    with _lock:
        try:
            os.remove(state_path())
        except FileNotFoundError:
            pass
