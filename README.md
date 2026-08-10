# Job Applier

An agentic job search that runs on your machine. It scans for openings, scores them
against your profile, asks an AI model (Claude or Gemini) whether each one is genuinely
worth your time, tailors a resume and cover letter for the ones that are, fills in the
application form, and tracks every application through to an offer — on a schedule, without
you.

You decide how far down that chain it's allowed to go. Out of the box it goes as far as
"planned it and wrote nothing".

Everything is local. Your resume, your profile, and your pipeline live in a SQLite file in
`data/` — nothing leaves the machine except the job description and profile you send to the
AI model, and whatever you type into an employer's form.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # add ONE api key: Anthropic or Gemini (see below)
python -m app                 # opens http://127.0.0.1:8765
```

**The AI features need a key, and you have a free option.** Set either:

- `ANTHROPIC_API_KEY` — Claude (console.anthropic.com), or
- `GEMINI_API_KEY` — Google Gemini, which has a **genuinely free tier**, no credit card
  (get one at aistudio.google.com/apikey).

The app auto-detects whichever you set. Everything except tailoring, fit review, and
interview prep works with no key at all.

Fill in your profile, add a few company boards, then open the **Agent** tab and press
**Run now**. The first run is a dry run: it costs nothing and shows you exactly which jobs
it would have gone after and why.

To let it fill application forms as well:

```bash
pip install playwright && playwright install chromium
```

## The loop

```
scan  ──▶  shortlist  ──▶  review  ──▶  tailor  ──▶  apply  ──▶  follow up
boards     local score      Claude's     resume +     fill the    nudge the
+ feeds    ≥ threshold      fit call     letter       form        quiet ones
```

**1. Scan.** Company career-page boards (six ATS platforms) plus open job feeds, on an
interval you set. See [Where jobs come from](#where-jobs-come-from) — plus paste-in support
for Google Jobs, Rozee.pk, LinkedIn, and anything else.

**2. Shortlist.** Every posting is scored 0-100 locally, with no API calls and no cost:

| Component | Weight | What it measures |
|---|---|---|
| Title fit | 35 | Overlap with your target titles |
| Skill overlap | 35 | How many of your skills the posting actually names |
| Location | 20 | Target locations, or remote when you're open to it |
| Freshness | 10 | Recent postings beat stale ones |

Penalties on top: seniority distance (8 points per rung) and excluded keywords (25 points
each). Only jobs over your threshold cost a model call.

**3. Review.** Claude assesses fit and returns a score, a verdict, strengths, and gaps. A
verdict of "skip", or a score under your threshold, ends that job's run — and the log says
which gap killed it.

**4. Tailor.** A resume rewritten for the posting and a cover letter, both bound by the
truthfulness rule below. The application moves to `ready`.

**5. Apply.** If you've enabled it, a real browser opens the form, fills what your profile
answers, uploads the tailored resume as a PDF, and **stops at the submit button**. See
[How applying works](#how-applying-works).

**6. Follow up.** Applications that have gone quiet for N days get flagged in the pipeline.

Every decision, including every skip and its reason, is written to the run log. If it
passed on a job you liked, you can see exactly why.

## How far it's allowed to go

Four settings, each one a bigger commitment than the last. The UI enforces the nesting —
you can't enable a rung without the one above it.

| Setting | Default | What it does |
|---|---|---|
| **Dry run** | on | Scores and plans. No model calls, no documents, nothing sent. |
| *(dry run off)* | | Reviews and tailors, then stops. Applications sit at `ready` for you to submit. |
| **Fill application forms** | off | Opens a browser and fills the form. Still stops at submit. |
| **Press submit too** | off | Sends it. Refuses on any form with unanswered required fields. |

Alongside those, hard limits it cannot exceed: reviews per run, resumes per run,
applications per run, and **applications per day** — counted from the database, so
restarting the app or triggering an extra run by hand can't be used to top up the day.

### What it will never answer

At any setting, with auto-submit on, these are left blank and listed back to you:

- demographic and EEO questions — gender, race, veteran status, disability
- work authorization, sponsorship, citizenship, clearance, background checks
- salary expectations, notice period, start date, willingness to relocate
- "How did you hear about us?"
- anything it can't answer from your profile, and any name field that belongs to
  somebody else (a school, a reference, an emergency contact)

Every skip is reported with its reason. A confidently wrong answer to "Are you legally
authorized to work in the US?" is worse than a blank one, and these are yours to answer.

## Where jobs come from

**Company career pages** — via each company's own public JSON board: **Greenhouse, Lever,
Ashby, Workable, SmartRecruiters, Recruitee**. No key, no scraping, full descriptions.
Between them these power a large share of company career pages worldwide, including many
outside the US. Paste any job URL from a company and the board is detected automatically.

**Open feeds** — [Remotive](https://remotive.com), [Arbeitnow](https://www.arbeitnow.com),
[Jobicy](https://jobicy.com), and [RemoteOK](https://remoteok.com) publish free,
documented, key-less APIs meant for exactly this. They're queried with your target titles,
and they're how the agent discovers companies you never thought to add. Their terms ask you
not to hammer them, hence the 6-hour default interval.

**Paste anything else** — Google Jobs, [Rozee.pk](https://rozee.pk), Mustakbil, a company
page, an email, a WhatsApp message. These have no clean public API and scraping them is
fragile and often against their terms, so instead you paste the posting into the
**Paste any job** box on the Companies tab. The reliable form is a few label lines then the
description:

```
Title: Senior Backend Engineer
Company: Systems Ltd
Location: Lahore, Pakistan
URL: https://www.rozee.pk/job/12345

Full job description here…
```

Separate several with a line of `---`. A rough copy-paste with no labels also works
best-effort (first line = title, second = company). Nothing is fetched; what you paste is
what gets scored and tailored.

**LinkedIn** — imported, never fetched. LinkedIn's User Agreement forbids automated access
and their anti-bot layer will lock an account that tries, so this app does not talk to
linkedin.com at all. Use the dedicated LinkedIn importer on the Companies tab: paste a
**job alert email**, a copied **search results page**, or **job URLs** one per line
(optionally `url | Title | Company | Location`). Where the company runs its own board, add
that instead — the agent can poll it properly and gets the full description.

## How applying works

Playwright drives a real browser. For each form it reads every field's visible label, works
out what belongs there, types it, uploads the resume, screenshots the result, and records
the attempt — what it filled, what it refused, and why.

The resume is rendered to a **PDF** with no external dependencies: one column, Helvetica,
text-selectable, which is the format ATS parsers get right most often.

**It stops at the submit button by default.** You get the filled form, a screenshot, and a
checklist of what still needs you. Auto-submit exists, it's off, and it refuses to fire when
anything required is unanswered.

Two things worth knowing before you turn it on:

- Some ATS platforms' terms prohibit automated submission, and a few will flag accounts
  that do it at volume. This is a real risk you take on, not a hypothetical one.
- Applications sent without a human read are worse applications. The value here is the
  30 minutes of typing your name and email for the hundredth time — not skipping the read.

Nothing bypasses a CAPTCHA, a login wall, or a bot check. If a form has one, the attempt
comes back `needs_review` and you finish it.

## Running it unattended

Either leave the app running with **Run on a schedule** enabled, or drive it from cron:

```bash
python -m app agent --dry-run     # plan only, print what it would do
python -m app agent --live        # review and tailor
python -m app agent --apply       # ...and fill forms
python -m app agent --json        # machine-readable run record
```

```cron
0 */6 * * * cd /path/to/Job-Applier && python -m app agent --live
```

The interval is measured from the last run's start time in the database, so a restart can't
reset the clock.

## On truthfulness

Every generation prompt carries a hard rule: use only facts present in your profile. The
model may reorder, re-weight, and rephrase what you've written, and use the job's vocabulary
for things you've genuinely done — but it must not invent employers, titles, dates,
credentials, or metrics. Where the job needs something you don't have, it says so as a gap
instead of papering over it.

Read what it produces before you send it. That is the whole reason the submit button is a
separate, opt-in setting.

## Project layout

```
app/
  config.py       Environment/config resolution, .env loading
  db.py           SQLite schema, migrations, and helpers
  matching.py     Local 0-100 scoring — no API calls
  documents.py    Generated-document storage (DB + disk)
  sources/        Company boards (6 ATS), open feeds (4), LinkedIn + paste importers
  pipeline.py     Sync, ingest, and scoring orchestration
  llm.py          LLM access — Anthropic and Gemini behind one interface
  tailor.py       Resume, cover letter, fit review, resume import
  prep.py         Interview prep and first-90-days plans
  apply/
    answers.py    Profile -> form answers, and the never-answer rules
    render.py     Markdown -> PDF, dependency-free
    browser.py    Playwright form driver
  agent/
    settings.py   The policy: thresholds, budgets, how far it may go
    runner.py     The loop, its budgets, and the action log
    scheduler.py  Interval scheduling in a background thread
  server.py       FastAPI JSON API + UI
  static/         Single-page UI (no build step)
tests/            177 tests
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Use Claude for review, tailoring, and prep |
| `GEMINI_API_KEY` | — | Use Google Gemini instead (free tier available) |
| `LLM_PROVIDER` | auto | Force `anthropic` or `gemini` when both keys are set |
| `ANTHROPIC_MODEL` | `claude-opus-5` | Claude model used for generation |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Gemini model used for generation |
| `JOB_APPLIER_DATA` | `./data` | Where the database and documents live |
| `JOB_APPLIER_PORT` | `8765` | Server port |
| `JOB_APPLIER_BROWSER_PATH` | — | Chromium binary, if Playwright's own isn't usable |

Generated documents land in `data/documents/` as Markdown, as uploadable PDFs, and as form
screenshots.

Without any API key the agent still runs: it scans, scores, and saves what matches, then
tells you that review and tailoring need credentials.

## Tests

```bash
python -m pytest
```

The LLM tests point the Anthropic SDK at a local mock endpoint, so request shapes are
verified without spending tokens. The browser tests drive real HTML in real Chromium and
skip themselves when Playwright isn't installed.
