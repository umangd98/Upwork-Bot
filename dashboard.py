"""
dashboard.py — Minimal Flask dashboard for Upwork profile metrics.

Routes
------
GET  /          -> redirect to /metrics
GET  /metrics   -> HTML dashboard: latest snapshot + last refresh date
POST /metrics/refresh -> immediate fetch, redirect back

Optional Basic Auth: set DASHBOARD_PASSWORD env var (username: admin).
Leave unset for open access (fine for private App Platform deployments).
"""

import logging
from datetime import datetime
from functools import wraps

from flask import Flask, Response, redirect, render_template_string, request

import config
import profile_metrics

logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Optional Basic Auth
# ---------------------------------------------------------------------------

def _requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        password = config.DASHBOARD_PASSWORD
        if not password:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or auth.username != "admin" or auth.password != password:
            return Response(
                "Unauthorized",
                401,
                {"WWW-Authenticate": 'Basic realm="Upwork Metrics"'},
            )
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------

@app.template_filter("money")
def money_filter(val):
    if not val:
        return "\u2014"
    try:
        return f"${float(val['rawValue']):,.2f}"
    except (TypeError, KeyError, ValueError):
        return "\u2014"


@app.template_filter("delta")
def delta_filter(val):
    if val is None:
        return "\u2014"
    try:
        return f"{float(val):+.4f}"
    except (TypeError, ValueError):
        return "\u2014"


@app.template_filter("friendly_date")
def friendly_date_filter(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y at %H:%M UTC")
    except Exception:
        return iso or "\u2014"


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Upwork Metrics</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#f5f7fa;color:#1a1a2e;min-height:100vh;padding:2rem 1rem}
    .wrap{max-width:860px;margin:0 auto}
    header{display:flex;justify-content:space-between;align-items:flex-start;
           gap:1rem;flex-wrap:wrap;margin-bottom:1.75rem}
    h1{font-size:1.45rem;font-weight:700;color:#14a800}
    .sub{font-size:.8rem;color:#888;margin-top:.3rem}
    .sub strong{color:#444}
    .btn{background:#14a800;color:#fff;border:none;padding:.55rem 1.1rem;
         border-radius:7px;cursor:pointer;font-size:.85rem;font-weight:600;
         white-space:nowrap;transition:background .15s}
    .btn:hover{background:#108a00}
    .card{background:#fff;border-radius:10px;
          box-shadow:0 1px 4px rgba(0,0,0,.08);padding:1.4rem;margin-bottom:1.2rem}
    .card-title{font-size:.65rem;text-transform:uppercase;letter-spacing:.08em;
                color:#aaa;margin-bottom:1rem;font-weight:700}
    .sgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(175px,1fr));gap:1.2rem}
    .sl{font-size:.72rem;color:#999;margin-bottom:.2rem}
    .sv{font-size:1.1rem;font-weight:700;color:#14a800}
    table{width:100%}
    tr+tr td{border-top:1px solid #f2f2f2}
    td{padding:.55rem 0;font-size:.875rem}
    td:first-child{color:#555}
    td:last-child{text-align:right;font-weight:600;color:#1a1a2e}
    .alert{padding:.7rem 1rem;border-radius:7px;margin-bottom:1rem;font-size:.85rem}
    .ok{background:#e8f5e9;color:#1b5e20}
    .err{background:#ffebee;color:#b71c1c}
    .empty{text-align:center;padding:4rem;color:#bbb;font-size:.9rem}
  </style>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <h1>Upwork Profile Metrics</h1>
      <p class="sub">
        Last refreshed:&nbsp;
        {% if snapshot %}
          <strong>{{ data.last_updated | friendly_date }}</strong>
        {% else %}
          <strong>Never</strong>
        {% endif %}
      </p>
    </div>
    <form method="POST" action="/metrics/refresh">
      <button class="btn" type="submit"
        onclick="this.disabled=true;this.textContent='Refreshing\u2026'">
        &#8635;&nbsp;Refresh Now
      </button>
    </form>
  </header>

  {% if message %}
    <div class="alert {{ 'err' if error else 'ok' }}">{{ message }}</div>
  {% endif %}

  {% if snapshot %}

    <div class="card">
      <div class="card-title">Earnings</div>
      <div class="sgrid">
        <div><div class="sl">Last 360 days</div>
             <div class="sv">{{ snapshot.totalCharge360 | money }}</div></div>
        <div><div class="sl">Last 365 days (no pending)</div>
             <div class="sv">{{ snapshot.totalCharge365NoPending | money }}</div></div>
        <div><div class="sl">Last 90 days</div>
             <div class="sv">{{ snapshot.totalCharge90 | money }}</div></div>
        <div><div class="sl">Last 360 days (no agency)</div>
             <div class="sv">{{ snapshot.totalCharge360NoAgency | money }}</div></div>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Activity</div>
      <table>
        <tr><td>Adjusted Score (360d)</td>
            <td>{% if snapshot.adjustedScore360 is not none %}{{ "%.2f"|format(snapshot.adjustedScore360) }} / 5{% else %}\u2014{% endif %}</td></tr>
        <tr><td>Long-term Clients</td><td>{{ snapshot.longTermClients }}</td></tr>
        <tr><td>Weeks Eligible (last 16 wks)</td><td>{{ snapshot.weeksEligibleWithin16wks | int }} / 16</td></tr>
        <tr><td>Top Category (90d)</td><td>{{ snapshot.topLevelJobCategoryApplied90Days or "\u2014" }}</td></tr>
      </table>
    </div>

    <div class="card">
      <div class="card-title">Proposals &middot; last 90 days</div>
      <table>
        <tr><td>Proposals Sent</td><td>{{ snapshot.proposalsCount90Days }}</td></tr>
        <tr><td>Median Proposals for Top Category (365d)</td><td>{{ snapshot.medianProposalsForTheTopLevelCategory365 }}</td></tr>
        <tr><td>Fit Proposals View Ratio</td><td>{{ snapshot.fitProposalsViewRatio90Days | delta }}</td></tr>
        <tr><td>Hidden Proposals Viewed Ratio</td><td>{{ snapshot.hiddenProposalsViewedRatio90Days | delta }}</td></tr>
        <tr><td>Total Proposals Viewed Ratio</td><td>{{ snapshot.totalProposalsViewedRatio90Days | delta }}</td></tr>
        <tr><td>Proposals Interviewed Ratio</td><td>{{ snapshot.proposalInterviewedRation90Days | delta }}</td></tr>
        <tr><td>Proposals Hired Ratio</td><td>{{ snapshot.proposalsHiredRatio90Days | delta }}</td></tr>
      </table>
    </div>

    <div class="card">
      <div class="card-title">Invites &middot; last 90 days</div>
      <table>
        <tr><td>Total Invites</td><td>{{ snapshot.totalInvites90Days }}</td></tr>
        <tr><td>Invite Responses</td><td>{{ snapshot.totalInviteResponses90Days }}</td></tr>
        <tr><td>Invite Responses per Day</td><td>{{ snapshot.inviteResponsesPerDay90Days }}</td></tr>
      </table>
    </div>

    <div class="card">
      <div class="card-title">Suspensions</div>
      <table>
        <tr><td>All-time</td><td>{{ snapshot.suspensions }}</td></tr>
        <tr><td>Last 360 days</td><td>{{ snapshot.suspensions360 }}</td></tr>
        <tr><td>Last 90 days (limited)</td><td>{{ snapshot.suspensions90limited }}</td></tr>
      </table>
    </div>

    {% if data.snapshots | length > 1 %}
    <div class="card">
      <div class="card-title">Snapshot History ({{ data.snapshots | length }} days)</div>
      <table>
        <tr>
          <td style="color:#aaa;font-size:.7rem;font-weight:700">DATE</td>
          <td style="color:#aaa;font-size:.7rem;font-weight:700;text-align:right">EARNINGS 360d</td>
        </tr>
        {% for s in data.snapshots %}
        <tr><td>{{ s.date }}</td><td>{{ s.totalCharge360 | money }}</td></tr>
        {% endfor %}
      </table>
    </div>
    {% endif %}

  {% else %}
    <div class="card empty">
      No data yet &mdash; click <strong>Refresh Now</strong> to fetch your first snapshot.
    </div>
  {% endif %}

</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect("/metrics")


@app.route("/metrics")
@_requires_auth
def metrics_view():
    data = profile_metrics._load_metrics()
    snapshots = data.get("snapshots", [])
    snapshot = snapshots[0] if snapshots else None
    return render_template_string(
        TEMPLATE,
        data=data,
        snapshot=snapshot,
        message=request.args.get("message"),
        error=request.args.get("error"),
    )


@app.route("/metrics/refresh", methods=["POST"])
@_requires_auth
def metrics_refresh():
    try:
        snapshot = profile_metrics.fetch_profile_metrics()
        profile_metrics.save_metrics(snapshot)
        logger.info("Manual refresh: metrics saved for %s.", snapshot["date"])
        return redirect("/metrics?message=Metrics+refreshed+successfully.")
    except Exception as exc:
        logger.exception("Manual metrics refresh failed.")
        msg = str(exc)[:120].replace(" ", "+")
        return redirect(f"/metrics?message=Refresh+failed:+{msg}&error=1")


# ---------------------------------------------------------------------------
# Runner (called from entrypoint.py after scheduler starts)
# ---------------------------------------------------------------------------

def run_dashboard() -> None:
    port = config.AUTH_SERVER_PORT
    logger.info("Dashboard running at http://0.0.0.0:%d/metrics", port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)