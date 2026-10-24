"""
applied_jobs.py — Which jobs have we already applied to on Upwork?

Uses `vendorProposals`, whose arguments (confirmed with check_proposals_api.py) are
filter{organizationId_eq, jobPostingIds_any, ...}, sortAttribute{field, sortOrder} and
pagination{first, after}. Two lookups, both read-only and best effort:

* check_jobs(ids): "do any of these job ids already have a proposal from us?" — catches
  jobs applied to outside the bot (by hand, or through the Upwork MCP).
* refresh_recent(): our latest proposals' titles, so a repost of a job we applied to
  under a NEW id can be flagged (it is then sent for approval with a warning, because
  a title alone can't prove it is the same client).

Any failure only logs a warning; the other duplicate guards (local table, repost
detection, Upwork refusing a second proposal) still protect us.
"""

import logging
import re
import time

import config
from upwork_client import execute_graphql, get_client

logger = logging.getLogger(__name__)

CHUNK = 25
TITLE_MAX_AGE_DAYS = 30

_QUERY = """
query VendorProposals($filter: VendorProposalFilter!, $sort: VendorProposalSortAttribute!, $pagination: Pagination!) {
  vendorProposals(filter: $filter, sortAttribute: $sort, pagination: $pagination) {
    totalCount
    edges { node { id marketplaceJobPosting { id content { title } } auditDetails { createdDateTime { rawValue } } } }
  }
}
"""
# Confirmed with /api/debug/schema: VendorProposalSortAttributeType = CREATEDDATETIME |
# MODIFIEDDATETIME and SortOrder = ASC | DESC. (Asking Upwork for several type
# descriptions in one query is rejected as "bad faith introspection".)
SORT = {"field": "CREATEDDATETIME", "sortOrder": "DESC"}

_cache = {"ids": set(), "titles": {}}


# ---------------------------------------------------------------------------
# Parsing helpers (pure)
# ---------------------------------------------------------------------------

def normalize_title(title: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def parse_proposals(result: dict) -> list[dict]:
    """[{job_id, title, created_ms}] out of a vendorProposals response."""
    edges = (((result or {}).get("data") or {}).get("vendorProposals") or {}).get("edges") or []
    out = []
    for edge in edges:
        node = (edge or {}).get("node") or {}
        job = node.get("marketplaceJobPosting") or {}
        if not job.get("id"):
            continue
        created = (((node.get("auditDetails") or {}).get("createdDateTime") or {}).get("rawValue"))
        try:
            created = int(created)
        except (TypeError, ValueError):
            created = None
        out.append({"job_id": str(job["id"]), "title": (job.get("content") or {}).get("title") or "",
                    "created_ms": created})
    return out


def parse(result: dict) -> set[str]:
    return {p["job_id"] for p in parse_proposals(result)}


# ---------------------------------------------------------------------------
# Upwork calls
# ---------------------------------------------------------------------------

def _client():
    return get_client(config.UPWORK_AGENCY_ORG_ID or None)


def _query(client, filter_obj: dict, first: int) -> list[dict] | None:
    """Run vendorProposals; None on any problem (callers treat that as 'unknown')."""
    result = execute_graphql(client, _QUERY, {
        "filter": filter_obj, "sort": SORT, "pagination": {"first": first}})
    if result and result.get("errors"):
        logger.warning("Proposal lookup rejected (%s); relying on the other duplicate checks.",
                       "; ".join(e.get("message", "?") for e in result["errors"])[:250])
        return None
    return parse_proposals(result)


def _base_filter() -> dict:
    return {"organizationId_eq": config.UPWORK_AGENCY_ORG_ID} if config.UPWORK_AGENCY_ORG_ID else {}


def check_jobs(job_ids: list[str]) -> bool:
    """Ask Upwork which of these jobs we already applied to. Returns True if the lookup worked."""
    ids = [str(j) for j in job_ids if j]
    if not ids:
        return True
    try:
        client = _client()
        for i in range(0, len(ids), CHUNK):
            chunk = ids[i:i + CHUNK]
            found = _query(client, {**_base_filter(), "jobPostingIds_any": chunk}, first=CHUNK)
            if found is None:
                return False
            _cache["ids"].update(p["job_id"] for p in found)
    except Exception:
        logger.exception("Could not check existing proposals; relying on the other duplicate checks.")
        return False
    return True


def refresh_recent(limit: int = 100) -> bool:
    """Remember the titles of our latest proposals (for repost warnings)."""
    try:
        found = _query(_client(), _base_filter(), first=limit)
    except Exception:
        logger.exception("Could not load recent proposals.")
        return False
    if found is None:
        return False
    _cache["titles"] = {}
    for p in found:
        key = normalize_title(p["title"])
        if key:
            _cache["titles"][key] = {"job_id": p["job_id"], "title": p["title"], "created_ms": p["created_ms"]}
    logger.info("Recent applied proposals loaded: %d", len(_cache["titles"]))
    return True


# ---------------------------------------------------------------------------
# Lookups used by the pipeline
# ---------------------------------------------------------------------------

def is_applied(job_id: str) -> bool:
    return str(job_id) in _cache["ids"]


def similar_applied(title: str | None) -> dict | None:
    """A recent proposal of ours with the same title (a possible repost), or None."""
    hit = _cache["titles"].get(normalize_title(title))
    if not hit:
        return None
    created = hit.get("created_ms")
    if created and (time.time() * 1000 - created) > TITLE_MAX_AGE_DAYS * 86400 * 1000:
        return None
    return hit


# ---------------------------------------------------------------------------
# Diagnostics (read-only), exposed through /api/debug/*
# ---------------------------------------------------------------------------

_TYPE_INFO = """
query TypeInfo($name: String!) {
  __type(name: $name) {
    kind name
    enumValues { name }
    inputFields { name type { name kind ofType { name kind ofType { name kind } } } }
    fields { name }
  }
}
"""


def type_info(name: str) -> dict:
    """Raw introspection of one GraphQL type (so the bot's queries can be written exactly)."""
    return execute_graphql(_client(), _TYPE_INFO, {"name": name})


def diagnose(job_ids: list[str]) -> dict:
    """Run the real lookup once and return everything Upwork said, errors included."""
    client = _client()
    out = {"organization": config.UPWORK_AGENCY_ORG_ID or None, "sort": SORT}
    filter_obj = _base_filter()
    if job_ids:
        filter_obj["jobPostingIds_any"] = [str(j) for j in job_ids]
    result = execute_graphql(client, _QUERY, {
        "filter": filter_obj, "sort": SORT, "pagination": {"first": 25}})
    out["filter_sent"] = filter_obj
    out["errors"] = (result or {}).get("errors")
    out["total_count"] = (((result or {}).get("data") or {}).get("vendorProposals") or {}).get("totalCount")
    out["found"] = parse_proposals(result)
    return out
