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

**A title must name a role.** One gate at the end of `classify` validates whatever any
path produced, because only `role_from_body` ever validated its own capture: the subject
rules and `company-was-actually-role` set titles unchecked, which is how "candidacy for
the", "joining Cloudflare and the time you invested in your application", a bare
"position", and a clause from a sentence about quantum superposition all became job
titles. A title naming no role, or merely repeating the employer, is rejected in favour
of "Unknown role", which is honest and reviewable. Prose is neither.

`resolve.repair_titles()` is the retroactive half, and it runs on every `resolve`. It
renames the role row in place rather than going through `set_identity`, because that
folds colliding rows: nine OpenAI applications share one role row, and folding them into
an existing "Unknown role" row would have deleted eight real applications. Where such a
row already exists at that company the replacement is made unique so nothing merges.

OpenAI is the honest case for admitting ignorance. Its acknowledgement reads "we will
review it for the role you applied to" and names nothing, in nine identical emails, so no
extraction rule could ever recover the title. "Unknown role" is the correct answer there,
not a fallback.

**Never guess.** Below 0.60 confidence, or missing company/role, goes to `review_queue`.
No fabricated fit scores: no JD means no score. `fit.py` returns `None` rather than a
number. Bad data is worse than absent data.

**A fix that reaches only new data leaves stored data permanently wrong.** This shape
recurred four times in one day and is now the first thing to check on any rule change.
Company aliases were applied in `get_or_create_company`, so they stopped the next
duplicate but never folded the row already stored. Comp bands were read only when a role
was first inserted, so every existing role stayed blind. `recruiter_outreach` stopped
creating applications, but the nine it had already created stood. And `reclassify`
excluded transitions to `unresolved`, so a verdict could be corrected but never
retracted, which kept a newsletter filed as an interview for 198 days after the rule
that caught it was fixed. Every corrective rule now ships with the retroactive half:
`resolve.demote_outreach_only()`, alias application inside `merge_companies`, comp
backfill in the existing-role branch, and demotion allowed in `cmd_reclassify`.

**Inbound outreach is a lead, not an application.** `recruiter_outreach` is deliberately
absent from `APPLICATION_EVENTS`. It could previously create a row from nothing at
confidence 0.55 against a 0.50 gate, which is how a Chase credit-card mailer and a state
job-board registration notice each became applications. It also duplicated real ones: two
"A Googler recently referred you!" notes created second rows beside applications already
tracked, and one reported `in_process` over a requisition already rejected. Outreach now
attaches to an application the company already has, preferring a title-compatible one via
`gmail._attach_only`, and otherwise stays unattached and visible under `unassigned`. It
never falls back to "any application at this company": naming a role that matches nothing
returns `None`, because an unattached event is recoverable and a wrongly attached one
silently rewrites history.

**Contact is not advancement.** `EVENT_TO_STATUS["recruiter_outreach"]` is `acked`, rank
1, alongside a plain acknowledgement. Only an invitation, an assessment or an offer moves
an application forward, and the advance-rate query and `POS` agree. Ranking outreach at 2
reported every referral notice and every "thanks for your interest" as an advance.

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

A live interview invitation failed both arms the same way a year later: Headway's
recruiter wrote "Hello From Headway! We'd Love to Chat" from the company's own domain,
which carries no application vocabulary and matches no ATS vendor. Outreach that *opens*
a conversation does not use application language at all, so the subject arm now carries
first-meeting vocabulary (chat, connect, opportunity, role, reaching out) and a third,
unqualified arm reaches the body and the scheduler link. Refetching found 51 events and
16 applications the system had never seen. The lesson generalises: the query has to cover
mail written before the vocabulary of "application" exists.

**Mail about mail is not mail.** Bounces, out-of-office replies, delivery-status
notifications and calendar cancellations all quote the message they concern, so they
inherit its vocabulary. "Undeliverable: EXT: Re: Screening Availability" became an
interview invitation on the word "availability", and "Canceled event: Zoom Interview"
counted an interview a second time in the direction of it not happening. `NOISE_PATTERNS`
now anchors on those prefixes.

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

**A cancelled or filled requisition is a rejection.** Twelve events carried closure
language; six sat as `unresolved`, so real outcomes from Microsoft, Amazon, Adobe,
DoorDash, Box and Cloudflare were never recorded at all. One, NVIDIA, read as forward
progress: "we are reaching out to inform you that we are no longer recruiting" tripped a
recruiter pattern, so a dead req outranked an acknowledgement.

**`reaching out` is ordinary English, not a recruiter signal.** It appears inside closure
mail and rejections. Recruiter outreach needs intent attached: `reaching out about`,
`came across your profile`, `would you be open`. Removing the bare pattern also let three
genuine Stripe interview threads classify correctly, since a weaker type had been
matching first.

**A definitive ack subject blocks every promotion, not just interviews.** The guard
originally covered `interview_invite` and `assessment` only, so `recruiter_outreach`
still trusted the body, and "we will be reaching out to candidates" inside a plain
acknowledgement promoted five of them to `in_process`. That inflated advanced from 11 to
16 and the advance rate from 3% to 5%. Any event type that outranks `acked` has to be
inside the guard; the family of bug repeats every time a new one is added.

**Outcome language inside a hypothetical clause is not an outcome.** Standard
acknowledgements carry "If you are not selected for this position, keep an eye on our jobs
page", and matching "not selected" there turned 14 acks into rejections. Closing a live
thread is the most destructive misread available: the role disappears from Do next and the
record says it is dead, so you never follow up. `_strip_conditionals` removes clauses
opened by if / should / unless / in the event before rejection and offer patterns run.

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

**A title names a job; prose that survives the role patterns does not.** Two bodies cleaned
fine, sat under the 12-word cap in `_clean_role`, and became role rows: "multiple states at
once", from quantum-computing copy, and "joining Cloudflare and the time you invested in your
application", from rejection boilerplate. `ROLE_NOUN` now requires one role or function noun
in the cleaned title. Measured against the corpus it rejects 25 of 481 distinct titles and
loses no real one: twelve are property-listing notifications ("467 Luther Dr has been opened")
that had become job applications, eight are company names sitting in the title column, and the
rest are sentence fragments. Rejecting returns None, which routes the event to `review_queue`
through the missing-role path rather than discarding it.

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

**Store the Gmail threadId, and let it own application identity.** Later messages in one
conversation resolve slightly different titles from their bodies, so "Launch PgM" and
"Launch Program Manager" became separate applications and a single interview loop counted
as several advances. Stripe read as 6 advances against a true 2. An event whose thread
already has an application attaches to it and never re-resolves a role.

**Thread alone is not enough, in either direction.** Gmail also threads on identical
subjects, so two real applications sharing "Thanks for applying to Stripe!" land in one
thread and must not be folded: `merge_threads` clusters by title compatibility inside a
thread and leaves incompatible roles apart. And one-off calendar confirmations get their
own thread, so interview confirmations for an existing loop stay orphaned; those need a
human. Never infer a merge from timestamps alone.

**Widen the refetch predicate whenever a stored field is added.** It only re-fetched when
the body was missing or CSS, so adding `thread_id` left the backfill a silent no-op across
686 events.

**Replay events in time order.** `reidentify` sorts by `occurred_at` so an ack opens a
submission before later events attach to it; unordered replay made a rejection create its
own application ahead of its own ack. `merge_orphan_outcomes` repairs any that slipped.

**Decode HTML entities on every path**, including text/plain. Oracle-hosted ATSes emit
`&nbsp;` inside plain text, which produced roles like "&nbsp;Technical Program Manager"
and split one application into two.

## Classifier: evidence scoring, not first match

`score_types()` scores every candidate event type; `_event_type` takes the winner only if
it clears a floor and beats the runner-up by a margin. First-match-wins treated a bare
word in a body as the same evidence as a phrase in a subject line, and pattern order
decided ties: "Thanks for your interest in Hims & Hers" was filed as
`recruiter_outreach` because the word "recruiter" appeared below the fold and outreach is
checked before ack.

| knob | value | why |
|---|---|---|
| subject match | 1.00 | the strongest position a phrase can occupy |
| body, `rejection` / `offer` | 1.00 | a verdict carries wherever it appears |
| body, `ack` / `interview_invite` / `assessment` | 0.75 | usually true, occasionally boilerplate |
| body, `recruiter_outreach` | 0.55 | "reaching out about" is ordinary English |
| `REJECT_WEAK_FACTOR` | 0.55 | phrases that appear in acknowledgements too |
| corroboration | +0.20 | said in the subject *and* the body |
| `ACK_SUBJECT_DAMPING` | 0.40 | a definitive ack subject cannot be promoted by a body |
| `MIN_VERDICT` / `MIN_MARGIN` | 0.50 / 0.15 | below either, the answer is `unresolved` |

The margin is recorded as `separation`, and it is wired into no gate. An earlier draft of
this section claimed `verdict_strength` had become the margin; it had not. The margin was
captured into a variable and discarded, and `verdict_strength` still answers its own
question, which is how self-contained the winning phrase is rather than how far it beat
the alternatives. The review gate is calibrated on that, and conflating the two is the
mistake this file already records once, when `confidence` and `verdict_strength` were a
single field. The 0.55 that a credit-card mailer, a job-board notice and a genuine
acknowledgement all shared was the sender-domain identity floor, not a verdict score.

Two rules that look like details and are not:

**`ack` is not a rival hypothesis.** Nearly every employer email acknowledges an
application somewhere, and a rejection *is* an acknowledgement plus a verdict. Scoring it
as a competitor made 90 correct rejections `unresolved` on two hundredths of a point. It
is excluded from the margin unless it wins outright.

**`SUBJECT_ONLY` is a fallback tier, not a competitor.** Its fragments are deliberately
weak; scoring them at 0.45 against a 0.50 floor meant they could never win, and thirty
real calendar invitations became `unresolved`. It is consulted only when nothing
separable won, and only when the message contains an employment word anywhere. "Next
steps" and "availability" are ordinary English: a newsletter headed "Next steps after
SCOTUS strikes down tariffs" sat in the funnel as an interview for 198 days.

**A phrase is only definitive when its grammar is.** "Not selected" states an outcome in
"you were not selected" and describes a portal in Microsoft's "Roles you are not selected
for stay visible in the Action Center". Promoting the bare phrase to `REJECT_STRONG`
turned that acknowledgement into a confident rejection, which closes a live application,
and closing a live thread is the most expensive error the system can make. The strong
pattern carries the subject and the past tense; the bare form stays weak. Note the repair
failed once before it worked, because the first version admitted the present tense, which
is precisely the wording that caused the problem.

**A pleasantry is not an invitation.** "Would love to connect" opens a cold sourcing mail
as readily as one proposing a time, so adding it to `interview_invite` stole genuine
outreach. What separates an invitation is the artefact: named dates, a duration, or a
scheduler link.

**Weak rejection evidence is weak.** `REJECT_WEAK` phrases appear in acknowledgements as
readily as rejections. An ack explaining that it focuses on "candidates whose backgrounds
best align", and three that promise to keep your resume on file while describing what
happens next, were all filed as rejections until weak evidence was scored as weak.
Definitive phrases moved the other way: `no longer recruiting` and `not selected` state
the outcome outright and belong in `REJECT_STRONG`.

**English defeats patterns in mundane ways.** Four gaps, each costing a real rejection:
contractions ("we won't be moving forward" defeats `\bnot\b`), the infinitive ("decided
to not move forward" where every pattern had the gerund), the formal register ("we regret
to inform you"), and refusals that never mention moving at all ("we are unable to offer
you an interview"). Add the form, not the instance.

## Finding the decision maker

**The reporting line is usually in the JD.** Employers publish "you will report to the
Director of Business Growth for Experiences" and almost nobody reads it. A title plus a
company name resolves to a person in one search; hunting an unnamed hiring manager does
not, and two Google insiders with internal board access could not produce a usable one.
`reports_to()` extracts it and the drill-down offers a LinkedIn search built from it.

Guard against "people report to you", which is a headcount statement rather than a
reporting line. 30 of the roles in the corpus publish theirs.

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

**The role supersedes the domain.** This rule replaces an earlier one that said the
opposite, and the correction matters. Excluding job families was wrong: `technical
program manager` was excluded wholesale, which blocked *monetization* TPM roles scoring
72. But excluding domains as bare substrings was wrong in the mirror image. `titles` is
already a whitelist of jobs worth taking, so a subject-matter word must not veto a match
against it. "Chief of Staff, Security Customer Engineering" is a chief of staff role
whatever the org is called, and excluding it on `security` discarded a remote
$211,000-$290,500 posting at a company the candidate has a direct prior connection to.

So there are two lists and they behave differently:

- `exclude_titles` names the **job** (`analyst`, `coordinator`, `platform engineer`,
  `site reliability`). These reject outright, wherever the word sits.
- `exclude_domains` names the **subject** (`security`, `infrastructure`, `compute`).
  These are a backstop for titles that match nothing on the whitelist, never an override
  of one that does.

Terms that look like domains but name a job belong in the first list; that is where
`platform engineer`, `privacy engineer`, `embedded` and `firmware` ended up. The role
noun was doing the real work in both directions all along. Splitting the lists took
`excluded_title` from 2,652 to 1,497 in one run and surfaced 30 new roles.

**A filter that removes rows silently is the one to test hardest.** Three of these in one
afternoon, none of which raised an error: the dashboard looked healthy while the roles simply
were not there. Absence is the failure mode of every gate in `discover`, so a gate change
needs a before/after count, not a glance at the output.

**Seniority in the title is a proxy for pay, and a proxy must not outrank the thing it stands
in for.** `_worth_relocating` required a seniority keyword AND coast AND comp. Anthropic posts
$270-310k San Francisco roles titled "GTM Strategy & Operations - AMER Enterprise Tech" with no
director / head / lead / principal / staff / VP anywhere in them, so fourteen roles were
discarded while a lower-paying one whose only difference was the word "Lead" came through. Above
`relocation.comp_override` the published range decides on its own.

**The two comp tests read opposite ends of the band on purpose.** `comp_floor` rejects only when
even the top of the range is too low; `comp_override` grants only when even the bottom clears it.
A $270-310k posting is not a $300k role, it is a role that might pay $270k, and that is the
number to plan a family move against.

**Title matching was a plain substring test, so "program manager" could not match "GTM Programs
Manager, AMER".** Singular and plural now both match, in either direction, via `_kw_pattern`.
Adding the missing keyword would have fixed one role; the matcher fixes the class.

**Comp floor never filters on absence.** Most JDs post no range. Only a *parsed* max
below the floor disqualifies. That tolerance is right, and it is also why a parsing gap
disables the gate silently rather than loudly.

**Hash the whole description, not a prefix of it.** `jd_hash` covered `jd_text[:2000]`,
so a parser change that appended text could not be detected. Widening the Lever reader to
include the requirements lists left every stored Lever role at its old truncated length,
because the first 2,000 characters were identical and the update was skipped: one Chief
of Staff posting sat at 2,278 of its 9,434 characters and had been scored on that
fragment, at 82. Rescored on the whole posting it is 60. Same shape as the refetch
predicate that was not widened when a stored field was added, which made a 686-event
backfill a silent no-op. When a field's *content* can grow, the change detector has to
read all of it.

**Read the board's own comp field before parsing prose for it.** Ashby publishes the band
as structured JSON in `compensation.compensationTiers`, not in the description text. The
request already carried `includeCompensation=true`, so the field was arriving and being
thrown away, and `comp_max` stayed NULL on every Ashby role. One employer went from 0 of
86 postings with a band to 76; across all boards the first corrected run filtered 38
roles as `below_comp` that had been passing unexamined, several of them scoring in the
80s on fit alone. `_ashby_comp()` takes USD annual salary components only: an hourly or
monthly band is not a floor comparison, so it is skipped rather than guessed at.

**Fit and level pull in opposite directions, and nothing corrects for it.** A
well-written description for a role a level below the candidate matches their background
*better*, not worse, because they have done all of it. The two highest-fit roles at one
employer were also its two lowest-paying. `level` is NULL on every row; until something
populates it, comp is the only counterweight, which is why the band must be read
correctly.

**Public board APIs only** (Greenhouse, Ashby, Lever). No LinkedIn/Indeed scraping:
against their terms, brittle, and unnecessary.

## Posting age beats fit

**`roles.posted_at` is the dominant variable in the whole system.** A hiring manager on a
desirable remote role described screening roughly the first 200 of 6,000 applicants and
stopping, and said that is normal. Being early is a ~30x advantage that has nothing to do
with the resume, which is why a 2% cold rate is what applying at random posting ages
returns.

So `prospects` and Do next band by age first and rank by fit inside the band. A 145-day-old
85 (Pinterest) is a worse bet than a 3-day-old 78 (Figma), and any ranking that puts them
in fit order is actively misleading.

All three boards expose it: Greenhouse `first_published`, Ashby `publishedAt`, Lever
`createdAt` in epoch milliseconds. `discovered_at` is not a substitute; it records when we
first looked, not when the employer posted.

Roles with no `posted_at` are usually delisted, which is itself a signal.

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

**Dismissal is the primitive; a cap is one automatic source of it.** Hardcoding employer
application limits does not generalise: they are undocumented, company-specific and change.
So `applications.snoozed_until` is the mechanism ("not now, for any reason"), and a cap is
simply a computed reason to hide something. `careerops snooze <id> --days 30` from the CLI,
offered as a copyable command in the drill-down, same as `apply`.

**A constraint is a hover, not a headline.** The cap first rendered as a red line of text
under every affected row, which shouted at the reader on rows they could do nothing about.
It is now a single inline warning glyph carrying the explanation in a tooltip, using one
delegated `[data-t]` handler shared with the table's rating chips.

**Some employers cap applications per window, and a rejection still burns a slot.**
Headway allows 2 across all roles per 60 days. A soft rejection in July had been sitting
misclassified as an ack, so the system showed one slot used when both were gone, and the
85-scoring Chief of Staff role could not be applied to. `company_policy.<name>.application_limit`
= `{count, days}`; Do next shows used/cap on every row for that company and locks the row
with the reopen date once the cap is hit. Where a cap exists, surface only the single
highest-scoring open role, never a batch.

**Soft rejections carry no rejection language.** "We do not feel that we have the best
match" and "we'll keep your information on file" evade every hard pattern and fall through
to the ack rule, leaving dead applications sitting in the live pipeline indefinitely.

**Conditional language needs stripping wherever a model reads free text, not just in the email
classifier.** `_strip_conditionals` exists because "If you are not selected for this position"
turned 14 acknowledgements into rejections. The same bug then appeared in fit scoring: Anthropic
attaches "For sales roles, the range provided is the role's On Target Earnings (OTE) range" to
every posting regardless of function, and the scorer read that hypothetical as a fact about a
non-quota IC role, called the base salary OTE, and scored an 87 as a 28. `profile.md` now states
the rule explicitly.

**A multi-location posting is judged on the location that qualifies.** The same scoring failed a
San Francisco role because it was also listed in New York, which does not qualify on its own.

**The relocation rule lives in three places and will drift.** `config.json` for the discover
gate, `profile.md` prose for the scorer, and the docstring in `_worth_relocating`. Changing one
produced a state where `discover` admitted a role and `fit` then penalised it for failing the
same test. Change all three together.

**Company policy** (`config.json → company_policy`) injects history into the prompt and
gates surfacing. Location uses `location_verdict()`: `remote` / `colorado` / `ambiguous`
/ `elsewhere`. Ambiguous national postings pass *with a flag*, because under-filtering beats
hiding a reachable role. A named non-CO city makes a posting concrete, not ambiguous.

## Resume generation

**`resume_dir` was read by `serve` and ignored by the CLI**, so the dashboard button wrote to
`~/Desktop/resumes` while the command line wrote into the repo: one operation, two homes,
depending on how it was invoked. `build()` now accepts either a directory or a full path.

**`--verify` is silent when Word blocks on a permission dialog.** The run exits cleanly having
rendered nothing, which reads as success. A resume reported as built with no page count printed
has not been verified.

`resume/master.json` is the single source of truth: 16 tagged bullets, 3 employers.
Do not fork it into variants: that is what produced three divergent `build*.js` files
with contradictory numbers.

`resume.py` ranks bullets deterministically (token/tag overlap: `emphasize` 6× and 4×,
title 3×, JD 1×) so selection is inspectable. Only the tagline and profile paragraph go
through the model; every factual claim comes from the master.

**Send the PDF, not the .docx.** The one-page break depends on Calibri metrics the
recipient may not have, and Greenhouse and Ashby both parse PDF fine. `--verify` already
renders one through Word, so it keeps it beside the .docx instead of deleting it. A
render that fails the page check must delete its PDF: that is the file you would send.

**`--verify` shrinks until Word says one page.** It drops the lowest-ranked bullet and
re-renders, up to three times, because a long bullet displacing a short one silently adds
a line and guessing at wording is slower than removing the weakest claim. A rule enforced
by hand is not enforced.

**`careerops resume <id> --verify` renders through Word and counts pages.** Word is
sandboxed: it prompts for access to unfamiliar directories, and a prompt naming a hidden
folder like `/tmp` hangs the AppleEvent with no visible cause and no error. Write the PDF
beside the .docx. Also send `open` to a document Word already has open and it stalls on
its own dialog; close first, or work on the open document.

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

**Panels in a row are the same height, and earn it.** The grid stretches, so the two list
panels match and both scroll inside whatever height the row settles on. A fixed-ratio
chart cannot stretch without leaving dead space beneath it, so the fix is to give the
shorter chart more drawing height rather than more padding: Weekly went from a 150 to a
214 viewBox to meet By company's ten rows at 335px.

**A chart that restates a hero card is not a chart.** Three were cut for this reason.
Fit distribution reranked what Do next already ranks. The funnel said "6 of 336 advanced"
next to a hero card reading "2%, 6 of 336" that filtered identically. Aging said "197
dormant" beside a card saying "196 dormant", with the per-company breakdown already in By
company. What survives is the four that answer different questions: what just moved, what
to do, how fast you are going, and where to concentrate.

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

**A funnel counts what an application EVER reached, not where it sits now.** Status is
monotonic and rejection outranks interview, so counting current status erased every loop
that ended in a no. Two Walmart interviews in May 2026 disappeared the moment the
rejection was recorded, and the funnel then reported zero advances for the whole year.
`ever_advanced` and `ever_interviewed` are derived from the event log, not from status.

**Advance rate, not response rate.** 96% "response rate" counted auto-acks and was
meaningless. Advance rate (past an ack ÷ submitted) is ~8% and is the real number.

**A scrollable panel needs a scrollbar that reserves layout width.** macOS uses overlay
scrollbars: zero width, hidden at rest, so the panel reads as truncated. Setting
`scrollbar-width` opts into that native path, and Chrome 121+ then ignores
`::-webkit-scrollbar` entirely, so styling it does nothing. Do not set the standard
properties on these panels; the webkit pseudo-elements are what force a classic bar with
its own gutter. Check `offsetWidth - clientWidth`, not the CSS.

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

## Two guards, and neither is sufficient

`careerops corpus` and `python3 -m unittest discover -s tests` cover different things and
a change is not verified until both pass.

The corpus holds language that has actually arrived in this inbox. The unit tests hold
language someone reasoned about, including phrasings that have never arrived but would be
expensive if they did. On the day the classifier was rewritten, three regressions passed
the corpus cleanly and were caught only by the unit suite: none of the 886 stored
messages contained Microsoft's "Roles you are not selected for", and none contained a
cold sourcing mail opening with "would love to connect". A corpus can only defend against
mail you have already received.

The reverse is also true, which is why both exist: the unit tests would not have caught a
newsletter counted as an interview for 198 days, because nobody thought to write that
test. Only the real corpus surfaced it.

## The corpus: 886 labelled emails

`careerops corpus [--export]` re-classifies a frozen set of real messages from their
stored text and diffs the result against a recorded judgement. Every classifier change
before this was checked by hand, one email at a time, which does not scale and cannot
catch what was already wrong.

Three states, because freezing the classifier's output as truth enshrines its bugs:

| state | meaning | a mismatch is |
|---|---|---|
| `verified: false` | not reviewed | *drift*: reported, not a failure |
| `verified: true` | a human decided this label | a **regression**, exit 1 |
| `verified: true, known_bad: true` | decided, and the classifier does not agree yet | expected; a *match* reports **FIXED** |

The third state is what makes it a specification rather than a change-detector: the
answer is written down before the code can produce it.

It has already earned its keep three times. It caught a margin rule that turned 90
correct rejections into `unresolved`, because a rejection is an acknowledgement plus a
verdict and the two scored two hundredths apart. It caught `SUBJECT_ONLY` being scored as
a competitor, which turned thirty real calendar invitations into `unresolved`. And it
caught a label a human had got wrong by accepting the classifier's output at the time,
which is precisely what `verified` exists to prevent.

`corpus/` is gitignored: it holds real subjects, senders and bodies. Only the tooling is
committed. Re-export never overwrites `expect`, `verified`, `known_bad` or `note` on a
reviewed record, or the corpus decays into a mirror of the classifier it checks.

## Refresh order

`refresh` chains the pipeline and the order is load-bearing: **sync, resolve, discover,
fit, dashboard.** Sync first so new acknowledgements attach and statuses settle before
anything reads them; resolve next so duplicates fold before discover adds more; discover
and fit before the dashboard, which is a pure projection and must run last.

## Commands

```
doctor · validate · init · ingest-csv · sync · reclassify · resolve
discover · fit [--rescore] · prospects · resume <id> · corpus [--export]
snooze <id> [--days N | --until DATE | --clear]
intel [--limit N] [--company X] [--refresh] [--show]
pipeline · why <id> · event · review · stats · analytics · dashboard [--artifact]
```

Tests: `python3 -m unittest discover -s tests -v` (94 tests) and `careerops corpus` (886
labelled messages). Run both before and after any change to `classify.py`. See "Two
guards, and neither is sufficient" above for why one passing means nothing.
