"""Does bullet selection answer what the posting asked for? A number, not an opinion.

`resume.rank_bullets` picks which master bullets reach the page. Four attempts to change
its weights each fixed one role by breaking another, and every one was judged by reading
five rankings and deciding whether they looked better. That is taste, and taste cannot
adjudicate between two formulas. This module makes the question measurable so a change to
the ranker is evidence-backed the same way a change to the classifier is (`corpus`).

The objective: for each scored role, what share of the posting's stated requirements does
the selected bullet set actually provide evidence for. Two requirement sources, because
they fail differently:

  jd     requirement lines parsed from the description itself (qualifications, what you
         will do, about you). Independent of any model, and the primary number. Absent
         for descriptions with no parseable section.
  meets  the fit scorer's `meets` list, cleaned. Available for every role, but the same
         model call also produced `emphasize`, which the ranker consumes, so it is not
         fully independent. Reported as the secondary number.

Coverage of one requirement is the best token overlap any selected bullet achieves with
it (share of the requirement's tokens present in the bullet). A role's soft coverage is
the mean of that over its requirements; hard coverage is the share of requirements whose
best overlap clears a threshold. Reference selections (random, longest) are scored the
same way so the metric is calibrated: a ranker that cannot beat picking the twelve longest
bullets is measuring length, not relevance.

Nothing here feeds back into the ranker. The requirement lists are the answer key and are
never given to the thing being graded.
"""
import json
import math
import random
import re
import sqlite3
from collections import Counter
from typing import Callable, Optional

from . import resume as R

CAP = 12            # where the one-page trim usually lands
HARD = 0.30         # a requirement counts as covered above this overlap
SEEDS = (1, 2, 3, 4, 5)

_HEADER = re.compile(
    r"(?i)^(?:what you(?:'| wi)ll (?:do|bring|need|own)|what you will (?:do|bring|own)|about you|"
    r"who you are|(?:minimum |preferred |basic |required )?qualifications?|requirements?|"
    r"what we(?:'| a)re looking for|you (?:have|bring|are|will)|responsibilities|"
    r"what you(?:'| wi)ll be doing|key responsibilities|your (?:role|impact)|in this role|"
    r"representative work|nice to haves?|bonus points|what you need)\b.{0,50}$"
)
_STOP_HEADER = re.compile(r"(?i)^(?:about (?:us|the (?:team|company)|\w+$)|benefits|compensation|salary|"
                          r"pay|perks|why (?:join|us|\w+)|our (?:team|values|culture)|equal (?:opportunity|employment)|"
                          r"what we offer|the interview process|how to apply|diversity|location)\b")
_TAG = re.compile(r"<[^>]+>")
_PAREN = re.compile(r"\([^)]*\)")
_NOT_A_REQ = re.compile(r"(?i)\b(remote|location|comp(?:ensation)?\b|salary|pay|band|floor|target|"
                        r"relocat|onsite|hybrid|denver|boulder|\$\d|visa|sponsor)")


def _lines(jd: str) -> list:
    """HTML or plain text to one item per line, with list items marked.

    Headers arrive as <h2><strong>Responsibilities:</strong></h2> or
    <p><strong><span ...>Minimum qualifications:</span></strong></p>, so a regex that
    expects the header text right after a tag or newline sees 23 of 315. Flattening to
    lines first, then reading the lines, sees them all.
    """
    import html as H
    t = re.sub(r"(?i)<li[^>]*>", "\n• ", jd or "")
    t = re.sub(r"(?i)</?(?:p|h\d|br|ul|ol|div|tr|section)[^>]*>", "\n", t)
    t = H.unescape(_TAG.sub(" ", t))
    return [re.sub(r"\s+", " ", ln).strip() for ln in t.split("\n") if ln.strip()]


def jd_requirements(jd: str, cap: int = 24) -> list:
    """Requirement lines from the description's own qualifications and duties sections."""
    out, lines = [], _lines(jd)
    i = 0
    while i < len(lines):
        ln = lines[i].strip(" •:-")
        if len(ln) <= 60 and _HEADER.match(ln):
            i += 1
            n = 0
            while i < len(lines) and n < cap:
                cur = lines[i]
                bare = cur.strip(" •:-")
                if (len(bare) <= 60 and (_HEADER.match(bare) or _STOP_HEADER.match(bare))) or \
                   (not cur.startswith("•") and len(bare) <= 40 and bare.endswith(":")):
                    break
                items = [cur] if cur.startswith("•") else re.split(r"(?<=[.;])\s+(?=[A-Z])", cur)
                for it in items:
                    it = it.strip(" •*–—-")
                    if 4 <= len(R._tokens(it)) and len(it) < 400 and not _NOT_A_REQ.search(it):
                        out.append(it)
                        n += 1
                i += 1
            continue
        i += 1
    seen, uniq = set(), []
    for it in out:
        k = it.lower()[:80]
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    return uniq[:cap]


def meets_requirements(reasoning: str) -> list:
    """The fit scorer's `meets`, with the candidate's own evidence stripped out.

    Entries read like "OKR design experience (Google $B business, Accenture merger)". The
    parenthetical is the answer, not the question; leaving it in would let a bullet match
    the requirement by naming itself. Location and pay lines are dropped because no bullet
    can evidence them.
    """
    try:
        f = json.loads(reasoning or "{}")
    except Exception:
        return []
    out = []
    for it in f.get("meets") or []:
        it = _PAREN.sub("", it)
        it = re.sub(r"\s+", " ", it).strip(" -:;,")
        if len(R._tokens(it)) >= 3 and not _NOT_A_REQ.search(it):
            out.append(it)
    return out


def _stem(tokens) -> set:
    """Fold plurals and gerunds so "pipelines" meets "pipeline". Crude, but symmetric."""
    out = set()
    for t in tokens:
        if len(t) > 5 and t.endswith("ing"):
            t = t[:-3]
        elif len(t) > 4 and t.endswith("es"):
            t = t[:-2]
        elif len(t) > 3 and t.endswith("s"):
            t = t[:-1]
        out.add(t)
    return out


def coverage(reqs: list, bullet_texts: list, idf: Optional[dict] = None) -> dict:
    """How well a bullet set evidences a requirement list.

    Overlap is weighted by token rarity across every description in the database, so a
    bullet that shares "capacity" and "allocation" with a requirement scores higher than
    one that shares "team" and "business". Raw overlap could not separate any two
    strategies on the first run, because generic words dominate every intersection.
    """
    if not reqs:
        return {"soft": None, "hard": None, "n": 0, "per": []}
    idf = idf or {}
    w = lambda t: idf.get(t, 1.0)
    bts = [_stem(R._tokens(t)) for t in bullet_texts]
    per = []
    for rq in reqs:
        rt = _stem(R._tokens(rq))
        denom = sum(w(t) for t in rt)
        best = max((sum(w(t) for t in rt & bt) / denom for bt in bts), default=0.0) if denom else 0.0
        per.append((rq, best))
    soft = sum(b for _, b in per) / len(per)
    hard = sum(1 for _, b in per if b >= HARD) / len(per)
    return {"soft": soft, "hard": hard, "n": len(per), "per": per}


# ---------------------------------------------------------------- selection strategies

def _ctx(m, emphasize, title, jd, idf=None):
    return {"emph": R._tokens(" ".join(emphasize or [])), "ttl": R._tokens(title),
            "jdt": R._tokens((jd or "")[:6000]), "idf": idf or {}}


def _by(scorer: Callable):
    """Wrap a per-bullet scorer into the same shape rank_bullets returns."""
    def rank(m, emphasize, title, jd, idf=None):
        c = _ctx(m, emphasize, title, jd, idf)
        out = []
        for group, b in R._all_bullets(m):
            bt = R._tokens(b["text"] + " " + b["label"])
            out.append({"id": b["id"], "group": group, "label": b["label"], "tags": b["tags"],
                        "score": round(scorer(bt, set(b["tags"]), c), 3)})
        out.sort(key=lambda x: -x["score"])
        return out
    return rank


def _original(bt, tags, c):
    return (6 * len(bt & c["emph"]) + 4 * len(tags & c["emph"]) +
            3 * len(bt & c["ttl"]) + 3 * len(tags & c["ttl"]) +
            1 * len(bt & c["jdt"]) + 1.5 * len(tags & c["jdt"]))


def _reweight(bt, tags, c):                       # attempt 1
    return (3 * len(bt & c["emph"]) + 2 * len(tags & c["emph"]) +
            3 * len(bt & c["ttl"]) + 3 * len(tags & c["ttl"]) +
            2 * len(bt & c["jdt"]) + 3 * len(tags & c["jdt"]))


def _normalised(bt, tags, c):                     # attempt 2
    f = lambda a, b: len(a & b) / len(a) if a else 0.0
    return 100 * (0.40 * f(c["emph"], bt) + 0.10 * f(c["emph"], tags) +
                  0.15 * f(c["ttl"], bt) + 0.20 * f(bt, c["jdt"]) + 0.15 * f(tags, c["jdt"]))


def _saturating(bt, tags, c):                     # attempt 3
    cov = lambda pool, x: len(pool & x) / len(pool) if pool else 0.0
    sat = lambda x, k: min(1.0, len(x) / k)
    return 100 * (0.38 * cov(c["emph"], bt) + 0.10 * cov(c["emph"], tags) +
                  0.12 * cov(c["ttl"], bt) + 0.25 * sat(bt & c["jdt"], 14) + 0.15 * sat(tags & c["jdt"], 4))


def _idf(w):
    """Original weights, with the description term paying by token rarity.

    The description has ~350 tokens and most are generic ("team", "business", "drive").
    A bullet earns the same point for "business" as for "capacity". Weighting each match
    by how rare the token is across every description in the database lets the specific
    asks count for more than the boilerplate, without touching the emphasis term at all.
    """
    def s(bt, tags, c):
        idf = c["idf"]
        wt = lambda t: idf.get(next(iter(_stem({t}))), 1.0)
        jd_text = sum(wt(t) for t in bt & c["jdt"])
        jd_tags = sum(wt(t) for t in tags & c["jdt"])
        return (6 * len(bt & c["emph"]) + 4 * len(tags & c["emph"]) +
                3 * len(bt & c["ttl"]) + 3 * len(tags & c["ttl"]) +
                w * (1 * jd_text + 1.5 * jd_tags))
    return s


STRATEGIES = {
    "original":    _by(_original),
    "reweight":    _by(_reweight),
    "normalised":  _by(_normalised),
    "saturating":  _by(_saturating),
    "idf x1":      _by(_idf(1)),
    "idf x2":      _by(_idf(2)),
    "idf x3":      _by(_idf(3)),
}


def _reference_longest(m, *_a, **_k):
    bs = [{"id": b["id"], "group": g, "label": b["label"], "tags": b["tags"],
           "score": len(R._tokens(b["text"]))} for g, b in R._all_bullets(m)]
    return sorted(bs, key=lambda x: -x["score"])


def _reference_random(seed):
    def rank(m, emphasize=None, title="", jd="", idf=None):
        # seeded per role, or every role gets the same twelve and diversity reads as 1.0
        rng = random.Random(f"{seed}|{title}|{(jd or '')[:200]}")
        bs = [{"id": b["id"], "group": g, "label": b["label"], "tags": b["tags"], "score": rng.random()}
              for g, b in R._all_bullets(m)]
        return sorted(bs, key=lambda x: -x["score"])
    return rank


# ---------------------------------------------------------------- the harness

def build_idf(jds: list) -> dict:
    """Inverse document frequency of tokens across descriptions, scaled to mean 1.0."""
    n = len(jds)
    df = Counter()
    for jd in jds:
        df.update(_stem(R._tokens((jd or "")[:6000])))
    raw = {t: math.log((n + 1) / (d + 1)) + 1 for t, d in df.items()}
    mean = sum(raw.values()) / len(raw) if raw else 1.0
    return {t: v / mean for t, v in raw.items()}


def load_roles(conn) -> list:
    rows = conn.execute("""SELECT a.id, a.fit_score, a.fit_reasoning, ro.title, ro.jd_text, co.name
                           FROM applications a JOIN roles ro ON ro.id = a.role_id
                           JOIN companies co ON co.id = ro.company_id
                           WHERE a.fit_reasoning IS NOT NULL AND ro.jd_text IS NOT NULL
                             AND LENGTH(ro.jd_text) > 200""").fetchall()
    out = []
    for r in rows:
        try:
            f = json.loads(r["fit_reasoning"])
        except Exception:
            continue
        out.append({"id": r["id"], "company": r["name"], "title": r["title"], "jd": r["jd_text"],
                    "fit": r["fit_score"], "emphasize": f.get("emphasize") or [],
                    "jd_reqs": jd_requirements(r["jd_text"]),
                    "meets_reqs": meets_requirements(r["fit_reasoning"])})
    return out


def selected_ids(rank_fn, m, role, idf, cap=CAP):
    scored = rank_fn(m, role["emphasize"], role["title"], role["jd"], idf)
    return R.select(scored, cap=cap, floor=R.MIN_PER_GROUP)


def evaluate(rank_fn, m, roles, idf, cap=CAP) -> dict:
    text_of = {b["id"]: b["text"] + " " + b["label"] for _, b in R._all_bullets(m)}
    jd_soft, jd_hard, me_soft, me_hard = [], [], [], []
    picks, sets = Counter(), []
    for role in roles:
        ids = selected_ids(rank_fn, m, role, idf, cap)
        texts = [text_of[i] for i in ids]
        picks.update(ids)
        sets.append(set(ids))
        cj = coverage(role["jd_reqs"], texts, idf)
        cm = coverage(role["meets_reqs"], texts, idf)
        if cj["soft"] is not None:
            jd_soft.append(cj["soft"]); jd_hard.append(cj["hard"])
        if cm["soft"] is not None:
            me_soft.append(cm["soft"]); me_hard.append(cm["hard"])
    mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    # diversity: how different the selections are from role to role
    jac = []
    for i in range(0, len(sets), 7):
        for j in range(i + 1, min(i + 7, len(sets))):
            a, b = sets[i], sets[j]
            jac.append(len(a & b) / len(a | b) if a | b else 1.0)
    return {"jd_soft": mean(jd_soft), "jd_hard": mean(jd_hard), "jd_n": len(jd_soft),
            "meets_soft": mean(me_soft), "meets_hard": mean(me_hard), "meets_n": len(me_soft),
            "distinct": len(picks), "jaccard": mean(jac)}


def compare(rank_a, rank_b, m, roles, idf, ids) -> list:
    """Per-role gained/lost between two strategies, for the roles that were argued about."""
    label = {b["id"]: b["label"] for _, b in R._all_bullets(m)}
    out = []
    for role in roles:
        if role["id"] not in ids:
            continue
        a = set(selected_ids(rank_a, m, role, idf))
        b = set(selected_ids(rank_b, m, role, idf))
        out.append((role["id"], role["company"], [label[i] for i in b - a], [label[i] for i in a - b]))
    return out


def run(conn, m=None, spot=(955, 1118, 771, 1008, 931), show_role: Optional[int] = None) -> dict:
    m = m or R._load_master()
    roles = load_roles(conn)
    idf = build_idf([r["jd"] for r in roles])
    with_jd = sum(1 for r in roles if r["jd_reqs"])
    print(f"{len(roles)} scored roles; {with_jd} with a parseable requirements section, "
          f"{sum(1 for r in roles if r['meets_reqs'])} with usable meets\n")

    if show_role is not None:
        role = next((r for r in roles if r["id"] == show_role), None)
        if role:
            text_of = {b["id"]: b["text"] + " " + b["label"] for _, b in R._all_bullets(m)}
            ids = selected_ids(STRATEGIES["original"], m, role, idf)
            cov = coverage(role["jd_reqs"], [text_of[i] for i in ids], idf)
            print(f"[{role['id']}] {role['company']} - {role['title']}  (original ranker)")
            for rq, best in sorted(cov["per"], key=lambda x: x[1]):
                flag = "  " if best >= HARD else "!!"
                print(f"  {flag} {best:4.2f}  {rq[:100]}")
            print()

    results = {}
    strategies = dict(STRATEGIES)
    strategies["ref: longest 12"] = _reference_longest
    hdr = f"{'strategy':16} {'jd soft':>8} {'jd hard':>8} {'meets soft':>11} {'meets hard':>11} {'distinct':>9} {'jaccard':>8}"
    print(hdr); print("-" * len(hdr))
    for name, fn in strategies.items():
        r = evaluate(fn, m, roles, idf)
        results[name] = r
        print(f"{name:16} {r['jd_soft']:8.3f} {r['jd_hard']:8.3f} {r['meets_soft']:11.3f} "
              f"{r['meets_hard']:11.3f} {r['distinct']:9d} {r['jaccard']:8.3f}")
    # Upper bound: pick the twelve with the answer key in hand. If a perfect ranker
    # cannot beat the current one by much, the bullet pool is the constraint and the
    # formula is not where the leverage is.
    mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    text_of = {b["id"]: b["text"] + " " + b["label"] for _, b in R._all_bullets(m)}
    ids_all = list(text_of)
    orig_o, orac_o = [], []
    for role in roles:
        if not role["jd_reqs"]:
            continue
        cov_of = lambda ids: coverage(role["jd_reqs"], [text_of[i] for i in ids], idf)["soft"]
        chosen = []
        while len(chosen) < CAP:
            chosen.append(max((i for i in ids_all if i not in chosen), key=lambda i: cov_of(chosen + [i])))
        orig_o.append(cov_of(selected_ids(STRATEGIES["original"], m, role, idf)))
        orac_o.append(cov_of(chosen))
    if orac_o:
        print(f"{'ref: greedy oracle':16} {mean(orac_o):8.3f} {'-':>8} {'-':>11} {'-':>11} {'-':>9} {'-':>8}"
              f"   (best {CAP} chosen with the answer key; headroom over original {mean(orac_o) - mean(orig_o):+.3f})")
    rs = [evaluate(_reference_random(s), m, roles, idf) for s in SEEDS]
    avg = {k: sum(x[k] for x in rs) / len(rs) for k in ("jd_soft", "jd_hard", "meets_soft", "meets_hard", "jaccard")}
    print(f"{'ref: random':16} {avg['jd_soft']:8.3f} {avg['jd_hard']:8.3f} {avg['meets_soft']:11.3f} "
          f"{avg['meets_hard']:11.3f} {'-':>9} {avg['jaccard']:8.3f}   (mean of {len(SEEDS)} seeds)")
    results["ref: random"] = avg

    print("\nGained / lost against the original, on the five roles that were argued about:")
    for name, fn in strategies.items():
        if name == "original":
            continue
        print(f"  {name}")
        for rid, co, gained, lost in compare(STRATEGIES["original"], fn, m, roles, idf, set(spot)):
            if gained or lost:
                print(f"     {rid:5} {co[:12]:12} +{gained}  -{lost}")
    return results
