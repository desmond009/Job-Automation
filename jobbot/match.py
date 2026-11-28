"""Two-stage matching: a free regex pre-filter on title/location, then Claude
scores the survivors against the resume in small batches."""
import re
from datetime import datetime, timedelta, timezone

from . import llm
from .config import resume_text

SENIOR = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head|vp|vice president|architect|"
    r"distinguished|fellow|intermediate|mid.?level|ii|iii|iv|l[4-9]|sde\s*-?\s*(2|3)|engineer\s+(2|3))\b", re.I)
NON_ENG = re.compile(
    r"\b(sales|account|recruit\w*|talent|marketing|designer|counsel|legal|finance|accountant|"
    r"customer success|support|people|hrbp|payroll|business development|representative|"
    r"solutions? consultant|technical writer|analyst|product manag\w*|program manag\w*|"
    r"operations|ops|compliance|investigations?|strategy|content|community|partnerships?)\b", re.I)
ENG = re.compile(
    r"\b(software|engineer\w*|developer|development|sde|swe|full.?stack|front.?end|back.?end|mobile|"
    r"flutter|android|ios|react|node|web|ai|ml|llm|platform|programmer|coding|tech)\b", re.I)
ENTRY = re.compile(r"\b(intern\w*|new grad\w*|graduate|junior|jr\.?|entry|fresher|associate|trainee|"
                   r"apprentice|2027|sde\s*-?\s*(1|i)|engineer\s+(1|i))\b", re.I)
INDIA = re.compile(r"india|bengaluru|bangalore|hyderabad|pune|mumbai|delhi|gurugram|gurgaon|noida|"
                   r"chennai|kolkata|ahmedabad|jaipur|kochi|dharwad|hubli|karnataka", re.I)
REMOTE = re.compile(r"remote", re.I)
WORLDWIDE = re.compile(r"anywhere|worldwide|global|international|distributed|apac|asia|any time ?zone", re.I)
# A remote headline naming one of these (and no worldwide signal) can't hire someone in India.
REGION_LOCKED = re.compile(r"\b(US|USA|U\.S\.?|UK|EU|EST|PST|CET|ET|PT)\b|(?i:united states|north america|americas|"
                           r"canada|europe|emea|latam|germany|poland|romania|netherlands|france|spain|brazil)")
# What's left of a location once the word "remote" and filler are removed; empty means "Remote" with no region.
REMOTE_FILLER = re.compile(r"remote|first|friendly|only|hybrid|in|or|and|\W|\d", re.I)
TRACK_SKILLS = re.compile(
    r"react|next\.?js|node|typescript|javascript|full.?stack|flutter|dart|mobile|firebase|mongo|"
    r"python|fastapi|llm|agent|langgraph|rag|\bai\b|web", re.I)


def prefilter(job, max_age_days, ats_max_age_days, remote_only=False, india_only=False):
    """Return None if the job is worth scoring, else the reason to drop it.

    remote_only keeps just remote roles that could hire someone living in India;
    Claude then checks the full posting.

    Company boards only list open roles, so they get a longer age limit than
    LinkedIn/HN, where old posts are usually stale. HN posts imported from a
    spreadsheet (source "sheet") were picked by hand, so they get it too.
    """
    title, loc = job["title"] or "", job["location"] or ""
    if job["source"] == "yc":
        # Founder outreach: no posting, level or location to check; the ask is a remote internship.
        desc = job.get("description") or ""
        if "(site unreachable)" in desc:
            return "website down"
        if india_only and not re.search(r"^India link: (HQ in India|Indian founder)", desc, re.M):
            return "no India link"
        if "India link: HQ in India" in desc:
            return None  # few of these; let Claude judge the product
        about = desc.split("\n\n", 1)[-1].replace("From their website:", "")
        if not TRACK_SKILLS.search(re.sub(r"https?://\S+", "", about)):
            return "no matching stack"
        return None
    if job["source"] in ("greenhouse", "lever", "ashby", "keka", "sheet"):
        max_age_days = ats_max_age_days
    if job.get("posted_at"):
        try:
            posted = datetime.fromisoformat(job["posted_at"].replace("Z", "+00:00"))
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            if posted < datetime.now(timezone.utc) - timedelta(days=max_age_days):
                return "too old"
        except ValueError:
            pass

    if job["source"] in ("hn", "sheet"):
        # The whole post is the posting; titles are free text.
        text = job.get("description") or ""
        if not TRACK_SKILLS.search(text):
            return "no matching stack"
        if remote_only:
            # Posts are often one paragraph; the "Company | Role | Location |" headline ends at the last pipe.
            line = text.split("\n", 1)[0]
            headline = line[: line.rfind("|") + 40] if "|" in line else line[:200]
            if not (REMOTE.search(text) or WORLDWIDE.search(text)):
                return "not remote"
            if REGION_LOCKED.search(headline) and not (INDIA.search(headline) or WORLDWIDE.search(headline)):
                return "location"
            return None
        # "REMOTE" alone on HN almost always means US-only, so ask for India or a worldwide signal.
        if not (INDIA.search(text) or WORLDWIDE.search(text)):
            return "location"
        return None

    if SENIOR.search(title):
        return "too senior"
    if NON_ENG.search(title) or not ENG.search(title):
        return "not engineering"
    if remote_only:
        # LinkedIn is searched with its remote filter and shows only the country as location.
        if job["source"] == "linkedin" or not loc.strip():
            return None
        if not (REMOTE.search(loc) or WORLDWIDE.search(loc)):
            return "not remote"
        if INDIA.search(loc) or WORLDWIDE.search(loc) or not REMOTE_FILLER.sub("", loc):
            return None
        return "location"
    if not loc.strip() or INDIA.search(loc) or WORLDWIDE.search(loc):
        return None
    if REMOTE.search(loc) and not REMOTE_FILLER.sub("", loc):
        return None
    return "location"


def priority(job):
    """Real postings before founder-outreach leads (India-HQ startups first); then explicit entry-level titles,
    India-based or worldwide, newest."""
    text = f"{job['title']} {(job.get('description') or '')[:300] if job['source'] in ('hn', 'sheet') else ''}"
    loc = job["location"] or ""
    yc = job["source"] == "yc"
    return (yc, yc and "India link: HQ in India" not in (job.get("description") or ""), not ENTRY.search(text), not (INDIA.search(loc) or WORLDWIDE.search(loc)), _neg_time(job.get("posted_at")))


def _neg_time(value):
    try:
        return -datetime.fromisoformat((value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


SCORE_PROMPT = """You are screening job postings for one candidate. Be strict and honest; a wrong
"eligible" wastes a recruiter's time and the candidate's reputation.

WHAT THE CANDIDATE WANTS
{looking_for}

CANDIDATE RESUME
{resume}

RESUME TRACKS (pick the one that fits each job best)
{tracks}

For each job decide:
- eligible: true only if BOTH hold:
  (a) level: an internship, a new-grad / 2027-batch role, or a role needing at most ~2 years of
      experience. If the posting requires 3+ years, it is NOT eligible.
  (b) location: fits WHAT THE CANDIDATE WANTS above, and the candidate keeps living in India.
      Remote roles limited to the US, Europe, or other regions are NOT eligible. Relocation abroad /
      visa sponsorship roles are NOT eligible.
  For "Who is hiring" posts that list several roles, judge the most junior relevant one.
  For "Founder outreach" entries there is no posting: the candidate would cold-email the founders
  asking for a remote internship. Skip (a) and (b); eligible = the company builds software, web,
  mobile or AI products where the candidate's skills would genuinely help, and is not a hardware,
  biotech, lab or other mostly non-software business. Score how well the resume fits their product.
- score: 0-100, how well the candidate's real skills match what the job needs (independent of eligible).
- track: one of {track_ids}
- reason: at most 15 words.

Return ONLY a JSON array with one object per job, in the same order:
[{{"id": "...", "eligible": true, "score": 82, "track": "fullstack", "reason": "..."}}]

JOBS
{jobs}"""


def score_batch(jobs, cfg):
    tracks = cfg["tracks"]
    base_resume = resume_text(tracks["fullstack"]["resume"])
    listing = "\n\n".join(
        f"### id: {j['id']}\nCompany: {j['company']}\nTitle: {j['title']}\nLocation: {j['location']}\n"
        f"Description:\n{(j.get('description') or '(none)')[:2500]}"
        for j in jobs
    )
    prompt = SCORE_PROMPT.format(
        looking_for=cfg["search"]["looking_for"].strip(),
        resume=base_resume,
        tracks="\n".join(f"- {k}: {t['label']}" for k, t in tracks.items()),
        track_ids=" | ".join(tracks),
        jobs=listing,
    )
    results = {r["id"]: r for r in llm.ask_json(prompt, cfg) if isinstance(r, dict) and "id" in r}
    for job in jobs:
        r = results.get(job["id"])
        if r and r.get("track") not in tracks:
            r["track"] = "fullstack"
        yield job, r
