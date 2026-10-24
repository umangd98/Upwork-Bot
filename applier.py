"""
applier.py — Submits a proposal to Upwork through the GraphQL API.

⚠ UNVERIFIED: the mutation name and input fields below mirror what the Upwork MCP
reports (createJobProposal → status / error / newProposalId), but Upwork's public
API may not expose it to this app's API key. Run `python check_apply_api.py` once
on the deployed bot to find out. If the call is rejected, apply() raises
ApplyError with Upwork's message; the pipeline shows that in Slack together with
the job link so nothing is lost.
"""

import logging

import config
from upwork_client import execute_graphql, get_client

logger = logging.getLogger(__name__)

# Upwork's estimatedDuration ids (observed via the MCP preview).
_DURATION_IDS = {"less_than_1_month": 4, "1_to_3_months": 3, "3_to_6_months": 2,
                 "more_than_6_months": 1}


class ApplyError(Exception):
    """Raised when Upwork refuses or fails the submission."""


def _gql_literal(v) -> str:
    # Reuse the same inline-literal approach as the search query.
    from filters import _gql_value
    return _gql_value(v)


def milestones_for(total: float, split: list) -> list[dict]:
    """Split a fixed price into milestones (rounding fixed so they sum to the bid)."""
    amounts = [round(total * pct / 100, 2) for pct in split]
    amounts[-1] = round(total - sum(amounts[:-1]), 2)
    labels = ["Start of the project", "Delivery and handover"]
    return [{"amount": a, "description": labels[min(i, len(labels) - 1)]}
            for i, a in enumerate(amounts)]


def build_mcp_params(app: dict, job: dict, filters: dict) -> dict:
    """
    Exact `params` for the Upwork MCP manage_proposals action=create, so the Claude
    session only has to pass them through (no judgement calls on money or terms).
    """
    cfg = filters.get("apply") or {}
    params = {
        "job_reference": str(job.get("id") or app["job_id"]),
        "charged_amount": float(app["bid"]),
        "cover_letter": app["letter"],
    }
    if (job.get("amount") or {}).get("rawValue") not in (None, ""):
        params["payment_mode"] = "by_milestone"
        params["estimated_duration"] = "less_than_1_month"
        params["milestones"] = milestones_for(float(app["bid"]), cfg.get("milestone_split") or [50, 50])
    return params


def build_input(app: dict, job: dict, filters: dict) -> dict:
    """Assemble the createJobProposal input for a stored application."""
    cfg = filters.get("apply") or {}
    payload = {
        "jobReference": str(job.get("id") or app["job_id"]),
        "coverLetter": app["letter"],
        "chargedAmount": float(app["bid"]),
        "agencyOrgId": config.UPWORK_AGENCY_ORG_ID,
        "teamOrgId": config.UPWORK_AGENCY_ORG_ID,
    }
    if config.UPWORK_FREELANCER_ID:
        payload["selectedContractor"] = {"id": config.UPWORK_FREELANCER_ID}

    is_fixed = (job.get("amount") or {}).get("rawValue") not in (None, "")
    if is_fixed:
        payload["milestones"] = milestones_for(
            float(app["bid"]), cfg.get("milestone_split") or [50, 50])
        payload["estimatedDuration"] = _DURATION_IDS["less_than_1_month"]
    return payload


def apply(app: dict, job: dict, filters: dict) -> str:
    """Submit the proposal. Returns the new proposal id or raises ApplyError."""
    payload = build_input(app, job, filters)
    query = (
        "mutation {\n"
        f"  createJobProposal(input: {_gql_literal(payload)}) {{\n"
        "    status error newProposalId\n"
        "  }\n"
        "}"
    )
    client = get_client()
    try:
        result = execute_graphql(client, query, {})
    except Exception as exc:  # network / SDK errors
        raise ApplyError(f"Upwork request failed: {exc}") from exc

    if result and result.get("errors"):
        msgs = "; ".join(e.get("message", str(e)) for e in result["errors"])
        raise ApplyError(f"Upwork rejected the request: {msgs}")

    data = ((result or {}).get("data") or {}).get("createJobProposal") or {}
    if data.get("status") != "SUCCESS" or not data.get("newProposalId"):
        raise ApplyError(f"Proposal not created: {data.get('error') or data or 'no response'}")
    return str(data["newProposalId"])
