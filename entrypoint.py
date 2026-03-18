#!/usr/bin/env python3
"""
entrypoint.py — Main entry point for the Upwork Job Scraper Bot.

• If no saved tokens exist → starts the auth server (Flask or CLI).
• Once tokens are available → starts the polling scheduler.
"""

import argparse
import logging
import sys

import config
from upwork_client import has_valid_token

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("upwork_bot")


def main() -> None:
    parser = argparse.ArgumentParser(description="Upwork Job Scraper Bot")
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Use CLI-based OAuth2 flow instead of the Flask callback server.",
    )
    parser.add_argument(
        "--auth-only",
        action="store_true",
        help="Run only the auth flow, then exit (don't start the scheduler).",
    )
    args = parser.parse_args()

    # ── Validate essential config ──
    if not config.UPWORK_CLIENT_ID or not config.UPWORK_CLIENT_SECRET:
        logger.error(
            "UPWORK_CLIENT_ID and UPWORK_CLIENT_SECRET must be set. "
            "Copy .env.example → .env and fill in your credentials."
        )
        sys.exit(1)

    # ── Step 1: Authenticate if needed ──
    if not has_valid_token():
        logger.info("No valid tokens found — starting authentication.")
        if args.cli:
            from auth_server import run_cli_auth
            run_cli_auth()
        else:
            from auth_server import run_flask_auth_server
            run_flask_auth_server()

        # Re-check after auth flow completes
        if not has_valid_token():
            logger.error("Authentication did not produce valid tokens. Exiting.")
            sys.exit(1)

    if args.auth_only:
        logger.info("--auth-only: tokens are saved. Exiting.")
        return

    # ── Step 2: Validate webhook ──
    if not config.WEBHOOK_URL:
        logger.warning(
            "WEBHOOK_URL is not set. Notifications will be skipped. "
            "Set it in .env to enable webhook delivery."
        )

    # ── Step 3: Start scheduler ──
    logger.info("Tokens OK. Starting the polling scheduler.")
    from scheduler import start_scheduler
    start_scheduler()


if __name__ == "__main__":
    main()
