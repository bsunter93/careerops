"""Fields a board's API leaves out and the employer's own listing page publishes.

Stripe hires through Greenhouse, and the Greenhouse feed gives each posting a free-text
location like "NYC, SF, Chicago, Seattle, US" and no pay at all (pay_input_ranges is
empty). The real fields appear only on stripe.com/jobs/listing/x/<gh_jid>: "Office
locations", "Remote location | Remote in United States" when the role is remote-eligible,
and "The annual US base salary range for this role is $X - $Y."

The feed string is wrong in both directions. One posting read as onsite with no band and
scored 15; with the page's "Remote in United States" and $189,400-$284,000 band it scored
80. Three others read "Remote US" or "SF, NYC, Seattle, Remote" in the feed while the page
names offices only. So when the page answers, its fields replace the feed's location.
Appending them would have left "Remote" standing on the office-only roles.

A page that 404s or yields no fields changes nothing. Stripe serves 404 for a closed
posting, and a redesign that hides the fields must not blank what the feed provided.
"""
import html as _html
import json
import re
import urllib.request
from typing import Callable, Optional

from . import discover as dsc

STRIPE_LISTING = "https://stripe.com/jobs/listing/x/{jid}"
_JID = re.compile(r"stripe\.com/.*[?&]gh_jid=(\d+)")

# <h3>Office locations</h3></dt><dd>Chicago, South San Francisco HQ</dd>. A single
# office is labelled "Office location".
_FACT = re.compile(r"<h3[^>]*>\s*(Office locations?|Remote locations?)\s*</h3>\s*</dt>\s*"
                   r"<dd[^>]*>(.*?)</dd>", re.S | re.I)
_SALARY = re.compile(r"annual US base salary range for this role is\s*\$\s*([\d,]+)\s*"
                     r"(?:-|\u2013|\u2014|to)\s*\$\s*([\d,]+)", re.I)
_LDJSON = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)


def covers(board: str, slug: str) -> bool:
    """Boards whose feed is known to leave out what the employer's page publishes."""
    return board == "greenhouse" and (slug or "").lower() == "stripe"


def stripe_jid(url: str) -> Optional[str]:
    m = _JID.search(url or "")
    return m.group(1) if m else None


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _job_posting(page: str) -> dict:
    for m in _LDJSON.finditer(page):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "JobPosting":
            return d
    return {}


def parse_stripe(page: str) -> Optional[dict]:
    """{"remote", "offices", "comp_min", "comp_max"} from one listing page, or None.

    Location comes from the rendered facts, which are the words Stripe shows the
    applicant; the schema.org block is the fallback. Pay comes from the schema.org
    baseSalary, a structured field, with the rendered sentence as the fallback. Only a
    USD annual band counts: a Toronto posting carries CAD, and that is not a floor
    comparison.
    """
    facts = {k.lower().rstrip("s"): _text(v) for k, v in _FACT.findall(page or "")}
    ld = _job_posting(page or "")
    remote = facts.get("remote location")
    offices = facts.get("office location")
    if remote is None and ld.get("jobLocationType") == "TELECOMMUTE":
        where = [x.get("name") for x in ld.get("applicantLocationRequirements") or []
                 if isinstance(x, dict) and x.get("name")]
        remote = "Remote in " + ", ".join(where) if where else "Remote"
    if offices is None:
        places = [((p.get("address") or {}).get("addressLocality") or "").strip()
                  for p in ld.get("jobLocation") or [] if isinstance(p, dict)]
        offices = ", ".join(x for x in places if x) or None

    lo = hi = None
    pay = ld.get("baseSalary") or {}
    val = pay.get("value") or {}
    if pay.get("currency") == "USD" and (val.get("unitText") or "YEAR") == "YEAR":
        lo, hi = val.get("minValue"), val.get("maxValue")
    if not (lo and hi):
        m = _SALARY.search(_html.unescape(page or ""))
        lo, hi = (int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))) if m else (None, None)

    if not (remote or offices or lo or hi):
        return None
    return {"remote": remote, "offices": offices,
            "comp_min": int(lo) if lo else None, "comp_max": int(hi) if hi else None}


def location(facts: dict) -> Optional[str]:
    """One string for roles.location, remote location first because that is the fact
    the Stripe rule turns on. An office-only role never carries the word "remote", since
    every location gate here matches on it."""
    remote, offices = facts.get("remote"), facts.get("offices")
    if remote and offices:
        return f"{remote} (offices: {offices})"
    if remote:
        return remote
    if offices:
        return f"Offices: {offices}"
    return None


def fetch_page(url: str) -> Optional[str]:
    """The page, or None for any failure. A 404 is how Stripe answers for a closed
    posting, and it is treated like an outage: no fields, nothing changed."""
    req = urllib.request.Request(url, headers={"User-Agent": dsc.UA})
    try:
        with urllib.request.urlopen(req, timeout=dsc.TIMEOUT) as r:
            return r.read().decode("utf-8", "replace")
    except OSError:
        return None


def stripe_facts(jid: str, get: Optional[Callable[[str], Optional[str]]] = None) -> Optional[dict]:
    page = (get or fetch_page)(STRIPE_LISTING.format(jid=jid)) if jid else None
    return parse_stripe(page) if page else None


def enrich(job: dict, get: Optional[Callable[[str], Optional[str]]] = None) -> bool:
    """Overwrite a fetched job's location and pay with the listing page's, in place,
    before the location gate reads it. Returns whether the page answered."""
    facts = stripe_facts(job.get("external_id"), get)
    loc = location(facts) if facts else None
    if not loc:
        return False
    job["location"] = loc
    job["remote"] = 1 if facts["remote"] else 0
    if facts["comp_min"] or facts["comp_max"]:
        job["comp_min"], job["comp_max"] = facts["comp_min"], facts["comp_max"]
    job["listed"] = True
    return True


def store(conn, role_id: int, job: dict) -> None:
    """Write an enriched job's fields to its role. The page outranks both the feed and
    the description prose, so a stored band is replaced, but a page that names no band
    leaves the stored one alone."""
    conn.execute("""UPDATE roles SET location = ?, remote = ?,
                           comp_min = COALESCE(?, comp_min), comp_max = COALESCE(?, comp_max)
                    WHERE id = ?""",
                 (job["location"], job["remote"], job.get("comp_min"), job.get("comp_max"),
                  role_id))


def unlisted_sql(conn) -> str:
    """Open roles at a Stripe listing whose page fields were never applied. remote is
    NULL until a page answers, and closed_at, where the column exists, removes postings
    already known to be gone."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(roles)")}
    return ("url LIKE '%stripe.com/%gh_jid=%' AND remote IS NULL"
            + (" AND closed_at IS NULL" if "closed_at" in cols else ""))


def backfill(conn, get: Optional[Callable[[str], Optional[str]]] = None,
             write: bool = True, only_unlisted: bool = False) -> dict:
    """The retroactive half: every stored role whose URL is a Stripe listing, not only
    the ones the next discover run happens to see. discover() keeps listed roles current;
    this reaches roles stored before the rule existed, roles the corrected gate now
    rejects, and ones whose title has since been renamed out from under the feed.
    `only_unlisted` limits it to roles no page has answered for yet, which is what
    `discover` runs after every sweep.

    Returns {"checked", "unanswered", "changed": [(role_id, before, after), ...]}.
    """
    where = unlisted_sql(conn) if only_unlisted else "url LIKE '%stripe.com/%gh_jid=%'"
    rows = conn.execute(f"""SELECT id, url, location, remote, comp_min, comp_max FROM roles
                            WHERE {where} ORDER BY id""").fetchall()
    out = {"checked": 0, "unanswered": [], "changed": []}
    pages = {}
    for r in rows:
        jid = stripe_jid(r["url"])
        if jid not in pages:
            pages[jid] = {"external_id": jid}
            enrich(pages[jid], get)
        out["checked"] += 1
        job = pages[jid]
        if not job.get("listed"):
            out["unanswered"].append(r["id"])
            continue
        before = {k: r[k] for k in ("location", "remote", "comp_min", "comp_max")}
        after = {"location": job["location"], "remote": job["remote"],
                 "comp_min": job.get("comp_min") or r["comp_min"],
                 "comp_max": job.get("comp_max") or r["comp_max"]}
        if before == after:
            continue
        out["changed"].append((r["id"], before, after))
        if write:
            store(conn, r["id"], job)
    if write:
        conn.commit()
    return out
