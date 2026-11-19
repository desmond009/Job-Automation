"""SQLite store. One row per job posting; `status` tracks the email pipeline and
`apply_state` tracks the manual application.

status:      new -> rejected | matched -> drafted -> emailed
                                matched -> no_contact
apply_state: NULL (in queue) | applied | skipped
"""
import json
import sqlite3

from .config import ROOT

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT, company TEXT, domain TEXT, title TEXT, location TEXT,
    url TEXT, apply_url TEXT, description TEXT, emails TEXT DEFAULT '[]',
    posted_at TEXT, first_seen TEXT DEFAULT (datetime('now', 'localtime')),
    status TEXT DEFAULT 'new',
    score INTEGER, track TEXT, reason TEXT,
    contact_email TEXT, contact_name TEXT, contact_title TEXT,
    subject TEXT, body TEXT,
    emailed_at TEXT, apply_state TEXT, applied_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
"""

COLUMNS = ("id", "source", "company", "domain", "title", "location", "url",
           "apply_url", "description", "emails", "posted_at")


def connect():
    conn = sqlite3.connect(ROOT / "jobs.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def insert_new(conn, jobs):
    """Insert jobs not seen before. Returns how many were new."""
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO jobs ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
        [tuple(json.dumps(j.get(c) or []) if c == "emails" else j.get(c) for c in COLUMNS) for j in jobs],
    )
    conn.commit()
    return conn.total_changes - before


def update(conn, job_id, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))
    conn.commit()


def as_dict(row):
    job = dict(row)
    job["emails"] = json.loads(job.get("emails") or "[]")
    return job
