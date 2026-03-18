"""
notifier.py — Formats job data and POSTs to the configured webhook.

Includes retry logic with exponential backoff.
"""

import logging
import time
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_BASE = 2  # seconds


# ---------------------------------------------------------------------------
# Payload formatting
# ---------------------------------------------------------------------------

def _format_job(job: dict) -> dict[str, Any]:
    """Turn a raw GraphQL job node into a clean webhook payload."""
    client = job.get("client") or {}
    spent_obj = client.get("totalSpent") or {}

    total_hires = client.get("totalHires") or 0
    total_posted = client.get("totalPostedJobs") or 0
    hire_rate = round(total_hires / total_posted, 2) if total_posted > 0 else 0

    # Collect skills from both legacy and ontology fields
    skills: list[str] = []
    for s in job.get("skills") or []:
        name = s.get("prettyName") or s.get("name")
        if name:
            skills.append(name)
    if not skills:
        for os_item in job.get("ontologySkills") or []:
            sk = os_item.get("skill") or {}
            name = sk.get("prettyName") or sk.get("name")
            if name:
                skills.append(name)

    # Budget display
    amount_obj = job.get("amount") or {}
    budget = amount_obj.get("amount")
    hourly_min = (job.get("hourlyBudgetMin") or {}).get("amount")
    hourly_max = (job.get("hourlyBudgetMax") or {}).get("amount")

    hourly_range = None
    if hourly_min is not None or hourly_max is not None:
        hourly_range = {
            "min": hourly_min,
            "max": hourly_max,
        }

    location = client.get("location") or {}

    return {
        "title": job.get("title", ""),
        "url": job.get("job_url", ""),
        "description": (job.get("description") or "")[:500],
        "posted_at": job.get("publishedDateTime", ""),
        "type": job.get("type", ""),
        "experience_level": job.get("contractorTier", ""),
        "duration": job.get("durationLabel") or job.get("duration", ""),
        "engagement": job.get("engagement", ""),
        "budget": budget,
        "hourly_range": hourly_range,
        "currency": amount_obj.get("currencyCode", "USD"),
        "total_applicants": job.get("totalApplicants"),
        "skills": skills,
        "client": {
            "company": client.get("companyName"),
            "location": f"{location.get('city', '')}, {location.get('country', '')}".strip(", "),
            "total_hires": total_hires,
            "total_posted_jobs": total_posted,
            "hire_rate": hire_rate,
            "total_spent": float(spent_obj.get("amount", 0) or 0),
            "total_spent_currency": spent_obj.get("currencyCode", "USD"),
            "total_reviews": client.get("totalReviews") or 0,
            "rating": client.get("totalFeedback") or 0,
            "verified": client.get("verificationStatus", ""),
        },
    }


# ---------------------------------------------------------------------------
# Webhook delivery
# ---------------------------------------------------------------------------

def _post_webhook(payload: dict) -> bool:
    """POST a single payload to the webhook with retries."""
    url = config.WEBHOOK_URL
    if not url:
        logger.warning("WEBHOOK_URL is not set — skipping notification.")
        return False

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code < 400:
                logger.debug("Webhook delivered (HTTP %d).", resp.status_code)
                return True
            logger.warning(
                "Webhook returned HTTP %d on attempt %d: %s",
                resp.status_code, attempt, resp.text[:200],
            )
        except requests.RequestException as exc:
            logger.warning("Webhook request failed on attempt %d: %s", attempt, exc)

        if attempt < MAX_RETRIES:
            wait = BACKOFF_BASE ** attempt
            logger.info("Retrying in %ds …", wait)
            time.sleep(wait)

    logger.error("Webhook delivery failed after %d attempts.", MAX_RETRIES)
    return False


def notify_jobs(jobs: list[dict]) -> int:
    """
    Format and send each job to the webhook.

    Returns the number of successfully delivered notifications.
    """
    if not jobs:
        return 0

    delivered = 0
    for job in jobs:
        payload = _format_job(job)
        if _post_webhook(payload):
            delivered += 1

    logger.info("Delivered %d / %d job notification(s).", delivered, len(jobs))
    return delivered
