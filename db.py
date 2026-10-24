"""
db.py — SQLite-based deduplication for notified jobs.

Stores job IDs we've already sent to the webhook so we never
notify about the same job twice.  Prunes entries older than 30 days.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS notified_jobs (
    job_id     TEXT PRIMARY KEY,
    notified_at TEXT NOT NULL
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_notified_at
ON notified_jobs (notified_at);
"""


_CREATE_APPLICATIONS = """
CREATE TABLE IF NOT EXISTS applications (
    job_id      TEXT PRIMARY KEY,
    status      TEXT NOT NULL,      -- pending | applying | applied | skipped | failed
    mode        TEXT NOT NULL,      -- auto | approval
    title       TEXT,
    job_json    TEXT,
    letter      TEXT,
    bid         REAL,
    slack_ts    TEXT,
    slack_channel TEXT,
    proposal_id TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    applied_at  TEXT
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(_CREATE_TABLE)
    conn.execute(_CREATE_INDEX)
    conn.execute(_CREATE_APPLICATIONS)
    conn.commit()
    return conn


def is_already_notified(job_id: str) -> bool:
    """Return True if this job_id has already been sent to the webhook."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM notified_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_notified(job_ids: list[str]) -> None:
    """Insert one or more job IDs as notified (ignores duplicates)."""
    if not job_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO notified_jobs (job_id, notified_at) VALUES (?, ?)",
            [(jid, now) for jid in job_ids],
        )
        conn.commit()
        logger.debug("Marked %d job(s) as notified.", len(job_ids))
    finally:
        conn.close()


def filter_unseen(job_ids: list[str]) -> set[str]:
    """Return the subset of job_ids that have NOT been notified yet."""
    if not job_ids:
        return set()
    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in job_ids)
        rows = conn.execute(
            f"SELECT job_id FROM notified_jobs WHERE job_id IN ({placeholders})",
            job_ids,
        ).fetchall()
        seen = {r[0] for r in rows}
        return set(job_ids) - seen
    finally:
        conn.close()


def prune_old_entries(days: int = 30) -> int:
    """Delete entries older than *days* days.  Returns count deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = _connect()
    try:
        cursor = conn.execute(
            "DELETE FROM notified_jobs WHERE notified_at < ?", (cutoff,)
        )
        conn.commit()
        deleted = cursor.rowcount
        if deleted:
            logger.info("Pruned %d old job entries (>%d days).", deleted, days)
        return deleted
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Applications (auto-apply + Slack approval queue)
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_application(job_id: str, mode: str, title: str, job: dict,
                    letter: str, bid: float, status: str = "pending") -> bool:
    """Insert a new application row. Returns False if the job already has one."""
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO applications "
            "(job_id, status, mode, title, job_json, letter, bid, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job_id, status, mode, title, json.dumps(job), letter, bid, _now()),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def get_application(job_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM applications WHERE job_id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


_UPDATABLE = {"status", "letter", "bid", "slack_ts", "slack_channel",
              "proposal_id", "error", "applied_at", "mode"}


def update_application(job_id: str, **fields) -> None:
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"Cannot update fields: {bad}")
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE applications SET {sets} WHERE job_id = ?",
            [*fields.values(), job_id],
        )
        conn.commit()
    finally:
        conn.close()


def claim_for_apply(job_id: str) -> bool:
    """Atomically move pending -> applying so a double-click can't apply twice."""
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE applications SET status = 'applying' "
            "WHERE job_id = ? AND status IN ('pending', 'failed')",
            (job_id,),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def queue_for_mcp(job_id: str) -> bool:
    """pending/failed -> queued (waiting for the Claude session to submit)."""
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE applications SET status = 'queued', error = NULL, applied_at = ? "
            "WHERE job_id = ? AND status IN ('pending', 'failed')",
            (_now(), job_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def claim_queued(job_id: str) -> bool:
    """queued -> submitting, so a re-fired loop can't submit the same job twice."""
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE applications SET status = 'submitting' WHERE job_id = ? AND status = 'queued'",
            (job_id,),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def list_by_status(status: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM applications WHERE status = ? ORDER BY created_at", (status,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_applications(status: str | None = None, limit: int = 50) -> list[dict]:
    conn = _connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT job_id, status, mode, title, bid, error, proposal_id, created_at FROM applications "
                "WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT job_id, status, mode, title, bid, error, proposal_id, created_at FROM applications "
                "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def counts_by_status() -> dict:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM applications GROUP BY status"
        ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}
    finally:
        conn.close()


def applications_in_window(hours: int = 24) -> int:
    """Proposals submitted (or being submitted) in the rolling window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM applications "
            "WHERE status IN ('applied', 'applying', 'queued', 'submitting') "
            "AND COALESCE(applied_at, created_at) >= ?",
            (cutoff,),
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()
