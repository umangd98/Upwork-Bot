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
_ENUMS_QUERY = """
{
  fields: __type(name: "VendorProposalSortAttributeType") { enumValues { name } }
  orders: __type(name: "SortOrder") { enumValues { name } }
}
"""

_cache = {"ids": set(), "titles": {}, "sort": None}


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


def pick_sort(enums_result: dict) -> dict | None:
    """Choose a sort field (prefer 'created') and order (prefer newest first) from Upwork's enums."""
    data = (enums_result or {}).get("data") or {}
    fields = [v["name"] for v in ((data.get("fields") or {}).get("enumValues") or [])]
    orders = [v["name"] for v in ((data.get("orders") or {}).get("enumValues") or [])]
    if not fields or not orders:
        return None
    field = next((f for f in fields if "CREAT" in f.upper()), fields[0])
    order = next((o for o in orders if o.upper().startswith("DESC")), orders[0])
    return {"field": field, "sortOrder": order}


# ---------------------------------------------------------------------------
# Upwork calls
# ---------------------------------------------------------------------------

def _client():
    return get_client(config.UPWORK_AGENCY_ORG_ID or None)


def _sort(client) -> dict | None:
    if _cache["sort"] is None:
        _cache["sort"] = pick_sort(execute_graphql(client, _ENUMS_QUERY, {}))
        if _cache["sort"] is None:
            logger.warning("Could not read vendorProposals sort options.")
    return _cache["sort"]


def _query(client, filter_obj: dict, first: int) -> list[dict] | None:
    """Run vendorProposals; None on any problem (callers treat that as 'unknown')."""
    sort = _sort(client)
    if not sort:
        return None
    result = execute_graphql(client, _QUERY, {
        "filter": filter_obj, "sort": sort, "pagination": {"first": first}})
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
