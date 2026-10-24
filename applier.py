"""
applier.py — Submits a proposal to Upwork through the GraphQL API.

The mutation and its input fields were confirmed with `python check_apply_api.py` on
the deployed bot. Whether this app's token is *authorised* to call it is only known
after a real submission. If Upwork rejects the call, apply() raises ApplyError with
Upwork's message; the pipeline shows that in Slack with the job link so nothing is
lost.
"""

import logging

import config
import scoring
from upwork_client import execute_graphql, get_client

logger = logging.getLogger(__name__)

# Upwork's estimatedDuration ids (observed via the MCP preview).
_DURATION_IDS = {"less_than_1_month": 4, "1_to_3_months": 3, "3_to_6_months": 2,
                 "more_than_6_months": 1}


class ApplyError(Exception):
    """Raised when Upwork refuses or fails the submission."""


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
    if scoring.fixed_budget(job) is not None:
        params["payment_mode"] = "by_milestone"
        params["estimated_duration"] = "less_than_1_month"
        params["milestones"] = milestones_for(float(app["bid"]), cfg.get("milestone_split") or [50, 50])
    return params


def build_input(app: dict, job: dict, filters: dict) -> dict:
    """
    The CreateJobProposalInput object (fields confirmed by check_apply_api.py):
    selectedContractor{id,oDeskUserID}, jobReference, chargedAmount, coverLetter,
    questions[{question,answer}], sri{percent,frequency}, agencyOrgId, teamOrgId,
    estimatedDuration (Int), milestones[{amount,description,dueDate}].
    """
    cfg = filters.get("apply") or {}
    payload = {
        "jobReference": str(job.get("id") or app["job_id"]),
        "coverLetter": app["letter"],
        "chargedAmount": float(app["bid"]),
        "agencyOrgId": config.UPWORK_AGENCY_ORG_ID,
        "teamOrgId": config.UPWORK_AGENCY_ORG_ID,
        "sri": {"percent": 0, "frequency": 0},
    }
    contractor = {}
    if config.UPWORK_FREELANCER_ID:
        contractor["id"] = config.UPWORK_FREELANCER_ID
    if config.UPWORK_FREELANCER_ODESK_ID:
        contractor["oDeskUserID"] = config.UPWORK_FREELANCER_ODESK_ID
    if contractor:
        payload["selectedContractor"] = contractor

    if scoring.fixed_budget(job) is not None:
        payload["milestones"] = milestones_for(
            float(app["bid"]), cfg.get("milestone_split") or [50, 50])
        payload["estimatedDuration"] = _DURATION_IDS["less_than_1_month"]
    return payload


_MUTATION = """
mutation CreateJobProposal($input: CreateJobProposalInput!) {
  createJobProposal(input: $input) {
    status
    error
    newProposalId
  }
}
"""


def apply(app: dict, job: dict, filters: dict) -> str:
    """Submit the proposal. Returns the new proposal id or raises ApplyError."""
    payload = build_input(app, job, filters)
    client = get_client()
    try:
        # Variables (not an inlined literal) so line breaks and quotes in the
        # cover letter can't break the GraphQL document.
        result = execute_graphql(client, _MUTATION, {"input": payload})
    except Exception as exc:  # network / SDK errors
        raise ApplyError(f"Upwork request failed: {exc}") from exc

    if result and result.get("errors"):
        msgs = "; ".join(e.get("message", str(e)) for e in result["errors"])
        raise ApplyError(f"Upwork rejected the request: {msgs}")

    data = ((result or {}).get("data") or {}).get("createJobProposal") or {}
    if data.get("status") != "SUCCESS" or not data.get("newProposalId"):
        raise ApplyError(f"Proposal not created: {data.get('error') or data or 'no response'}")
    return str(data["newProposalId"])
