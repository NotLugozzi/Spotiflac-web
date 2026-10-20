"""
Status of everything that can hold up a download, for the Status page and
the top-bar pill:

  * service: the public SpotiFLAC download servers (break schedule / up-down,
    from the break monitor);
  * spotify: the Spotify Web API quota (cooldowns per endpoint and app, from
    spotify_limits);
  * ours: the download service's rate limit on *this* install (SpotiFLAC's
    verification backoff file). The servers can be open for everyone while
    we're still blocked;
  * providers (events only, no chart lane): providers the runner skips for a
    while (circuit_breaker.json, e.g. qobuz after a rate-limited search). Jobs
    keep running on the other providers, so this is never "limited".

``record()`` runs every minute and stores only state changes (StatusEvent);
the history chart rebuilds a step line from them.
"""

import contextlib
import glob
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.config import get_settings
from app.database import get_db_session, get_session_factory
from app.models import StatusEvent
from app.services.break_monitor import format_minutes, format_utc, get_break_monitor
from app.services import spotify_limits

logger = logging.getLogger(__name__)

COMPONENTS = ("service", "spotify", "ours")
# Recorded like the others, but only listed as events (no chart lane, no card)
EVENT_ONLY = ("providers",)
KEEP_DAYS = 30

# Chart level per state (higher = better); unknown is a gap
LEVELS = {"ok": 2, "partial": 1, "break": 1, "limited": 0, "down": 0}


def download_backoff() -> Dict[str, Any]:
    """SpotiFLAC's backoff for the shared download service (zarz-v2), from
    ~/.spotiflac/signed_sessions/.*auth-backoff.json: the latest not_before
    and its failure count. Same files the runner checks before starting."""
    home = os.path.expanduser("~/.spotiflac")
    until, failures = 0.0, 0
    for path in glob.glob(os.path.join(home, "signed_sessions", ".*auth-backoff.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            not_before = float(data.get("not_before") or 0)
        except (OSError, ValueError, AttributeError, TypeError):
            continue
        if not_before > until:
            until, failures = not_before, int(data.get("failures") or 0)
    return {"until": until, "failures": failures}


def _minutes_until(ts: float) -> int:
    return max(1, int((ts - time.time()) // 60))


def _hhmm(ts: float) -> str:
    return format_utc(ts)


def _service_status() -> Dict[str, Any]:
    snap = get_break_monitor().snapshot()
    if not snap["fresh"]:
        return {"state": "unknown", "label": "Unknown",
                "detail": "Status page unreachable" + (f": {snap['error']}" if snap["error"] else ""),
                "until": None, "providers": snap["providers"]}
    mins = snap["remaining_minutes"]
    if snap["paused"] and snap["on_break"]:
        state, label = "break", "Scheduled break"
        detail = f"Ends in {format_minutes(mins)}"
    elif snap["paused"]:
        state, label, detail = "down", "Down", "Download servers unavailable"
    else:
        state, label = "ok", "Open"
        detail = f"Next break in {format_minutes(mins)}" if mins is not None else "No break scheduled"
    until = time.time() + mins * 60 if mins is not None else None
    event = f"Scheduled break until {_hhmm(until)}" if state == "break" and until else label
    return {"state": state, "label": label, "detail": detail, "until": until, "event": event,
            "providers": snap["providers"]}


def _spotify_status() -> Dict[str, Any]:
    s = get_settings()
    slots = [slot for slot, _, _ in spotify_limits.credential_slots(
        s.spotify_client_id, s.spotify_client_secret,
        s.spotify_client_id_backup, s.spotify_client_secret_backup)]
    if not slots:
        return {"state": "unknown", "label": "Not configured", "detail": "No Spotify credentials",
                "until": None, "endpoints": []}
    active = spotify_limits.active_cooldowns()
    families = sorted({key.split(":", 1)[-1] for key in active})
    endpoints = []
    for family in families:
        blocked = [slot for slot in slots if spotify_limits.cooldown_key(slot, family) in active]
        endpoints.append({
            "family": family,
            "blocked_apps": blocked,
            # Every app cooling down: calls to this endpoint fail until the earliest ends
            "until": spotify_limits.cooldown_until(family, slots),
        })
    hard = [e for e in endpoints if e["until"]]
    if hard:
        first = min(hard, key=lambda e: e["until"])
        names = ", ".join(f"/{e['family']}" for e in hard)
        return {"state": "limited", "label": "Rate limited",
                "detail": f"{names} blocked, back at {_hhmm(first['until'])}",
                "event": f"Rate limited: {names} blocked until {_hhmm(first['until'])}",
                "until": first["until"], "endpoints": endpoints}
    if endpoints:
        names = ", ".join(f"/{e['family']}" for e in endpoints)
        detail = (f"{names} on the backup app" if len(slots) > 1
                  else f"{names} cooling down")
        return {"state": "partial", "label": "Partly limited", "detail": detail,
                "event": f"Partly limited: {detail}", "until": None, "endpoints": endpoints}
    apps = "both apps" if len(slots) > 1 else "1 app"
    return {"state": "ok", "label": "OK", "detail": f"No cooldowns ({apps})",
            "until": None, "endpoints": []}


def _ours_status() -> Dict[str, Any]:
    backoff = download_backoff()
    if backoff["until"] > time.time() + 30:
        minutes = _minutes_until(backoff["until"])
        return {"state": "limited", "label": "Rate limited",
                "detail": f"Resumes in {format_minutes(minutes)} ({_hhmm(backoff['until'])})",
                "until": backoff["until"], "minutes": minutes,
                "resume_at": _hhmm(backoff["until"]), "failures": backoff["failures"],
                "event": f"Rate limited until {_hhmm(backoff['until'])}"
                         + (f" ({backoff['failures']} failed attempt{'s' if backoff['failures'] != 1 else ''})"
                            if backoff["failures"] else "")}
    return {"state": "ok", "label": "Not limited", "detail": "Download service accepts our requests",
            "until": None, "failures": backoff["failures"]}


def circuit_breaker_path() -> str:
    """The runner's per-provider cooldowns (scripts/spotiflac_runner.py)."""
    return os.path.expanduser("~/.spotiflac/circuit_breaker.json")


def _read_breaker() -> Dict[str, Any]:
    try:
        with open(circuit_breaker_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def provider_cooldowns() -> List[Dict[str, Any]]:
    """Providers the runner is skipping right now, soonest end first."""
    now = time.time()
    out = []
    for provider, entry in _read_breaker().items():
        try:
            until = float((entry or {}).get("penalty_until") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if until > now:
            out.append({"provider": provider, "name": provider.removeprefix("ext:"),
                        "until": until, "resume_at": _hhmm(until), "minutes": _minutes_until(until),
                        "reason": str(entry.get("reason") or "Turnstile failures")})
    return sorted(out, key=lambda c: c["until"])


def clear_provider_cooldowns(provider: Optional[str] = None) -> List[str]:
    """Drop the cooldown of one provider (or all); returns the ones removed.
    A run in progress may write the file again; this applies from the next run."""
    return _drop_cooldowns(lambda p, entry: provider is None or p == provider)


def clear_turnstile_cooldowns() -> List[str]:
    """Drop the Turnstile cooldowns a break causes, keeping rate-limit ones
    (qobuz's 24 h skip: retrying it early only earns another 429)."""
    return _drop_cooldowns(lambda p, entry: not (isinstance(entry, dict)
                                                 and entry.get("reason") == "rate limited"))


def _drop_cooldowns(match) -> List[str]:
    data = _read_breaker()
    removed = [p for p, entry in data.items() if match(p, entry)]
    if not removed:
        return []
    for p in removed:
        data.pop(p)
    path = circuit_breaker_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return removed


def _providers_status() -> Dict[str, Any]:
    cooldowns = provider_cooldowns()
    if cooldowns:
        text = ", ".join(f"{c['name']} skipped until {c['resume_at']} ({c['reason']})" for c in cooldowns)
        return {"state": "partial", "label": "Provider skipped", "detail": text,
                "event": text, "cooldowns": cooldowns}
    return {"state": "ok", "label": "All providers in use", "detail": "No provider is being skipped",
            "cooldowns": []}


def current_ours() -> Dict[str, Any]:
    """Just our own download-service limit (cheap: one small file read), for the top-bar pill."""
    return _ours_status()


def current() -> Dict[str, Dict[str, Any]]:
    return {"service": _service_status(), "spotify": _spotify_status(), "ours": _ours_status(),
            "providers": _providers_status()}


def _last_states(db) -> Dict[str, Optional[StatusEvent]]:
    last = {}
    for component in COMPONENTS + EVENT_ONLY:
        last[component] = db.query(StatusEvent).filter(StatusEvent.component == component) \
            .order_by(StatusEvent.created_at.desc(), StatusEvent.id.desc()).first()
    return last


def _event_text(status: Dict[str, Any]) -> str:
    return (status.get("event") or status["label"])[:500]


# Their event text names a fixed end time: a changed text (limit extended,
# another endpoint blocked) is worth an event even in the same state.
# The service's break end is estimated from a minute countdown and jitters.
TEXT_TRACKED = ("spotify", "ours", "providers")


def record() -> None:
    """Scheduler job: store a StatusEvent for each component whose state changed."""
    try:
        now_status = current()
        # Before the break monitor's first poll the service state is just "not yet known"
        if get_break_monitor().snapshot()["fetched_at"] is None:
            now_status.pop("service")
        with get_db_session() as db:
            last = _last_states(db)
            for component, status in now_status.items():
                prev = last.get(component)
                text = _event_text(status)
                if prev is None or prev.state != status["state"] or \
                        (component in TEXT_TRACKED and prev.detail != text):
                    db.add(StatusEvent(component=component, state=status["state"], detail=text))
                    logger.info(f"Status: {component} -> {status['state']} ({status['detail']})")
            db.query(StatusEvent).filter(
                StatusEvent.created_at < datetime.utcnow() - timedelta(days=KEEP_DAYS)
            ).delete(synchronize_session=False)
    except Exception as e:
        logger.warning(f"Status recording failed: {e}")
    # The dynamic quota notices a penalty here too: while blocked, no job
    # checks the cap, and it must see the block while it's still active
    if get_settings().quota_mode == "dynamic":
        try:
            from app.services.download_manager import daily_cap_state
            daily_cap_state()
        except Exception as e:
            logger.warning(f"Dynamic quota check failed: {e}")


def history(hours: int) -> Dict[str, Any]:
    """Events in the last ``hours`` plus each component's state at the start
    of the window, for the chart and the event list."""
    start = datetime.utcnow() - timedelta(hours=hours)
    # Plain session: a read only, no need for the background write lock
    with contextlib.closing(get_session_factory()()) as db:
        initial = {}
        for component in COMPONENTS:
            prev = db.query(StatusEvent).filter(
                StatusEvent.component == component, StatusEvent.created_at < start
            ).order_by(StatusEvent.created_at.desc(), StatusEvent.id.desc()).first()
            initial[component] = prev.state if prev else None
        rows = db.query(StatusEvent).filter(StatusEvent.created_at >= start) \
            .order_by(StatusEvent.created_at, StatusEvent.id).all()
        events = [{
            "component": r.component, "state": r.state, "detail": r.detail,
            "at": r.created_at.isoformat() + "Z",
        } for r in rows]
    return {"start": start.isoformat() + "Z", "end": datetime.utcnow().isoformat() + "Z",
            "initial": initial, "events": events, "levels": LEVELS}


def summary(hours: int = 24) -> Dict[str, Any]:
    """Per component: how many times it went bad and the share of the window
    it was OK (unknown time excluded), for the stat cards."""
    data = history(hours)
    start = datetime.fromisoformat(data["start"][:-1])
    end = datetime.fromisoformat(data["end"][:-1])
    out = {}
    for component in COMPONENTS:
        state = data["initial"][component]
        t = start
        ok = known = 0.0
        incidents = 0
        for e in [e for e in data["events"] if e["component"] == component] + [None]:
            at = datetime.fromisoformat(e["at"][:-1]) if e else end
            span = (at - t).total_seconds()
            if state is not None and state != "unknown":
                known += span
                if state == "ok":
                    ok += span
            if e:
                if e["state"] in ("limited", "down") and state not in ("limited", "down"):
                    incidents += 1
                state, t = e["state"], at
        out[component] = {
            "ok_pct": round(100 * ok / known) if known else None,
            "incidents": incidents,
        }
    return out
