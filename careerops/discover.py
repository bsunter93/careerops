"""Job discovery via public ATS board APIs.

Only endpoints companies publish for their own job boards. No scraping of
LinkedIn/Indeed: against their terms, brittle, and unnecessary since most
targets sit on Greenhouse, Ashby, or Lever anyway.
"""
import json, re, hashlib, urllib.request, urllib.error
from functools import lru_cache
from typing import Iterable, Optional
from . import db

UA = "careerops/0.1 (personal job tracker)"
TIMEOUT = 20

ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "ashby":      "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
    "lever":      "https://api.lever.co/v0/postings/{slug}?mode=json",
}


def _get(url: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, json.JSONDecodeError):
        # OSError covers socket.timeout, URLError and HTTPError. On Python 3.9
        # socket.timeout is not TimeoutError, so naming TimeoutError let one slow
        # board raise through and abort the sweep across all 87 of them.
        return None


def _strip_html(s: str) -> str:
    import html as _h
    return re.sub(r"\s{2,}", " ", _h.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _epoch_iso(ms) -> "Optional[str]":
    """Lever returns epoch milliseconds."""
    if not ms:
        return None
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()[:19]
    except Exception:
        return None


def fetch(board: str, slug: str) -> list:
    """Normalize each board's shape into one dict."""
    data = _get(ENDPOINTS[board].format(slug=slug))
    if not data:
        return []
    out = []
    if board == "greenhouse":
        for j in data.get("jobs", []):
            cmin, cmax = _greenhouse_comp(j)
            out.append({
                "title": j.get("title"),
                "location": (j.get("location") or {}).get("name"),
                "url": j.get("absolute_url"),
                "jd_text": _strip_html(j.get("content", "")),
                "external_id": str(j.get("id")),
                "posted_at": (j.get("first_published") or j.get("updated_at") or "")[:19],
                "comp_min": cmin, "comp_max": cmax,
            })
    elif board == "ashby":
        for j in data.get("jobs", []):
            cmin, cmax = _ashby_comp(j)
            out.append({
                "title": j.get("title"),
                "location": j.get("location"),
                "url": j.get("jobUrl"),
                "jd_text": _strip_html(j.get("descriptionPlain") or j.get("descriptionHtml") or ""),
                "external_id": str(j.get("id")),
                "posted_at": (j.get("publishedAt") or "")[:19],
                "comp_min": cmin, "comp_max": cmax,
            })
    elif board == "lever":
        for j in data:
            cmin, cmax = _lever_comp(j)
            # Lever splits the posting across fields; the requirements lists and the
            # closing block are where the pay language usually sits when it is prose.
            body = " ".join(filter(None, [
                j.get("descriptionPlain") or j.get("description") or "",
                j.get("additionalPlain") or j.get("additional") or "",
                j.get("salaryDescriptionPlain") or "",
                " ".join((x.get("text") or "") + " " + (x.get("content") or "")
                         for x in (j.get("lists") or []))]))
            out.append({
                "title": j.get("text"),
                "location": (j.get("categories") or {}).get("location"),
                "url": j.get("hostedUrl"),
                "posted_at": _epoch_iso(j.get("createdAt")),
                "jd_text": _strip_html(body),
                "external_id": str(j.get("id")),
                "comp_min": cmin, "comp_max": cmax,
            })
    return [o for o in out if o.get("title")]


def _lever_comp(j: dict):
    """Lever returns salaryRange as structured JSON and omits the figures from the
    description entirely, so no amount of prose parsing can recover them. A Chief of
    Staff posting paying $180,000-$220,000 read as "comp unconfirmed, verify it meets
    the floor" while the numbers sat in the API response.
    """
    r = j.get("salaryRange") or {}
    if (r.get("currency") or "USD") != "USD":
        return (None, None)
    if "year" not in (r.get("interval") or "per-year-salary"):
        return (None, None)          # hourly or monthly is not a floor comparison
    lo, hi = r.get("min"), r.get("max")
    return (int(lo) if lo else None, int(hi) if hi else None)


def _greenhouse_comp(j: dict):
    """Greenhouse's public board API does not currently return pay_input_ranges: across
    six boards and 1,881 postings it yielded a band for none of them. Greenhouse comp
    therefore comes from the description prose, which is where Wiz publishes
    $211,000-$290,500 and where extract_comp finds it.

    This is kept as a correct-when-present fallback rather than removed, but it is
    deliberately documented as returning nothing today, so nobody reads its existence as
    evidence that Greenhouse comp is covered structurally. It is not.
    """
    lo = hi = None
    for r in (j.get("pay_input_ranges") or []):
        if (r.get("currency_type") or "USD") != "USD":
            continue
        a, b = r.get("min_cents"), r.get("max_cents")
        a = int(a) // 100 if a else None
        b = int(b) // 100 if b else None
        if a: lo = a if lo is None else min(lo, a)
        if b: hi = b if hi is None else max(hi, b)
    return (lo, hi)


def _ashby_comp(j: dict):
    """Ashby publishes the band as structured JSON, not as prose in the description.

    Headway's "Revenue Strategy & Operations Manager (Insights & AI)" pays $121.6K-$190K,
    and the API said so in compensation.compensationTiers. The parser read only the
    description, which never mentions pay, so comp_max stayed NULL. The comp gate is
    written to let unknown pay through rather than discard a role over a parsing gap, so
    a role paying well under the floor scored 83 on fit alone and sat in Do next.
    """
    lo = hi = None
    comp = j.get("compensation") or {}
    for tier in (comp.get("compensationTiers") or []):
        for c in (tier.get("components") or []):
            if (c.get("compensationType") != "Salary"
                    or (c.get("currencyCode") or "USD") != "USD"):
                continue
            a, b = c.get("minValue"), c.get("maxValue")
            # An hourly or monthly band is not a floor comparison; skip rather than guess.
            if (c.get("interval") or "1 YEAR") != "1 YEAR":
                continue
            if a: lo = a if lo is None else min(lo, a)
            if b: hi = b if hi is None else max(hi, b)
    return (int(lo) if lo else None, int(hi) if hi else None)


SALARY_CONTEXT = re.compile(
    r"(salary range|base salary|base pay|compensation range|pay range|salary|compensation)",
    re.I)
MONEY = re.compile(r"\$\s?(\d{2,3})(?:,(\d{3}))?(?:\.\d+)?\s?([kK])?")


def extract_comp(jd: str):
    """Best-effort (min, max) annual base from JD text. Returns (None, None) when
    unsure -- a wrong number is worse than no number."""
    if not jd:
        return (None, None)
    vals = []
    for ctx in SALARY_CONTEXT.finditer(jd):
        window = jd[ctx.start(): ctx.start() + 400]
        for m in MONEY.finditer(window):
            whole, thousands, k = m.groups()
            n = int(whole) * 1000 if (k or not thousands) else int(whole + thousands)
            if 50_000 <= n <= 900_000:
                vals.append(n)
        if len(vals) >= 2:
            break
    if len(vals) < 2:
        return (None, None)
    return (min(vals), max(vals))


def _loc_hit(loc_low: str, patterns: Iterable[str]) -> bool:
    """Location patterns match on word boundaries, not raw substrings.

    The watchlist carries "us" as a catch-all for nationwide postings, and a plain
    substring test finds it inside Austin, Houston, Columbus and Tuscaloosa, quietly
    admitting every role in those cities as if they were home market. Patterns that
    start or end with punctuation, like ", ca", keep their literal form.
    """
    for p in patterns:
        p = (p or "").lower().strip()
        if not p:
            continue
        left = r"\b" if p[0].isalnum() else ""
        right = r"\b" if p[-1].isalnum() else ""
        if re.search(left + re.escape(p) + right, loc_low):
            return True
    return False


def _kw_pattern(kw: str) -> "re.Pattern":
    """A keyword matches its own plural, and vice versa.

    The filter was a plain substring test, so "program manager" could not match
    "GTM Programs Manager, AMER" and the role was invisible. Same silent-absence
    failure as the relocation gate: nothing errors, the row simply never appears.

    Each word may carry a trailing s, in either direction, so "operations" still
    matches "operations". No word boundaries, to keep the old substring semantics.
    """
    words = kw.lower().split()
    stems = [(w[:-1] if len(w) > 3 and w.endswith("s") else w) for w in words]
    return re.compile(r"\s+".join(re.escape(x) + "s?" for x in stems))


@lru_cache(maxsize=512)
def _kw(kw: str):
    return _kw_pattern(kw)


def matches(job: dict, titles: Iterable[str], locations: Iterable[str],
            excludes: Iterable[str] = (), relocation: "Optional[dict]" = None,
            comp_max: "Optional[int]" = None,
            comp_min: "Optional[int]" = None,
            exclude_domains: Iterable[str] = ()) -> bool:
    t = (job.get("title") or "").lower()
    # Hard excludes describe the job itself: an analyst role is junior wherever the word
    # sits in the title, so these reject outright.
    if any(x.lower() in t for x in excludes):
        return False
    # The role supersedes the domain. `titles` is already a whitelist of jobs worth
    # taking, so a subject-matter word has no business vetoing a match against it:
    # "Chief of Staff, Security Customer Engineering" is a chief of staff role whatever
    # the org is called, and excluding it on "security" threw away a remote
    # $211,000-$290,500 posting. Domain terms are a backstop for titles that match
    # nothing on the whitelist, not an override of one that does.
    if titles:
        if not any(_kw(k).search(t) for k in titles):
            return False
    elif any(x.lower() in t for x in (exclude_domains or ())):
        return False
    if not locations:
        return True
    loc = (job.get("location") or "").lower()
    if _loc_hit(loc, locations):
        return True
    return _worth_relocating(t, loc, relocation, comp_max, comp_min)


def _worth_relocating(title_low: str, loc_low: str, rules: "Optional[dict]",
                      comp_max: "Optional[int]",
                      comp_min: "Optional[int]" = None) -> bool:
    """A role outside the home market has to be worth moving a family for.

    The right coast is always required, and a published range is always required: an
    unverified guess is exactly the wrong thing to relocate on. California and Washington
    both mandate pay ranges in postings, so the roles this rule targets almost always
    state one.

    Above `comp_override` the pay decides on its own. Below it, the old rule still holds:
    a senior title as well as pay above `comp_floor`.

    The seniority keywords are a proxy for pay, and a proxy must not outrank the thing it
    stands in for. Anthropic posts $270-310k San Francisco roles titled "GTM Strategy &
    Operations - AMER Enterprise Tech" and "Sales Strategy, Operational Excellence",
    neither carrying director / head / lead / principal / staff / VP, and the title test
    was discarding both while admitting a lower-paying role whose only difference was the
    word "Lead". Same family of bug as filtering on job title instead of domain.
    """
    if not rules:
        return False
    if not _loc_hit(loc_low, rules.get("locations", ())):
        return False
    floor = rules.get("comp_floor") or 0
    if not (comp_max and comp_max >= floor):
        return False
    # The two comp tests deliberately read opposite ends of the band, because each is
    # conservative in a different direction. The floor rejects only when even the top of
    # the range is too low. The override grants only when even the bottom of the range
    # clears it: a $270-310k posting is not a $300k role, it is a role that might pay
    # $270k, and that is the number to plan a family move against.
    override = rules.get("comp_override") or 0
    if override and comp_min and comp_min >= override:
        return True
    return any(k.lower() in title_low for k in rules.get("seniority", ()))


def discover(conn, watchlist: list, titles: list, locations: list,
             excludes: list = (), comp_floor: int = 0,
             relocation: "Optional[dict]" = None,
             exclude_domains: list = ()) -> dict:
    """watchlist: [{"company": "Databricks", "board": "greenhouse", "slug": "databricks"}, ...]"""
    stats = {"boards": 0, "fetched": 0, "matched": 0, "new": 0,
             "excluded_title": 0, "below_comp": 0, "failed": []}
    for w in watchlist:
        stats["boards"] += 1
        jobs = fetch(w["board"], w["slug"])
        if not jobs:
            stats["failed"].append(f'{w["company"]}({w["board"]}:{w["slug"]})')
            continue
        stats["fetched"] += len(jobs)
        cid = db.get_or_create_company(conn, w["company"])
        for j in jobs:
            # Normalize once. Ashby returns titles with a trailing space, and
            # get_or_create_role strips before inserting, so the raw title never
            # matched the stored one: the existence check below missed every time,
            # "new" counted a row that was never inserted, and the update branch that
            # refreshes jd_text and posted_at was skipped for the life of the role.
            j["title"] = (j.get("title") or "").strip()
            t = j["title"].lower()
            if any(x.lower() in t for x in excludes):
                stats["excluded_title"] += 1
                continue
            # Comp is parsed before matching now: the relocation rule needs the number
            # to decide whether a role outside the home market is worth moving for.
            jd = j.get("jd_text") or ""
            cmin, cmax = extract_comp(jd)
            # The board's structured band beats anything guessed out of the prose.
            cmin = j.get("comp_min") or cmin
            cmax = j.get("comp_max") or cmax
            if not matches(j, titles, locations, excludes, relocation, cmax, cmin,
                           exclude_domains):
                continue
            # Only filter when comp was actually parsed; unknown never disqualifies.
            if comp_floor and cmax and cmax < comp_floor:
                stats["below_comp"] += 1
                continue
            stats["matched"] += 1
            h = hashlib.sha1((j.get("title", "") + jd[:2000]).encode()).hexdigest()[:16]
            existing = conn.execute(
                "SELECT id, jd_hash FROM roles WHERE company_id=? AND title=? COLLATE NOCASE",
                (cid, j["title"])).fetchone()
            if existing:
                conn.execute("UPDATE roles SET posted_at = COALESCE(?, posted_at) WHERE id=?",
                             (j.get("posted_at"), existing["id"]))
                if cmin or cmax:
                    conn.execute("""UPDATE roles SET comp_min=COALESCE(comp_min,?),
                                    comp_max=COALESCE(comp_max,?) WHERE id=?""",
                                 (cmin, cmax, existing["id"]))
                if existing["jd_hash"] != h:
                    conn.execute("UPDATE roles SET jd_text=?, jd_hash=?, url=?, location=? WHERE id=?",
                                 (jd, h, j.get("url"), j.get("location"), existing["id"]))
                continue
            db.get_or_create_role(conn, cid, j["title"], location=j.get("location"),
                                  source=w["board"], url=j.get("url"), jd_text=jd, jd_hash=h,
                                  comp_min=cmin, comp_max=cmax, posted_at=j.get("posted_at"))
            stats["new"] += 1
    conn.commit()
    stats.update(scoring_backlog(conn))
    return stats


def scoring_backlog(conn) -> dict:
    """How many discovered roles are waiting on `fit`, and how many can never get it.

    discover can add more roles than fit scores in a run, and an unscored role has no
    application row at all, so it appears nowhere: not on the dashboard, not in Do next,
    not in any count. Today's expansion left 31 roles in that state and the only reason
    it surfaced was someone going to look. On the 07:30 job it would have been silent.

    `unscored` mirrors score_pending's own predicate exactly, so it reads as the number
    of roles the next `fit` run would pick up. `unscorable` is the quieter problem: a
    posting whose JD never came through is permanently invisible, because fit skips it
    every time and nothing else ever mentions it.
    """
    # Only roles that are still candidates for scoring. A role reached through Gmail
    # carries no JD, and once it has been applied to its fit score is irrelevant, so
    # counting those reported 319 permanently "unscorable" roles that needed nothing.
    q = """SELECT COUNT(*) n FROM roles r LEFT JOIN applications a ON a.role_id = r.id
           WHERE (a.id IS NULL OR (a.fit_score IS NULL AND a.status = 'prospect'))
             AND %s"""
    scorable = "r.jd_text IS NOT NULL AND LENGTH(r.jd_text) > 200"
    return {
        "unscored": conn.execute(q % scorable).fetchone()["n"],
        "unscorable": conn.execute(q % f"NOT ({scorable})").fetchone()["n"],
    }
