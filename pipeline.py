"""
pipeline.py — Turns matched jobs into applications.

For each new matched job:
  1. draft a cover letter (and let the scope gate drop jobs that don't fit),
  2. decide the mode: fixed budget under `apply.auto_below_budget` (and every hourly
     job) is "auto"; anything else is "approval",
  3. auto jobs are submitted right away (if AUTO_APPLY_ENABLED and the 24h cap has
     room), approval jobs wait for you; either way n8n posts the job description
     and cover letter to Slack.

Everything that submits goes through apply_application(), so the Slack Apply
button and auto-apply share the same cap check and double-submit protection.
"""

import json
import logging
import re

import applied_jobs
import applier
import config
import cover_letter
import db
import events
import scoring

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def _amount(obj) -> float | None:
    try:
        return float((obj or {}).get("rawValue"))
    except (TypeError, ValueError):
        return None


def choose_mode(job: dict, cfg: dict) -> str:
    """'auto' for fixed jobs under the threshold and hourly jobs, else 'approval'."""
    fixed = scoring.fixed_budget(job)
    if fixed is not None:
        return "auto" if fixed < float(cfg.get("auto_below_budget", 3000)) else "approval"
    if _amount(job.get("hourlyBudgetMin")) is not None or _amount(job.get("hourlyBudgetMax")) is not None:
        return "auto"
    return "approval"  # no budget information: ask first


def choose_bid(job: dict, cfg: dict) -> float:
    fixed = scoring.fixed_budget(job)
    if fixed is not None:
        return fixed
    bid = float(cfg.get("hourly_bid", 40))
    top = _amount(job.get("hourlyBudgetMax"))
    return min(bid, top) if top else bid


# ---------------------------------------------------------------------------
# Duplicate guards
# ---------------------------------------------------------------------------

def repost_key(job: dict) -> tuple:
    """Same title + client country + client lifetime spend + review count = same client
    reposting the same job under a new id (Upwork's own duplicate check won't catch it)."""
    client = job.get("client") or {}
    title = re.sub(r"[^a-z0-9]+", " ", (job.get("title") or "").lower()).strip()
    spent = (client.get("totalSpent") or {}).get("rawValue")
    return (title, (client.get("location") or {}).get("country"), str(spent), client.get("totalReviews"))


def find_repost(job: dict) -> str | None:
    """Job id of a recent, non-skipped application that looks like the same posting."""
    key = repost_key(job)
    if not key[0]:
        return None
    for row in db.recent_applications(14):
        if row["job_id"] == job.get("id"):
            continue
        try:
            other = json.loads(row["job_json"] or "{}")
        except ValueError:
            continue
        if repost_key(other) == key:
            return row["job_id"]
    return None


def _mark_skipped(job: dict, mode: str, bid: float, reason: str) -> None:
    db.add_application(job["id"], mode, job.get("title", ""), job, "", bid, status="skipped")
    db.update_application(job["id"], error=reason)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def _emit(event: str, app: dict, filters: dict, **extra) -> None:
    job = json.loads(app["job_json"]) if app.get("job_json") else {}
    cap = int((filters.get("apply") or {}).get("daily_cap", 10))
    payload = events.build_payload(
        event, app, job,
        window_count=db.applications_in_window(24), cap=cap, extra=extra or None,
    )
    events.post(payload)


def event_for(app: dict) -> str:
    """The event name that describes an application's current state."""
    status = app["status"]
    if status in ("queued", "submitting"):
        return "queued"
    if status == "applied":
        return "applied"
    if status == "skipped":
        return "skipped"
    if status == "failed":
        return "apply_failed"
    return "needs_approval" if app["mode"] == "approval" else "dry_run"


def current_payload(job_id: str, filters: dict) -> dict | None:
    """Same shape as an n8n event, built from the stored state (used to refresh Slack)."""
    app = db.get_application(job_id)
    if not app:
        return None
    job = json.loads(app["job_json"]) if app.get("job_json") else {}
    cap = int((filters.get("apply") or {}).get("daily_cap", 10))
    return events.build_payload(event_for(app), app, job,
                                window_count=db.applications_in_window(24), cap=cap)


# ---------------------------------------------------------------------------
# Applying (shared by auto-apply and the Slack button)
# ---------------------------------------------------------------------------

def apply_application(job_id: str, filters: dict, notify: bool = True) -> dict:
    """
    Submit a stored application. Returns {"ok": bool, "status": str, "message": str}.
    Safe to call twice: the second call sees status != pending and does nothing.
    """
    app = db.get_application(job_id)
    if not app:
        return {"ok": False, "status": "missing", "message": "Unknown job."}
    if app["status"] == "applied":
        return {"ok": True, "status": "applied", "message": "Already applied."}
    if app["status"] == "applying":
        return {"ok": False, "status": "applying", "message": "Already being submitted."}

    if app["status"] in ("queued", "submitting"):
        return {"ok": True, "status": app["status"], "message": "Already queued for submission."}

    cap = int((filters.get("apply") or {}).get("daily_cap", 10))
    if db.applications_in_window(24) >= cap:
        return {"ok": False, "status": app["status"],
                "message": f"Daily cap reached ({cap} proposals in 24h). Try again later."}

    if config.APPLY_BACKEND == "mcp":
        # The Claude Code session submits queued jobs through the Upwork MCP.
        if not db.queue_for_mcp(job_id):
            return {"ok": False, "status": app["status"], "message": "Could not queue this job."}
        if notify:
            _emit("queued", db.get_application(job_id), filters)
        return {"ok": True, "status": "queued",
                "message": "Queued: the Claude session will submit it shortly."}

    if not db.claim_for_apply(job_id):
        return {"ok": False, "status": app["status"], "message": "Could not claim this job."}

    app = db.get_application(job_id)
    job = json.loads(app["job_json"])
    try:
        proposal_id = applier.apply(app, job, filters)
    except applier.AlreadyApplied as exc:
        logger.info("Already applied to %s: %s", job_id, exc)
        db.update_application(job_id, status="skipped", error=f"Already applied on Upwork ({exc})")
        if notify:
            _emit("skipped", db.get_application(job_id), filters)
        return {"ok": False, "status": "skipped", "message": "Already applied on Upwork; nothing was sent."}
    except applier.ApplyError as exc:
        logger.error("Apply failed for %s: %s", job_id, exc)
        db.update_application(job_id, status="failed", error=str(exc))
        if notify:
            _emit("apply_failed", db.get_application(job_id), filters)
        return {"ok": False, "status": "failed", "message": str(exc)}

    db.update_application(job_id, status="applied", proposal_id=proposal_id,
                          applied_at=db._now(), error=None)
    if notify:
        _emit("applied", db.get_application(job_id), filters)
    return {"ok": True, "status": "applied", "message": f"Applied (proposal {proposal_id})."}


# ---------------------------------------------------------------------------
# MCP fallback: queue for the Claude session, then record its result
# ---------------------------------------------------------------------------

def mcp_queue(filters: dict) -> list[dict]:
    """Queued jobs with the exact MCP params, oldest first."""
    items = []
    for app in db.list_by_status("queued"):
        job = json.loads(app["job_json"]) if app.get("job_json") else {}
        items.append({
            "job_id": app["job_id"], "title": app["title"], "mode": app["mode"],
            "url": job.get("job_url", ""),
            "params": applier.build_mcp_params(app, job, filters),
        })
    return items


def record_result(job_id: str, ok: bool, filters: dict, proposal_id: str | None = None,
                  error: str | None = None, questions: list | None = None) -> dict:
    """Store what the Claude session reported and tell Slack."""
    app = db.get_application(job_id)
    if not app:
        return {"ok": False, "message": "Unknown job."}
    if app["status"] not in ("queued", "submitting"):
        return {"ok": False, "message": f"Job is {app['status']}, not waiting for a result."}
    if ok:
        if not proposal_id:
            return {"ok": False, "message": "proposal_id is required when ok is true."}
        db.update_application(job_id, status="applied", proposal_id=str(proposal_id),
                              applied_at=db._now(), error=None)
        _emit("applied", db.get_application(job_id), filters)
        return {"ok": True, "message": "Recorded as applied."}
    if applier.is_duplicate_error(error or ""):
        db.update_application(job_id, status="skipped", error=f"Already applied on Upwork ({(error or '')[:200]})")
        _emit("skipped", db.get_application(job_id), filters)
        return {"ok": True, "message": "Recorded as already applied."}
    db.update_application(job_id, status="failed", error=(error or "Submission failed.")[:1000])
    extra = {"screening_questions": questions} if questions else {}
    _emit("apply_failed", db.get_application(job_id), filters, **extra)
    return {"ok": True, "message": "Recorded as failed."}


# ---------------------------------------------------------------------------
# Per-job processing
# ---------------------------------------------------------------------------

def process_job(job: dict, filters: dict) -> bool:
    """
    Draft, store, and route one job. Returns True if the job is handled (don't
    retry it next cycle), False on a transient failure (retry next cycle).
    """
    job_id = job.get("id")
    if not job_id:
        return True
    if db.get_application(job_id):
        return True

    cfg = filters.get("apply") or {}

    if applied_jobs.is_applied(job_id):
        logger.info("Skipping '%s': already applied on Upwork.", job.get("title"))
        _mark_skipped(job, choose_mode(job, cfg), choose_bid(job, cfg), "Already applied on Upwork.")
        return True
    original = find_repost(job)
    if original:
        logger.info("Skipping '%s': repost of %s.", job.get("title"), original)
        _mark_skipped(job, choose_mode(job, cfg), choose_bid(job, cfg), f"Repost of job {original}.")
        return True

    similar = applied_jobs.similar_applied(job.get("title"))

    try:
        draft = cover_letter.draft(job, filters)
    except Exception:
        logger.exception("Cover letter drafting failed for %s — will retry.", job_id)
        return False

    mode = choose_mode(job, cfg)
    if similar:
        mode = "approval"   # a title match may be a repost of something we applied to: ask first
    bid = choose_bid(job, cfg)

    if not draft["apply"]:
        logger.info("Skipping '%s': %s", job.get("title"), draft.get("skip_reason"))
        db.add_application(job_id, mode, job.get("title", ""), job, draft["cover_letter"],
                           bid, status="skipped")
        db.update_application(job_id, error=draft.get("skip_reason") or "model said skip")
        return True

    db.add_application(job_id, mode, job.get("title", ""), job, draft["cover_letter"], bid)
    if similar:
        db.update_application(job_id, error=(
            f"Possible repost: you already applied to a job titled '{similar['title']}' "
            f"(id {similar['job_id']}). Check before applying."))
    app = db.get_application(job_id)
    cap = int(cfg.get("daily_cap", 10))
    extra = {"screening_questions": draft.get("screening_questions_in_post") or []}

    if mode == "approval":
        _emit("needs_approval", app, filters, **extra)
    elif db.applications_in_window(24) >= cap:
        _emit("cap_reached", app, filters, **extra)
    elif not config.AUTO_APPLY_ENABLED:
        _emit("dry_run", app, filters, **extra)
    else:
        apply_application(job_id, filters)
    return True


def process_jobs(jobs: list[dict], filters: dict) -> set[str]:
    """Process jobs in order; returns the ids that were handled."""
    handled: set[str] = set()
    limit = int((filters.get("apply") or {}).get("max_per_cycle", 20))
    if len(jobs) > limit:
        logger.info("Processing %d of %d new jobs this cycle; the rest wait.", limit, len(jobs))
    for job in jobs[:limit]:
        try:
            if process_job(job, filters):
                handled.add(job.get("id"))
        except Exception:
            logger.exception("Unexpected error processing %s", job.get("id"))
    return handled
