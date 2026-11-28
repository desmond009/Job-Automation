"""Founder outreach leads from a startup directory sheet (e.g. YC founder lists).

Each row is a company, not a job posting: name, website, description, founders.
Every company becomes one lead (source "yc") asking its founders for a remote
software engineering internship. The company website is read once at import for
product text and any email addresses printed on it; contacts.from_founders()
picks who to write to.
"""
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from urllib.parse import urljoin, urlparse

from .sources import find_emails, session, strip_html

TITLE = "Founder outreach: Software Engineering Intern (remote)"
FOUNDERS_LINE = re.compile(r"^Founders: (.*)$", re.M)
PAGES = ("", "/contact", "/about")


def is_founder_sheet(rows):
    return bool(rows) and any("Founder" in c for c in rows[0]) and any("Website" in c for c in rows[0])


def domain_of(url):
    url = url.strip()
    if url and "://" not in url:
        url = "https://" + url
    return urlparse(url).netloc.lower().removeprefix("www.").split(":")[0]


def companies(rows):
    """Row dicts keyed by header, one per company with a website."""
    head = [c.strip() for c in rows[0]]
    out = []
    for row in rows[1:]:
        rec = {h: (row[i].strip() if i < len(row) else "") for i, h in enumerate(head)}
        if rec.get("Company Name") and domain_of(rec.get("Website", "")):
            out.append(rec)
    return out


def _site_text(url, domain):
    """(visible text of the homepage, emails at the company's domain) or (None, []) if the site is down."""
    if "://" not in url:
        url = "https://" + url
    text, emails = None, []
    for path in PAGES:
        try:
            r = session.get(urljoin(url, path) if path else url, timeout=15)
        except Exception:
            if not path:
                return None, []
            continue
        if not r.ok:
            if not path:
                return None, []
            continue
        html = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
        mailtos = " ".join(re.findall(r"mailto:([^\"'?>\s]+)", r.text, re.I))
        page = strip_html(html)
        if not path:
            text = page[:2500]
        emails += [e for e in find_emails(page + " " + mailtos) if e.split("@")[1].endswith(domain)]
    return text, list(dict.fromkeys(emails))


def lead(rec, site_text, emails):
    domain = domain_of(rec["Website"])
    names = [n.strip() for n in rec.get("Founder Name", "").split(",") if n.strip() and n.strip() != "N/A"]
    links = [l.strip() for l in rec.get("Founder LinkedIn", "").split(",") if l.strip()]
    founders = ", ".join(f"{n} ({links[i]})" if i < len(links) else n for i, n in enumerate(names))
    num = lambda v: v.removesuffix(".0") if v else "?"
    description = (
        "Founder outreach lead from a YC founder list. There is no job posting: this is a small startup\n"
        "the candidate would cold-email asking for a remote software engineering internship.\n\n"
        f"Company: {rec['Company Name']} ({rec['Website']})\n"
        f"About: {rec.get('Company Description', '')}\n"
        f"Industry: {rec.get('Industry', '')}\n"
        f"Founded: {num(rec.get('Founded'))}   Team size: {num(rec.get('Team Size'))}\n"
        f"Founders: {founders}\n"
        f"\nFrom their website:\n{'(site unreachable)' if site_text is None else site_text or '(no text on homepage)'}"
    )
    return {
        "id": f"yc:{domain}", "source": "yc", "company": rec["Company Name"], "domain": domain,
        "title": TITLE, "location": rec.get("Location") or "", "url": rec["Website"], "apply_url": None,
        "description": description, "emails": emails, "posted_at": None,
    }


def fetch_leads(rows, skip_ids, log):
    """Leads for companies not in skip_ids, plus the ids whose website is down."""
    recs, seen = [], set(skip_ids)
    for rec in companies(rows):
        key = f"yc:{domain_of(rec['Website'])}"
        if key not in seen:
            seen.add(key)
            recs.append(rec)
    log(f"  {len(recs)} new companies; reading their websites")
    with ThreadPoolExecutor(16) as pool:
        sites = list(pool.map(lambda r: _site_text(r["Website"], domain_of(r["Website"])), recs))
    leads, down = [], []
    for rec, (text, emails) in zip(recs, sites):
        job = lead(rec, text, emails)
        leads.append(job)
        if text is None:
            down.append(job["id"])
    return leads, down


def founders_of(job):
    """[(first name, full name)] parsed from the lead's description."""
    m = FOUNDERS_LINE.search(job.get("description") or "")
    if not m:
        return []
    out = []
    for part in m.group(1).split("),"):
        name = re.sub(r"\s*\(.*$", "", part).strip()
        if name:
            out.append((name.split()[0], name))
    return out


@lru_cache(maxsize=None)
def has_mx(domain):
    try:
        out = subprocess.run(["dig", "+short", "+time=3", "MX", domain], capture_output=True, text=True, timeout=10)
        return bool(out.stdout.strip())
    except Exception:
        return False


INDIA_LINK = re.compile(r"^India link: (.*)$", re.M)

INDIAN_PROMPT = """For each startup below, say whether at least one founder is most likely Indian or of
Indian origin, judging from the founder's name (and the company text if it helps). Be conservative:
answer true only when the name is clearly Indian (e.g. Sharma, Patel, Iyer, Reddy, Gupta, Raghav, Nandini).

Return ONLY a JSON array, one object per startup, same order:
[{{"id": "...", "indian_founder": "full name of that founder, or null"}}]

STARTUPS
{listing}"""


def tag_india(jobs, cfg, log):
    """Set the 'India link' line on each founder lead; returns {id: link or None}."""
    from . import llm
    from .match import INDIA
    links, ask = {}, []
    for job in jobs:
        if INDIA.search(job.get("location") or ""):
            links[job["id"]] = "HQ in India"
        else:
            ask.append(job)
    for i in range(0, len(ask), 60):
        batch = ask[i:i + 60]
        listing = "\n".join(f"- id: {j['id']} | {j['company']} | founders: "
                            f"{', '.join(full for _, full in founders_of(j)) or 'unknown'}" for j in batch)
        try:
            answers = {a["id"]: a.get("indian_founder") for a in llm.ask_json(INDIAN_PROMPT.format(listing=listing), cfg)
                       if isinstance(a, dict) and "id" in a}
        except Exception as e:
            log(f"  ! founder check failed for {len(batch)} leads, they stay untagged: {e}")
            continue
        for j in batch:
            name = answers.get(j["id"])
            links[j["id"]] = f"Indian founder: {name}" if name and name != "null" else None
        log(f"  checked founders {min(i + 60, len(ask))}/{len(ask)}")
    for job in jobs:
        if job["id"] in links:
            desc = INDIA_LINK.sub("", job["description"]).rstrip()
            line = f"India link: {links[job['id']] or 'none'}"
            job["description"] = desc.replace("\nFounders:", f"\n{line}\nFounders:", 1)
    return links
