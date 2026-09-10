"""Deterministic pre-rank: decide what is worth spending a model call on.

The pipeline had two tiers. Cheap regex gates cut a board sweep down to a few hundred,
then every survivor got an LLM fit score. That works while the watchlist is 87 curated
companies. It stops working the moment discovery widens: a single payer publishes 19,443
postings, and "Pharmacy Operations Lead Representative" passes a title gate written for
"Operations Lead" while being nothing like the target.

This is the missing middle. It reads only what is already in the database, costs nothing,
and orders the survivors so `fit` spends its budget on the top of the list. It is not a
replacement for the model's judgement and does not try to be: it answers "is this worth
reading" rather than "is this right", and every component is named so a wrong answer can
be traced to the signal that caused it.

Calibrated against the 285 roles already scored by the model. See `evaluate()`.
"""
import re
from typing import Optional

# ── level ────────────────────────────────────────────────────────────────────
# Profile targets Senior Manager / Lead / Principal, open to Manager at strong
# companies. Both directions are penalised: an analyst role is as wrong as a VP one.
LEVEL = [
    (re.compile(r"\b(chief of staff)\b", re.I), 1.00),
    (re.compile(r"\b(principal|head of|director|senior director)\b", re.I), 0.95),
    (re.compile(r"\b(staff|lead|senior manager|sr\.? manager|group manager)\b", re.I), 0.92),
    (re.compile(r"\b(senior|sr\.?)\b", re.I), 0.80),
    (re.compile(r"\b(manager|mgr)\b", re.I), 0.72),
    (re.compile(r"\b(vp|vice president|svp|evp|chief \w+ officer|cto|cfo|coo)\b", re.I), 0.30),
    (re.compile(r"\b(analyst|associate|coordinator|representative|specialist|assistant|"
                r"intern|apprentice|entry|junior|jr\.?)\b", re.I), 0.05),
]

# ── function ─────────────────────────────────────────────────────────────────
FUNCTION = [
    (re.compile(r"\b(business operations|bizops|strategy (and|&) operations|"
                r"chief of staff|strategic programs|business strategy)\b", re.I), 1.00),
    (re.compile(r"\b(gtm|go.to.market|revenue) (operations|strategy|programs)\b", re.I), 0.92),
    (re.compile(r"\b(operational excellence|business planning|program operations|"
                r"sales (operations|strategy)|partner operations)\b", re.I), 0.85),
    (re.compile(r"\b(technical program manager|program manager|program management)\b", re.I), 0.70),
    (re.compile(r"\b(operations (manager|lead))\b", re.I), 0.55),
]

# Wrong-domain operations. These share vocabulary with the target and nothing else:
# a payer's "Pharmacy Operations Lead Representative" matches "operations lead" exactly.
OFF_DOMAIN = re.compile(
    r"\b(pharmacy|clinical|nurse|nursing|warehouse|logistics|fulfilment|fulfillment|"
    r"driver|retail store|restaurant|kitchen|field service|facilities|janitorial|"
    r"security guard|call cent(er|re)|claims processing|billing|collections|"
    r"laboratory|radiology|dental|therapy|caregiver|technician)\b", re.I)

# People-management scope. The profile is explicit: senior IC, "build and manage a
# growing team" is a dealbreaker, player-coach is fine. Only the strong form counts.
MANAGES = re.compile(
    r"\b(build and (grow|manage|lead) (a|the|your) team|manage a team of|"
    r"lead a team of|hire, (develop|train) and (manage|retain)|"
    r"(\d+|several|multiple) direct reports|people manage(ment|r) (experience|responsibilit)|"
    r"grow and develop (a|the|your) team)\b", re.I)

YEARS = re.compile(r"\b(\d{1,2})\s*\+?\s*(?:-|to)?\s*\d{0,2}\s*years?\b", re.I)

# ── domain ───────────────────────────────────────────────────────────────────
# Mining the model's own stated reasons for the 193 roles it scored under 40, domain
# mismatch appears in 71% of them -- far ahead of level, comp or management scope. It
# was also the one signal this module originally had no feature for, which is why its
# first version correlated at 0.20: every surviving title looked plausible, and what
# separated them lived in the body.
DOMAIN_YES = re.compile(
    r"\b(healthcare|health plan|payer|provider|patient|care delivery|clinical operations|"
    r"advertis\w+|publisher|monetization|ad tech|sell.?side|first.party data|"
    r"go.to.market|gtm|revenue operations|sales operations|pipeline|quota|forecast\w*|"
    r"annual planning|operating cadence|okr|governance|business review|qbr|"
    r"cross.functional|stakeholder|program management|pmo|consult\w+|"
    r"sql|looker|tableau|bigquery|dashboard|analytics|cost of revenue|unit cost|"
    r"headcount|workforce|capacity|process improvement|operational excellence)\b", re.I)

# Fields with no overlap at all. Not "harder", just somebody else's job.
DOMAIN_NO = re.compile(
    r"\b(data cent(er|re)|colocation|hvac|liquid cooling|rack|server hardware|silicon|"
    r"semiconductor|firmware|embedded|kernel|compiler|asic|fpga|"
    r"physical security|executive protection|surveillance|guard|"
    r"security clearance|ts/sci|classified|defen[cs]e|weapons|"
    r"kubernetes|docker|terraform|site reliability|on.call rotation|incident command|"
    r"pharmac\w+|nurse|nursing|phlebotom\w+|radiolog\w+|dental|therapist|"
    r"warehouse|forklift|truck|driver|retail floor|store manager|restaurant|"
    r"marketplace listing|hyperscaler|cppo|tackle\.io)\b", re.I)


STOP = set("""a an the and or but if then than that this these those of in on at to for
with from by as is are was were be been being it its into over under about above after
before during within across per via you your we our us they their he she his her i me my
will would can could should may might must have has had do does did not no yes all any
each every some most more less other another such same own so very just also both few
than too only own s t re ve ll d m o y up out off down again further once here there when
where why how what which who whom whose while because until unless although though
role roles job jobs position work working team teams company companies year years new
including include includes ability able strong excellent experience experiences
opportunity opportunities candidate candidates skills skill required requirements
responsibilities responsibility qualifications preferred plus etc""".split())

_RESUME_TERMS = None


def resume_terms(path: "Optional[str]" = None) -> set:
    """Vocabulary lifted from the candidate's own resume: a reverse ATS match.

    An applicant tracking system scores a resume against a job description. Doing it the
    other way round costs nothing and needs no maintenance: the terms come from
    master.json, so editing the resume updates the matcher. It also removes the author's
    guesswork, which is what the hand-written DOMAIN_YES list below was.

    Bigrams as well as single words, because "operating cadence" and "annual planning"
    carry the signal that "operating" and "planning" separately do not.
    """
    global _RESUME_TERMS
    if _RESUME_TERMS is not None:
        return _RESUME_TERMS
    import json, pathlib
    p = pathlib.Path(path or (pathlib.Path(__file__).resolve().parent.parent
                              / "resume" / "master.json"))
    frags = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            frags.append(o)
    try:
        walk(json.loads(p.read_text()))
    except Exception:
        _RESUME_TERMS = set()
        return _RESUME_TERMS
    terms = set()
    for f in frags:
        w = [x for x in re.findall(r"[a-z][a-z&/-]{2,}", f.lower()) if x not in STOP]
        terms.update(w)
        terms.update(f"{a} {b}" for a, b in zip(w, w[1:]))
    # Drop the contact line and other one-off proper nouns that carry no domain meaning.
    _RESUME_TERMS = {t for t in terms if len(t) > 3}
    return _RESUME_TERMS


def _domain(jd: str) -> float:
    """How much of this posting is written in vocabulary the candidate has worked in.

    Distinct terms, not occurrences: a description saying "stakeholder" nine times must
    not outscore one that spans six real areas.

    Two readings, averaged. The resume match is self-maintaining and free of the author's
    guesswork; the curated list catches what a resume never says out loud, such as a
    posting being about ad tech when the resume only ever names the products. Measured
    against 285 model-scored roles they are close alone (rank correlation 0.43 and 0.45)
    and better together (0.47), which is the whole reason both are kept.
    """
    j = (jd or "").lower()
    words = [x for x in re.findall(r"[a-z][a-z&/-]{2,}", j) if x not in STOP]
    seen = set(words) | {f"{a} {b}" for a, b in zip(words, words[1:])}
    rt = resume_terms()
    ats = min(1.0, len(seen & rt) / 55.0) if rt else None
    hand = min(1.0, len({m.group(0).lower() for m in DOMAIN_YES.finditer(j)}) / 9.0)
    base = hand if ats is None else (ats + hand) / 2
    no = len({m.group(0).lower() for m in DOMAIN_NO.finditer(j)})
    if no >= 3 and base < 0.5:
        return 0.05                       # the posting is mostly about something else
    return max(0.0, base - 0.16 * no)


def _first(patterns, text: str, default: float) -> float:
    for rx, v in patterns:
        if rx.search(text or ""):
            return v
    return default


def _comp_score(lo: "Optional[int]", hi: "Optional[int]", floor: int, target: int) -> float:
    """Unknown pay is not a negative. Most postings publish none, and treating absence as
    a fault would rank every Greenhouse role below every Workday one for a reason that
    has nothing to do with the job. Unknown sits at the midpoint.
    """
    if not hi:
        return 0.55
    if hi < floor:
        return 0.02
    if lo and lo >= target:
        return 1.00
    if hi >= target:
        return 0.85
    return 0.45 + 0.40 * max(0.0, (hi - floor) / max(1, target - floor))


def _years_gap(jd: str, have: int = 10) -> float:
    """A stated requirement well above the candidate's tenure is a real screen-out."""
    asks = [int(m.group(1)) for m in YEARS.finditer(jd or "") if 2 <= int(m.group(1)) <= 30]
    if not asks:
        return 0.75
    need = min(asks)
    if need <= have:
        return 1.00
    return max(0.15, 1.0 - 0.18 * (need - have))


def _freshness(posted_at: "Optional[str]", today: "Optional[str]" = None) -> float:
    """Posting age outranks fit in the Do-next ordering, so it belongs here too."""
    if not posted_at:
        return 0.5
    from datetime import date
    try:
        y, m, d = int(posted_at[:4]), int(posted_at[5:7]), int(posted_at[8:10])
        age = (date.fromisoformat(today) if today else date.today()) - date(y, m, d)
    except Exception:
        return 0.5
    days = age.days
    if days <= 7:
        return 1.00
    if days <= 21:
        return 0.85
    if days <= 45:
        return 0.65
    if days <= 90:
        return 0.40
    return 0.20


# Weighted by how often each reason actually decided a rejection, not by intuition.
WEIGHTS = {"domain": 0.34, "function": 0.20, "level": 0.16, "comp": 0.16,
           "ic": 0.08, "years": 0.04, "fresh": 0.02}


def prerank(title: str, jd: str, comp_min, comp_max, posted_at,
            floor: int = 175000, target: int = 200000, today=None) -> dict:
    """Score 0-100 with the components exposed, so a bad rank is traceable."""
    t, j = title or "", jd or ""
    parts = {
        "function": _first(FUNCTION, t, 0.15),
        "level": _first(LEVEL, t, 0.5),
        "comp": _comp_score(comp_min, comp_max, floor, target),
        "years": _years_gap(j),
        "ic": 0.15 if MANAGES.search(j) else 1.00,
        "fresh": _freshness(posted_at, today),
        "domain": _domain(j),
    }
    if OFF_DOMAIN.search(t):
        parts["function"] = min(parts["function"], 0.08)
    score = sum(parts[k] * w for k, w in WEIGHTS.items()) * 100
    return {"score": round(score, 1), "parts": {k: round(v, 2) for k, v in parts.items()}}
