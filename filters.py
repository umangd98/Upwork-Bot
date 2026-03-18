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

SEARCH_QUERY = """
query marketplaceJobPostingsSearch(
  $marketPlaceJobFilter: MarketplaceJobPostingsSearchFilter
) {
  marketplaceJobPostingsSearch(
    marketPlaceJobFilter: $marketPlaceJobFilter
    searchType: USER_JOBS_SEARCH
  ) {
    totalCount
    edges {
      node {
        id
        title
        description
        ciphertext
        publishedDateTime
        type
        contractorTier
        totalApplicants
        duration
        durationLabel
        engagement
        amount {
          amount
          currencyCode
        }
        hourlyBudgetMin {
          amount
        }
        hourlyBudgetMax {
          amount
        }
        skills {
          name
          prettyName
        }
        ontologySkills {
          skill {
            name
            prettyName
          }
        }
        client {
          totalHires
          totalPostedJobs
          totalSpent {
            amount
            currencyCode
          }
          totalReviews
          totalFeedback
          verificationStatus
          companyName
          location {
            city
            country
          }
        }
      }
      cursor
    }
    pageInfo {
      endCursor
      hasNextPage
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Build the filter variables from filters.yaml
# ---------------------------------------------------------------------------

def _build_api_filter(filters: dict, cursor: str | None = None) -> dict:
    """
    Translate our YAML config into the GraphQL filter variables.
    """
    gql_filter: dict[str, Any] = {}

    # Keywords → searchExpression_eq
    if filters["keywords"]:
        gql_filter["searchExpression_eq"] = filters["keywords"]

    # Skills → skillExpression_eq (comma-separated, OR logic)
    if filters["skills"]:
        gql_filter["skillExpression_eq"] = ",".join(filters["skills"])

    # Experience level
    level = filters["experience_level"].upper()
    if level in ("ENTRY_LEVEL", "INTERMEDIATE", "EXPERT"):
        gql_filter["experienceLevel_eq"] = level

    # Contract type
    jtype = filters["job_type"].upper()
    if jtype in ("HOURLY", "FIXED_PRICE"):
        gql_filter["jobType_eq"] = jtype

    # Budget range (fixed-price)
    b_min = filters["budget_min"]
    b_max = filters["budget_max"]
    if b_min is not None or b_max is not None:
        budget_range: dict[str, int] = {}
        if b_min is not None:
            budget_range["min"] = int(b_min)
        if b_max is not None:
            budget_range["max"] = int(b_max)
        gql_filter["budgetRange_eq"] = budget_range

    # Hourly rate range
    h_min = filters["hourly_rate_min"]
    h_max = filters["hourly_rate_max"]
    if h_min is not None or h_max is not None:
        hourly_range: dict[str, int] = {}
        if h_min is not None:
            hourly_range["min"] = int(h_min)
        if h_max is not None:
            hourly_range["max"] = int(h_max)
        gql_filter["hourlyRate_eq"] = hourly_range

    # Verified payment
    if filters["verified_payment_only"]:
        gql_filter["verifiedPaymentOnly_eq"] = True

    # Days posted
    if filters["days_posted"]:
        gql_filter["daysPosted_eq"] = int(filters["days_posted"])

    # Pagination (cursor-based)
    page_size = filters["page_size"]
    pagination: dict[str, Any] = {"first": page_size}
    if cursor:
        pagination["after"] = cursor
    gql_filter["pagination_eq"] = pagination

    return {"marketPlaceJobFilter": gql_filter}


# ---------------------------------------------------------------------------
# Post-filters (applied in Python)
# ---------------------------------------------------------------------------

def _passes_post_filters(job: dict, filters: dict) -> bool:
    """Return True if the job passes all post-filter thresholds."""
    client = job.get("client") or {}

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
        spent_amount = float(spent_obj.get("amount", 0) or 0)
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
    cursor: str | None = None
    page = 0
    max_pages = 5  # safety cap

    while page < max_pages:
        page += 1
        variables = _build_api_filter(filters, cursor)
        logger.info("Fetching page %d (cursor=%s) …", page, cursor or "start")

        try:
            result = execute_graphql(client, SEARCH_QUERY, variables)
        except Exception:
            logger.exception("GraphQL request failed on page %d", page)
            break

        search_data = (result or {}).get("data", {}).get(
            "marketplaceJobPostingsSearch", {}
        )
        edges = search_data.get("edges") or []
        page_info = search_data.get("pageInfo") or {}
        total = search_data.get("totalCount", "?")

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

        # Advance cursor
        if page_info.get("hasNextPage") and edges:
            cursor = page_info.get("endCursor") or edges[-1].get("cursor")
        else:
            break

    logger.info(
        "Search complete: %d job(s) passed all filters.", len(all_jobs)
    )
    return all_jobs
