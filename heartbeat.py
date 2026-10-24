"""
heartbeat.py — Lets you see that the bot is alive, without a message per poll.

* Every poll records how many jobs were matched / new / handled (or the error).
* Every `apply.heartbeat_hours` (default 6, 0 = off) a "💓 running" summary of the period
  goes to Slack via n8n.
* When the bot (re)starts and is logged in, a one-off "started" message says so, which also
  confirms a login after a deploy.
* If a poll fails (search error, expired login ...), a "poll_error" alert is sent, at most
  once every `apply.error_alert_hours` (default 2).

State is in memory: it resets with the process, like the rest of the container.
"""

import logging
import threading
import time
from datetime import datetime, timezone

import config
import db
import events

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state: dict = {}


def reset() -> None:
    with _lock:
        _state.clear()
        _state.update({
            "process_started_at": time.time(),
            "last_poll_at": None, "last_poll_ok": None, "last_error": None, "last_poll_seconds": None,
            "last_beat_at": time.time(), "last_error_alert_at": 0.0,
            "window": {"polls": 0, "errors": 0, "matched": 0, "new": 0, "handled": 0},
        })


reset()


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def record_poll(matched: int, new: int, handled: int, seconds: float, error: str | None = None) -> None:
    with _lock:
        _state["last_poll_at"] = time.time()
        _state["last_poll_ok"] = error is None
        _state["last_error"] = error
        _state["last_poll_seconds"] = round(seconds, 1)
        w = _state["window"]
        w["polls"] += 1
        w["errors"] += 1 if error else 0
        w["matched"] += matched
        w["new"] += new
        w["handled"] += handled


def snapshot() -> dict:
    """What /api/status shows."""
    with _lock:
        return {
            "process_started_at": _iso(_state["process_started_at"]),
            "last_poll_at": _iso(_state["last_poll_at"]),
            "last_poll_ok": _state["last_poll_ok"],
            "last_poll_seconds": _state["last_poll_seconds"],
            "last_error": _state["last_error"],
            "since_last_heartbeat": dict(_state["window"]),
        }


def _cfg(filters: dict) -> dict:
    return filters.get("apply") or {}


def _base_payload(event: str, filters: dict) -> dict:
    cap = int(_cfg(filters).get("daily_cap", 10))
    return {
        "event": event,
        "at": _iso(time.time()),
        "backend": config.APPLY_BACKEND,
        "auto_apply": "ON" if config.AUTO_APPLY_ENABLED else "dry-run (nothing is sent)",
        "poll_minutes": config.POLL_INTERVAL_MINUTES,
        "applied_in_last_24h": db.applications_in_window(24),
        "daily_cap": cap,
    }


def _post(payload: dict) -> bool:
    try:
        return events.post(payload)
    except Exception:
        logger.exception("Could not send %s message.", payload.get("event"))
        return False


def send_started(filters: dict) -> bool:
    """One-off message once the scheduler is running, i.e. the bot is logged in."""
    reset()
    return _post(_base_payload("started", filters))


def maybe_beat(filters: dict, force: bool = False) -> bool:
    """Send the periodic summary if it is due. Returns True if one was sent."""
    hours = float(_cfg(filters).get("heartbeat_hours", 6))
    with _lock:
        due = force or (hours > 0 and time.time() - _state["last_beat_at"] >= hours * 3600)
        if not due:
            return False
        since = _state["last_beat_at"]
        window = dict(_state["window"])
        last = {"at": _iso(_state["last_poll_at"]), "ok": _state["last_poll_ok"],
                "error": _state["last_error"]}
    outcomes = db.counts_since(_iso(since))
    payload = _base_payload("heartbeat", filters)
    payload.update({
        "period_hours": round((time.time() - since) / 3600, 1),
        "window": window,
        "outcomes": outcomes,                    # applications created in the period, by status
        "pending_total": db.counts_by_status().get("pending", 0),
        "last_poll": last,
    })
    sent = _post(payload)
    if sent:
        with _lock:
            _state["last_beat_at"] = time.time()
            _state["window"] = {"polls": 0, "errors": 0, "matched": 0, "new": 0, "handled": 0}
    return sent


def alert_error(message: str, filters: dict) -> bool:
    """Tell Slack that polling is failing, at most once per `error_alert_hours`."""
    hours = float(_cfg(filters).get("error_alert_hours", 2))
    with _lock:
        if time.time() - _state["last_error_alert_at"] < hours * 3600:
            return False
        _state["last_error_alert_at"] = time.time()
    payload = _base_payload("poll_error", filters)
    payload["error"] = message[:500]
    return _post(payload)
