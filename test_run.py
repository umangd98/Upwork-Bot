#!/usr/bin/env python3
"""
test_run.py — One-shot test: run a search cycle and send results to a webhook.

Usage:
    python test_run.py [webhook_url]
"""

import json
import logging
import os
import sys

# Allow HTTP OAuth
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("test_run")


def main():
    # Override webhook URL if provided as argument
    webhook_override = sys.argv[1] if len(sys.argv) > 1 else None

    import config
    if webhook_override:
        config.WEBHOOK_URL = webhook_override
        logger.info("Webhook URL overridden to: %s", webhook_override)

    from upwork_client import has_valid_token
    if not has_valid_token():
        logger.error("No valid tokens found. Authenticate first.")
        sys.exit(1)

    logger.info("=== TEST RUN: Starting search ===")

    import filters
    import notifier
    import requests

    # 1. Search
    try:
        jobs = filters.search_jobs()
    except Exception:
        logger.exception("Search failed")
        sys.exit(1)

    logger.info("Search returned %d job(s) passing all filters.", len(jobs))

    if not jobs:
        # Send a test ping even if no jobs found
        logger.info("No matching jobs — sending a test ping to webhook.")
        payload = {
            "test": True,
            "message": "Upwork Bot test ping — search returned 0 matching jobs.",
            "filters_loaded": config.load_filters(),
        }
        try:
            resp = requests.post(config.WEBHOOK_URL, json=payload, timeout=15)
            logger.info("Test ping sent — HTTP %d", resp.status_code)
        except Exception as exc:
            logger.error("Test ping failed: %s", exc)
        return

    # 2. Send first 3 jobs (or all if fewer) as test notifications
    test_jobs = jobs[:3]
    logger.info("Sending %d job(s) to webhook as test...", len(test_jobs))
    delivered = notifier.notify_jobs(test_jobs)
    logger.info("=== TEST COMPLETE: delivered %d notification(s) ===", delivered)

    # Also print the first job for visibility
    if test_jobs:
        first = notifier._format_job(test_jobs[0])
        print("\n--- Sample payload (first job) ---")
        print(json.dumps(first, indent=2, default=str))


if __name__ == "__main__":
    main()
