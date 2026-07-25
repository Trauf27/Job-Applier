# Job Applier

A local-first job search assistant. It builds a profile from your resume, pulls open
roles from company job boards, scores them against your profile, tailors a resume and
cover letter per role, tracks every application through the pipeline, and prepares you
for the interview and the first 90 days on the job.

Everything runs on your machine. Your resume, your profile, and your pipeline live in a
SQLite file in `data/` — nothing is uploaded anywhere except the job description and
profile you explicitly send to Claude when you press a Generate button.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # add your ANTHROPIC_API_KEY
python -m app                 # opens http://127.0.0.1:8765
```

The app works without an API key — searching, scoring, and tracking are all local. The
key only enables the generation features (resume tailoring, fit review, interview prep,
first-90-days plan, and resume import).

## How it works

**1. Profile.** Paste your resume and Claude extracts it into structured fields, or fill
them in by hand. Add target titles, target locations, skills, seniority, and any
exclude keywords ("security clearance", "unpaid"). The profile is the single source of
truth for matching and for every generated document.

**2. Companies.** Add the company boards you care about. Paste any job URL from the
company and the board is detected automatically:

| ATS | Example URL you can paste |
|---|---|
| Greenhouse | `https://boards.greenhouse.io/acme/jobs/12345` |
| Lever | `https://jobs.lever.co/acme/8f2c...` |
| Ashby | `https://jobs.ashbyhq.com/acme/1a2b...` |

These are the companies' own public JSON endpoints — no API key, no scraping, no rate
limit games. A board that moves or 404s is reported per-company in the sync results.

**3. Sync and score.** **Sync boards** pulls every enabled company and scores each
posting 0-100 locally — no API calls, no cost:

| Component | Weight | What it measures |
|---|---|---|
| Title fit | 35 | Overlap with your target titles |
| Skill overlap | 35 | How many of your skills the posting actually names |
| Location | 20 | Target locations, or remote when you're open to it |
| Freshness | 10 | Recent postings beat stale ones |

Penalties on top: seniority distance (8 points per rung) and excluded keywords (25
points each). Every job shows exactly why it scored what it did.

**4. Apply.** Open a job and generate:

- **Fit review** — a blunt second-pass assessment: score, verdict, strengths, gaps, what
  to lead with.
- **Tailored resume** — your history rewritten to speak to this posting.
- **Cover letter** — 220-320 words, specific, no filler.

Then open the posting and submit it yourself. The app deliberately does not autofill or
auto-submit: the ATS platforms ban accounts that do, and reviewing before you send is
what keeps quality up when you're applying at volume.

**5. Track.** Every job you touch moves through `saved → ready → applied → screening →
interview → offer` (plus `rejected` / `withdrawn`). Status changes are timestamped in an
event log, so you can see when you applied and how long each stage took.

**6. Prepare.** Once you're in the loop, generate:

- **Interview prep** — the likely loop, technical questions this posting invites,
  behavioural questions mapped to STAR stories from your real history, your gaps and
  how to handle them honestly, questions to ask them, and a 48-hour plan.
- **First 90 days** — what success looks like in this role, a 30/60/90 plan, the skills
  to close before you start, and early warning signs.

### On truthfulness

Every generation prompt carries a hard rule: use only facts present in your profile.
The model may reorder, re-weight, and rephrase what you've written, and use the job's
vocabulary for things you've genuinely done — but it must not invent employers, titles,
dates, credentials, or metrics. Where the job needs something you don't have, it says so
as a gap instead of papering over it. Read what it produces before you send it.

## Project layout

```
app/
  config.py       Environment/config resolution, .env loading
  db.py           SQLite schema and helpers
  matching.py     Local 0-100 scoring — no API calls
  sources/        Greenhouse / Lever / Ashby board clients + URL detection
  pipeline.py     Sync + scoring orchestration
  llm.py          Anthropic Messages API wrapper
  tailor.py       Resume, cover letter, fit review, resume import
  prep.py         Interview prep and first-90-days plans
  server.py       FastAPI JSON API + UI
  static/         Single-page UI (no build step)
tests/            36 tests: scoring, board parsing, API, LLM request shapes
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Enables all generation features |
| `ANTHROPIC_MODEL` | `claude-opus-5` | Model used for generation |
| `JOB_APPLIER_DATA` | `./data` | Where the database and documents live |
| `JOB_APPLIER_PORT` | `8765` | Server port |

Generated documents are written to `data/documents/` as Markdown and are also
downloadable from the UI.

## Tests

```bash
python -m pytest
```

The LLM tests point the Anthropic SDK at a local mock endpoint, so request shapes are
verified for real without spending tokens.

## Scope notes

- **Job sources** are company ATS boards. LinkedIn and Indeed actively block automated
  access and forbid it in their terms, so they aren't built in — paste a URL from those
  sites into the company detector only if the company's own board is behind it.
- **Applying is manual by design.** The app gets you to a tailored, reviewed application
  and opens the posting; you submit.
