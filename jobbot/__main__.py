"""jobbot: find jobs that fit your resume, cold-email the hiring contact, and queue applications.

  ./jobs run [--dry-run]     fetch -> score -> find contacts -> write emails -> send (capped)
  ./jobs apply [--limit N]   open matched jobs' application forms, pre-filled, for you to submit
  ./jobs status              counts, today's sends, and the top of the apply queue
  ./jobs sync                push to / pull from the Notion "Job Hunt" database
  ./jobs import FILE|URL     add HN hiring posts or YC founder leads from a spreadsheet
                             (.xlsx or a Google Sheets link), pre-filtered
  ./jobs schedule on|off     run daily via launchd (default 10:00)
"""
import argparse
import json
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import apply, contacts, db, founders, mailer, match, notion, sheet, sources, writer
from .config import ROOT, load
from .llm import ClaudeError


def log(msg):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def today():
    return datetime.now().strftime("%Y-%m-%d")


def cmd_run(cfg, dry_run):
    conn = db.connect()

    log("1/5 Fetching postings")
    new = db.insert_new(conn, sources.fetch_all(cfg, log))
    log(f"  {new} new postings since last run")

    log("2/5 Pre-filtering and scoring with Claude")
    pending = [db.as_dict(r) for r in conn.execute("SELECT * FROM jobs WHERE status = 'new'")]
    keep = []
    for job in pending:
        reason = match.prefilter(job, cfg["search"]["max_age_days"], cfg["search"]["ats_max_age_days"],
                                 cfg["search"].get("remote_only"), cfg["search"].get("founders_india_only"))
        if reason:
            db.update(conn, job["id"], status="rejected", reason=f"prefilter: {reason}")
        else:
            keep.append(job)
    keep.sort(key=match.priority)
    to_score = keep[: cfg["scoring"]["max_jobs_per_run"]]
    log(f"  {len(keep)} passed the pre-filter; scoring {len(to_score)} this run")

    size, matched = cfg["scoring"]["batch_size"], 0
    for i in range(0, len(to_score), size):
        batch = [sources.hydrate(j) for j in to_score[i:i + size]]
        for job in batch:  # hydrate may have found the description/emails
            db.update(conn, job["id"], description=job.get("description"), emails=json.dumps(job.get("emails") or []))
        try:
            results = list(match.score_batch(batch, cfg))
        except (ClaudeError, ValueError) as e:
            log(f"  ! scoring batch failed, will retry next run: {e}")
            continue
        for job, r in results:
            if not r:
                continue
            ok = bool(r.get("eligible")) and int(r.get("score", 0)) >= cfg["scoring"]["min_score"]
            matched += ok
            db.update(conn, job["id"], status="matched" if ok else "rejected", score=int(r.get("score", 0)),
                      track=r.get("track"), reason=r.get("reason"))
        log(f"  scored {min(i + size, len(to_score))}/{len(to_score)}  (matches so far: {matched})")

    log("3/5 Finding contacts and writing emails")
    email_cfg = cfg["email"]
    sent_today = conn.execute("SELECT COUNT(*) FROM jobs WHERE emailed_at LIKE ?", (today() + "%",)).fetchone()[0]
    drafted = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'drafted'").fetchone()[0]
    need = max(0, email_cfg["daily_cap"] - sent_today - drafted)
    apollo_key = cfg["env"].get("APOLLO_API_KEY") if cfg["apollo"]["enabled"] else None
    lookups = 0
    cooldown = f"-{email_cfg['company_cooldown_days']} days"

    for row in conn.execute("SELECT * FROM jobs WHERE status = 'matched' ORDER BY score DESC").fetchall():
        if need <= 0:
            break
        job = db.as_dict(row)
        busy = conn.execute(
            "SELECT 1 FROM jobs WHERE lower(company) = lower(?) AND id != ? AND (status = 'drafted' "
            "OR (status = 'emailed' AND emailed_at > datetime('now', 'localtime', ?)))",
            (job["company"], job["id"], cooldown)).fetchone()
        if busy:
            continue

        contact = contacts.from_founders(job) if job["source"] == "yc" else contacts.from_posting(job)
        if not contact and apollo_key and lookups < cfg["apollo"]["max_lookups_per_run"]:
            lookups += 1
            try:
                contact = contacts.from_apollo(job, cfg, apollo_key)
            except contacts.ApolloUnavailable as e:
                log(f"  ! Apollo unavailable, skipping it for this run: {e}")
                apollo_key = None
            except Exception as e:
                log(f"  ! Apollo lookup failed for {job['company']}: {e}")
        if not contact:
            if not apollo_key:  # no way to find one later either; leave it for `apply`
                db.update(conn, job["id"], status="no_contact")
            continue
        if conn.execute("SELECT 1 FROM jobs WHERE contact_email = ? AND status IN ('drafted','emailed')",
                        (contact["email"],)).fetchone():
            db.update(conn, job["id"], status="no_contact", reason=(job["reason"] or "") + " (contact already emailed)")
            continue

        try:
            subject, body = writer.write(job, contact, cfg)
        except (ClaudeError, ValueError) as e:
            log(f"  ! could not write email for {job['company']}: {e}")
            continue
        db.update(conn, job["id"], status="drafted", contact_email=contact["email"], contact_name=contact["name"],
                  contact_title=contact["title"], subject=subject, body=body)
        need -= 1
        log(f"  drafted: {job['company']} | {job['title'][:60]} -> {contact['email']}")

    log("4/5 Sending")
    env = cfg["env"]
    live = email_cfg["auto_send"] and not dry_run and env.get("GMAIL_ADDRESS") and env.get("GMAIL_APP_PASSWORD")
    drafts = [db.as_dict(r) for r in conn.execute("SELECT * FROM jobs WHERE status = 'drafted' ORDER BY score DESC")]
    if not live:
        why = "--dry-run" if dry_run else "auto_send is off" if not email_cfg["auto_send"] else "no Gmail app password in .env"
        outbox = write_outbox(cfg, drafts)
        log(f"  not sending ({why}); {len(drafts)} drafts written to {outbox}")
    else:
        budget = email_cfg["daily_cap"] - sent_today
        for n, job in enumerate(drafts[:budget]):
            if n:
                time.sleep(random.uniform(email_cfg["min_gap_seconds"], email_cfg["max_gap_seconds"]))
            try:
                mailer.send(cfg, job["contact_email"], job["subject"], job["body"], cfg["tracks"][job["track"]]["resume"])
            except Exception as e:
                log(f"  ! send failed to {job['contact_email']}: {e}")
                continue
            db.update(conn, job["id"], status="emailed", emailed_at=datetime.now().isoformat(timespec="seconds"))
            log(f"  sent {n + 1}/{min(budget, len(drafts))}: {job['company']} -> {job['contact_email']}")

    log("5/5 Syncing Notion")
    notion.sync(conn, cfg, log)
    cmd_status(cfg, brief=True)


def cmd_import(cfg, path):
    """Insert the sheet's posts as new jobs and pre-filter them now, so the list
    you see is what the next `./jobs run` will score."""
    conn = db.connect()
    search = cfg["search"]
    log(f"Importing {path}")
    path = sheet.local_copy(path)
    rows = sheet.read_rows(path)
    if founders.is_founder_sheet(rows):
        known = {r[0] for r in conn.execute("SELECT id FROM jobs WHERE source = 'yc'")}
        new, down = founders.fetch_leads(rows, known, log)
        if search.get("founders_india_only"):
            log("  checking which startups are India-based or have an Indian founder")
            founders.tag_india([j for j in new if j["id"] not in down], cfg, log)
        db.insert_new(conn, new)
        log(f"  {len(new)} founder leads added ({len(down)} with a dead website); companies already in jobs.db skipped")
    else:
        jobs = list(sheet.fetch_jobs(path, search["ats_max_age_days"], log))
        known = {r[0] for r in conn.execute(
            f"SELECT id FROM jobs WHERE id IN ({','.join('?' * len(jobs))})", [j["id"] for j in jobs])}
        new = [j for j in jobs if j["id"] not in known]
        db.insert_new(conn, new)
        log(f"  {len(jobs)} posts fetched; {len(known)} already in jobs.db, {len(new)} added")

    drops, kept = {}, []
    for job in new:
        reason = match.prefilter(job, search["max_age_days"], search["ats_max_age_days"], search.get("remote_only"),
                                 search.get("founders_india_only"))
        if reason:
            drops[reason] = drops.get(reason, 0) + 1
            db.update(conn, job["id"], status="rejected", reason=f"prefilter: {reason}")
        else:
            kept.append(job)
    log(f"  dropped by pre-filter: {', '.join(f'{n} {r}' for r, n in drops.items()) or 'none'}")
    log(f"  {len(kept)} left for Claude to score on the next `./jobs run`:")
    for job in sorted(kept, key=match.priority):
        mail = f"  <{job['emails'][0]}>" if job["emails"] else ""
        what = job["company"] if job["source"] == "yc" else job["title"]
        print(f"    {(job['posted_at'] or '')[:10]:<10}  {what[:90]}{mail}")


def write_outbox(cfg, drafts):
    folder = ROOT / "outbox" / today()
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.txt"):
        old.unlink()
    for n, job in enumerate(drafts, 1):
        name = re.sub(r"[^A-Za-z0-9]+", "-", job["company"]).strip("-")[:40]
        who = f" ({job['contact_name']}, {job['contact_title']})" if job["contact_name"] else ""
        resume = Path(cfg["tracks"][job["track"]]["resume"]).name
        (folder / f"{n:02d}-{name}.txt").write_text(
            f"To: {job['contact_email']}{who}\nSubject: {job['subject']}\n"
            f"Attach: {resume} (sent as {cfg['attachment_name']})\n"
            f"Job: {job['title']} | {job['location']}\nLink: {job['url']}\n"
            f"Match: {job['score']}/100, {job['reason']}\n\n{job['body']}\n")
    return folder


def cmd_status(cfg, brief=False):
    conn = db.connect()
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
    applied = conn.execute("SELECT COUNT(*) FROM jobs WHERE apply_state = 'applied'").fetchone()[0]
    sent_today = conn.execute("SELECT COUNT(*) FROM jobs WHERE emailed_at LIKE ?", (today() + "%",)).fetchone()[0]
    queue = conn.execute(
        "SELECT company, title, score, track, source FROM jobs WHERE status IN ('matched','drafted','emailed','no_contact') "
        "AND apply_state IS NULL AND apply_url IS NOT NULL ORDER BY score DESC LIMIT 15").fetchall()
    print(f"\n  Total postings seen: {sum(counts.values())}")
    for status in ("new", "rejected", "matched", "drafted", "emailed", "no_contact"):
        print(f"    {status:<11} {counts.get(status, 0)}")
    print(f"  Emails sent today: {sent_today}/{cfg['email']['daily_cap']}   Applications marked done: {applied}")
    if queue:
        print(f"\n  Top of apply queue (run ./jobs apply):")
        for r in queue[: 5 if brief else 15]:
            print(f"    {r['score']:>3}  {r['company'][:22]:<22} {r['title'][:55]:<55} [{r['track']}, {r['source']}]")
    print()


def cmd_schedule(state, at):
    plist = Path.home() / "Library/LaunchAgents/com.jobbot.daily.plist"
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    if state == "off":
        plist.unlink(missing_ok=True)
        print("Daily run turned off.")
        return
    hour, minute = (int(x) for x in at.split(":"))
    (ROOT / "logs").mkdir(exist_ok=True)
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jobbot.daily</string>
  <key>ProgramArguments</key><array>
    <string>/bin/zsh</string><string>-lc</string><string>'{ROOT}/jobs' run</string>
  </array>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer>
  </dict>
  <key>StandardOutPath</key><string>{ROOT}/logs/run.log</string>
  <key>StandardErrorPath</key><string>{ROOT}/logs/run.log</string>
</dict></plist>
""")
    subprocess.run(["launchctl", "load", str(plist)], check=True)
    print(f"Daily run scheduled at {at}. Log: {ROOT}/logs/run.log  (missed runs happen when the Mac wakes)")


def main():
    parser = argparse.ArgumentParser(prog="jobbot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="fetch, score, write and send")
    run.add_argument("--dry-run", action="store_true", help="write drafts to outbox/ instead of sending")
    ap = sub.add_parser("apply", help="open pre-filled application forms")
    ap.add_argument("--limit", type=int, default=15)
    sub.add_parser("status", help="show counts and the apply queue")
    sub.add_parser("sync", help="sync with the Notion Job Hunt database")
    im = sub.add_parser("import", help="add HN hiring posts or YC founder leads from a spreadsheet")
    im.add_argument("file", help=".xlsx path or Google Sheets link")
    sc = sub.add_parser("schedule", help="turn the daily run on or off")
    sc.add_argument("state", choices=["on", "off"])
    sc.add_argument("--at", default="10:00", help="HH:MM, 24h")
    args = parser.parse_args()

    cfg = load()
    if args.cmd == "run":
        cmd_run(cfg, args.dry_run)
    elif args.cmd == "apply":
        apply.run(cfg, args.limit)
    elif args.cmd == "status":
        cmd_status(cfg)
    elif args.cmd == "sync":
        notion.sync(db.connect(), cfg, log)
    elif args.cmd == "import":
        cmd_import(cfg, args.file)
    elif args.cmd == "schedule":
        cmd_schedule(args.state, args.at)


if __name__ == "__main__":
    sys.exit(main())
