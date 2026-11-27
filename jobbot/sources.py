"""Job fetchers. Each yields plain dicts with the keys in db.COLUMNS.

Greenhouse and LinkedIn list endpoints return no description, so those jobs are
fetched in two steps: list everything cheaply, then `hydrate()` only the jobs
that pass the pre-filter.
"""
import html
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
session = requests.Session()
session.headers["User-Agent"] = UA

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
JUNK_EMAIL = re.compile(r"\.(png|jpe?g|gif|svg|webp)$|example\.(com|org)|sentry|wixpress", re.I)


def strip_html(text):
    text = html.unescape(text or "")  # Greenhouse double-escapes its HTML
    text = re.sub(r"<(br|/p|/li|/div|/h\d|/tr)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def find_emails(text):
    text = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", text, flags=re.I)
    text = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", text, flags=re.I)
    found = (e.lower().rstrip(".") for e in EMAIL_RE.findall(text))
    return list(dict.fromkeys(e for e in found if not JUNK_EMAIL.search(e)))


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def greenhouse(slug, name, domain):
    r = session.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=30)
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        yield {
            "id": f"gh:{slug}:{j['id']}", "source": "greenhouse", "company": name, "domain": domain,
            "title": j["title"], "location": (j.get("location") or {}).get("name", ""),
            "url": j["absolute_url"], "apply_url": f"https://job-boards.greenhouse.io/{slug}/jobs/{j['id']}",
            "posted_at": j.get("first_published") or j.get("updated_at"),
        }


def lever(slug, name, domain):
    r = session.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, timeout=30)
    r.raise_for_status()
    for j in r.json():
        cats = j.get("categories") or {}
        location = ", ".join(cats.get("allLocations") or [cats.get("location") or ""])
        if j.get("workplaceType") == "remote":
            location += " (Remote)"
        parts = [j.get("descriptionPlain")]
        parts += [f"{lst['text']}\n{strip_html(lst['content'])}" for lst in j.get("lists") or []]
        parts.append(j.get("additionalPlain"))
        yield {
            "id": f"lever:{slug}:{j['id']}", "source": "lever", "company": name, "domain": domain,
            "title": j["text"], "location": location, "url": j["hostedUrl"], "apply_url": j.get("applyUrl"),
            "description": "\n\n".join(p for p in parts if p), "posted_at": _iso(j["createdAt"]),
        }


def ashby(slug, name, domain):
    r = session.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=30)
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        locations = [j.get("location")] + [s.get("location") for s in j.get("secondaryLocations") or []]
        location = ", ".join(l for l in locations if l)
        if j.get("isRemote"):
            location += " (Remote)"
        yield {
            "id": f"ashby:{slug}:{j['id']}", "source": "ashby", "company": name, "domain": domain,
            "title": j["title"], "location": location, "url": j["jobUrl"], "apply_url": j.get("applyUrl"),
            "description": j.get("descriptionPlain"), "posted_at": j.get("publishedAt"),
        }


def keka(slug, name, domain):
    # Keka careers portals (common with Indian startups) serve their job list as public JSON.
    r = session.get(f"https://{slug}.keka.com/careers/api/jobs/default/active", timeout=30, allow_redirects=False)
    r.raise_for_status()
    for j in r.json():
        location = ", ".join(l.get("city") or l.get("name") or "" for l in j.get("jobLocations") or [])
        description = strip_html(j.get("description"))
        if j.get("experience"):
            description = f"Experience: {j['experience']}\n\n{description}"
        url = f"https://{slug}.keka.com/careers/jobdetails/{j['id']}"
        yield {
            "id": f"keka:{slug}:{j['id']}", "source": "keka", "company": name, "domain": domain,
            "title": j["title"], "location": location, "url": url, "apply_url": url,
            "description": description, "posted_at": j.get("publishedOn"),
        }


def hn_who_is_hiring():
    hits = session.get("https://hn.algolia.com/api/v1/search_by_date", timeout=30, params={
        "tags": "story,author_whoishiring", "query": "who is hiring", "hitsPerPage": 5,
    }).json()["hits"]
    story = next(h for h in hits if h["title"].startswith("Ask HN: Who is hiring?"))
    thread = session.get(f"https://hn.algolia.com/api/v1/items/{story['objectID']}", timeout=90).json()
    for post in thread.get("children", []):
        if post.get("text"):
            yield hn_post(post)


def hn_post(post, source="hn"):
    """Turn one HN "Who is hiring?" comment (Algolia item JSON) into a job."""
    text = strip_html(post["text"])
    first_line = text.split("\n", 1)[0]
    urls = [u for u in URL_RE.findall(text) if "ycombinator.com" not in u]
    emails = find_emails(text)
    domain = next((e.split("@")[1] for e in emails if not e.endswith(("gmail.com", "proton.me"))), None)
    if not domain and urls:
        domain = urlparse(urls[0]).netloc.removeprefix("www.")
    return {
        "id": f"hn:{post['id']}", "source": source,
        "company": re.sub(r"\(.*?\)|https?://\S+", "", first_line.split("|")[0]).strip()[:80],
        "domain": domain, "title": first_line[:200], "location": first_line[:200],
        "url": f"https://news.ycombinator.com/item?id={post['id']}",
        "apply_url": urls[0] if urls else None, "description": text, "emails": emails,
        "posted_at": post.get("created_at"),
    }


def linkedin(query, location, pages, max_age_days, remote_only=False):
    """Public, logged-out job search. Read-only, so your LinkedIn account is never involved."""
    for page in range(pages):
        params = {
            "keywords": query, "location": location, "f_E": "1,2",  # internship, entry level
            "f_TPR": f"r{max_age_days * 86400}", "sortBy": "DD", "start": page * 10,
        }
        if remote_only:
            params["f_WT"] = "2"  # remote roles open to people in `location`
        r = session.get("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search", timeout=30, params=params)
        if r.status_code != 200:
            break
        cards = r.text.split("<li>")[1:]
        for card in cards:
            job_id = re.search(r"jobPosting:(\d+)", card)
            title = re.search(r'base-search-card__title">\s*(.*?)\s*</h3>', card, re.S)
            company = re.search(r'base-search-card__subtitle">.*?<a[^>]*>\s*(.*?)\s*</a>', card, re.S)
            place = re.search(r'job-search-card__location">\s*(.*?)\s*</span>', card, re.S)
            posted = re.search(r'datetime="([\d-]+)"', card)
            if not (job_id and title):
                continue
            url = f"https://www.linkedin.com/jobs/view/{job_id.group(1)}"
            yield {
                "id": f"linkedin:{job_id.group(1)}", "source": "linkedin",
                "company": html.unescape(company.group(1)) if company else "",
                "title": html.unescape(title.group(1)), "location": html.unescape(place.group(1)) if place else "",
                "url": url, "apply_url": url, "posted_at": posted.group(1) if posted else None,
            }
        if len(cards) < 10:
            break
        time.sleep(random.uniform(1.5, 3.5))


def hydrate(job):
    """Fill in the description (and posting emails) for jobs whose list endpoint lacks it."""
    if job.get("description"):
        return job
    if job["source"] == "greenhouse":
        _, slug, gh_id = job["id"].split(":")
        r = session.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{gh_id}", timeout=30)
        job["description"] = strip_html(r.json().get("content")) if r.ok else ""
    elif job["source"] == "linkedin":
        time.sleep(random.uniform(1.0, 2.5))
        r = session.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job['id'].split(':')[1]}", timeout=30)
        body = re.search(r"show-more-less-html__markup[^>]*>(.*?)</div>", r.text, re.S) if r.ok else None
        job["description"] = strip_html(body.group(1)) if body else ""
    job["emails"] = job.get("emails") or find_emails(job["description"])
    return job


def fetch_all(cfg, log):
    """Yield every job from every enabled source. One broken board never stops the run."""
    fetchers = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "keka": keka}
    for ats, boards in cfg["companies"].items():
        count = 0
        for slug, (name, domain) in (boards or {}).items():
            try:
                for job in fetchers[ats](str(slug), name, domain):
                    count += 1
                    yield job
            except Exception as e:
                log(f"  ! {ats}/{slug}: {e}")
        log(f"  {ats}: {count} postings")

    search = cfg["search"]
    if search.get("hn_who_is_hiring"):
        try:
            jobs = list(hn_who_is_hiring())
            log(f"  hn: {len(jobs)} postings")
            yield from jobs
        except Exception as e:
            log(f"  ! hn: {e}")

    li = search.get("linkedin") or {}
    if li.get("enabled"):
        count = 0
        for query in li["queries"]:
            try:
                for job in linkedin(query, li["location"], li["pages_per_query"], search["max_age_days"],
                                    search.get("remote_only")):
                    count += 1
                    yield job
            except Exception as e:
                log(f"  ! linkedin '{query}': {e}")
        log(f"  linkedin: {count} postings")
