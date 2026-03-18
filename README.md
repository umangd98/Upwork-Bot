# Upwork Job Scraper Bot

A Python bot that monitors Upwork for new job postings matching your filters and sends them to a custom webhook endpoint. Runs on a configurable schedule (default: every 10 minutes), deduplicates with SQLite, and is fully Dockerized for easy hosting.

## Features

- **Keyword search** — free-text search with partial Lucene syntax support
- **Skill-based filtering** — OR logic (matches if job has *any* of your listed skills)
- **Client quality filters** — hire rate, total spend, review count, rating
- **Verified payment** — only show clients with verified payment methods
- **Deduplication** — SQLite-backed tracking so you never get the same job twice
- **Webhook notifications** — POST each matching job to your custom endpoint with retry logic
- **Dual auth modes** — browser-based Flask OAuth2 flow *or* CLI paste-the-URL flow
- **Auto token refresh** — access tokens are refreshed and persisted automatically
- **Dockerized** — single `docker compose up` to run

## Prerequisites

1. **Upwork API credentials** — Apply at [upwork.com/developer/keys/apply](https://www.upwork.com/developer/keys/apply). You'll receive a `client_id` and `client_secret` after approval (~2 weeks).
2. **Docker & Docker Compose** installed on your host machine.
3. **A webhook endpoint** — any URL that accepts POST requests with JSON bodies.

## Quick Start

### 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials:

```dotenv
UPWORK_CLIENT_ID=your_client_id
UPWORK_CLIENT_SECRET=your_client_secret
UPWORK_REDIRECT_URI=http://localhost:9876/callback
WEBHOOK_URL=https://your-endpoint.example.com/webhook
POLL_INTERVAL_MINUTES=10
AUTH_SERVER_PORT=9876
```

> Make sure the `UPWORK_REDIRECT_URI` exactly matches the redirect URI in your Upwork app settings.

### 2. Configure filters

Edit `filters.yaml` to set your job search criteria:

```yaml
# Free-text keyword search
keywords: "zapier OR make.com OR n8n"

# Skills (OR logic — matches if job has ANY of these)
skills:
  - python
  - react
  - zapier

# Client quality thresholds
verified_payment_only: true
days_posted: 3
min_hire_rate: 0.7
min_total_spend: 10000
min_reviews: 5
min_rating: 4.2
```

See [Filter Reference](#filter-reference) below for all available options.

### 3. Build and run

```bash
docker compose up --build
```

On first run, the bot will start a local auth server. Open your browser to:

```
http://localhost:9876/login
```

Authorize the app with your Upwork account. After successful auth, the bot automatically starts polling.

### 4. (Alternative) CLI auth mode

If you prefer not to use the browser flow:

```bash
docker compose run --rm upwork-bot python entrypoint.py --cli
```

This prints the authorization URL — visit it, authorize, then paste the full callback URL back into the terminal.

## Project Structure

```
├── entrypoint.py        # Main entry — routes to auth or scheduler
├── auth_server.py       # Flask OAuth2 callback server + CLI fallback
├── config.py            # Loads .env + filters.yaml
├── upwork_client.py     # Upwork SDK wrapper — token load/save/refresh
├── filters.py           # GraphQL query builder, pagination, post-filters
├── notifier.py          # Webhook POST with retry + exponential backoff
├── db.py                # SQLite dedup — tracks notified job IDs
├── scheduler.py         # APScheduler 10-min polling loop
├── lookup_skills.py     # Helper to discover Upwork skill slugs
├── filters.yaml         # Your filter configuration
├── .env.example         # Environment variable template
├── requirements.txt     # Python dependencies
├── Dockerfile           # python:3.11-slim image
├── docker-compose.yml   # Volume mounts, port mapping, auto-restart
└── data/                # Persistent data (auto-created)
    ├── tokens.json      # OAuth2 tokens (auto-managed)
    └── jobs.db          # SQLite dedup database
```

## Filter Reference

### API-Side Filters (sent in the GraphQL query)

| Setting | Type | Description | Example |
|---|---|---|---|
| `keywords` | string | Free-text search (Lucene partial syntax) | `"python web scraping"` |
| `skills` | list | Skill slugs — OR logic | `- python` |
| `experience_level` | string | `ENTRY_LEVEL`, `INTERMEDIATE`, `EXPERT`, or blank | `INTERMEDIATE` |
| `job_type` | string | `HOURLY`, `FIXED_PRICE`, or blank | `HOURLY` |
| `budget_min` | int/null | Minimum fixed-price budget ($) | `500` |
| `budget_max` | int/null | Maximum fixed-price budget ($) | `5000` |
| `hourly_rate_min` | int/null | Minimum hourly rate ($/hr) | `25` |
| `hourly_rate_max` | int/null | Maximum hourly rate ($/hr) | `100` |
| `verified_payment_only` | bool | Only verified-payment clients | `true` |
| `days_posted` | int | Jobs from the last N days | `3` |

### Post-Filters (applied in Python after fetching)

| Setting | Type | Description | Example |
|---|---|---|---|
| `min_hire_rate` | float | Min ratio of `hires / jobs_posted` (0.0–1.0) | `0.5` |
| `min_total_spend` | float | Min total $ spent by client on Upwork | `1000` |
| `min_reviews` | int | Min number of client reviews | `5` |
| `min_rating` | float | Min client feedback score (0.0–5.0) | `4.0` |

## Skill Lookup

The `skills` filter uses Upwork's canonical slug names (e.g. `python`, `crm-software`). To find the right slugs:

**Interactive mode:**
```bash
docker compose exec upwork-bot python lookup_skills.py
```

**Batch mode:**
```bash
docker compose exec upwork-bot python lookup_skills.py --batch "Python, React, Zapier, n8n"
```

This outputs YAML-ready skill slugs you can paste directly into `filters.yaml`.

## Webhook Payload

Each matching job is POSTed to your webhook as a JSON object:

```json
{
  "title": "Build a Zapier Integration for CRM",
  "url": "https://www.upwork.com/jobs/~01abc123...",
  "description": "We need an automation expert to...",
  "posted_at": "2026-03-18T10:30:00Z",
  "type": "FIXED_PRICE",
  "experience_level": "INTERMEDIATE",
  "duration": "1 to 3 months",
  "engagement": "30+ hrs/week",
  "budget": 2000,
  "hourly_range": null,
  "currency": "USD",
  "total_applicants": 5,
  "skills": ["Zapier", "Python", "API Integration"],
  "client": {
    "company": "Acme Corp",
    "location": "New York, United States",
    "total_hires": 42,
    "total_posted_jobs": 50,
    "hire_rate": 0.84,
    "total_spent": 125000.00,
    "total_spent_currency": "USD",
    "total_reviews": 38,
    "rating": 4.9,
    "verified": "VERIFIED"
  }
}
```

## Common Operations

| Task | Command |
|---|---|
| Start the bot | `docker compose up --build -d` |
| View logs | `docker compose logs -f` |
| Restart (reload filters) | `docker compose restart` |
| Stop | `docker compose down` |
| Re-authenticate | `docker compose exec upwork-bot python entrypoint.py --cli --auth-only` |
| Look up skill slugs | `docker compose exec upwork-bot python lookup_skills.py` |

## Notes

- **Rate limits** — Upwork allows 40,000 API requests/day. At 10-min intervals the bot uses ~450/day.
- **Token refresh** — Access tokens expire after 24 hours. The bot auto-refreshes using the refresh token and persists the new token to `data/tokens.json`.
- **Dedup pruning** — The SQLite database auto-prunes entries older than 30 days on each poll cycle.
- **Filters reload** — `filters.yaml` is re-read on every poll cycle, so changes take effect within 10 minutes without restarting.
