"""
filters.py — Builds the GraphQL query, paginates through results,
and applies post-filters that the API can't handle natively.
"""

import logging
from typing import Any

import config
from upwork_client import execute_graphql, get_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL query for marketplaceJobPostingsSearch
# ---------------------------------------------------------------------------

# Response fragment — the fields we want for each job
_RESPONSE_FIELDS = """
    totalCount
    edges {
      node {
        id title description ciphertext publishedDateTime createdDateTime
        experienceLevel totalApplicants duration durationLabel engagement
        hourlyBudgetType
        amount { rawValue currency displayValue }
        hourlyBudgetMin { rawValue currency displayValue }
        hourlyBudgetMax { rawValue currency displayValue }
        skills { name prettyName }
        client {
          totalHires totalPostedJobs
          totalSpent { rawValue currency displayValue }
          totalReviews totalFeedback verificationStatus
          location { city country }
        }
      }
      cursor
    }
    pageInfo { endCursor hasNextPage }
"""


# ---------------------------------------------------------------------------
# Build the inline GraphQL query with all filters embedded
# ---------------------------------------------------------------------------

def _gql_value(v: Any) -> str:
    """Convert a Python value to a GraphQL inline literal."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        # Escape quotes in strings
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, dict):
        inner = ", ".join(f"{k}: {_gql_value(val)}" for k, val in v.items())
        return f"{{ {inner} }}"
    if isinstance(v, (list, tuple)):
        inner = ", ".join(_gql_value(item) for item in v)
        return f"[{inner}]"
    return str(v)


def _build_inline_filter(filters: dict, offset: str = "0") -> str:
    """
    Build the complete inline filter block for the GraphQL query.

    We inline everything (including pagination) because the Upwork API
    returns a 500 error when pagination_eq is passed via variables.
    """
    parts: list[str] = []

    # Keywords
    if filters["keywords"]:
        parts.append(f'searchExpression_eq: {_gql_value(filters["keywords"])}')

    # Skills (OR logic)
    if filters["skills"]:
        skill_expr = ",".join(filters["skills"])
        parts.append(f'skillExpression_eq: {_gql_value(skill_expr)}')

    # Experience level
    level = filters["experience_level"].upper()
    if level in ("ENTRY_LEVEL", "INTERMEDIATE", "EXPERT"):
        parts.append(f"experienceLevel_eq: {level}")

    # Contract type
    jtype = filters["job_type"].upper()
    if jtype in ("HOURLY", "FIXED_PRICE"):
        parts.append(f"jobType_eq: {jtype}")

    # Budget range
    b_min = filters["budget_min"]
    b_max = filters["budget_max"]
    if b_min is not None or b_max is not None:
        r_parts = []
        if b_min is not None:
            r_parts.append(f"rangeStart: {int(b_min)}")
        if b_max is not None:
            r_parts.append(f"rangeEnd: {int(b_max)}")
        parts.append(f"budgetRange_eq: {{ {', '.join(r_parts)} }}")

    # Hourly rate range
    h_min = filters["hourly_rate_min"]
    h_max = filters["hourly_rate_max"]
    if h_min is not None or h_max is not None:
        r_parts = []
        if h_min is not None:
            r_parts.append(f"rangeStart: {int(h_min)}")
        if h_max is not None:
            r_parts.append(f"rangeEnd: {int(h_max)}")
        parts.append(f"hourlyRate_eq: {{ {', '.join(r_parts)} }}")

    # Verified payment
    if filters["verified_payment_only"]:
        parts.append("verifiedPaymentOnly_eq: true")

    # Client locations (country/city, OR logic)
    if filters["locations"]:
        loc_list = ", ".join(_gql_value(loc) for loc in filters["locations"])
        parts.append(f"locations_any: [{loc_list}]")

    # Pagination (inlined to avoid the variables bug)
    page_size = filters["page_size"]
    parts.append(f'pagination_eq: {{ after: "{offset}", first: {page_size} }}')

    return ", ".join(parts)


def _build_search_query(filters: dict, offset: str = "0") -> str:
    """Build the complete GraphQL query with all filters inlined."""
    filter_block = _build_inline_filter(filters, offset)
    return (
        "query {\n"
        "  marketplaceJobPostingsSearch(\n"
        f"    marketPlaceJobFilter: {{ {filter_block} }}\n"
        "    searchType: USER_JOBS_SEARCH\n"
        "  ) {\n"
        f"    {_RESPONSE_FIELDS}\n"
        "  }\n"
        "}"
    )


# ---------------------------------------------------------------------------
# Post-filters (applied in Python)
# ---------------------------------------------------------------------------

def _passes_post_filters(job: dict, filters: dict) -> bool:
    """Return True if the job passes all post-filter thresholds."""
    client = job.get("client") or {}

    # ── Days posted (not available as API filter on authenticated query) ──
    days_posted = filters.get("days_posted")
    if days_posted and days_posted > 0:
        from datetime import datetime, timedelta, timezone
        published = job.get("publishedDateTime") or job.get("createdDateTime")
        if published:
            try:
                # Handle various datetime formats
                pub_str = published.replace("Z", "+00:00")
                pub_dt = datetime.fromisoformat(pub_str)
                cutoff = datetime.now(timezone.utc) - timedelta(days=days_posted)
                if pub_dt < cutoff:
                    return False
            except (ValueError, TypeError):
                pass  # If we can't parse, don't filter out

    # ── Hire rate ──
    min_hire_rate = filters["min_hire_rate"]
    if min_hire_rate > 0:
        total_hires = client.get("totalHires") or 0
        total_posted = client.get("totalPostedJobs") or 0
        hire_rate = (total_hires / total_posted) if total_posted > 0 else 0
        if hire_rate < min_hire_rate:
            return False

    # ── Total spend ──
    min_spend = filters["min_total_spend"]
    if min_spend > 0:
        spent_obj = client.get("totalSpent") or {}
        spent_amount = float(spent_obj.get("rawValue", 0) or 0)
        if spent_amount < min_spend:
            return False

    # ── Reviews ──
    min_reviews = filters["min_reviews"]
    if min_reviews > 0:
        reviews = client.get("totalReviews") or 0
        if reviews < min_reviews:
            return False

    # ── Rating ──
    min_rating = filters["min_rating"]
    if min_rating > 0:
        rating = client.get("totalFeedback") or 0
        if float(rating) < min_rating:
            return False

    return True


# ---------------------------------------------------------------------------
# Main search function
# ---------------------------------------------------------------------------

def search_jobs() -> list[dict]:
    """
    Query the Upwork API, paginate through results, apply post-filters,
    and return a list of matching job dicts.

    Each returned dict has the raw GraphQL node fields plus a computed
    `job_url` key.
    """
    filters = config.load_filters()
    client = get_client()

    all_jobs: list[dict] = []
    offset: str = "0"
    page = 0
    max_pages = 5  # safety cap

    while page < max_pages:
        page += 1
        query = _build_search_query(filters, offset)
        logger.info("Fetching page %d (offset=%s) …", page, offset)

        try:
            result = execute_graphql(client, query, {})
        except Exception:
            logger.exception("GraphQL request failed on page %d", page)
            break

        search_data = ((result or {}).get("data") or {}).get(
            "marketplaceJobPostingsSearch"
        ) or {}
        edges = search_data.get("edges") or []
        page_info = search_data.get("pageInfo") or {}
        total = search_data.get("totalCount", "?")

        if result and result.get("errors"):
            logger.warning("GraphQL errors: %s", result["errors"])

        logger.info(
            "Page %d: received %d edges (total available: %s)",
            page, len(edges), total,
        )

        for edge in edges:
            node = edge.get("node") or {}
            if _passes_post_filters(node, filters):
                # Attach a convenience URL
                cipher = node.get("ciphertext", "")
                node["job_url"] = f"https://www.upwork.com/jobs/~{cipher}" if cipher else ""
                all_jobs.append(node)

        # Advance cursor (endCursor is a numeric string offset like "10", "20")
        if page_info.get("hasNextPage"):
            cursor = page_info.get("endCursor")
            if not cursor:
                break
            offset = cursor
        else:
            break

    logger.info(
        "Search complete: %d job(s) passed all filters.", len(all_jobs)
    )
    return all_jobs
