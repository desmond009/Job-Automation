# jobbot

Finds jobs that fit your resume, cold-emails the hiring contact with the right resume attached,
and pre-fills application forms for you to submit.

```
./jobs run --dry-run   # everything except sending; drafts land in outbox/<date>/
./jobs run             # same, and sends up to the daily cap through Gmail
./jobs apply           # opens matched jobs' application forms, pre-filled
./jobs status          # counts + top of the apply queue
./jobs schedule on     # run every day at 10:00 (./jobs schedule on --at 09:30, or off)
```

## How a run works

1. **Fetch** open postings from the boards in `companies.yaml` (Greenhouse, Lever, Ashby, Keka),
   the latest HN "Who is hiring?" thread, and LinkedIn's public job search (logged out).
2. **Pre-filter** for free: drops senior titles, non-engineering roles, stale posts, and
   locations outside India unless the role is remote worldwide.
3. **Score** up to 60 new jobs per run with Claude (via your `claude` CLI login). Each job
   gets eligible yes/no, a 0-100 fit score, and the best resume track. Matches need
   `eligible` and score >= 70.
4. **Contact**: an email address printed in the posting, else Apollo.io (if you add a key).
   Each company gets at most one email per 45 days, and each address at most one email ever.
5. **Write** a 90-150 word email per match from the matching resume only, with real numbers,
   no invented skills. Drafts that fail the checks are discarded.
6. **Send** through Gmail SMTP: max 20/day, 1-3 minutes apart, resume attached as
   `Vijender_Yadav_Resume.pdf`. Sent mail shows up in your Gmail Sent folder.

Matched jobs without a contact go to the **apply queue**. `./jobs apply` opens each one in
a browser, fills name, email, phone, links and resume, and waits. You answer anything left,
click Submit, then mark it `a`pplied / `s`kip / `l`ater in the terminal. Nothing is ever
submitted for you, and LinkedIn pages are only opened, never filled.

## Notion

Every match is a row in the **Job Hunt** database
(https://app.notion.com/p/41a34c387b9a8106a0ffda158c19213f), synced at the end of each run,
before and after `./jobs apply`, and on demand with `./jobs sync`.

- The script moves rows through **To apply → Email drafted → Emailed**, and to **Applied**
  when you mark one in `./jobs apply`. Drafted emails appear inside the row's page.
- You own everything after that: set **Applied, Interview, Offer, Rejected or Skipped** in
  Notion and the script reads it back, never overwrites it, and drops the job from the apply queue.
- **Chance** is High (score 85+), Medium (75-84) or Stretch (70-74).

## Setup still needed

1. `cp .env.example .env`
2. Gmail app password: turn on 2-Step Verification, create one at
   https://myaccount.google.com/apppasswords, and paste it as `GMAIL_APP_PASSWORD`.
3. Notion: create an internal integration at https://www.notion.so/profile/integrations, paste
   its secret as `NOTION_TOKEN`, then open the Job Hunt database > ••• > Connections and add it.
   Check with `./jobs sync`.
4. Optional: `APOLLO_API_KEY` from Apollo > Settings > Integrations > API. Apollo's docs say
   people search isn't on the free plan; if your key gets refused, the run logs it and carries on
   with posting emails only.

## Files

| Path | What it is |
|---|---|
| `config.yaml` | Your profile, resume per track, filters, caps |
| `companies.yaml` | Company boards to watch (add your own slugs) |
| `jobs.db` | SQLite history of every posting seen, score, email and application |
| `outbox/` | Dry-run drafts, one `.txt` per email |
| `logs/run.log` | Output of scheduled runs |
| `jobbot/` | The code |

Not covered: Naukri (blocks scripts with reCAPTCHA) and Wellfound (no API, blocks bots). Many Wellfound startups are covered anyway through their own Greenhouse, Ashby or Keka boards in `companies.yaml`. Zoho Recruit and Darwinbox portals are not supported yet.
