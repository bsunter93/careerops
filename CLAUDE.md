# Career Ops: architecture and decisions

A personal job-search pipeline: discovers roles, scores them against a profile,
tracks applications from Gmail, and renders a dashboard. Local Python + SQLite,
no hosted services.

## The core model

**Applications are entities with derived state. Emails are events that mutate it.**

Everything follows from that. The predecessor was a Google Sheet with one row per
email, which is why it held sender addresses in a "Company" column and `Setup Resume ID`
in every Fit Score cell. One row per *application*, with an append-only event log
beside it, is the whole fix.

`companies → roles → applications → events`, plus `artifacts` and `review_queue`.

## Rules that must not be broken

**Status is derived, never written by hand.** `db.recompute_status()` reads the event
log and recomputes. `STATUS_RANK` makes it monotonic so a late acknowledgement cannot
downgrade an interview.

**`prospect` ranks below `applied` (-1).** A discovered role has no events; without
this it silently became "applied" and the tool claimed you'd applied to things you
hadn't. 49 records were corrupted this way before it was caught.

**Dormancy is per company.** `config.json -> company_policy.<name>.dormant_days` overrides
`default_dormant_days` (21). Google states an eight-week review window, so 56 days is right
there; a fixed 21 marked a live referred application dead.

**`dormant` is derived, not destructive.** No activity for `DORMANT_DAYS` (21) with
nothing better than an ack. A later event revives it automatically. Never delete a
stale application to "clean up", deletion loses the event history and the record of
having applied.

**A resolvable company is not an application.** `creates_application()` gates row
creation on a real application event type plus a confidence floor. Without it, any mail
whose sender domain parses becomes an application: AppSheet emailing about an app named
"Master Job Application Pipeline" produced a phantom Google application.

**Never guess.** Below 0.60 confidence, or missing company/role, goes to `review_queue`.
No fabricated fit scores: no JD means no score. `fit.py` returns `None` rather than a
number. Bad data is worse than absent data.

**Ingestion is idempotent.** Events key on `external_id` (`gmail:<msgid>`). Re-running
`sync` never duplicates. Safe to cron.

**Events retain their body** (2000 chars). Learned the hard way: reclassifying without
bodies destroyed 48 genuine rejections, because rejection language lives in the body,
not the subject. A system that cannot reproduce its own classifications cannot be
corrected safely. `reclassify` should be a no-op when nothing has changed.

**The v1 CSV tracker was derived from this same inbox.** Every CSV row has a Gmail
original carrying a body, a real subject and a parsable role, so a CSV event beside a
Gmail one is a strictly worse duplicate. `drop_csv_ghosts` removes them; it kept 2 of 22
where no Gmail original existed. Never re-run `ingest-csv` after a full sync.

## Ingestion: the Gmail query is the real gate

**A message never fetched cannot be reclassified later.** Everything downstream
(review queue, blacklist, `--refetch`) can be re-run against stored events; the search
query cannot. Under-fetching is therefore the one irreversible failure in the pipeline,
and over-fetching only costs a trip through the classifier.

An Anthropic rejection sat unseen for a year because it failed both arms of the query:
its subject read "Anthropic Follow-Up for [Pipeline] Product Manager, Monetization"
(no trigger word: "follow-up" was not one) and it came from `appreview.gem.com`, which
was not on the vendor list. Widening both arms recovered 44 events and 13 applications.
Keep the vendor domain list ahead of the ATSes actually seen in the corpus.

**Widening the net admits contract body-shops.** They write real interview language
("Interview next week", "Interview Update - please confirm your work authorization")
about roles never applied for, and they landed as `interview_invite`, inflating the one
metric that matters. `STAFFING` demotes them on the tell that is always present in the
body: `right to represent`, `W2 or C2C`, `hourly rate: $`, `18+ months contract`.
Aggregators drip-mail about an employer literally named "Confidential", so
`_clean_company` rejects that placeholder outright.

**An application whose every event is noise was never an application.** When
reclassification demotes them all, the row survives asserting a submission that never
happened. `drop_noise_only` (in `resolve`) removes it, requiring at least one event so
discovered prospects, which legitimately have none, are untouched.

## Classifier

**ATS vendors are not employers.** Domain *suffix* match, so `us.greenhouse-mail.io`
never becomes a company.

**A definitive ack subject blocks every promotion, not just interviews.** The guard
originally covered `interview_invite` and `assessment` only, so `recruiter_outreach`
still trusted the body, and "we will be reaching out to candidates" inside a plain
acknowledgement promoted five of them to `in_process`. That inflated advanced from 11 to
16 and the advance rate from 3% to 5%. Any event type that outranks `acked` has to be
inside the guard; the family of bug repeats every time a new one is added.

**Weak interview signals are subject-only.** ATS acks routinely say "we'll be in touch
about next steps" in boilerplate, which promoted "Thank you for applying to DoorDash"
to an interview. Strong patterns (`invitation to interview`) may match the body; weak
ones (`next steps`, `availability`) may not. A definitive ack subject blocks promotion
entirely. This took 22 false interviews down to 7 real ones.

**Blacklist is scoped to subject + sender, never the body.** ATS footers contain
"subscription", "payment", "order", so body-scanning silently discards real applications.

**Most acks name the role only in the body.** `role_from_body()` carries ~14 patterns
because every ATS phrases it differently ("apply for the X role", "the position of X",
"filled the X role", "Role: X Location:"). Three bugs cost real coverage here and are
worth not repeating: `appl(?:ying|ication)` missed the word "apply"; a `[^.!?]` character
class broke titles at "Sr."; and validating the raw capture on a word count threw away
good titles before the cleaner could strip trailing city lists and req ids. When coverage
looks low, scan bodies for role-like text that fails to parse rather than fixing one
company at a time.

**Store the body correctly or none of the above works.** Strip `<style>`/`<script>`
BEFORE tags: an Amazon `@font-face` block is ~2000 chars and filled the whole stored
body, so 81 events held CSS instead of text. `sync --refetch` repairs them.

**Never rename a role row that several applications share.** They all inherit the new
title. `set_identity` creates a new role instead when the row is shared; this silently
mislabelled 12 Google applications once.

**The portal is reference, not truth.** It is wrong in both directions: it shows
"Submitted" for roles already rejected by email, and it never archived three 2021
applications, one of which was accepted and led to four years of employment. Entries
known to be wrong carry `portal_status='Submitted (stale)'` and a `note`.
`portal_snapshot` stores Google's own role list
and status. It supplies role identity for acks that name none, but it never writes status:
the portal shows "Submitted" for roles already rejected by email. When the portal reveals a
decision the inbox never carried (Google emails a rejection only when a referral is
attached), record it as a `rejection` event with `source='portal'` so status still derives
from the log. Applications whose role was matched from the portal carry
`channel='portal-inferred'` and a note saying the mapping is approximate.

**The From display name is the best company signal.** `Match Group <no-reply@hire.lever.co>`
carries the employer where the domain carries only the vendor. When a subject says
"Thanks for applying to X" and X reads like a job title, X is the ROLE and the company
comes from the display name.

**Replay events in time order.** `reidentify` sorts by `occurred_at` so an ack opens a
submission before later events attach to it; unordered replay made a rejection create its
own application ahead of its own ack. `merge_orphan_outcomes` repairs any that slipped.

**Decode HTML entities on every path**, including text/plain. Oracle-hosted ATSes emit
`&nbsp;` inside plain text, which produced roles like "&nbsp;Technical Program Manager"
and split one application into two.

## Company intel

`intel.py` fetches public employee sentiment: rating out of 5, sub-ratings, pros/cons.

**Nothing here scrapes Glassdoor or Blind.** No public API, bot detection, a work-email
gate on Blind, and terms that forbid it. It runs a web search through the Messages API's
server-side search tool and reads public result snippets, which is what a person gets
from a search engine. Every row stores its sources.

**Reference only, exactly like `portal_snapshot`.** It never touches `fit_score` or
derived status. A third-party aggregate of self-selected reviews must not quietly
promote a role.

**Ambiguity is the failure mode, not availability.** "Headway" matches at least five
unrelated employers. The prompt forces the model to name the entity it landed on
(industry, HQ, size) and return `confidence: low` when it cannot confirm; low-confidence
rows are labelled wherever they surface. Never estimate a missing number: a null is
correct, a plausible guess is not.

**The rating bar is drawn against a fixed 5**, never against the best company present.
A relative scale makes 3.3/5 look like a top score.

## Discovery and filtering

**Filter on domain, not title.** `technical program manager` was excluded wholesale;
that blocked Reddit and Pinterest *monetization* TPM roles scoring 72, squarely in
the ads/GTM background, while the real problem was `infrastructure`, `security`,
`compute`, `machine learning`. Exclusions are domain and level, never job family.

**Comp floor never filters on absence.** Most JDs post no range. Only a *parsed* max
below the floor disqualifies.

**Public board APIs only** (Greenhouse, Ashby, Lever). No LinkedIn/Indeed scraping:
against their terms, brittle, and unnecessary.

## Fit scoring

Runs on the Anthropic Messages API (`ANTHROPIC_API_KEY` from a gitignored `.env`),
falling back to the local `claude` CLI. Note the shell exports `ANTHROPIC_BASE_URL`
for Claude Code; `fit.py` deliberately ignores it and targets `api.anthropic.com`.
Use `CAREEROPS_API_BASE` to override without colliding.

`max_tokens` is 2500. At 1200 the longer responses truncated mid-JSON and returned
`None` intermittently.

**Scores capability AND preference.** `profile.md` has a "Preferences and dealbreakers"
section; dealbreakers cap the score under 40 regardless of skill match. Encoding the
IC-track preference moved a role from 79 to 22 because its mandate was building a team.

**Company policy** (`config.json → company_policy`) injects history into the prompt and
gates surfacing. Location uses `location_verdict()`: `remote` / `colorado` / `ambiguous`
/ `elsewhere`. Ambiguous national postings pass *with a flag*, because under-filtering beats
hiding a reachable role. A named non-CO city makes a posting concrete, not ambiguous.

## Resume generation

`resume/master.json` is the single source of truth: 16 tagged bullets, 3 employers.
Do not fork it into variants: that is what produced three divergent `build*.js` files
with contradictory numbers.

`resume.py` ranks bullets deterministically (token/tag overlap: `emphasize` 6× and 4×,
title 3×, JD 1×) so selection is inspectable. Only the tagline and profile paragraph go
through the model; every factual claim comes from the master.

**Always verify page count through real Word** (`osascript` → `save as ... format PDF`).
Quick Look substitutes fonts (Calibri ships inside Office, not system-wide) and reports
one page for a document Word renders as two.

## Dashboard

A pure projection: reads SQLite, writes a self-contained HTML file with data embedded
as JSON. Disposable and regenerable, so never hand-edit it or it becomes a second
competing record.

`--artifact` emits a skeleton-free build for publishing as a private Artifact.

**One page. Never tabs.** Splitting the charts into a second tab fixed the scroll
distance by hiding half the tool, which is the wrong trade: everything must stay reachable
from one screen. The distance problem is solved with navigation and disclosure instead.
A sticky rail carries jump links (styled as plain links behind a "Jump to" label, never as
a segmented control: three padded items in a row with a boxed active state reads as tabs
even when it only scrolls, and that misread is the whole problem) (Do next / Records / Analysis) with a scroll spy, plus the
active filters and the live record count, so any section is one click away and the filter
state is legible from anywhere on the page.

**Analysis sits above the decision list.** Read where the search stands, then act on it.
The order is Analysis, Do next, Records. Microcharts inside the summary cards were tried
and removed: a sparkline that duplicates a chart 400px below it is decoration, and the
full chart is the thing worth reading.

**The funnel is an infographic inside the aging panel.** Trapezoids narrowing left to
right, so the collapse after acknowledgement is a silhouette rather than four numbers to
compare: full height at 338 and 337, then a sliver at 16 and 9. Thin stages keep a
five-pixel minimum band and carry their count above the shape, since a few pixels cannot
hold a label. A transparent full-height hit rect per stage sits under the polygon, or a
5px band is unclickable. It shares the panel with the aging chart, which is what brings
that container up to the size of the others.

Superseded, kept as a record: it was first tried as a full-width ribbon of four cards
under the summary. A funnel reads
left to right, and putting it up top freed the quadrant slot for the decision list, which
now sits beside the charts rather than below them (`.top2`: Analysis left, Do next right).
Stages are equal width with proportional bars underneath; proportional widths render 16
and 9 as slivers next to 338. Each stage also states its own drop, which is where the
story is: -95% from acknowledged to advanced.

**The analysis grid is two explicit columns, not auto-fit.** `auto-fit` with a 430px
minimum silently collapses to one column in an artifact panel around 890px wide, so the
quadrant read as four charts stacked vertically. `repeat(2,minmax(0,1fr))` holds the
quadrant until a genuine mobile breakpoint. The charts are sized for shape, not precision:
the row drill-down is where exact numbers live.

**A folded panel has no width to measure.** Its chart draws at the 300px floor and appears
stretched when opened, so unfolding calls `drawAll()` again.

**Pacing is measured on rolling 7-day windows, never calendar weeks.** The current
calendar week is partial six days out of seven, so comparing it to a finished week always
reports a collapse in output that did not happen. `pace` carries the last 7 days against
the 7 before, for sent, replies and advanced, plus a four-week average as the baseline.
Deltas are signed, because the direction is the whole point.

**Do next is a quadrant cell, not a section.** It sits top-right where the funnel chart
used to be, so the decision list is read alongside the charts rather than after them. To
fit, a row is one line: company in bold, role muted and truncated, fit score right, and
the location plus everything else moves into the drill-down. `#actions` caps its height
and scrolls, sized so the Housekeeping group and its dormant count stay visible without
scrolling. Rows still open in place, inside the panel.

**A Do next item needs a way to be done.** Applying happens in a browser, and the system
only learned about it when the acknowledgement email arrived, so a role stayed on the
decision list after it had been actioned. `careerops apply <id>` records the submission as
a `submitted` event with `source='manual'`; status still derives from the log, so the
invariant holds and the row drops off on its own. The dashboard offers the command rather
than writing the database, because a projection that writes state becomes a second record.

**`reclassify` must only touch `source='gmail'`.** It ran over every event, so the
classifier would re-judge a manually recorded submission from its empty subject line,
demote it to noise, and silently revert the application to a prospect.

**A recent-activity feed must show the source.** Portal-derived and hand-recorded events
carry the date they were written down, not the date anything happened. Fifteen Google
rejections logged from a portal snapshot in one sitting read as a mass rejection that
arrived yesterday. Non-gmail rows are labelled with their source.

**One decision list, and rows open in place.** "Do next" and "Where to apply next" are the
same question, so they are one section. A row expands where it sits; making it filter the
table instead meant clicking a company, being sent down the page, and clicking the same
company again to see anything.

**Sentiment has no section.** It is wanted in exactly two moments, choosing where to apply
and reviewing where you applied, and both are rows. `sentimentHTML`, `trackHTML` and
`fitHTML` render the same three drill-downs into an action row and a table row alike, so
a recommendation carries the score reasoning, the record against that company, and what
employees say, without cross-referencing a separate list. `trackHTML` is what connects a
suggestion to performance: 2 applied, 0 advanced is the context that makes fit 80 mean
something.

**Charts size to their container, and that is a trap.** Each chart measures its panel and
sets its viewBox to that width, so it fills the panel and every label renders at the same
size in every chart. But a grid or flex child defaults to `min-width:auto`, so the SVG's
intrinsic width feeds back into the track and the column grows on every redraw. Pin
`min-width:0` on grid children. A redraw must also clear its host first or resizing
appends a second chart.

**Panels fold to their heading, they do not disappear.** A collapsed panel still shows its
title and its toggle, so nothing leaves the page. Fold state persists in localStorage.

**Filters are deep links.** The filter state serialises to the URL hash
(`#s=prospect&fit=70`), so a view can be bookmarked or sent. `readHash` runs before the
first render and on `hashchange`; it ignores anchors (`#s-analysis`) so the jump rail and
the filter parser never fight over the same hash.

**The action list is grouped and ranked, not flat.** Seven identical rows said everything
was equally urgent, which says nothing is. In play, then Apply next, then Housekeeping;
the ranking score is shown at full size on the right rather than buried in the caption,
and the dormant count is demoted out of the decision list.

**Top-level `const` in the dashboard script shares scope with `window`.** `const top=...`
threw "Identifier 'top' has already been declared" and blanked the whole page, because
`window.top` exists. Same trap waits on `name`, `status`, `length`, `origin`.

**Ordered by what the reader does, not by what the data is.** Four numbered sections:
*Do next* (the action list, first, because it is the only section that asks for a
decision), *Where to apply next* (fit distribution, sentiment: forward looking),
*How the search is performing* (funnel, aging, weekly, by company: backward looking),
then *All records*. Grouping charts by chart type instead put the action list under six
panels and mixed prospective with retrospective, which reads as jumping around.

**Every visible part of a chart filters, including its dead space.** A bar drawn to 3%
of its track leaves 97% of the row looking clickable and doing nothing, which reads as a
broken control rather than a miss. Each row or column binds one handler to its label, its
background track and its fill, and bar charts get a transparent full-height hit rect per
category so a short bar is still an easy target and an empty band is not a dead column.
The action list carries the filter each row stands for; it looked interactive long before
it was. Anything that filters gets the `.hit` class, which is the affordance.

**A chart click must not move the page.** Charts sit far above the table they filter, so
auto-scrolling to the table on every click dragged the reader to the bottom and back for
each one. A sticky bar reports the active filters and the resulting record count in place,
and offers the jump once, on a button.

**Advance rate, not response rate.** 96% "response rate" counted auto-acks and was
meaningless. Advance rate (past an ack ÷ submitted) is ~8% and is the real number.

Filter state lives in one `Fs` object with composable predicates. Watch for falsy
zero: hero card index 0 needs `!== null`, not a truthiness check.

## House style

**Corner radius is a decision, not a default.** One radius stamped on everything is a
recognisable generated-design tell, and so is squaring everything in response. The scale
here has three tiers and a reason for each: structure (panels, tiles, table, chart
tracks) stays square, because right angles are what make it read as an instrument;
controls and inline tokens (pills, chips, buttons, inputs) take `--r-ctl` 3px, because a
soft edge at small size reads as a token rather than a box; glyphs (the info badge, the
chip's remove button) stay circular, because squaring them made them look like broken
buttons. Data fills carry a 2px end so a bar tip reads as a measured value against its
square track.

- **No em dashes anywhere** in generated documents or correspondence. Recast the
  sentence rather than substituting a hyphen.
- Resumes are one page, verified in Word, never shipped with placeholder text.
- Claims must survive a follow-up question. Prefer "1.56 to 0.46 FTE per 100 cases"
  over "cut overhead 70%".

## Commands

```
doctor · validate · init · ingest-csv · sync · reclassify · resolve
discover · fit [--rescore] · prospects · resume <id>
intel [--limit N] [--company X] [--refresh] [--show]
pipeline · why <id> · event · review · stats · analytics · dashboard [--artifact]
```

Tests: `python3 -m unittest discover -s tests -v` (28 tests, guard the state machine).
