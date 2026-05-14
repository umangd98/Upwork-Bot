"""
profile_metrics.py — Fetches and persists daily Upwork profile stats.

Queries user → freelancerProfile → aggregates → profileStats via GraphQL
and stores a rolling-window of daily snapshots in data/profile_metrics.json.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone, timedelta

import config
from upwork_client import execute_graphql, get_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL query — FreelancerProfileStats
# https://www.upwork.com/developer/documentation/graphql/api/docs/index.html#definition-FreelancerProfileStats
# ---------------------------------------------------------------------------

PROFILE_STATS_QUERY = """
query {
  user {
    freelancerProfile {
      aggregates {
        profileStats {
          totalCharge360 { rawValue currency displayValue }
          totalCharge360NoAgency { rawValue currency displayValue }
          totalCharge365NoPending { rawValue currency displayValue }
          totalCharge90 { rawValue currency displayValue }
          adjustedScore360
          longTermClients
          suspensions
          suspensions360
          suspensions90limited
          topLevelJobCategoryApplied90Days
          proposalsCount90Days
          medianProposalsForTheTopLevelCategory365
          fitProposalsViewRatio90Days
          hiddenProposalsViewedRatio90Days
          totalProposalsViewedRatio90Days
          proposalInterviewedRation90Days
          proposalsHiredRatio90Days
          hideReasonsForProposals
          totalInvites90Days
          totalInviteResponses90Days
          inviteResponsesPerDay90Days
          weeksEligibleWithin16wks
        }
      }
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_profile_metrics() -> dict:
    """
    Fetch FreelancerProfileStats from the Upwork GraphQL API.

    Returns a snapshot dict with all stats plus 'date' (YYYY-MM-DD) and
    'fetched_at' (UTC ISO 8601) injected.

    Raises RuntimeError if the API response is missing expected fields.
    """
    client = get_client()
    result = execute_graphql(client, PROFILE_STATS_QUERY, {})

    try:
        stats = (
            result["data"]["user"]["freelancerProfile"]["aggregates"]["profileStats"]
        )
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            f"Unexpected response structure from Upwork API: {exc}. "
            f"Raw response: {result}"
        ) from exc

    logger.info("Raw profileStats from Upwork API:\n%s", json.dumps(stats, indent=2))

    now_utc = datetime.now(timezone.utc)
    snapshot = {
        "date": now_utc.strftime("%Y-%m-%d"),
        "fetched_at": now_utc.isoformat().replace("+00:00", "Z"),
        **stats,
    }
    return snapshot


def save_metrics(snapshot: dict) -> None:
    """
    Persist *snapshot* to data/profile_metrics.json.

    Behaviour:
    - If a snapshot for the same 'date' already exists it is replaced
      (makes repeated same-day runs idempotent).
    - Snapshots are ordered newest-first.
    - Entries older than config.METRICS_RETENTION_DAYS are pruned.
    - Write is atomic: temp file → os.replace(), so a crash mid-write
      never corrupts the existing file.
    """
    existing = _load_metrics()
    snapshots: list = existing.get("snapshots", [])

    today = snapshot["date"]

    # Remove any existing snapshot for today (dedup / idempotent)
    snapshots = [s for s in snapshots if s.get("date") != today]

    # Prepend the new snapshot
    snapshots.insert(0, snapshot)

    # Prune entries outside the retention window
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=config.METRICS_RETENTION_DAYS)
    ).strftime("%Y-%m-%d")
    snapshots = [s for s in snapshots if s.get("date", "") >= cutoff]

    payload = {
        "last_updated": snapshot["fetched_at"],
        "retention_days": config.METRICS_RETENTION_DAYS,
        "snapshots": snapshots,
    }

    _atomic_write(config.METRICS_FILE, payload)
    logger.info(
        "Saved profile metrics snapshot for %s (%d total snapshots).",
        today,
        len(snapshots),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_metrics() -> dict:
    """Load existing metrics JSON; return empty structure if not found."""
    if not os.path.exists(config.METRICS_FILE):
        return {"snapshots": []}
    try:
        with open(config.METRICS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not parse %s — starting fresh.", config.METRICS_FILE)
        return {"snapshots": []}


def _atomic_write(path: str, data: dict) -> None:
    """Write *data* as JSON to *path* atomically via a temp file."""
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise
