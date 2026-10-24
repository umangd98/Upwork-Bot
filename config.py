"""
config.py — Loads environment variables and filter configuration.
"""

import os
import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
load_dotenv()

UPWORK_CLIENT_ID: str = os.getenv("UPWORK_CLIENT_ID", "")
UPWORK_CLIENT_SECRET: str = os.getenv("UPWORK_CLIENT_SECRET", "")
UPWORK_REDIRECT_URI: str = os.getenv("UPWORK_REDIRECT_URI", "http://localhost:9876/callback")
WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")
POLL_INTERVAL_MINUTES: int = int(os.getenv("POLL_INTERVAL_MINUTES", "30"))
AUTH_SERVER_PORT: int = int(os.getenv("AUTH_SERVER_PORT", "9876"))

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TOKEN_FILE = os.path.join(DATA_DIR, "tokens.json")
DB_FILE = os.path.join(DATA_DIR, "jobs.db")
FILTERS_FILE = os.path.join(BASE_DIR, "filters.yaml")
METRICS_FILE = os.path.join(DATA_DIR, "profile_metrics.json")

# Profile metrics retention (days to keep daily snapshots)
METRICS_RETENTION_DAYS: int = int(os.getenv("METRICS_RETENTION_DAYS", "90"))

# Dashboard basic auth — set DASHBOARD_PASSWORD to enable (username: admin)
# Leave empty for open access (fine for private/internal deployments)
DASHBOARD_PASSWORD: str = os.getenv("DASHBOARD_PASSWORD", "")

# n8n posts to Slack for us. The bot sends job events to N8N_WEBHOOK_URL;
# n8n calls back into /api/* (authenticated with BOT_API_SECRET) when you click
# Apply / Skip or save an edited cover letter in Slack.
N8N_WEBHOOK_URL: str = os.getenv("N8N_WEBHOOK_URL", "")
BOT_API_SECRET: str = os.getenv("BOT_API_SECRET", "")

# Cover letters are drafted with the Anthropic API.
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")

# Which Upwork account proposals go out under (agency org_uid + the freelancer).
UPWORK_AGENCY_ORG_ID: str = os.getenv("UPWORK_AGENCY_ORG_ID", "")
UPWORK_FREELANCER_ID: str = os.getenv("UPWORK_FREELANCER_ID", "")

# How approved proposals are submitted:
#   "api": the bot calls Upwork's GraphQL API itself (needs the app key to allow it)
#   "mcp": the bot only queues them; a Claude Code session loop submits them through
#          the Upwork MCP and reports back via POST /api/result/<job_id>.
APPLY_BACKEND: str = os.getenv("APPLY_BACKEND", "api").lower()

# Safety switch. While false, nothing is ever submitted: jobs that WOULD be
# auto-applied are posted to Slack as a dry run with an Apply button instead.
AUTO_APPLY_ENABLED: bool = os.getenv("AUTO_APPLY_ENABLED", "false").lower() == "true"

# Optional private cover-letter context (kept out of git, lives in data/).
LETTER_CONTEXT_FILE = os.path.join(DATA_DIR, "letter_context.md")

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
def load_filters() -> dict:
    """Load and return the filter configuration from filters.yaml."""
    with open(FILTERS_FILE, "r") as f:
        raw = yaml.safe_load(f) or {}

    return {
        # API-side filters
        "keywords": raw.get("keywords", "") or "",
        "skills": raw.get("skills") or [],
        "experience_level": raw.get("experience_level", "") or "",
        "job_type": raw.get("job_type", "") or "",
        "budget_min": raw.get("budget_min"),
        "budget_max": raw.get("budget_max"),
        "hourly_rate_min": raw.get("hourly_rate_min"),
        "hourly_rate_max": raw.get("hourly_rate_max"),
        "verified_payment_only": raw.get("verified_payment_only", True),
        "locations": raw.get("locations") or [],
        "days_posted": raw.get("days_posted", 3),
        # Post-filters
        "min_hire_rate": float(raw.get("min_hire_rate", 0)),
        "min_total_spend": float(raw.get("min_total_spend", 0)),
        "min_reviews": int(raw.get("min_reviews", 0)),
        "min_rating": float(raw.get("min_rating", 0)),
        # Fit scoring (see scoring.py)
        "scoring": raw.get("scoring") or {},
        # Apply rules
        "apply": raw.get("apply") or {},
        # Pagination
        "page_size": min(int(raw.get("page_size", 50)), 50),
    }
