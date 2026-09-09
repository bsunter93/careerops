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
                      "ziprecruiter.com", "sourcehire.app", "dice.com", "monster.com"}

# Personal/transactional mail. Checked against SUBJECT + SENDER only: ATS footers
# routinely contain "subscription", "payment", "order", so body-scanning them
# silently discards real applications.
BLACKLIST = [
    r"\binvoice\b", r"\bbilling\b", r"\byour bill\b", r"\bstatement\b", r"\breceipt\b",
    r"\border (?:confirmation|shipped)\b", r"\bshipping\b", r"\bdelivered\b",
    r"\bflight\b", r"\bhotel\b", r"\breservation\b", r"\bgfiber\b",
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
    r"(thank you for (?:your )?appl|thanks for applying|we(?:'|.)?ve received your appl|"
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
    r"\bnot (?:be )?(?:proceeding|moving forward)\b",
    # "decided to proceed with other candidates" is as definitive as "decided not to",
    # and matches none of the negated patterns above. One pattern covers the family:
    # proceed/move forward/continue/pursue, in any inflection, with other/another.
    r"\b(?:proceed|mov|continu|pursu)\w*\s+(?:forward\s+)?with\s+(?:other|another|a different)\b",
    r"\bwill not be moving\b", r"\bdecided not to\b",
    r"\bunfortunately\b", r"\bno longer under consideration\b",
]
REJECT_WEAK = [
    r"\bkeep your (?:information|resume|r\u00e9sum\u00e9|details|profile|application) on file\b",
    r"\bnot (?:a |the )?(?:best|right|strong(?:est)?) (?:match|fit)\b",
    r"\bno longer (?:recruiting|hiring|accepting|pursuing|considering)\b",
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
                           r"\bshare (?:some )?(?:dates|times|your availability)\b",
                           r"\b\d{1,2}\s?-?\s?min(?:ute)?s?\s+(?:zoom|phone|video|intro|initial)?\s*(?:call|chat|meeting|conversation)\b",
                           r"(?:calendly\.com|ashbyhq\.com/meeting|savvycal\.com|hubspot\.com/meetings)",
                           r"\b(?:love|like) to (?:connect|chat|speak|talk)\b"]),
    ("assessment",        [r"\bonline assessment\b", r"\btake[- ]home\b", r"\bcoding challenge\b",
                           r"\bskills assessment\b", r"\bcomplete (?:an|the) assessment\b"]),
    ("recruiter_outreach",[r"\bsharing your resume\b", r"\brecruiter\b",
                           r"\breaching out (?:to you )?(?:about|regarding|because|as|with)\b",
                           r"\bcame across your (?:profile|background|resume|experience)\b",
                           r"\bwould you be (?:open|interested|available)\b"]),
    ("ack",               [r"\bwe(?:'|.)?ve received your application\b", r"\bapplication received\b",
                           r"\bthank you for (?:your )?appl", r"\bthanks for applying\b",
                           r"\bthank you for your interest\b", r"\bconfirmation of\b",
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


def _clean_role(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    import html as _html
    s = _html.unescape(_html.unescape(s)).replace("\xa0", " ")
    s = re.sub(r"&[a-z]+;|&#\d+;", " ", s)                 # any entity that survived
    s = re.sub(r"^\s*\[[^\]]{1,30}\]\s*", "", s.strip())      # "[Pipeline] Product Manager"
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


def _event_type(subject: str, body: str = "") -> "tuple":
    """Return (type, literal matched text). Strong patterns may match subject or
    body; weak ones only the subject. A definitive ack subject blocks promotion."""
    subj_low = (subject or "").lower()
    # Hypotheticals are stripped for every event type, not just outcomes. The rule was
    # written for "if you are not selected" and applied only to rejections, which left
    # the mirror image live: "if you were asked to complete an online assessment" turned
    # a referral-routing email into an assessment, and a promotion invented out of a
    # conditional is the same error as a rejection invented out of one. It just flatters
    # instead of stinging, so it survives longer before anyone questions it.
    both_low = _strip_conditionals(f"{subject} {body}".lower())
    outcome_low = both_low
    ack_subject = bool(ACK_SUBJECT.search(subject or ""))

    for etype, pats in EVENT_PATTERNS:
        # Rejections and offers are trustworthy in a body. Promotion past "acked" is not:
        # ATS acks routinely say "we will be reaching out to candidates" and "a recruiter
        # will follow up", which promoted five definitive acknowledgements to in_process
        # and inflated the advance rate.
        scope = (subj_low if (ack_subject and etype in ("interview_invite", "assessment",
                                                        "recruiter_outreach"))
                 else outcome_low if etype in ("rejection", "offer")
                 else both_low)
        for p in pats:
            m = re.search(p, scope)
            if m:
                return etype, m.group(0).strip()

    for etype, pats in SUBJECT_ONLY:
        for p in pats:
            m = re.search(p, subj_low)
            if m:
                return etype, m.group(0).strip()
    return "unresolved", None


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

    etype, trigger = _event_type(subject, strip_boilerplate(body or ""))
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
    c.verdict_strength = _verdict_strength(c.event_type, subject, body or "")
    if (c.event_type in CLOSING and c.verdict_strength < WEAK_VERDICT
            and ACK_SUBJECT.search(subject)):
        c.held = True
        c.held_reason = ("closing verdict on weak evidence under an acknowledgement subject"
                         + (f': "{c.trigger}"' if c.trigger else ""))
        c.reasons.append("held:weak-verdict-under-ack-subject")
    return c
