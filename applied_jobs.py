"""
applied_jobs.py — Which jobs have we already applied to on Upwork?

Best effort: asks Upwork's GraphQL API for the proposals already submitted by this
account and remembers the job ids for the rest of the process. The query shape comes
from what the Upwork MCP returns (vendorProposals → edges → node →
marketplaceJobPosting.id) and has NOT been verified against this app's API key, so any
failure just logs a warning and returns nothing. The other duplicate guards (the
local application table, repost detection and Upwork rejecting a second proposal)
still apply. Run `python check_proposals_api.py` on the bot to see if this works.
"""

import logging
import time

from upwork_client import execute_graphql, get_client

logger = logging.getLogger(__name__)

_QUERY = """
{
  vendorProposals {
    totalCount
    edges { node { id marketplaceJobPosting { id } } }
  }
}
"""

REFRESH_SECONDS = 25 * 60
_cache = {"ids": set(), "at": 0.0, "ok": False}


def parse(result: dict) -> set[str]:
    """Job ids out of a vendorProposals response (empty set if the shape is off)."""
    edges = (((result or {}).get("data") or {}).get("vendorProposals") or {}).get("edges") or []
    ids = set()
    for edge in edges:
        job = ((edge or {}).get("node") or {}).get("marketplaceJobPosting") or {}
        if job.get("id"):
            ids.add(str(job["id"]))
    return ids


def refresh(force: bool = False) -> bool:
    """Reload the applied-job ids if the cache is stale. Returns True if the fetch worked."""
    if not force and _cache["ok"] and time.time() - _cache["at"] < REFRESH_SECONDS:
        return True
    try:
        result = execute_graphql(get_client(), _QUERY, {})
    except Exception:
        logger.exception("Could not fetch existing proposals; relying on the other duplicate checks.")
        return False
    if result and result.get("errors"):
        logger.warning("Proposal list query was rejected (%s); relying on the other duplicate checks.",
                       "; ".join(e.get("message", "?") for e in result["errors"])[:200])
        return False
    ids = parse(result)
    _cache.update(ids=ids, at=time.time(), ok=True)
    logger.info("Known applied jobs on Upwork: %d", len(ids))
    return True


def is_applied(job_id: str) -> bool:
    return str(job_id) in _cache["ids"]
