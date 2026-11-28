"""Two-way sync with the "Job Hunt" Notion database.

Push: every matched job becomes a row; the pipeline moves it through
To apply -> Email drafted -> Emailed (and Applied via `./jobs apply`).
Pull: statuses you set by hand in Notion (Applied, Interview, Offer, Rejected,
Skipped) are read back and never overwritten; they also take the job out of the
apply queue.

Needs NOTION_TOKEN in .env (an internal integration connected to the database).
"""
import time

import requests

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"

SOURCES = {"linkedin": "LinkedIn", "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "keka": "Keka", "hn": "HN",
           "sheet": "Imported", "yc": "YC founders"}
TRACKS = {"fullstack": "Full-Stack", "fullstack_ai": "Full-Stack + AI", "flutter": "Flutter"}
PIPELINE = ("To apply", "Email drafted", "Emailed")  # statuses the script owns
DONE_BY_USER = {"Applied": "applied", "Interview": "applied", "Offer": "applied",
                "Rejected": "applied", "Skipped": "skipped"}


def chance(score):
    return "High" if score >= 85 else "Medium" if score >= 75 else "Stretch"


def pipeline_status(job):
    if job["apply_state"] == "applied":
        return "Applied"
    if job["apply_state"] == "skipped":
        return "Skipped"
    return {"drafted": "Email drafted", "emailed": "Emailed"}.get(job["status"], "To apply")


def email_markdown(job):
    """Page content for a job: why it fits, plus the cold email when there is one."""
    lines = [f"**Why it fits:** {job['reason']}", "", f"[Open the job posting]({job['url']})"]
    if job.get("body"):
        lines += ["", "## Cold email", "", f"**To:** {job['contact_email']}  ", f"**Subject:** {job['subject']}", ""]
        lines += [p if p.strip() else "" for p in job["body"].split("\n")]
    return "\n".join(lines)


class Notion:
    def __init__(self, token, database_id):
        self.db = database_id
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Notion-Version": VERSION,
                               "Content-Type": "application/json"})

    def call(self, method, path, body=None):
        for attempt in range(4):
            r = self.s.request(method, f"{API}/{path}", json=body, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if not r.ok:
                raise RuntimeError(f"Notion {method} {path}: {r.status_code} {r.text[:300]}")
            time.sleep(0.35)  # stay under ~3 requests/second
            return r.json()
        raise RuntimeError(f"Notion {method} {path}: gave up after retries")

    def rows(self):
        """Map Job ID -> (page_id, status) for every row in the database."""
        out, cursor = {}, None
        while True:
            body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            data = self.call("POST", f"databases/{self.db}/query", body)
            for page in data["results"]:
                props = page["properties"]
                job_id = "".join(t["plain_text"] for t in props["Job ID"]["rich_text"])
                status = (props["Status"].get("status") or {}).get("name")
                if job_id:
                    out[job_id] = (page["id"], status)
            if not data.get("has_more"):
                return out
            cursor = data["next_cursor"]


def _text(value):
    return {"rich_text": [{"text": {"content": (value or "")[:2000]}}]}


def _date(value):
    return {"date": {"start": value[:10]} if value else None}


def properties(job):
    return {
        "Role": {"title": [{"text": {"content": job["title"][:200]}}]},
        "Company": _text(job["company"]),
        "Status": {"status": {"name": pipeline_status(job)}},
        "Fit score": {"number": job["score"]},
        "Chance": {"select": {"name": chance(job["score"] or 0)}},
        "Resume": {"select": {"name": TRACKS.get(job["track"], "Full-Stack")}},
        "Location": _text(job["location"]),
        "Source": {"select": {"name": SOURCES[job["source"]]}},
        "Job link": {"url": job["url"]},
        "Apply link": {"url": job["apply_url"]},
        "Contact": {"email": job["contact_email"]},
        "Emailed on": _date(job["emailed_at"]),
        "Applied on": _date(job["applied_at"]),
        "Found on": _date(job["first_seen"]),
        "Why it fits": _text(job["reason"]),
        "Job ID": _text(job["id"]),
    }


def _paragraphs(markdown):
    """Very small Markdown -> Notion blocks: '## ' headings and plain paragraphs."""
    blocks = []
    for line in markdown.split("\n"):
        if line.startswith("## "):
            blocks.append({"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": line[3:]}}]}})
        elif line.strip():
            text = line.replace("**", "").rstrip()
            blocks.append({"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": text[:2000]}}]}})
    return blocks


def sync(conn, cfg, log):
    """Pull your manual status changes, then push new and changed jobs. Never fails the run."""
    token, database_id = cfg["env"].get("NOTION_TOKEN"), (cfg.get("notion") or {}).get("database_id")
    if not (token and database_id):
        log("  Notion: skipped (add NOTION_TOKEN to .env to turn on syncing)")
        return
    try:
        notion = Notion(token, database_id)
        remote = notion.rows()
        jobs = [dict(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE status IN ('matched','drafted','emailed','no_contact') OR apply_state IS NOT NULL")]
        created = updated = pulled = 0
        for job in jobs:
            page_id, remote_status = remote.get(job["id"], (None, None))

            if remote_status in DONE_BY_USER:  # you moved it by hand: respect that
                if job["apply_state"] != DONE_BY_USER[remote_status]:
                    conn.execute("UPDATE jobs SET apply_state = ? WHERE id = ?", (DONE_BY_USER[remote_status], job["id"]))
                    pulled += 1
                continue

            props = properties(job)
            if not page_id:
                page = notion.call("POST", "pages", {"parent": {"database_id": database_id}, "properties": props})
                notion.call("PATCH", f"blocks/{page['id']}/children", {"children": _paragraphs(email_markdown(job))})
                created += 1
            elif remote_status != pipeline_status(job):
                notion.call("PATCH", f"pages/{page_id}", {"properties": props})
                if job["status"] in ("drafted", "emailed") and remote_status == "To apply":
                    notion.call("PATCH", f"blocks/{page_id}/children",
                                {"children": _paragraphs(email_markdown(job).split("\n", 3)[3])})
                updated += 1
        conn.commit()
        log(f"  Notion: {created} added, {updated} updated, {pulled} status changes read back")
    except Exception as e:
        log(f"  ! Notion sync failed (the run itself is fine): {e}")
