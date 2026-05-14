"""
scheduler.py — APScheduler-based poll loop.

Runs the search → filter → dedup → notify pipeline on a fixed interval.
"""

import logging

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.background import BackgroundScheduler

import config
import db
import filters
import notifier
import profile_metrics

logger = logging.getLogger(__name__)


def poll_cycle() -> None:
    """Single poll iteration: search, dedup, notify, prune."""
    logger.info("⏳ Starting poll cycle …")

    # 1. Search & post-filter
    try:
        jobs = filters.search_jobs()
    except Exception:
        logger.exception("Search failed — skipping this cycle.")
        return

    if not jobs:
        logger.info("No matching jobs found this cycle.")
        return

    # 2. Dedup — only keep jobs we haven't notified about yet
    job_ids = [j["id"] for j in jobs if j.get("id")]
    unseen_ids = db.filter_unseen(job_ids)
    new_jobs = [j for j in jobs if j.get("id") in unseen_ids]

    if not new_jobs:
        logger.info("All %d matching job(s) were already notified.", len(jobs))
        return

    logger.info("%d new job(s) to notify (out of %d matches).", len(new_jobs), len(jobs))

    # 3. Notify
    delivered = notifier.notify_jobs(new_jobs)

    # 4. Mark as notified (only those successfully delivered)
    notified_ids = [j["id"] for j in new_jobs[:delivered]]
    db.mark_notified(notified_ids)

    # 5. Prune old entries
    db.prune_old_entries(days=30)

    logger.info("✅ Poll cycle complete. Delivered %d notification(s).", delivered)


def metrics_cycle() -> None:
    """Fetch today's profile stats snapshot and persist it to JSON."""
    logger.info("⏳ Fetching profile metrics …")
    try:
        snapshot = profile_metrics.fetch_profile_metrics()
        profile_metrics.save_metrics(snapshot)
        logger.info("✅ Profile metrics saved for %s.", snapshot["date"])
    except Exception:
        logger.exception("Profile metrics fetch failed — will retry next run.")


def _job_listener(event):
    if event.exception:
        logger.error("Scheduled job crashed: %s", event.exception)
    else:
        logger.debug("Scheduled job finished successfully.")


def start_scheduler() -> BackgroundScheduler:
    """Configure and start the background scheduler. Returns the instance."""
    interval = config.POLL_INTERVAL_MINUTES
    logger.info("🚀 Starting scheduler — polling every %d minute(s).", interval)

    scheduler = BackgroundScheduler()

    scheduler.add_listener(_job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

    scheduler.add_job(
        poll_cycle,
        trigger="interval",
        minutes=interval,
        id="upwork_poll",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
    )

    # Run one poll cycle immediately on start-up
    scheduler.add_job(
        poll_cycle,
        trigger="date",          # run once, right now
        id="upwork_poll_initial",
        replace_existing=True,
    )

    # Daily cron: fetch profile metrics at midnight UTC
    scheduler.add_job(
        metrics_cycle,
        trigger="cron",
        hour=0,
        minute=0,
        id="profile_metrics_daily",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    # Also run one metrics fetch immediately on start-up
    scheduler.add_job(
        metrics_cycle,
        trigger="date",
        id="profile_metrics_initial",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started in background.")
    return scheduler
