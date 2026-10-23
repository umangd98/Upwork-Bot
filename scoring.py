"""
scoring.py — Rates how well a job fits the freelancer's profile (0-100).

Pure function over the raw GraphQL job node, so it is cheap to run on every
poll and easy to tweak via the `scoring:` block in filters.yaml.
"""

import re
from typing import Any

_DEFAULTS: dict[str, Any] = {
    "min_score": 0,
    "core_terms": [
        "n8n", "make.com", "integromat", "zapier", "ai agent", "ai agents",
        "ai automation", "workflow automation", "gohighlevel", "highlevel",
        "automation", "llm", "claude", "openai", "api integration",
    ],
    "avoid_terms": [
        "virtual assistant", "executive assistant", "data entry", "cold call",
        "appointment setter", "sdr", "social media manager", "graphic design",
        "wordpress", "wix", "shopify theme",
    ],
    "min_fixed_budget": 1000,
    "min_hourly": 35,
    "max_applicants": 20,
}


def _text(job: dict) -> str:
    skills = " ".join(
        (s.get("prettyName") or s.get("name") or "") for s in job.get("skills") or []
    )
    return f"{job.get('title', '')} {skills} {job.get('description', '')}".lower()


def _title(job: dict) -> str:
    return (job.get("title") or "").lower()


def _has(term: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def _amount(obj: Any) -> float | None:
    try:
        return float((obj or {}).get("rawValue"))
    except (TypeError, ValueError):
        return None


def score_job(job: dict, cfg: dict | None = None) -> tuple[int, list[str]]:
    """Return (score 0-100, reasons) for a job node."""
    c = {**_DEFAULTS, **(cfg or {})}
    text, title = _text(job), _title(job)
    score, why = 0, []

    # Topic fit (max 50): core term in title counts double vs. body only.
    title_hits = [t for t in c["core_terms"] if _has(t, title)]
    body_hits = [t for t in c["core_terms"] if _has(t, text) and t not in title_hits]
    topic = min(50, 30 * bool(title_hits) + min(20, 7 * (len(title_hits) + len(body_hits))))
    score += topic
    if topic:
        why.append(f"topic +{topic} ({', '.join((title_hits + body_hits)[:4])})")

    avoided = [t for t in c["avoid_terms"] if _has(t, title)]
    if avoided:
        score -= 40
        why.append(f"avoid-term in title -40 ({', '.join(avoided)})")

    # Budget (max 20)
    fixed = _amount(job.get("amount"))
    hmin, hmax = _amount(job.get("hourlyBudgetMin")), _amount(job.get("hourlyBudgetMax"))
    if hmin is not None or hmax is not None:
        top = hmax if hmax is not None else hmin
        if top >= c["min_hourly"]:
            score += 20
            why.append("hourly budget ok +20")
        else:
            score -= 20
            why.append(f"hourly cap ${top:g} below floor -20")
    elif fixed is not None:
        if fixed >= c["min_fixed_budget"]:
            score += 20
            why.append("fixed budget ok +20")
        else:
            score -= 20
            why.append(f"fixed ${fixed:g} below floor -20")

    # Client quality (max 20)
    client = job.get("client") or {}
    spent = _amount(client.get("totalSpent")) or 0
    posted = client.get("totalPostedJobs") or 0
    hires = client.get("totalHires") or 0
    if spent >= 10000:
        score += 10; why.append("client spend ≥$10k +10")
    elif spent >= 1000:
        score += 5; why.append("client spend ≥$1k +5")
    if posted and hires / posted >= 0.5:
        score += 10; why.append("hire rate ≥50% +10")

    # Competition (max 10)
    applicants = job.get("totalApplicants")
    if isinstance(applicants, (int, float)):
        if applicants <= c["max_applicants"] // 2:
            score += 10; why.append("low competition +10")
        elif applicants > c["max_applicants"]:
            score -= 10; why.append("crowded -10")

    return max(0, min(100, score)), why
