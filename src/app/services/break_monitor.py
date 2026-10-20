"""
Break monitor for the upstream SpotiFLAC download servers.

The servers behind tidal-web / qobuz-web / amazon pause on a schedule. The
public status page (https://spotbye.qzz.io) exposes it at /api/status:

    spotiflac.breaks.<provider> = {"enabled": true, "is_break": true,
                                   "remaining_minutes": 113, "available": true}
    spotiflac.status.<provider> = "up" | "down"

During a break ``remaining_minutes`` counts down to its end; otherwise it
counts down to the next break. The download worker pauses the queue while
every tracked provider in a task's chain is on break or down, and resumes it
once one has been available for BREAK_RESUME_STABLE_SECONDS (the status
flaps for a few minutes after most breaks).

``remaining_minutes`` is the time left rounded up to a whole minute, and
breaks start and end on a whole minute, so one poll pins the exact second:
the only whole minute in (now + (r-1) min, now + r min]. Running jobs stop
BREAK_STOP_LEAD_SECONDS before it; near a break the monitor polls every 10 s
and logs how far off the prediction was.
"""

import logging
import math
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# Treat a snapshot older than this as unknown, so a dead status page never
# blocks downloads indefinitely
STALE_AFTER = timedelta(minutes=10)
# Poll every 10 s from this long before a predicted break until it starts
NEAR_BREAK_SECONDS = 150


def whole_minute(ts: float, minutes: int) -> float:
    """The break time behind a rounded-up ``remaining_minutes`` read at ``ts``."""
    return math.floor((ts + minutes * 60) / 60) * 60


def format_clock(ts: float) -> str:
    """"20:59:30 UTC"."""
    return time.strftime("%H:%M:%S UTC", time.gmtime(ts))


def format_seconds(seconds: float) -> str:
    """"57m 12s", "45s", "1h 02m"."""
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds}s"


def provider_key(service: str) -> str:
    """Map an extension ID like ``ext:tidal-web`` to a status key like ``tidal``."""
    name = service.split(":", 1)[-1]
    return name.split("-", 1)[0].lower()


def format_utc(ts: float) -> str:
    """"14:03 UTC" today, "Tue 08:31 UTC" on another day (UTC)."""
    import time
    fmt = "%H:%M UTC" if time.gmtime(ts)[:3] == time.gmtime()[:3] else "%a %H:%M UTC"
    return time.strftime(fmt, time.gmtime(ts))


def format_minutes(minutes: Optional[int]) -> str:
    if minutes is None:
        return "?"
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60}m"
    return f"{minutes} min"


class BreakMonitor:
    """Polls the status page and answers "can we download right now?"."""

    def __init__(self):
        self._lock = threading.Lock()
        self._providers: Dict[str, Dict[str, Any]] = {}
        self._fetched_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._was_paused: Optional[bool] = None
        # Per provider, unix times: since when it has been blocked (break or
        # down) / available, the next break start and the current break end
        self._blocked_since: Dict[str, float] = {}
        self._clear_since: Dict[str, float] = {}
        self._next_break: Dict[str, float] = {}
        self._break_end: Dict[str, float] = {}
        self._announced_break: Optional[float] = None

    def poll(self) -> None:
        """Fetch the status page and store per-provider state. Never raises."""
        settings = get_settings()
        try:
            resp = httpx.get(
                settings.break_status_url,
                timeout=15,
                headers={"Cache-Control": "no-cache"},
            )
            resp.raise_for_status()
            data = resp.json().get("spotiflac") or {}
        except Exception as e:
            with self._lock:
                self._last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"Break monitor: status fetch failed: {e}")
            return

        now = time.time()
        breaks = data.get("breaks") or {}
        statuses = data.get("status") or {}
        providers = {}
        for name in set(breaks) | set(statuses):
            brk = breaks.get(name) if isinstance(breaks.get(name), dict) else {}
            scheduled = bool(brk.get("available") and brk.get("enabled"))
            providers[name] = {
                "is_break": scheduled and bool(brk.get("is_break")),
                "remaining_minutes": brk.get("remaining_minutes") if scheduled else None,
                "status": statuses.get(name),
            }

        with self._lock:
            first = self._fetched_at is None
            was_on_break = {n: p["is_break"] for n, p in self._providers.items()}
            predicted = dict(self._next_break)
            for name, p in providers.items():
                if self._blocked(p):
                    self._clear_since.pop(name, None)
                    self._blocked_since.setdefault(name, now)
                else:
                    self._blocked_since.pop(name, None)
                    # At startup assume it has been up for a while
                    self._clear_since.setdefault(name, 0.0 if first else now)
                mins = p["remaining_minutes"]
                if mins is None:
                    self._next_break.pop(name, None)
                    self._break_end.pop(name, None)
                elif p["is_break"]:
                    self._break_end[name] = whole_minute(now, mins)
                    self._next_break.pop(name, None)
                else:
                    self._next_break[name] = whole_minute(now, mins)
                    self._break_end.pop(name, None)
                if p["is_break"] and not was_on_break.get(name, True) and name in predicted:
                    off = now - predicted[name]
                    logger.info(f"Break monitor: {name} break seen at {format_clock(now)} "
                                f"(predicted {format_clock(predicted[name])}, {off:+.0f}s)")
            self._providers = providers
            self._fetched_at = datetime.utcnow()
            self._last_error = None

        services = get_settings().spotiflac_services
        upcoming = self.break_at(services)
        if upcoming and upcoming != self._announced_break:
            logger.info(f"Break monitor: next break at {format_clock(upcoming)}")
            self._announced_break = upcoming

        paused = self.paused(list(providers))
        if paused != self._was_paused:
            if paused:
                logger.info(f"Break monitor: download servers paused ({self.pause_message(list(providers))})")
            else:
                logger.info("Break monitor: download servers available, queue resumes")
            self._was_paused = paused

    def poll_near_break(self) -> None:
        """Scheduled every 10 s: poll only around a predicted break start, so
        its prediction is checked (and corrected) to within ~10 s."""
        now = time.time()
        with self._lock:
            near = any(-60 <= at - now <= NEAR_BREAK_SECONDS for at in self._next_break.values())
        if near:
            self.poll()

    def ensure_polled(self) -> None:
        """Poll once if nothing has been fetched yet (e.g. queue recovery at
        startup runs before the scheduler's first poll)."""
        if self._fetched_at is None and self._last_error is None:
            self.poll()

    def _fresh(self) -> bool:
        return bool(self._fetched_at) and datetime.utcnow() - self._fetched_at < STALE_AFTER

    @staticmethod
    def _blocked(p: Dict[str, Any]) -> bool:
        return p["is_break"] or p["status"] == "down"

    def _known(self, services: List[str]) -> List[Dict[str, Any]]:
        return [self._providers[k] for k in map(provider_key, services) if k in self._providers]

    def paused(self, services: List[str]) -> bool:
        """True when every provider in ``services`` that the status page tracks
        is on a scheduled break or down. Untracked providers (e.g. ytmusic)
        are ignored; unknown or stale status never pauses."""
        with self._lock:
            if not self._fresh():
                return False
            known = self._known(services)
            return bool(known) and all(self._blocked(p) for p in known)

    def _known_names(self, services: List[str]) -> List[str]:
        return [k for k in map(provider_key, services) if k in self._providers]

    def on_break(self, services: List[str]) -> bool:
        """Paused, and by a scheduled break (not just servers reported down)."""
        with self._lock:
            known = self._known(services) if self._fresh() else []
            on_break = any(p["is_break"] for p in known)
        return on_break and self.paused(services)

    def blocked_for(self, services: List[str]) -> float:
        """Seconds every tracked provider in ``services`` has been blocked (0 if not paused)."""
        if not self.paused(services):
            return 0.0
        with self._lock:
            since = [self._blocked_since.get(n, time.time()) for n in self._known_names(services)]
        return time.time() - max(since) if since else 0.0

    def clear_for(self, services: List[str]) -> float:
        """Seconds the steadiest available provider in ``services`` has been up
        (large when the status is unknown, stale or untracked)."""
        with self._lock:
            if not self._fresh():
                return float("inf")
            names = self._known_names(services)
            if not names:
                return float("inf")
            since = [self._clear_since[n] for n in names if n in self._clear_since]
        return time.time() - min(since) if since else 0.0

    def break_at(self, services: List[str]) -> Optional[float]:
        """Unix time the chain's next break starts (the queue pauses once every
        tracked provider is on break, so the latest of theirs), or None when
        unknown, already paused or some provider has no schedule."""
        with self._lock:
            if not self._fresh():
                return None
            names = self._known_names(services)
            if not names or any(self._blocked(self._providers[n]) for n in names):
                return None
            starts = [self._next_break.get(n) for n in names]
        if any(s is None for s in starts):
            return None
        return max(starts)

    def stop_at(self, services: List[str]) -> Optional[float]:
        """When a running job should stop for the next break."""
        at = self.break_at(services)
        return at - max(0, get_settings().break_stop_lead_seconds) if at else None

    def ready(self, services: List[str]) -> Tuple[bool, str]:
        """Whether a job may start now, else the queue label why not:
        servers on break/down, back but not yet steady, or the next break too
        close for a run to get past its warm-up."""
        settings = get_settings()
        if self.paused(services):
            return False, self.pause_message(services)
        steady = max(0, settings.break_resume_stable_seconds)
        clear = self.clear_for(services)
        if clear < steady:
            return False, f"Waiting – servers back, starting in {format_seconds(steady - clear)}"
        at = self.break_at(services)
        if at and at - time.time() < settings.break_min_window_minutes * 60:
            return False, f"Waiting – break at {format_utc(at)} is too close to start"
        return True, ""

    def minutes_until_window(self, services: List[str]) -> Optional[int]:
        """Minutes until the earliest break among ``services`` ends."""
        with self._lock:
            mins = [
                p["remaining_minutes"] for p in self._known(services)
                if p["is_break"] and p["remaining_minutes"] is not None
            ]
            return max(0, min(mins)) if mins else None

    def pause_message(self, services: List[str]) -> str:
        """Queue label for a paused task."""
        mins = self.minutes_until_window(services)
        if mins is not None:
            return f"Waiting – break ends in {format_minutes(mins)}"
        return "Waiting – download servers unavailable"

    def snapshot(self) -> Dict[str, Any]:
        """Current state for the API and UI, judged against the configured chain."""
        services = get_settings().spotiflac_services
        with self._lock:
            now = datetime.utcnow()
            providers = {}
            for name, p in self._providers.items():
                mins = p["remaining_minutes"]
                providers[name] = {
                    **p,
                    # During a break this is when it ends; otherwise when the next one starts
                    "changes_at": (now + timedelta(minutes=mins)).isoformat() + "Z" if mins is not None else None,
                }
            fresh = self._fresh()
            known = self._known(services)
            scheduled = [p["remaining_minutes"] for p in known if p["remaining_minutes"] is not None]
            on_break = [p["remaining_minutes"] for p in known if p["is_break"] and p["remaining_minutes"] is not None]
            fetched_at = self._fetched_at
            error = self._last_error

        paused = self.paused(services)
        break_at = self.break_at(services)
        stop_at = self.stop_at(services)
        return {
            "break_at": datetime.utcfromtimestamp(break_at).isoformat() + "Z" if break_at else None,
            "stop_at": datetime.utcfromtimestamp(stop_at).isoformat() + "Z" if stop_at else None,
            "seconds_to_break": int(break_at - time.time()) if break_at else None,
            "fresh": fresh,
            "paused": paused,
            "on_break": paused and bool(on_break),
            # Paused: minutes until the break ends. Open: minutes until the next break.
            "remaining_minutes": max(0, min(on_break if paused else scheduled)) if fresh and (on_break if paused else scheduled) else None,
            "providers": providers,
            "fetched_at": fetched_at.isoformat() + "Z" if fetched_at else None,
            "error": error,
            "gating_enabled": get_settings().break_gate_downloads,
        }


_break_monitor: Optional[BreakMonitor] = None


def get_break_monitor() -> BreakMonitor:
    """Get the global break monitor instance."""
    global _break_monitor
    if _break_monitor is None:
        _break_monitor = BreakMonitor()
    return _break_monitor
