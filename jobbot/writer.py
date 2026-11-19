"""Writes one tailored cold email per job with Claude, using only facts from the resume."""
import re

from . import llm
from .config import resume_text

EMAIL_PROMPT = """Write a short cold email from a final-year CS student applying for a specific job.

SENDER'S RESUME (the ONLY source of facts about the sender)
{resume}

JOB
Company: {company}
Title: {title}
Location: {location}
Posting:
{description}

RECIPIENT
{recipient}

Rules:
- Start the body with exactly: "{greeting}"
- 90 to 150 words in the body. Plain text, short paragraphs, no markdown, no bullet points.
- First line after the greeting: name the role and who you are (B.Tech CSE at IIIT Dharwad, graduating 2027).
- Then two concrete achievements from the resume, with their real numbers, picked because they match
  what THIS posting asks for. Say how they relate to the role in plain words.
- Then one sentence on why this company, based on something specific in the posting.
- End by saying the resume is attached and asking if they would consider you for the role or point
  you to the right person.
- Use ONLY facts in the resume. Never invent skills, experience, numbers, dates, or availability.
  If the posting needs something the resume lacks, leave it out.
- Sound like a sharp, direct student. No em dashes. Never use: "passionate", "leverage", "excited to",
  "I am writing to", "I hope this email finds you", "synergy", "thrilled", "delve".
- Do not add a sign-off or signature; it is added automatically.
- Subject: under 70 characters, specific to the role, mentions IIIT Dharwad '27.

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

LEGAL_SUFFIX = re.compile(r"[\s,]*\b(pvt\.?|private|ltd\.?|limited|inc\.?|llc|llp|corp\.?|gmbh|co\.)(?=\s|$)", re.I)

BANNED = re.compile(r"\[|\]|\{|\}|passionate|leverage|I am writing to|hope this (email|message) finds", re.I)


def signature(profile):
    return (f"\n\nBest,\n{profile['first_name']} {profile['last_name']}\n"
            f"{profile['degree']}, {profile['school']} ({profile['grad_year']}) | {profile['phone']}\n"
            f"LinkedIn: {profile['linkedin']}\nGitHub: {profile['github']} | {profile['website']}")


def write(job, contact, cfg):
    """Return (subject, body) or raise ValueError if Claude's draft fails the checks twice."""
    first = (contact.get("name") or "").strip().split(" ")[0]
    greeting = f"Hi {first}," if first else f"Hi {LEGAL_SUFFIX.sub('', job['company']).strip()} team,"
    recipient = (f"{contact.get('name')} ({contact.get('title') or 'title unknown'})"
                 if contact.get("name") else "Unknown person at the company's hiring address.")
    prompt = EMAIL_PROMPT.format(
        resume=resume_text(cfg["tracks"][job["track"]]["resume"]),
        company=job["company"], title=job["title"], location=job["location"],
        description=(job.get("description") or "")[:3500], recipient=recipient, greeting=greeting,
    )
    problem = None
    for _ in range(2):
        draft = llm.ask_json(prompt, cfg)
        subject = str(draft.get("subject", "")).strip()
        body = str(draft.get("body", "")).strip().replace(" — ", ", ").replace("—", ", ")
        words = len(body.split())
        if not subject or len(subject) > 90:
            problem = f"bad subject: {subject!r}"
        elif not 70 <= words <= 190:
            problem = f"body is {words} words"
        elif BANNED.search(body) or BANNED.search(subject):
            problem = f"placeholder or banned phrase: {BANNED.search(body + subject).group(0)!r}"
        else:
            return subject, body + signature(cfg["profile"])
    raise ValueError(problem)
