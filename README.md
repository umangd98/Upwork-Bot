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

## Auto-apply with Slack alerts and approvals

Every poll (default **every 30 minutes**) the bot searches Upwork, scores jobs, drafts a
cover letter with the Anthropic API, and then:

| Job | What happens |
|---|---|
| Fixed budget **under $3,000**, or any hourly job | **Auto-apply** (if `AUTO_APPLY_ENABLED=true` and fewer than 10 proposals went out in the last 24h). Slack gets a "✅ Applied automatically" message with the job description and the letter that was sent. |
| Fixed budget **$3,000 or more** (or no budget shown) | Slack gets "🟡 Needs your approval" with the description, the letter and **Apply now / Edit letter / Skip** buttons. Nothing is sent until you press Apply. |
| Cap reached (10 in a rolling 24h) | Slack gets "⛔ Daily cap reached" and nothing is sent; the Apply button also refuses until the window frees up. |
| Scope doesn't fit the budget tier ($1,200 ≈ 1 week, $2,000 ≈ 2, $3,000 ≈ 2.5–3), tutoring/VA-type jobs | Silently skipped (logged only). |

Thresholds, cap, hourly bid and milestone split live in the `apply:` block of `filters.yaml`.

### Architecture
```
bot (DigitalOcean)  ──event──▶  n8n webhook ──▶ Slack message (description + letter + buttons)
        ▲                                                │ you click Apply / Edit / Skip
        └──────── /api/apply|skip|letter (Bearer) ◀── n8n ◀── Slack interactivity
```
The bot never talks to Slack directly; n8n does.

### Setup
1. **Bot env vars** (DigitalOcean → Settings): `ANTHROPIC_API_KEY`, `N8N_WEBHOOK_URL`,
   `BOT_API_SECRET` (long random string), `UPWORK_AGENCY_ORG_ID`, `UPWORK_FREELANCER_ID`.
   Leave `AUTO_APPLY_ENABLED=false` at first. If you previously set `POLL_INTERVAL_MINUTES`,
   change it to `30`.
2. **n8n**: import `n8n/upwork_slack_workflow.json`, then
   - create a *Header Auth* credential named `Upwork Bot API` (name `Authorization`,
     value `Bearer <BOT_API_SECRET>`) and attach it to the first webhook and every `Bot:` node;
   - attach your existing Slack credential to the three `Slack:` nodes;
   - in every `Bot:` node replace `https://REPLACE_BOT_URL` with your bot's public URL;
   - in `Build Slack message` and `Build updated message` set `CHANNEL`; in `Parse Slack payload`
     set `ALLOWED_USER` to your Slack member ID (only that user can click the buttons);
   - rename the interactivity webhook path to something unguessable and activate the workflow.
3. **Slack app**: enable *Interactivity* and set the Request URL to the n8n *production* URL of
   the `Slack interactivity` webhook. The app token needs `chat:write`.
4. **Check the Upwork API can submit proposals**: run `python check_apply_api.py` on the bot.
   If `createJobProposal` isn't listed, auto-apply can't work through the API (see below).
5. **Roll out safely**: with `AUTO_APPLY_ENABLED=false` every would-be auto-apply shows up in Slack
   as "🧪 Dry run" with an Apply button. When the messages look right, set it to `true`.

### Testing Slack alerts without HTTPS (buttons off)
Slack's buttons need an HTTPS callback, so the workflow ships with `const SHOW_BUTTONS = false;`
(top of the `Build Slack message` and `Build updated message` nodes). Alerts still arrive with the
job description, cover letter and an "Open on Upwork" link; jobs that need approval just alert you.
Set both constants to `true` once n8n is served over HTTPS.

To test the Slack side on its own, set `N8N_WEBHOOK_URL` and `BOT_API_SECRET` in `.env` and run
`python send_test_event.py` (sends a sample of every alert type, marked `[TEST]`).

### Fallback: submit through a Claude Code session (`APPLY_BACKEND=mcp`)
If `check_apply_api.py` shows Upwork's API won't accept proposals, set `APPLY_BACKEND=mcp`.
The bot then never submits itself. Instead:

1. Auto-apply jobs (and jobs you approve in Slack) are marked **queued** and Slack shows "⏳ Queued".
2. A Claude Code session with the Upwork MCP runs the loop in `claude_loop/mcp_apply_loop.md`
   (`/loop 5m Follow the instructions in claude_loop/mcp_apply_loop.md`). It reads `GET /api/queue`,
   claims each job, creates and confirms the proposal through the MCP with the exact parameters the bot
   computed (bid, 50/50 milestones, your edited letter), then reports `POST /api/result/<job_id>`.
3. The bot records the result and Slack updates to "✅ Applied" or "❌ Apply failed" (screening questions
   the job requires are listed so you can answer them on Upwork).

The session refuses to confirm if the preview's bid, milestones or letter differ from the queue, if the
job needs screening answers, or if it would cost more than 40 Connects. Queued and in-flight jobs count
toward the 10-per-24h cap; a failed submission frees its slot. Limits: the loop only runs while that
session is open and idle, expires after 7 days, and a click waits up to one loop interval.

### Bot API (used by n8n)
`POST /api/apply/<job_id>`, `POST /api/skip/<job_id>`, `POST /api/letter/<job_id>` (`{"letter": "..."}`),
`GET /api/application/<job_id>`, `GET /api/status`; MCP fallback adds `GET /api/queue`,
`POST /api/queue/<job_id>/claim`, `POST /api/result/<job_id>`. All need `Authorization: Bearer <BOT_API_SECRET>`
and return 403 if the secret isn't configured. Button calls pass `?notify=0` so n8n updates the
original Slack message instead of posting a second one.

### Known limits
- **Unverified Upwork call**: `applier.py` uses the `createJobProposal` mutation that the Upwork MCP
  exposes. It is not confirmed that this app's API key may call it. A refusal is shown in Slack as
  "❌ Apply failed" with Upwork's message and the job link; nothing is lost.
- Jobs with **required screening questions** will likely fail to submit; the post's questions are listed
  in the Slack message so you can answer them on Upwork.
- The bot can't look up the client's first name from reviews (the search API doesn't return them);
  the letter uses a name only if the post states it.
- Slack edit boxes hold 3,000 characters; longer letters can't be edited in Slack.
- n8n can't verify Slack's request signature in this workflow; protect it with the unguessable webhook
  path and the `ALLOWED_USER` check.
- Run the offline tests with `python -m unittest discover -s tests`.

## Notes

- **Rate limits** — Upwork allows 40,000 API requests/day. At 30-min intervals the bot uses far less than that.
- **Token refresh** — Access tokens expire after 24 hours. The bot auto-refreshes using the refresh token and persists the new token to `data/tokens.json`.
- **Dedup pruning** — The SQLite database auto-prunes entries older than 30 days on each poll cycle.
- **Filters reload** — `filters.yaml` is re-read on every poll cycle, so changes take effect within 10 minutes without restarting.
