"""
api_routes.py — Endpoints n8n calls when you act in Slack.

All routes need  Authorization: Bearer <BOT_API_SECRET>  and fail closed (403) if
the secret isn't configured.

POST /api/apply/<job_id>[?notify=0]  submit the stored proposal now (button: Apply)
POST /api/letter/<job_id>           body {"letter": "..."} save an edited letter
POST /api/skip/<job_id>[?notify=0]   mark skipped (button: Skip)
GET  /api/application/<job_id>      current state in the same shape as an n8n event
GET  /api/status                    proposals used in the rolling 24h window + queue counts
GET  /api/applications[?status=]    list applications (skipped ones show the reason)
GET  /api/debug/schema?type=X       read-only introspection of an Upwork GraphQL type
GET  /api/debug/proposals?job_ids=  run the already-applied lookup once, show Upwork's full answer
GET  /api/queue                     (APPLY_BACKEND=mcp) jobs the Claude session must submit
POST /api/queue/<job_id>/claim      session claims a job right before submitting
POST /api/result/<job_id>           session reports {ok, proposal_id | error}
"""

import hmac
import json
import logging
from functools import wraps

from flask import Blueprint, jsonify, request

import applied_jobs
import config
import db
import events
import heartbeat
import pipeline

logger = logging.getLogger(__name__)

api = Blueprint("api", __name__, url_prefix="/api")

MAX_LETTER = 5000  # Upwork's cover-letter limit


def _authorized() -> bool:
    secret = config.BOT_API_SECRET
    if not secret:
        return False
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    return hmac.compare_digest(token, secret)


def _requires_secret(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _authorized():
            return jsonify({"ok": False, "message": "Forbidden"}), 403
        return f(*args, **kwargs)
    return wrapper


def _filters() -> dict:
    return config.load_filters()


def _notify() -> bool:
    """n8n passes ?notify=0 on button clicks: it updates the Slack message itself."""
    return request.args.get("notify", "1") != "0"


@api.get("/status")
@_requires_secret
def status():
    cap = int((_filters().get("apply") or {}).get("daily_cap", 10))
    return jsonify({"applied_in_last_24h": db.applications_in_window(24), "daily_cap": cap,
                    "backend": config.APPLY_BACKEND, "by_status": db.counts_by_status(),
                    "heartbeat": heartbeat.snapshot()})


@api.get("/applications")
@_requires_secret
def applications():
    """List stored applications, newest first: ?status=pending|skipped|applied|failed|queued&limit=50.
    Skipped rows include the reason in `error`."""
    limit = max(1, min(int(request.args.get("limit", 50) or 50), 200))
    return jsonify({"ok": True, "items": db.list_applications(request.args.get("status"), limit)})


@api.get("/debug/schema")
@_requires_secret
def debug_schema():
    """Read-only introspection of one Upwork GraphQL type: /api/debug/schema?type=SortOrder"""
    import re
    name = request.args.get("type", "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,80}", name):
        return jsonify({"ok": False, "message": "Pass ?type=<GraphQL type name>."}), 400
    try:
        return jsonify({"ok": True, "result": applied_jobs.type_info(name)})
    except Exception as exc:
        return jsonify({"ok": False, "message": f"Upwork request failed: {exc}"}), 502


@api.get("/debug/proposals")
@_requires_secret
def debug_proposals():
    """Run the already-applied lookup once and show Upwork's full answer: ?job_ids=1,2&first=25"""
    ids = [i for i in request.args.get("job_ids", "").split(",") if i.strip()][:25]
    try:
        first = max(1, min(int(request.args.get("first", 25)), 200))
    except ValueError:
        return jsonify({"ok": False, "message": "first must be a number."}), 400
    try:
        return jsonify({"ok": True, **applied_jobs.diagnose(ids, first)})
    except Exception as exc:
        return jsonify({"ok": False, "message": f"Upwork request failed: {exc}"}), 502


@api.get("/queue")
@_requires_secret
def queue():
    """Jobs waiting for the Claude session to submit them through the Upwork MCP."""
    return jsonify({"ok": True, "items": pipeline.mcp_queue(_filters())})


@api.post("/queue/<job_id>/claim")
@_requires_secret
def claim(job_id):
    """The session calls this right before submitting; only one caller can win."""
    if not db.claim_queued(job_id):
        return jsonify({"ok": False, "message": "Not queued (already claimed or changed)."}), 409
    return jsonify({"ok": True, "message": "Claimed."})


@api.post("/result/<job_id>")
@_requires_secret
def result(job_id):
    """Body: {"ok": true, "proposal_id": "..."} or
    {"ok": false, "error": "...", "screening_questions": ["..."]}."""
    body = request.get_json(silent=True) or {}
    res = pipeline.record_result(
        job_id, bool(body.get("ok")), _filters(), proposal_id=body.get("proposal_id"),
        error=body.get("error"), questions=body.get("screening_questions"))
    return jsonify(res), (200 if res["ok"] else 409)


@api.get("/application/<job_id>")
@_requires_secret
def get_application(job_id):
    payload = pipeline.current_payload(job_id, _filters())
    if not payload:
        return jsonify({"ok": False, "message": "Unknown job."}), 404
    return jsonify({"ok": True, **payload})


@api.post("/letter/<job_id>")
@_requires_secret
def save_letter(job_id):
    app = db.get_application(job_id)
    if not app:
        return jsonify({"ok": False, "message": "Unknown job."}), 404
    if app["status"] not in ("pending", "failed"):
        return jsonify({"ok": False, "message": f"Can't edit a {app['status']} proposal."}), 409
    letter = ((request.get_json(silent=True) or {}).get("letter") or "").strip()
    if not letter:
        return jsonify({"ok": False, "message": "Letter is empty."}), 400
    if len(letter) > MAX_LETTER:
        return jsonify({"ok": False,
                        "message": f"Letter is {len(letter)} chars; Upwork's limit is {MAX_LETTER}."}), 400
    db.update_application(job_id, letter=letter)
    return jsonify({"ok": True, "message": "Cover letter updated.", "cover_letter": letter})


@api.post("/apply/<job_id>")
@_requires_secret
def apply_now(job_id):
    result = pipeline.apply_application(job_id, _filters(), notify=_notify())
    code = 200 if result["ok"] else 409
    return jsonify(result), code


@api.post("/skip/<job_id>")
@_requires_secret
def skip(job_id):
    app = db.get_application(job_id)
    if not app:
        return jsonify({"ok": False, "message": "Unknown job."}), 404
    if app["status"] in ("applied", "applying"):
        return jsonify({"ok": False, "message": f"Already {app['status']}."}), 409
    db.update_application(job_id, status="skipped")
    if _notify():
        fresh = db.get_application(job_id)
        job = json.loads(fresh["job_json"])
        cap = int((_filters().get("apply") or {}).get("daily_cap", 10))
        events.post(events.build_payload("skipped", fresh, job,
                                         window_count=db.applications_in_window(24), cap=cap))
    return jsonify({"ok": True, "message": "Skipped."})
