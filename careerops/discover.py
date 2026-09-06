"""Job discovery via public ATS board APIs.

Only endpoints companies publish for their own job boards. No scraping of
LinkedIn/Indeed: against their terms, brittle, and unnecessary since most
targets sit on Greenhouse, Ashby, or Lever anyway.
"""
import json, re, hashlib, urllib.request, urllib.error
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
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None


def _strip_html(s: str) -> str:
    import html as _h
    return re.sub(r"\s{2,}", " ", _h.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def fetch(board: str, slug: str) -> list:
    """Normalize each board's shape into one dict."""
    data = _get(ENDPOINTS[board].format(slug=slug))
    if not data:
        return []
    out = []
    if board == "greenhouse":
        for j in data.get("jobs", []):
            out.append({
                "title": j.get("title"),
                "location": (j.get("location") or {}).get("name"),
                "url": j.get("absolute_url"),
                "jd_text": _strip_html(j.get("content", "")),
                "external_id": str(j.get("id")),
            })
    elif board == "ashby":
        for j in data.get("jobs", []):
            out.append({
                "title": j.get("title"),
                "location": j.get("location"),
                "url": j.get("jobUrl"),
                "jd_text": _strip_html(j.get("descriptionPlain") or j.get("descriptionHtml") or ""),
                "external_id": str(j.get("id")),
            })
    elif board == "lever":
        for j in data:
            out.append({
                "title": j.get("text"),
                "location": (j.get("categories") or {}).get("location"),
                "url": j.get("hostedUrl"),
                "jd_text": _strip_html(j.get("descriptionPlain") or j.get("description") or ""),
                "external_id": str(j.get("id")),
            })
    return [o for o in out if o.get("title")]


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


def matches(job: dict, titles: Iterable[str], locations: Iterable[str],
            excludes: Iterable[str] = ()) -> bool:
    t = (job.get("title") or "").lower()
    if any(x.lower() in t for x in excludes):
        return False
    if not any(k.lower() in t for k in titles):
        return False
    if not locations:
        return True
    loc = (job.get("location") or "").lower()
    return any(l.lower() in loc for l in locations)


def discover(conn, watchlist: list, titles: list, locations: list,
             excludes: list = (), comp_floor: int = 0) -> dict:
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
            t = (j.get("title") or "").lower()
            if any(x.lower() in t for x in excludes):
                stats["excluded_title"] += 1
                continue
            if not matches(j, titles, locations, excludes):
                continue
            jd = j.get("jd_text") or ""
            cmin, cmax = extract_comp(jd)
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
                if existing["jd_hash"] != h:
                    conn.execute("UPDATE roles SET jd_text=?, jd_hash=?, url=?, location=? WHERE id=?",
                                 (jd, h, j.get("url"), j.get("location"), existing["id"]))
                continue
            db.get_or_create_role(conn, cid, j["title"], location=j.get("location"),
                                  source=w["board"], url=j.get("url"), jd_text=jd, jd_hash=h,
                                  comp_min=cmin, comp_max=cmax)
            stats["new"] += 1
    conn.commit()
    return stats
