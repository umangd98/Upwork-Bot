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
        "tutor", "tutoring", "mentor", "coaching", "teach me", "trainer",
    ],
    "min_fixed_budget": 800,
    "min_hourly": 35,
    "max_applicants": 20,
    "budget_tiers": [
        {"up_to": 1500, "weeks": 1},
        {"up_to": 2000, "weeks": 2},
        {"up_to": 3000, "weeks": 3},
    ],
}


def weeks_for_budget(budget: float, cfg: dict | None = None) -> float | None:
    """Weeks of delivery time our rules allow for a fixed budget (None = above all tiers: needs manual scope + approval, never auto-bid)."""
    tiers = {**_DEFAULTS, **(cfg or {})}["budget_tiers"]
    for tier in sorted(tiers, key=lambda t: t["up_to"]):
        if budget <= tier["up_to"]:
            return tier["weeks"]
    return None


_URL_RE = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|io|co|ai|net|org|app)\b)", re.I)


def is_credible_new_client(job: dict, cfg: dict | None = None) -> bool:
    """True if a client with no history is worth a bet: well budgeted, or a detailed post naming a website."""
    bet = ((cfg or {}).get("new_client_bet")) or {}
    if not bet.get("enabled"):
        return False
    fixed = _amount(job.get("amount"))
    hmin, hmax = _amount(job.get("hourlyBudgetMin")), _amount(job.get("hourlyBudgetMax"))
    hourly_top = hmax if hmax is not None else hmin
    if fixed is not None and fixed >= bet.get("min_fixed_budget", 1000):
        return True
    if hourly_top is not None and hourly_top >= bet.get("min_hourly", 40):
        return True
    desc = job.get("description") or ""
    return len(desc) >= bet.get("min_description_chars", 800) and bool(_URL_RE.search(desc))


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
