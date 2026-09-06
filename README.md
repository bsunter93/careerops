# Career Ops

A self-hosted job-search pipeline. It finds roles on public ATS boards, scores them
against your profile, reconstructs your application history from Gmail, and renders a
dashboard. Local Python and SQLite, no hosted services, no account to create.

It was built to replace a spreadsheet that had one row per email, which is why it kept
sender addresses in a "Company" column and could not answer "how many places have I
actually applied?"

## The one idea

**Applications are entities with derived state. Emails are events that mutate it.**

Everything else follows. Status is never hand-edited; `recompute_status()` reads the
event log and derives it, and a rank table makes it monotonic so a late acknowledgement
cannot downgrade an interview. Re-running any command is safe, because events key on
their Gmail message id.

```
companies -> roles -> applications -> events
                                  \-> artifacts, review_queue, portal_snapshot
```

## What it does

```
discover   poll Greenhouse / Ashby / Lever board APIs for matching roles
fit        score each JD against profile.md, with reasoning, via the Anthropic API
resume     generate a tailored one-page .docx from one master file plus the fit analysis
sync       read Gmail, classify each message, attach it to the right application
intel      fetch public employee sentiment per company, with sources
dashboard  render a single self-contained HTML file
```

## Setup

Four things, and the second is the annoying one.

**1. Install**

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.json config.json
cp profile.example.md profile.md
cp .env.example .env
```

Edit all three. `profile.md` is the one that matters: a vague profile produces confident
nonsense, because the scorer has nothing concrete to weigh against.

**2. Gmail access (about 20 minutes, unavoidable)**

Create a Google Cloud project, enable the Gmail API, configure an OAuth consent screen
as **External / Testing**, add yourself as a test user, then create an **OAuth client ID**
of type *Desktop app* and download it as `credentials.json` into the repo root. The scope
used is `gmail.readonly`. Nothing is ever sent anywhere; the token stays in `token.json`.

**3. An Anthropic API key** in `.env`, for fit scoring and company intel. Scoring a couple
of hundred roles costs a few dollars.

**4. Check it**

```bash
.venv/bin/python -m careerops.cli doctor      # what is configured, what is missing
.venv/bin/python -m careerops.cli init
```

## Use

```bash
python3 -m careerops.cli discover             # poll the boards in config.json
python3 -m careerops.cli fit --limit 20       # score what was found
python3 -m careerops.cli prospects            # ranked, with the reasoning
python3 -m careerops.cli resume 51            # tailored .docx for application 51
python3 -m careerops.cli sync --since 1y      # pull and classify Gmail
python3 -m careerops.cli resolve              # merge duplicates, drain the review queue
python3 -m careerops.cli dashboard --open
```

`why <id>` explains any single application. `review` shows what the classifier refused to
guess at. `analytics` prints traction by company, channel and referral.

## Design notes

**[CLAUDE.md](CLAUDE.md) is the interesting document.** It is the list of invariants this
system holds, each one paired with the bug that produced it: the day 49 discovered roles
silently became "applied", the ATS boilerplate that manufactured 22 interviews out of 7,
the Gmail query that dropped a year of one company's mail because its subject line said
"Follow-Up" instead of "application".

Four rules do most of the work:

- **Status is derived, never written.** A system that cannot recompute its own conclusions
  cannot be corrected.
- **Never guess.** Below a confidence floor, or missing a company or role, an event goes to
  a review queue. No JD means no fit score. Absent data beats wrong data.
- **Events keep their body.** Reclassifying without the source text once destroyed 48
  genuine rejections, because rejection language lives in the body, not the subject.
- **Reference data never writes state.** The employer portal and public sentiment inform
  the human. They never move a status or a score.

## Limits

- Self-hosted and single-user by design. It reads your inbox; that should stay on your machine.
- Public board APIs only. No LinkedIn or Indeed scraping.
- Page counts for generated resumes are verified through real Word via AppleScript, because
  Quick Look substitutes fonts and will happily report one page for a two-page document.
- `resume/master.json` is yours to write. The generator is generic; the content is not.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

28 tests, mostly guarding the state machine and the classifier against regressions that
have already happened once.

## License

MIT.
