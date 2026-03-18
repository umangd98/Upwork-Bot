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
POLL_INTERVAL_MINUTES: int = int(os.getenv("POLL_INTERVAL_MINUTES", "10"))
AUTH_SERVER_PORT: int = int(os.getenv("AUTH_SERVER_PORT", "9876"))

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TOKEN_FILE = os.path.join(DATA_DIR, "tokens.json")
DB_FILE = os.path.join(DATA_DIR, "jobs.db")
FILTERS_FILE = os.path.join(BASE_DIR, "filters.yaml")

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
        # Pagination
        "page_size": min(int(raw.get("page_size", 50)), 50),
    }
