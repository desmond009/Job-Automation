"""Find who to email: an address printed in the posting first, then Apollo.io."""
import re

import requests

GOOD_LOCAL = re.compile(r"job|career|hiring|recruit|talent|hr|people|team|founder|ceo|cto|hello|work", re.I)
BAD_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|privacy|legal|security|abuse|support|help|billing|"
                       r"press|media|sales|accommodation|accessibility|gdpr|dpo|unsubscribe)", re.I)

# Postings often print addresses that are NOT for applications, e.g. "if you need an
# accommodation, contact hr@...". Skip any address whose surrounding text says so.
WRONG_PURPOSE = re.compile(r"accommodat|disabilit|assistive|reasonable adjust|privacy|data protection|"
                           r"personal data|gdpr|fraud|scam|phishing|unsubscribe|report (a|any) concern", re.I)

APOLLO = "https://api.apollo.io/api/v1"


class ApolloUnavailable(RuntimeError):
    pass


def _for_applications(email, text):
    for m in re.finditer(re.escape(email), text, re.I):
        if WRONG_PURPOSE.search(text[max(0, m.start() - 300): m.end() + 40]):
            return False
    return True


def from_posting(job):
    text = job.get("description") or ""
    emails = [e for e in job.get("emails") or [] if not BAD_LOCAL.match(e) and _for_applications(e, text)]
    if not emails:
        return None
    best = sorted(emails, key=lambda e: not GOOD_LOCAL.search(e.split("@")[0]))[0]
    # "pratibha.shukla@..." is a person; greet them by first name.
    person = re.fullmatch(r"([a-z]{3,})[._]([a-z]{2,})", best.split("@")[0])
    name = person.group(1).capitalize() if person and not GOOD_LOCAL.search(best.split("@")[0]) else None
    return {"email": best, "name": name, "title": None}


def from_apollo(job, cfg, api_key):
    """Look up a verified work email for a recruiter / eng lead at the company.

    Raises ApolloUnavailable when the key or plan can't use the API, so the caller
    can stop trying for the rest of the run.
    """
    headers = {"x-api-key": api_key, "Content-Type": "application/json", "Cache-Control": "no-cache"}

    def post(path, body):
        r = requests.post(f"{APOLLO}/{path}", json=body, headers=headers, timeout=30)
        if r.status_code in (401, 403, 422) or "API_INACCESSIBLE" in r.text:
            raise ApolloUnavailable(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        r.raise_for_status()
        return r.json()

    domain = job.get("domain")
    if not domain:
        orgs = post("mixed_companies/search", {"q_organization_name": job["company"], "per_page": 1})
        orgs = orgs.get("organizations") or orgs.get("accounts") or []
        domain = orgs[0].get("primary_domain") if orgs else None
    if not domain:
        return None

    people = []
    for locations in (["India"], None):  # prefer someone in India, else anyone
        body = {"q_organization_domains_list": [domain], "person_titles": cfg["apollo"]["titles"], "per_page": 5}
        if locations:
            body["person_locations"] = locations
        people = post("mixed_people/api_search", body).get("people") or []
        if people:
            break

    for person in people[:3]:
        match = post("people/match", {"id": person["id"], "reveal_personal_emails": False}).get("person") or {}
        email = match.get("email") or ""
        if "@" in email and "not_unlocked" not in email and match.get("email_status") == "verified":
            return {"email": email.lower(), "name": match.get("first_name"), "title": match.get("title")}
    return None


def from_founders(job):
    """Founder-outreach lead: an email on the company site that belongs to a founder,
    else a general address printed there (hello@, founders@...), else a guessed
    firstname@domain when the domain receives mail."""
    from .founders import founders_of, has_mx

    people = founders_of(job)
    first = people[0][0] if people else None
    emails = [e for e in job.get("emails") or [] if not BAD_LOCAL.match(e)]
    for e in emails:
        local = e.split("@")[0]
        for fname, full in people:
            parts = [p.lower() for p in re.findall(r"[A-Za-z]+", full)]
            if any(len(p) > 2 and p in local for p in (parts[:1] + parts[-1:])):
                return {"email": e, "name": fname, "title": "Co-founder"}
    general = sorted(emails, key=lambda e: not GOOD_LOCAL.search(e.split("@")[0]))
    if general:
        return {"email": general[0], "name": first, "title": "Co-founder (company address)"}
    domain = job.get("domain")
    if first and domain and re.fullmatch(r"[A-Za-z]+", first) and has_mx(domain):
        return {"email": f"{first.lower()}@{domain}", "name": first, "title": "Co-founder (guessed address)"}
    return None
