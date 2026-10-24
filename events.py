"""
events.py — Sends job events to the n8n webhook, which posts them to Slack.

Event types (field "event"):
  applied          auto-applied (or approved) and submitted successfully
  needs_approval   waiting for you; Slack shows Apply / Edit / Skip buttons
  dry_run          AUTO_APPLY_ENABLED is false: would have auto-applied
  cap_reached      qualifies, but 10 proposals are already out in 24h
  apply_failed     Upwork refused the submission (error included)
  skipped          you pressed Skip
"""

import logging
import time

import requests

import config

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


def _budget(job: dict) -> str:
    fixed = (job.get("amount") or {}).get("displayValue")
    if fixed:
        return f"${fixed} fixed"
    lo = (job.get("hourlyBudgetMin") or {}).get("rawValue")
    hi = (job.get("hourlyBudgetMax") or {}).get("rawValue")
    if lo or hi:
        return f"${lo or '?'}–${hi or '?'}/hr"
    return "not stated"


def build_payload(event: str, app: dict, job: dict, *, window_count: int,
                  cap: int, extra: dict | None = None) -> dict:
    client = job.get("client") or {}
    loc = client.get("location") or {}
    payload = {
        "event": event,
        "job_id": app["job_id"],
        "mode": app["mode"],            # "auto" or "approval"
        "status": app["status"],
        "title": app.get("title") or job.get("title"),
        "url": job.get("job_url", ""),
        "budget": _budget(job),
        "bid": app.get("bid"),
        "description": job.get("description") or "",
        "cover_letter": app.get("letter") or "",
        "fit_score": job.get("fit_score"),
        "client": {
            "country": loc.get("country"),
            "total_spent": (client.get("totalSpent") or {}).get("displayValue"),
            "reviews": client.get("totalReviews"),
            "rating": client.get("totalFeedback"),
        },
        "applied_in_last_24h": window_count,
        "daily_cap": cap,
        "proposal_id": app.get("proposal_id"),
        "error": app.get("error"),
    }
    if extra:
        payload.update(extra)
    return payload


def post(payload: dict) -> bool:
    """POST to n8n with retries. Never raises (alerts must not break applying)."""
    url = config.N8N_WEBHOOK_URL
    if not url:
        logger.warning("N8N_WEBHOOK_URL not set — skipping %s event.", payload.get("event"))
        return False
    headers = {"Authorization": f"Bearer {config.BOT_API_SECRET}"} if config.BOT_API_SECRET else {}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            if resp.status_code < 400:
                return True
            logger.warning("n8n returned HTTP %d (attempt %d)", resp.status_code, attempt)
        except requests.RequestException as exc:
            logger.warning("n8n request failed (attempt %d): %s", attempt, exc)
        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)
    logger.error("Could not deliver %s event to n8n.", payload.get("event"))
    return False
