"""Apply assist: opens each matched job's application form in a real browser,
fills the standard fields and attaches the right resume. You answer any custom
questions and press Submit yourself; nothing is submitted automatically.
LinkedIn jobs are only opened, never filled, so your account stays safe."""
import re

from . import db, notion
from .config import ROOT

FIELDS = [
    (r"^first\s*name", "first_name"),
    (r"^(last\s*name|surname|family\s*name)", "last_name"),
    (r"^(full\s*name|your\s*name|name)\b(?!.*(company|school|university|preferred|pronounc))", "full_name"),
    (r"^e-?mail", "email"),
    (r"^(phone|mobile|contact\s*number)", "phone"),
    (r"linkedin", "linkedin"),
    (r"github", "github"),
    (r"^(website|portfolio|personal\s*(site|website))", "website"),
    (r"^(current\s*(company|employer)|company|organi[sz]ation)$", "current_company"),
    (r"^(school|university|college)", "school"),
    (r"^(location|city|current\s*location)", "location"),
]
RESUME_INPUT = "input[type=file][id*=resume i], input[type=file][name*=resume i], input[type=file]"


def _values(profile):
    return {**profile, "full_name": f"{profile['first_name']} {profile['last_name']}"}


def fill(page, profile, resume_path):
    values, filled = _values(profile), []
    for pattern, key in FIELDS:
        fields = page.get_by_label(re.compile(pattern, re.I))
        for i in range(min(fields.count(), 2)):
            field = fields.nth(i)
            try:
                kind = field.evaluate("e => e.tagName.toLowerCase() + ':' + (e.type || '')")
                if not (kind.startswith("textarea") or kind in ("input:text", "input:email", "input:tel", "input:url", "input:")):
                    continue
                if field.is_visible() and not field.input_value():
                    field.fill(str(values[key]))
                    filled.append(key)
            except Exception:
                pass
    try:
        page.locator(RESUME_INPUT).first.set_input_files(resume_path, timeout=5000)
        filled.append("resume")
    except Exception:
        pass
    return filled


def run(cfg, limit):
    from playwright.sync_api import sync_playwright

    conn = db.connect()
    notion.sync(conn, cfg, print)  # drop anything you already marked Applied/Skipped in Notion
    queue = [db.as_dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE status IN ('matched','drafted','emailed','no_contact') "
        "AND apply_state IS NULL AND apply_url IS NOT NULL ORDER BY score DESC LIMIT ?", (limit,))]
    if not queue:
        print("Apply queue is empty. Run `./jobs run` first.")
        return
    print(f"{len(queue)} jobs to review. For each: check the form in the browser, answer anything left, "
          "click Submit yourself, then come back here.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(str(ROOT / ".browser-profile"), headless=False,
                                                       no_viewport=True, args=["--start-maximized"])
        page = browser.pages[0] if browser.pages else browser.new_page()
        for n, job in enumerate(queue, 1):
            print(f"[{n}/{len(queue)}] {job['company']} | {job['title']}  (score {job['score']}, {job['track']})")
            print(f"    {job['apply_url']}")
            try:
                page.goto(job["apply_url"], wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                if job["source"] != "linkedin":
                    resume = cfg["tracks"][job["track"]]["resume"]
                    print(f"    filled: {', '.join(fill(page, cfg['profile'], resume)) or 'nothing (fill by hand)'}")
            except Exception as e:
                print(f"    could not load/fill: {e}")
            answer = input("    [a]pplied  [s]kip  [l]ater  [q]uit > ").strip().lower()[:1]
            if answer == "a":
                db.update(conn, job["id"], apply_state="applied", applied_at=_now())
            elif answer == "s":
                db.update(conn, job["id"], apply_state="skipped")
            elif answer == "q":
                break
        browser.close()
    notion.sync(conn, cfg, print)


def _now():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")
