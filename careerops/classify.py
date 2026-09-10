"""Resolve raw mail into {company, role, event_type, confidence}.

Deterministic rules first: application mail is highly templated, so most of it
resolves without an LLM. Anything below THRESHOLD goes to the review queue
rather than being guessed at.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

THRESHOLD = 0.60

# Event types that evidence an actual job application. An "unresolved" event never
# creates one: a product notification from a vendor whose name happens to parse
# (AppSheet emailing about an app called "Master Job Application Pipeline") is not
# an application, however confidently the company resolves.
# recruiter_outreach is deliberately absent. Inbound contact is a lead, not proof that
# an application exists, and treating it as proof let a Chase credit-card mailer and a
# Connecting Colorado registration notice each conjure an application. It also duplicated
# real ones: two "A Googler recently referred you!" notes created second rows beside
# applications already tracked, and one of those masked a rejection with in_process.
# Outreach still attaches to an application when the company already has one; it just
# cannot bring one into existence. See _attach_only in gmail.sync.
APPLICATION_EVENTS = {"ack", "rejection", "interview_invite", "assessment", "offer"}
ATTACH_ONLY_EVENTS = {"recruiter_outreach"}
MIN_APPLICATION_CONF = 0.50


def creates_application(c) -> bool:
    return c.event_type in APPLICATION_EVENTS and c.confidence >= MIN_APPLICATION_CONF

# Sender domains that are ATS vendors, not employers. Company must come from the subject.
ATS_DOMAINS = {
    "greenhouse-mail.io", "greenhouse.io", "myworkday.com", "workday.com", "wd1.myworkdayjobs.com",
    "ashbyhq.com", "lever.co", "hire.lever.co", "smartrecruiters.com", "icims.com",
    "taleo.net", "successfactors.com", "jobvite.com", "workablemail.com", "breezy.hr",
    "gem.com", "appreview.gem.com", "avature.net", "teamtailor.com", "recruitee.com",
    "eightfold.ai", "phenompeople.com", "bamboohr.com", "applytojob.com", "rippling.com",
    "ripplingats.com", "dayforcehcm.com", "ultipro.com", "paylocity.com",
    "oraclecloud.com", "myworkdayjobs.com", "jazz.co",
}

# Aggregators/newsletters: never an application event.
AGGREGATOR_DOMAINS = {"ladders.com", "theladders.com", "linkedin.com", "indeed.com",
                      "ziprecruiter.com", "sourcehire.app", "dice.com", "monster.com",
                      # A state job board. Its registration notices open with
                      # "reaching out to you because", same as a recruiter would.
                      "connectingcolorado.gov"}

# Personal/transactional mail. Checked against SUBJECT + SENDER only: ATS footers
# routinely contain "subscription", "payment", "order", so body-scanning them
# silently discards real applications.
BLACKLIST = [
    r"\binvoice\b", r"\bbilling\b", r"\byour bill\b", r"\bstatement\b", r"\breceipt\b",
    r"\border (?:confirmation|shipped)\b", r"\bshipping\b", r"\bdelivered\b",
    r"\bflight\b", r"\bhotel\b", r"\breservation\b", r"\bgfiber\b",
    # Chase sold a credit card with "We're reaching out about your Chase credit card",
    # which is the recruiter_outreach pattern verbatim. Consumer-finance mail is the
    # one category that shares recruiting's opening line.
    r"\bcredit card\b", r"\bdebit card\b", r"\bcard ending\b",
    # A rental listing mails "your application has been declined" in the same words an
    # employer would.
    # A street address where a job title belongs is the tell, and it generalises.
    r"\b\d{1,6}\s+[\w.'-]+(?:\s+[\w.'-]+)?\s+"
    r"(?:dr|drive|st|street|ave|avenue|rd|road|ln|lane|ct|court|blvd|boulevard|way|pl|place)\b",
]

# Mail inviting you to APPLY to something is marketing, not a response to an
# application you made. Anthropic's Fellows blast goes to every research applicant.
SOLICITATION = [
    r"you(?:'|\u2019)?re receiving this because you (?:recently )?applied",
    r"\bnow accepting applications\b", r"\bapply using this link\b",
    r"\bthought you might be interested in\b", r"\bwe wanted to make sure you also knew\b",
    r"\bjobs? you might like\b", r"\brecommended (?:jobs?|roles?) for you\b",
    r"\bapplications? (?:for the \w+ cohort )?close\b",
]

NOISE_PATTERNS = [
    r"^security code\b", r"\bverify your (candidate )?account\b", r"\breset your password\b",
    r"\bconfirm your email\b", r"\bapplication statuses\b", r"\bpassword reset\b",
    r"\bone[- ]time (code|password)\b", r"\bactivate your account\b",
    r"^your\s+\w+\s+account$", r"\bcandidate account\b", r"\bconfirm your account\b",
    # verification codes and post-application surveys are not applications
    r"^candidate verification$", r"\bcode required to complete your form\b",
    r"\byour feedback matters\b", r"\bapplication experience survey\b",
    r"\bprovide feedback on your application\b", r"\bplease rate\b",
    r"\bcomplete your (?:form|profile)\b",
    # Mail about mail. A bounce for a screening request is not a screening request, and
    # an out-of-office reply is not a reply. "Undeliverable: EXT: Re: Screening
    # Availability" became an interview invitation on the word "availability".
    r"^undeliverable\b", r"^out of office\b", r"^automatic reply\b",
    r"\bdelivery status notification\b", r"^auto(?:matic)?[- ]reply\b",
    # A cancellation is not an invitation. The invitation it cancels is already an
    # event of its own, so counting this one too would book the interview twice.
    r"^cancell?ed(?: event)?:", r"^declined:", r"\bhas been cancell?ed and removed\b",
]

# Contract body-shops and job-board drip mail. These use genuine interview language
# ("Interview next week", "Interview Update") but no application was ever submitted,
# so they arrived as interview_invite and put three fake interviews in the funnel.
# Matched against subject AND body, because the tell is always in the body.
STAFFING = [
    r"\bright to represent\b", r"\bRTR\b", r"\bC2C\b", r"\bcorp[- ]to[- ]corp\b",
    r"\bW2\s*(?:or|/|and)\s*C2C\b", r"\bhourly rate\s*[:*]?\s*\$",
    r"\b\d+\+?\s*months?\s+contract\b", r"\burgent requirement\b",
    r"\blet me know your interest\b", r"represent my services as a contractor",
    r"\bimplementation partner\b", r"\bpreferred vendor\b",
]

# Weak signals that appear in ordinary ATS boilerplate ("we'll be in touch about
# next steps"). Only trusted in a SUBJECT line, never in a body.
SUBJECT_ONLY = [
    ("interview_invite", [r"\bnext steps\b", r"\bavailability\b",
                          r"\bwe(?:'| woul)d like to (?:speak|chat|meet|connect)\b",
                          r"\bphone screen\b", r"\binterview\b"]),
    ("assessment",       [r"\bassessment\b"]),
]

# Subject phrases that definitively mark a plain acknowledgement. If one of these
# matches, body text cannot promote the event to an interview.
ACK_SUBJECT = re.compile(
    r"(thank you for (?:your )?appl|thanks? (?:you )?for applying|we(?:'|.)?ve received your appl|"
    r"application received|thank you for your interest|we received your appl)", re.I)

# Order matters: strongest signal wins.
# Rejection language splits by whether the phrase carries the verdict on its own.
#
# STRONG phrases are self-contained: no surrounding sentence turns "we regret to inform"
# into anything but a decline. WEAK phrases only mean rejection in context. They appear
# verbatim inside ordinary acknowledgements describing policy or a hypothetical future
# ("if the job goes inactive, you were not selected"; "we keep resumes on file"), so a
# weak phrase alone is evidence, not a verdict.
REJECT_STRONG = [
    # Soft rejections evade every hard pattern: no "unfortunately", no "other candidates",
    # just a polite decline. Left unmatched they fall through to the ack rule and sit in
    # the pipeline as live applications forever.
    r"\bwe (?:do|did) not feel\b",
    r"\bpursu(?:e|ing) other candidates\b",
    r"\b(?:position|role|requisition|req) (?:has been |was |is )?(?:filled|closed|cancell?ed)\b",
    r"\bwe have (?:filled|closed|cancell?ed)\b",
    r"\bfilled (?:this|the) (?:position|role)\b",
    # "We've made the decision to not move forward at this time" is the infinitive, not
    # the gerund, and matched neither this nor "will not be moving". PandaDoc's
    # rejection survived only on "keep your resume on file", which is weak evidence.
    r"\bnot (?:be )?(?:proceeding|mov(?:e|ing) forward|going forward|continuing)\b",
    # "decided to proceed with other candidates" is as definitive as "decided not to",
    # and matches none of the negated patterns above. One pattern covers the family:
    # proceed/move forward/continue/pursue, in any inflection, with other/another.
    r"\b(?:proceed|mov|continu|pursu)\w*\s+(?:forward\s+)?with\s+(?:other|another|a different)\b",
    r"\bwill not be moving\b", r"\bdecided not to\b",
    # Contractions defeat \bnot\b: "we won't be moving forward at this time" matched
    # nothing and Gusto's rejection was filed as an acknowledgement.
    r"\bwo(?:n\u2019t|n't|nt) be (?:moving|proceeding|continuing|going)\b",
    # And a refusal need not mention moving at all. Addepar wrote "we are unable to
    # offer you an interview for the Product Operations Lead role".
    r"\bunable to (?:offer|proceed|progress|move forward)\b",
    r"\bunfortunately\b", r"\bno longer under consideration\b",
    # "We regret to inform you that you were not selected" is as definitive as English
    # gets, and matched only the weak half, which AMD's ack language then outscored.
    r"\bregret to inform\b",
    # Promoted out of REJECT_WEAK: both state the outcome outright. NVIDIA's "we are no
    # longer recruiting for JR2012976" lost to the "thank you for your interest" that
    # opened the same email.
    r"\bno longer (?:recruiting|hiring|accepting|pursuing|considering)\b",
    # Only definitive with a subject attached. Bare "not selected" describes a UI as
    # readily as an outcome: Microsoft's acknowledgement says "Roles you are not
    # selected for stay visible in the Action Center", and promoting the bare phrase
    # turned that into a confident rejection, which closes a live application. That is
    # the most expensive misread available, so the bare form stays weak.
    # Past tense only. "Roles you ARE not selected for stay visible" is Microsoft
    # describing its portal; "you WERE not selected" is the outcome.
    r"\byou (?:were|have been) not selected\b",
]
# Phrases that appear in acknowledgements as readily as in rejections, so they are
# evidence only in the absence of something better. Anduril's acknowledgement explains
# that it focuses on "candidates whose backgrounds best align" and mentions other
# candidates; Apple's says it will keep your resume on file while telling you what
# happens next. Both were filed as rejections. Scored at REJECT_WEAK_FACTOR below.
REJECT_WEAK = [
    r"\bkeep your (?:information|resume|r\u00e9sum\u00e9|details|profile|application) on file\b",
    r"\bnot (?:a |the )?(?:best|right|strong(?:est)?) (?:match|fit)\b",
    r"\bother candidates\b", r"\bnot selected\b",
]

EVENT_PATTERNS = [
    ("offer",             [r"\bwe(?:'| a)re (?:pleased|excited) to (?:extend|offer)\b", r"\byour offer\b", r"\boffer letter\b"]),
    ("rejection",         REJECT_STRONG + REJECT_WEAK),
    ("interview_invite",  [r"\binvitation to interview\b", r"\binterview invitation\b",
                           r"\bschedule (?:a|your) (?:call|interview|chat)\b",
                           r"\binterview (?:update|availability|request)\b",
                           # Headway's invite read "share some dates and times that work
                           # for you for a 30 min zoom call" and linked an Ashby
                           # scheduler. Nothing above matched, so it fell through to ack
                           # on the pleasantry "your application for" and was filed as an
                           # acknowledgement. An invitation to book time is the least
                           # ambiguous signal in the inbox, and a scheduling link is
                           # close to proof.
                           r"\bdates and times that work\b",
                           # A recruiter proposing a first call writes neither "schedule
                           # a call" nor "invitation to interview". Stripe's read
                           # "Interview Scheduling at Stripe" over "it would be great to
                           # set up time to chat", which scored as a bare acknowledgement
                           # on the pleasantry that opened it.
                           r"\binterview scheduling\b", r"\bscheduling your interview\b",
                           r"\bset up (?:some )?time to (?:chat|talk|speak|connect|meet)\b",
                           r"\bshare (?:some )?(?:dates|times|your availability)\b",
                           r"\b\d{1,2}\s?-?\s?min(?:ute)?s?\s+(?:zoom|phone|video|intro|initial)?\s*(?:call|chat|meeting|conversation)\b",
                           r"(?:calendly\.com|ashbyhq\.com/meeting|savvycal\.com|hubspot\.com/meetings)"]),
                           # "would love to connect" is deliberately NOT here. A cold
                           # recruiter opens with it as readily as someone proposing a
                           # time ("I came across your profile and would love to connect
                           # about an opening"), and treating the pleasantry as the
                           # signal stole those from recruiter_outreach. What separates
                           # an invitation is the concrete artefact: named dates, a
                           # duration, or a scheduler link. Headway's invite still lands
                           # on "dates and times that work" and its Ashby meeting URL.
    ("assessment",        [r"\bonline assessment\b", r"\btake[- ]home\b", r"\bcoding challenge\b",
                           r"\bskills assessment\b", r"\bcomplete (?:an|the) assessment\b"]),
    ("recruiter_outreach",[r"\bsharing your resume\b", r"\brecruiter\b",
                           r"\breaching out (?:to you )?(?:about|regarding|because|as|with)\b",
                           r"\bcame across your (?:profile|background|resume|experience)\b",
                           r"\bwould you be (?:open|interested|available)\b"]),
    ("ack",               [r"\bwe(?:'|.)?ve received your application\b", r"\bapplication received\b",
                           r"\bthank you for (?:your )?appl", r"\bthanks for applying\b",
                           # "Thanks for your interest" is the same sentence as "Thank you for your
                           # interest" and matched neither spelling before, which left a Hims & Hers
                           # acknowledgement with no ack evidence at all.
                           r"\bthanks? (?:you )?for your interest\b", r"\bconfirmation of\b",
                           r"\breceived your application\b", r"\byour application (?:for|to)\b"]),
]

# Subject shapes -> (company, role). Tried in order.
SUBJECT_RULES = [
    (r"we[’'`]?ve received your application for (?P<role>.+?) at (?P<company>.+?)[.!]?$", 0.95),
    (r"thank you for your application to (?P<company>.+?)\s+for\s+(?P<role>.+?)[.!]?$",   0.95),
    (r"application received\s*[-–]\s*(?P<role>.+?) at (?P<company>.+?)[.!]?$",            0.95),
    (r"thanks for applying to the (?P<role>.+?) role at (?P<company>.+?)[.!]?$",          0.95),
    (r"your application for (?:the )?(?P<role>.+?) at (?P<company>.+?)[.!]?$",            0.92),
    (r"(?P<company>.+?) invitation to interview for (?P<role>.+?)[.!]?$",                 0.90),
    (r"^thank(?:s| you)?(?:\s+you)?\s+for\s+apply(?:ing)?\s+(?:to|at)\s+(?P<company>.+?)[.!]?$", 0.88),
    (r"(?:recent )?application (?:to|with)\s+(?P<company>[A-Z][\w&.'\- ]{1,30})[.!]?$",           0.82),
    (r"^following up on your .*application (?:to|with)\s+(?P<company>.+?)[.!]?$",                0.82),
    (r"^thank\s+you\s+from\s+(?P<company>.+?)[.!]?$",                                    0.80),
    (r"thank you for your interest in joining (?P<company>.+?)[.!]?$",                    0.80),
    (r"thank you for your interest in (?P<company>.+?)[.!]?$",                            0.75),
    (r"^(?P<role>.+?) position at (?P<company>.+?)[.!]?$",                                0.85),
    (r"^(?P<company>[A-Z][\w&.'\- ]{1,24}):\s*application (?:update|status)\s*\|\s*(?P<role>.+?)$", 0.90),
    (r"your application for\s+(?P<role>.+?)[.!]?$",                                       0.55),
]

# Many ATS acks put the company in the subject and the role only in the body
# ("Thanks for applying to Stripe!" / "...your application for the Program Manager,
# GTM Planning role!"). Without these, every such ack collapses onto one application.
BODY_ROLE_RULES = [
    # "the position/role of X"  (Oracle, Akamai, Amazon)
    r"(?:position|role) of (?P<role>[^!?\n]{4,130}?)\s*(?:\(|,\s*(?:req|job)|[.!]|$)",
    # "apply/applied/applying for the X role|position|opportunity"  (Engine, Adobe, DeepMind, DoorDash)
    r"appl(?:y|ied|ying|ication)\b[^.!?\n]{0,24}?\bfor (?:the |our |a )?(?P<role>[^!?\n]{4,130}?)\s*(?:role|position|opportunity|opening)\b",
    # "consider you for the X" / "we've filled the X role" / "no longer recruiting for the X role"
    r"consider(?:ing)? you for (?:the |our )?(?P<role>[^!?\n]{4,130}?)\s*(?:role|position|[.!]|$)",
    r"(?:filled|closed) the (?P<role>[^!?\n]{4,130}?)\s*(?:role|position)\b",
    r"(?:recruiting|hiring) for (?:the |our )?(?P<role>[^!?\n]{4,130}?)\s*(?:role|position)\b",
    r"information for our (?P<role>[^!?\n]{4,130}?)\s*(?:role|position|opening)\b",
    # "Role: X   Location: ..." (staffing agencies)
    r"\brole\s*:\s*(?P<role>[^!?\n]{4,130}?)\s*(?:location|$)",
    # "submitting your availability for the X position" (interview scheduling)
    r"availability for (?:the |our )?(?P<role>[^!?\n]{4,130}?)\s*(?:role|position)\b",
    # "confirm the receipt of your application for the X at Company"
    r"receipt of your application for (?:the |our )?(?P<role>[^!?\n]{4,130}?)\s+at\s+[A-Z]",
    # "considering the<no space>Program Manager - X position" (broken ATS templates)
    r"consider(?:ing)? the\s*(?P<role>[^!?\n]{4,130}?)\s*(?:role|position)\b",
    # "invited you to apply for the X role" (Google referral acks/rejections)
    r"(?:invited you to apply for|application (?:for|to)) (?:the |our )?(?P<role>[^!?\n]{4,130}?)\s*(?:role|position)\b",
    # "...for the X (ID: 3186267) position" / "interest in X (ID: 3186267)" (Amazon)
    r"application for (?:the |our )?(?P<role>[^!?\n]{4,130}?)\s*\(ID:\s*\d+\)",
    r"interest in (?:the |our )?(?P<role>[^!?\n]{4,130}?)\s*\(ID:\s*\d+\)",
    r"submitt(?:ed|ing) your application for (?:the |our )?(?P<role>[^!?\n]{4,130}?)\s*(?:role|position|opening|req)\b",
    r"your application (?:for|to) (?:the |our )?(?P<role>[^!?\n]{4,130}?)\s*(?:role|position|opening)\b",
    r"appl(?:ying|ication) (?:for|to) (?:the |our )?(?P<role>[^!?\n]{4,130}?)\s*(?:role|position|opening)\b",
    r"interest in (?:the |our )?(?P<role>[^!?\n]{4,130}?)\s*(?:role|position|opening)\b",
    r"considered for (?:the |our )?(?P<role>[^!?\n]{4,130}?)\s*(?:role|position)\b",
    r"the (?P<role>[^!?\n]{4,130}?)\s*(?:role|position) (?:at|with)\s",
    r"application for (?:the |our )?(?P<role>[^!?\n]{4,130}?)\s*(?:\(|,\s*(?:req|job)|$)",
]

ROLE_JUNK = re.compile(r"^(a|an|this|that|any|your|our|the|open|new|same|following)$", re.I)


SENTENCE_BREAK = re.compile(r"(?<=\w{4})\.\s+(?=[A-Z])")   # "Services. We" breaks; "Sr. Program" does not


# A title names a job; prose that survives the patterns above does not. Both of these
# cleaned fine and sat under the 12-word cap, and became role rows: "multiple states at
# once" (from quantum-computing copy) and "joining Cloudflare and the time you invested
# in your application" (from rejection boilerplate). Requiring one role or function noun
# rejects 26 of 481 titles in the corpus, of which exactly one is a real title.
#
# Returning None sends the event to review_queue via the missing-role path, which is the
# house rule: below the confidence floor or missing a role, a human looks. Never guess.
ROLE_NOUN = re.compile(
    r"\b(manager|mgr|director|lead|leader|principal|staff|head|chief|officer|vp|president|"
    r"analyst|engineer|specialist|coordinator|architect|strategist|consultant|associate|"
    r"scientist|designer|developer|administrator|technician|advisor|adviser|counsel|"
    r"partner|partnerships|recruiter|controller|accountant|planner|producer|editor|"
    r"operations|ops|strategy|strategic|program|programme|programs|projects|product|"
    r"marketing|sales|finance|revenue|data|business|technical|solutions|success|intern|"
    r"fellow|apprentice|generalist|governance|planning|excellence|enablement|integration)\b",
    re.I)


def role_from_body(body: str) -> Optional[str]:
    """Pull a job title out of an ack body. Deterministic; no model call."""
    if not body:
        return None
    text = re.sub(r"\s+", " ", body[:2500])
    for rx in BODY_ROLE_RULES:
        m = re.search(rx, text, re.I)
        if not m:
            continue
        r = (m.group("role") or "").strip(" \t,-–—:;\"'")
        r = SENTENCE_BREAK.split(r)[0]                        # never run past a sentence end
        if re.search(r"\b(unsubscribe|privacy|cookie|http)\b|@", r, re.I):
            continue
        # Clean first: trailing city lists and req ids push a good title past any
        # word cap, so validating the raw capture throws away real matches.
        r = _clean_role(r)
        if not r or ROLE_JUNK.match(r):
            continue
        if not ROLE_NOUN.search(r):        # prose, not a title: let a human look
            continue
        return r
    return None


ROLE_WORDS_IN_COMPANY = re.compile(
    r"\b(manager|director|lead|principal|analyst|engineer|specialist|coordinator|"
    r"architect|strategist|consultant|associate|officer|program|operations)\b", re.I)

COMPANY_NOISE = re.compile(
    r"\b(hiring team|talent acquisition|recruiting|recruitment|careers|team|the)\b", re.I)


@dataclass
class Classification:
    company: Optional[str] = None
    role: Optional[str] = None
    event_type: str = "unresolved"
    trigger: Optional[str] = None      # literal phrase that fired the classification
    confidence: float = 0.0            # how sure we are of company + role, NOT of the verdict
    verdict_strength: float = 1.0      # how sure we are of event_type, on its own evidence
    separation: float = 0.0            # margin the winning verdict beat the runner-up by
    held: bool = False                 # verdict recorded but withheld from status derivation
    held_reason: Optional[str] = None
    reasons: list = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return self.event_type != "noise" and (
            self.held or self.confidence < THRESHOLD or not self.company or not self.role)


VENDOR_NAMES = re.compile(r"^(greenhouse|lever|workday|ashby|smartrecruiters|icims|jobvite|"
                          r"taleo|no[- ]?reply|do[- ]?not[- ]?reply|notifications?|careers?|"
                          r"talent|recruiting|hr|team|jobs|"
                          # Workday signs its mail "AutoNotification workday", which is a
                          # product name, not an employer. It reached the board as a
                          # company called "AutoNotification workday" holding two real
                          # Autodesk rejections.
                          r"auto[- ]?notifications?(?:\s+workday)?|workday\s+auto[- ]?notifications?|"
                          r"my[- ]?workday|talent\s+acquisition)$", re.I)


def company_from_ats_address(sender: str) -> "Optional[str]":
    """Workday hosts every employer on one domain and puts the tenant in the local part:
    autodesk@myworkday.com is Autodesk. When the display name is the vendor's own, that
    local part is the only place the employer appears."""
    m = re.search(r"<?([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+)>?\s*$", sender or "")
    if not m:
        return None
    local, dom = m.group(1).lower(), m.group(2).lower()
    if not (dom.endswith("myworkday.com") or dom.endswith("myworkdayjobs.com")):
        return None
    local = re.sub(r"[._-]*(careers?|jobs|recruiting|talent|hr|noreply|no-reply|notifications?)[._-]*",
                   "", local)
    if len(local) < 3 or local in ("info", "mail", "auto", "admin", "system"):
        return None
    return local.replace(".", " ").replace("_", " ").title()


def sender_name(sender: str) -> Optional[str]:
    """The company often sits in the From display name even when the domain is the ATS
    vendor: 'Match Group <no-reply@hire.lever.co>'. Ignoring it throws away the most
    reliable company signal in the message."""
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', sender or "")
    if not m:
        return None
    name = re.sub(r"\s+", " ", m.group(1)).strip(" -|,")
    name = re.sub(r"\s*\b(careers?|talent acquisition|recruiting|talent|hiring|hr|team|jobs)\b\s*$",
                  "", name, flags=re.I).strip()
    if not name or VENDOR_NAMES.match(name) or len(name) > 40 or "@" in name:
        return None
    return name


def _user_identity():
    import json as _j, pathlib as _p
    global _UID
    try:
        _UID
    except NameError:
        try:
            cfg=_j.loads((_p.Path(__file__).resolve().parent.parent/"config.json").read_text())
            u=cfg.get("user_identity", {})
            _UID=([e.lower() for e in u.get("emails",[])], [n.lower() for n in u.get("names",[])])
        except Exception:
            _UID=([], [])
    return _UID


def _is_self(sender: str) -> bool:
    emails, names = _user_identity()
    s=(sender or "").lower()
    return any(e in s for e in emails) or any(s.startswith(n) or f"<{n}" in s for n in names)


def _domain(sender: str) -> str:
    m = re.search(r"@([\w.-]+)", sender or "")
    return m.group(1).lower() if m else ""


def _in(dom: str, domains: set) -> bool:
    """Suffix match: us.greenhouse-mail.io is greenhouse-mail.io."""
    return any(dom == d or dom.endswith("." + d) for d in domains)


def _clean_company(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = re.sub(r"[!.,\s]+$", "", s.strip())
    s = COMPANY_NOISE.sub("", s).strip(" -–|")
    s = re.sub(r"\s{2,}", " ", s)
    if re.fullmatch(r"confidential|undisclosed|stealth", s or "", flags=re.I):
        return None          # aggregator placeholder, not a company
    return s or None


def strip_company_suffix(title: Optional[str], company: Optional[str] = None) -> Optional[str]:
    """Drop a trailing "at <Company>" from a role title.

    "Senior Principal Program Manager, GTM PMO at Autodesk" is a role and a company in
    one string. The company belongs in its own column, and leaving it on the title
    splits one role into two rows the moment a message names it without the suffix.

    Only a company we can actually name is stripped. A general "at <Capitalized Words>"
    rule looks tempting and quietly eats real titles: "Analytics at Scale", "Trust at
    Work", "Data at Rest" all end in something that parses as a company and is not one.
    """
    if not (title and company):
        return title
    m = re.search(r"\s+(?:at|@|with)\s+" + re.escape(company) + r"\s*$", title, re.I)
    return title[:m.start()].strip() if m else title


# "the Strategy and Operations position at Maybell Quantum Industries" is one phrase an
# acknowledgement uses, and strip_company_suffix cannot remove the tail because the legal
# name in the sentence ("... Industries") is longer than the company as resolved. Cut at
# the connective instead of trying to match the employer.
ROLE_TAIL = re.compile(r"\s+(?:position|role|opening|req(?:uisition)?|opportunity|job)\b"
                       r"(?:\s+(?:at|with|for|in)\b.*)?$", re.I)
ROLE_LEAD = re.compile(r"^(?:open|the|our|a|an|this)\s+", re.I)


def _clean_role(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    import html as _html
    s = _html.unescape(_html.unescape(s)).replace("\xa0", " ")
    s = re.sub(r"&[a-z]+;|&#\d+;", " ", s)                 # any entity that survived
    s = re.sub(r"^\s*\[[^\]]{1,30}\]\s*", "", s.strip())      # "[Pipeline] Product Manager"
    s = ROLE_LEAD.sub("", s.strip())                          # "open Staff, Technology Operations"
    s = ROLE_TAIL.sub("", s.strip())                          # "... position at <employer>"
    s = re.sub(r"\s*\(open\)\s*$", "", s.strip(), flags=re.I)
    s = re.sub(r"\s*\((?:ID|Job ID|Req(?:uisition)? ID)[:\s#]*[\w-]+\)", "", s, flags=re.I)
    s = re.sub(r"^(?:R|JR|REQ|JOB)[-_ ]?\d{4,}\s+", "", s, flags=re.I)      # leading req id
    s = re.sub(r"\s*[-–]\s*\d{3,}\s*$", "", s)                            # trailing req id
    s = re.sub(r"\s*[-–]\s*(?:[A-Z][\w.]*(?:,\s*)?){1,6}\s+or\s+[A-Z][\w.]*\s*$", "", s)  # trailing city list
    s = re.sub(r"^\s*(?:the|your|our|a|an)\s+", "", s, flags=re.I)
    s = re.sub(r"^\s*\d+WD\d+\s+", "", s)            # Workday req ids
    s = re.sub(r"\s+(?:role|position|opening|opportunity)\s*$", "", s, flags=re.I)
    s = re.sub(r"[<>{}\[\]|]", " ", s)                 # stray markup from HTML bodies
    s = re.sub(r"\s{2,}", " ", s)
    s = re.sub(r"[!.,\s\-–—:;]+$", "", s).strip()
    if not s or len(s) < 4 or not re.search(r"[A-Za-z]{3}", s):
        return None
    if len(s.split()) > 12:
        return None
    return s


CONDITIONAL = re.compile(
    r"\b(?:if|should|unless|in the event|in case)\b[^.!?;]*", re.I)


def _strip_conditionals(text: str) -> str:
    """Remove hypothetical clauses before matching outcome language.

    Standard acknowledgements carry "If you are not selected for this position, keep an
    eye on our jobs page". Matching "not selected" there turns an ack into a rejection and
    closes a live application, which is the most destructive misread available: it hides
    the role from Do next and marks the thread dead.

    The clause runs to the end of its sentence, not a fixed character budget. A cap cuts
    long conditionals mid-word and leaves the tail matchable: Microsoft's ack says "If you
    see the job moved to an inactive state, that means the position is either no longer
    open, you withdrew from consideration, or you were not selected for the role", and a
    120-character window left "...or you were not selected for the role" behind.

    Losing a real rejection whose outcome shares a sentence with a conditional is the
    accepted trade. A missed rejection leaves a dead row on the board; a false one deletes
    a live opportunity.

    Whitespace is collapsed first, because email bodies are hard-wrapped and a line break
    lands mid-clause constantly. Excluding newlines from the clause was an attempt to stop
    a runaway match in a body with no punctuation, and it recreated the exact bug it
    replaced: Google's referral mail wrapped "if you haven't heard\r\nfrom us in eight
    weeks... we likely proceeded with other candidates", the strip stopped at the break,
    and the surviving tail read as a rejection of a live application. The outcome patterns
    already match across line breaks, so the stripper has to as well.
    """
    return CONDITIONAL.sub(" ", re.sub(r"\s+", " ", text or ""))


# Verdicts that close an application. Their failure is asymmetric: a false close deletes a
# live opportunity and hides it from Do next, while a missed one leaves a stale row that
# costs a glance. They are the only verdicts worth withholding.
CLOSING = ("rejection", "offer")
WEAK_VERDICT = 0.5


def _verdict_strength(etype: str, subject: str, body: str) -> float:
    """How far the event type is supported by its own evidence.

    This is deliberately not `confidence`, which scores company and role extraction. The
    two were conflated, and the review gate read the wrong one: Adobe's correct rejection
    scored 0.0 because its subject named no role, while Microsoft's false rejection scored
    0.55 because its subject named one cleanly. A verdict now answers for itself.
    """
    if etype not in CLOSING:
        return 1.0                      # promotions and acks are cheap to get wrong
    if etype == "offer":
        return 0.9                      # every offer pattern is self-contained
    text = _strip_conditionals(f"{subject} {body}".lower())
    if any(re.search(p, text) for p in REJECT_STRONG):
        return 0.9
    return 0.4                          # a weak fragment, and nothing else


# Recruiting mail ends in boilerplate that is about the company, not the candidate:
# EEO statements, accessibility notices, privacy policies, unsubscribe links. Google's
# accommodation footer offers to "schedule a call with a specialist", which read as an
# interview invitation on a referral-routing email. Nothing after these markers concerns
# this application, so nothing after them should be able to classify it.
BOILERPLATE = re.compile(
    r"\b(?:equal opportunity employ|disability accommodation|employ-?ability|"
    r"reasonable accommodation|unsubscribe|privacy policy|confidentiality notice|"
    r"this e-?mail and any attachments|do not reply to this )", re.I)
BOILERPLATE_FLOOR = 200          # never gut a short message


def strip_boilerplate(body: str) -> str:
    """Cut a message at its first footer marker."""
    m = BOILERPLATE.search(body or "")
    return body[:m.start()] if (m and m.start() >= BOILERPLATE_FLOOR) else (body or "")


# ── layer 3: evidence scoring ────────────────────────────────────────────────
# First-match-wins treated a bare word in a body as the same evidence as a phrase in a
# subject line. "Thanks for your interest in Hims & Hers" lost to recruiter_outreach
# because the word "recruiter" appeared somewhere below the fold, and outreach is
# checked first. Score every candidate instead, weight the evidence by where it appeared
# and how far that location can be trusted for that verdict, and require the winner to
# beat the runner-up by a margin. Anything closer is a guess, and a guess belongs in the
# review queue rather than in an application's history.
BODY_TRUST = {
    "rejection": 1.00, "offer": 1.00,        # a verdict carries wherever it appears
    "ack": 0.75, "interview_invite": 0.75, "assessment": 0.75,
    # Referral-routing mail carries its only evidence in the body ("A Googler recently
    # referred you!" names nothing in its subject), so this has to clear MIN_VERDICT on
    # its own. What disqualifies Chase is the corroboration test below, not this weight.
    "recruiter_outreach": 0.55,
}
REJECT_WEAK_FACTOR = 0.55                    # weak phrases are evidence, not a verdict
SUBJECT_WEIGHT = 1.00
SUBJECT_ONLY_WEIGHT = 0.45                   # weak fragments, subject line only
CORROBORATION_BONUS = 0.20                   # said in the subject and again in the body
ACK_SUBJECT_DAMPING = 0.40                   # an ack subject cannot be promoted by a body
MIN_VERDICT = 0.50
MIN_MARGIN = 0.15

# A second, independent sign the message concerns employment at all. "Reaching out
# about" is ordinary English, and Chase used it to sell a credit card. An outreach
# verdict resting on one generic phrase, with nothing else job-related anywhere in the
# message, is not a verdict.
RECRUITING_CONTEXT = re.compile(
    r"\b(roles?|positions?|opportunit(?:y|ies)|opening|candidate|hiring|recruit\w*|"
    r"r\u00e9sum\u00e9|resume|cv|jobs?|careers?|interview|compensation|salary|"
    r"headhunter|talent)\b", re.I)

# Ties fall back to the old precedence: offer beats rejection beats invite, and so on
# down EVENT_PATTERNS. Small enough never to overturn real evidence.
_ORDER = {t: (len(EVENT_PATTERNS) - i) * 0.001 for i, (t, _) in enumerate(EVENT_PATTERNS)}
_WEAK_EVIDENCE = frozenset(REJECT_WEAK)


def score_types(subject: str, body: str = "") -> dict:
    """Every candidate event type with the weight of the evidence behind it."""
    subj_low = (subject or "").lower()
    both_low = _strip_conditionals(f"{subject} {body}".lower())
    ack_subject = bool(ACK_SUBJECT.search(subject or ""))
    scores, lits = {}, {}

    for etype, pats in EVENT_PATTERNS:
        best, lit, in_subj, in_body = 0.0, None, False, False
        damp = (ACK_SUBJECT_DAMPING
                if ack_subject and etype in ("interview_invite", "assessment",
                                             "recruiter_outreach") else 1.0)
        for p in pats:
            weak = REJECT_WEAK_FACTOR if p in _WEAK_EVIDENCE else 1.0
            m = re.search(p, subj_low)
            if m:
                in_subj = True
                w = SUBJECT_WEIGHT * weak * (damp if etype == "recruiter_outreach" else 1.0)
                if w > best:
                    best, lit = w, m.group(0).strip()
            m = re.search(p, both_low)
            if m:
                in_body = True
                w = BODY_TRUST.get(etype, 0.60) * weak * damp
                if w > best:
                    best, lit = w, m.group(0).strip()
        if best:
            if in_subj and in_body:
                best = min(1.0, best + CORROBORATION_BONUS)
            scores[etype], lits[etype] = best, lit

    if "recruiter_outreach" in scores:
        # Look for the corroborating signal anywhere except the phrase that fired.
        rest = both_low.replace((lits.get("recruiter_outreach") or "").lower(), " ", 1)
        if not RECRUITING_CONTEXT.search(rest):
            scores["recruiter_outreach"] *= 0.40
    return {t: (v + _ORDER.get(t, 0.0), lits[t]) for t, v in scores.items()}


def _subject_only(subject: str, body: str = "") -> "tuple":
    """Weak subject fragments, trusted only if the message concerns employment at all.

    "Next steps" and "availability" are ordinary English. Fortune's newsletter "Next
    steps after SCOTUS strikes down tariffs" became an interview invitation on the first
    and sat in the funnel for 198 days; a Walmart bounce, "Undeliverable: EXT: Re:
    Screening Availability", became one on the second. Neither message contains a single
    employment word. Requiring one costs nothing on real mail, which is saturated with
    them, and removes the whole class.
    """
    subj_low = (subject or "").lower()
    for etype, pats in SUBJECT_ONLY:
        for p in pats:
            m = re.search(p, subj_low)
            if m:
                if not RECRUITING_CONTEXT.search(f"{subject} {body}"):
                    return "unresolved", None, 0.0
                return etype, m.group(0).strip(), SUBJECT_ONLY_WEIGHT
    return "unresolved", None, 0.0


def _event_type(subject: str, body: str = "") -> "tuple":
    """Return (type, literal matched text, strength).

    Strength is the winner's margin over the runner-up, so it reports how separable the
    verdict actually was. It used to be a per-rule constant, which is why a Chase
    mailer, a state job-board notice and a genuine acknowledgement all scored 0.55.
    """
    scored = score_types(subject, body)
    if not scored:
        return _subject_only(subject, body)
    ranked = sorted(scored.items(), key=lambda kv: -kv[1][0])
    top, (tv, lit) = ranked[0]
    # ack is not a rival hypothesis. Nearly every message from an employer acknowledges
    # an application somewhere in it, and a rejection is an acknowledgement plus a
    # verdict. Scoring it as a competitor made 90 correct rejections unresolved, because
    # "Thank you for your interest" sat two hundredths below "we will not be moving
    # forward". Only verdicts that genuinely exclude one another need separating.
    rivals = [v for t, (v, _) in ranked[1:] if not (top != "ack" and t == "ack")]
    second = rivals[0] if rivals else 0.0
    if tv < MIN_VERDICT or (tv - second) < MIN_MARGIN:
        # SUBJECT_ONLY is a fallback tier, not a rival. Its fragments are weak by
        # construction and were never meant to compete: scoring them at 0.45 against a
        # 0.50 floor turned thirty real calendar invitations ("Invitation: Interview
        # with Included Health") into unresolved. Reach for them only when the scored
        # evidence produced no separable verdict, which is what the old order did.
        etype, elit = _subject_only(subject, body)[:2]
        if etype != "unresolved":
            return etype, elit, SUBJECT_ONLY_WEIGHT
        return "unresolved", lit, round(max(0.0, tv - second), 2)
    return top, lit, round(min(1.0, tv), 2)


def classify(subject: str, sender: str = "", body: str = "") -> Classification:
    subject = (subject or "").strip()
    low = subject.lower()
    c = Classification()
    dom = _domain(sender)

    # mail you sent yourself is not an inbound application event
    if _is_self(sender):
        c.event_type, c.confidence = "noise", 0.95
        c.reasons.append("sent-by-user")
        return c

    blob = f"{subject} {(body or '')[:900]}".lower()
    for p in STAFFING:
        if re.search(p, blob, re.I):
            c.event_type, c.confidence = "noise", 0.92
            c.reasons.append("staffing:" + p[:34])
            return c
    for p in SOLICITATION:
        if re.search(p, blob):
            c.event_type, c.confidence = "noise", 0.92
            c.reasons.append("solicitation:" + p[:34])
            return c

    scope = f"{subject} {sender}".lower()
    for p in BLACKLIST:
        if re.search(p, scope):
            c.event_type, c.confidence = "noise", 0.95
            c.reasons.append("blacklist:" + p)
            return c

    for p in NOISE_PATTERNS:
        if re.search(p, low):
            c.event_type, c.confidence = "noise", 0.98
            c.reasons.append("noise:" + p)
            return c
    if _in(dom, AGGREGATOR_DOMAINS) and not re.search(r"application for", low):
        c.event_type, c.confidence = "noise", 0.90
        c.reasons.append("aggregator:" + dom)
        return c

    etype, trigger, separation = _event_type(subject, strip_boilerplate(body or ""))
    c.event_type = etype
    c.trigger = trigger
    if trigger:
        c.reasons.append('matched "%s"' % trigger)

    best = 0.0
    for rx, conf in SUBJECT_RULES:
        m = re.search(rx, subject, re.I)
        if m and conf > best:
            g = m.groupdict()
            c.company = _clean_company(g.get("company")) or c.company
            c.role = _clean_role(g.get("role")) or c.role
            best = conf
            c.reasons.append("subject:" + rx[:38])

    # Bare-company subjects like "Databricks!" or "Wiz!"
    if (not c.company and re.fullmatch(r"[A-Z][\w&.\- ]{1,28}!?", subject)
            and not re.search(r"\b(for|to|your|our|the|thank|thanks|apply|applying|received|we|"
                              r"interview|confirmation|update|status|next steps|application)\b",
                              subject, re.I)):
        c.company = _clean_company(subject)
        best = max(best, 0.50)
        c.reasons.append("bare-company-subject")
        if c.event_type == "unresolved":
            c.event_type = "ack"

    # Sender domain is only trustworthy when it isn't an ATS vendor.
    if not c.company and dom and not _in(dom, ATS_DOMAINS) and not _in(dom, AGGREGATOR_DOMAINS):
        guess = dom.split(".")[-2] if dom.count(".") >= 1 else dom
        if guess not in {"gmail", "google", "us", "otp", "mail"}:
            c.company = guess.replace("-", " ").title()
            best = max(best, 0.55)
            c.reasons.append("sender-domain:" + dom)

    if c.company and ROLE_WORDS_IN_COMPANY.search(c.company) and not c.role:
        c.role, c.company = _clean_role(c.company), None      # subject named the role, not the company
        c.reasons.append("company-was-actually-role")

    # the From display name is the most reliable company signal when present
    if not c.company:
        sn = sender_name(sender)
        if sn:
            c.company = _clean_company(sn)
            best = max(best, 0.85)
            c.reasons.append("sender-display-name")
    if not c.company:
        ats = company_from_ats_address(sender)
        if ats:
            c.company = _clean_company(ats)
            best = max(best, 0.80)
            c.reasons.append("ats-tenant-address")

    if not c.role and body:
        r = role_from_body(body)
        if r:
            c.role = _clean_role(r)
            c.reasons.append("role-from-body")
            best = max(best, 0.80 if c.company else 0.55)

    c.confidence = round(min(best, 0.99), 2) if c.event_type != "unresolved" else round(best * 0.5, 2)

    # A closing verdict resting on a weak fragment, inside a mail whose subject is a plain
    # acknowledgement, is the shape of Microsoft's confirmation email. Record it, but do
    # not let it close the row: the event stands as evidence and goes to review instead.
    c.role = strip_company_suffix(c.role, c.company)
    # One gate for every path that can set a role. role_from_body validates its own
    # capture; the subject rules and company-was-actually-role never did, which is how
    # "candidacy for the", "joining Cloudflare and the time you invested in your
    # application", a bare "position", and a clause from a sentence about quantum
    # superposition ("particles can exist in a superposition of multiple states at once")
    # all became job titles. A title naming no role, and a title that merely repeats the
    # employer, are both worse than admitting the role is unknown: "Unknown role" is
    # honest and shows up in review, prose is neither.
    if c.role:
        _r = c.role.strip()
        _bad = (not ROLE_NOUN.search(_r)
                or (c.company and _r.lower() == c.company.lower())
                or len(_r.split()) > 12)
        if _bad:
            c.reasons.append(f'role-rejected:"{_r[:44]}"')
            c.role = None
    # Separation is recorded, not wired into any gate. It reports how separable the
    # verdict was, which verdict_strength does not: that answers a different question
    # (how self-contained the winning phrase is) and the review gate is calibrated on
    # it. Two numbers, two jobs, and conflating them is the mistake this file already
    # made once when confidence and verdict_strength were the same field.
    c.separation = separation
    c.verdict_strength = _verdict_strength(c.event_type, subject, body or "")
    if (c.event_type in CLOSING and c.verdict_strength < WEAK_VERDICT
            and ACK_SUBJECT.search(subject)):
        c.held = True
        c.held_reason = ("closing verdict on weak evidence under an acknowledgement subject"
                         + (f': "{c.trigger}"' if c.trigger else ""))
        c.reasons.append("held:weak-verdict-under-ack-subject")
    return c
