"""
db.py — SQLite-based deduplication for notified jobs.

Stores job IDs we've already sent to the webhook so we never
notify about the same job twice.  Prunes entries older than 30 days.
"""

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


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(_CREATE_TABLE)
    conn.execute(_CREATE_INDEX)
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
