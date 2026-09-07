"""Open discovery: find roles at companies not already on the watchlist.

The watchlist is a poller, not a search. It only ever sees the ~46 companies
listed in advance, so a Chief of Staff req at InStride Health scoring 85 sat
unseen for three weeks because nobody had thought to add InStride.

Two tiers fix that, and they do different jobs:

  aggregators  broad reach, weak dates. Free public JSON feeds covering
               thousands of employers. Their publication_date is when the
               aggregator indexed the posting, not when the company published
               it, so it understates age.

  ATS boards   narrow reach, authoritative dates. Greenhouse first_published,
               Ashby publishedAt, Lever createdAt are the real posting dates,
               and posting age is the variable that actually predicts a reply.

So aggregators are used to DISCOVER employers, and the ATS board is then used
to TRACK them. When an aggregator surfaces a matching role, resolve_slug()
probes the three ATS APIs for that company and, on a hit, adds the board to the
watchlist. The next run polls it directly with true dates. The watchlist grows
itself instead of depending on what its owner already knew to look for.

No LinkedIn or Indeed scraping: against their terms and brittle.
"""
import json, re, urllib.request, urllib.error
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

UA = "careerops/0.1 (personal job tracker)"
TIMEOUT = 25

# Free, public, no API key. Each returns a list of postings.
FEEDS = {
    "arbeitnow": "https://www.arbeitnow.com/api/job-board-api",
    "remotive":  "https://remotive.com/api/remote-jobs?limit=400",
    "remoteok":  "https://remoteok.com/api",
    "jobicy":    "https://jobicy.com/api/v2/remote-jobs?count=100",
    "themuse":   "https://www.themuse.com/api/public/jobs?page={page}",
}
ATS_PROBE = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "ashby":      "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "lever":      "https://api.lever.co/v0/postings/{slug}?mode=json",
}


def _get(url: str) -> "Optional[object]":
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _epoch_iso(sec) -> Optional[str]:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(sec), tz=timezone.utc).isoformat()[:19]
    except Exception:
        return None


def _strip_html(s: str) -> str:
    import html as _h
    return re.sub(r"\s{2,}", " ", _h.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def fetch_feed(name: str, pages: int = 3) -> list:
    """Normalize one aggregator into the shape discover.fetch() already returns."""
    out = []
    if name == "themuse":
        raw = []
        for p in range(1, pages + 1):
            d = _get(FEEDS[name].format(page=p)) or {}
            raw += d.get("results", [])
            if not d.get("results"):
                break
        for j in raw:
            out.append({
                "title": j.get("name"),
                "company": ((j.get("company") or {}).get("name")),
                "location": ", ".join(l.get("name", "") for l in (j.get("locations") or [])),
                "url": (j.get("refs") or {}).get("landing_page"),
                "jd_text": _strip_html(j.get("contents", "")),
                "external_id": f"themuse:{j.get('id')}",
                "posted_at": (j.get("publication_date") or "")[:19],
            })
        return out

    d = _get(FEEDS[name])
    if d is None:
        return []
    if name == "arbeitnow":
        for j in d.get("data", []):
            out.append({
                "title": j.get("title"), "company": j.get("company_name"),
                "location": j.get("location") or ("Remote" if j.get("remote") else ""),
                "url": j.get("url"), "jd_text": _strip_html(j.get("description", "")),
                "external_id": f"arbeitnow:{j.get('slug')}",
                "posted_at": _epoch_iso(j.get("created_at")),
            })
    elif name == "remotive":
        for j in d.get("jobs", []):
            out.append({
                "title": j.get("title"), "company": j.get("company_name"),
                "location": j.get("candidate_required_location") or "Remote",
                "url": j.get("url"), "jd_text": _strip_html(j.get("description", "")),
                "external_id": f"remotive:{j.get('id')}",
                "posted_at": (j.get("publication_date") or "")[:19],
            })
    elif name == "remoteok":
        for j in (d[1:] if isinstance(d, list) else []):
            out.append({
                "title": j.get("position") or j.get("title"), "company": j.get("company"),
                "location": j.get("location") or "Remote",
                "url": j.get("url") or j.get("apply_url"),
                "jd_text": _strip_html(j.get("description", "")),
                "external_id": f"remoteok:{j.get('id')}",
                "posted_at": (j.get("date") or "")[:19],
            })
    elif name == "jobicy":
        for j in d.get("jobs", []):
            out.append({
                "title": j.get("jobTitle"), "company": j.get("companyName"),
                "location": j.get("jobGeo") or "Remote",
                "url": j.get("url"), "jd_text": _strip_html(j.get("jobDescription", "")),
                "external_id": f"jobicy:{j.get('id')}",
                "posted_at": (j.get("pubDate") or "")[:19],
            })
    return out


def slug_candidates(company: str) -> list:
    """ATS slugs are the company name with the punctuation taken out. Try the
    plausible spellings rather than one guess: Clean Harbors publishes as
    cleanharbors, In-Stride Health as instridehealth."""
    c = (company or "").strip().lower()
    if not c:
        return []
    base = re.sub(r"[^a-z0-9]+", "", c)
    dashed = re.sub(r"[^a-z0-9]+", "-", c).strip("-")
    nosuffix = re.sub(r"(inc|llc|ltd|corp|co|company|group|labs|technologies|tech)$", "", base)
    return [s for s in dict.fromkeys([base, dashed, nosuffix]) if len(s) > 2]


def resolve_slug(company: str) -> "Optional[tuple]":
    """Probe the three ATS APIs for a company name. Returns (board, slug) or None."""
    for slug in slug_candidates(company):
        for board, url in ATS_PROBE.items():
            d = _get(url.format(slug=slug))
            if not d:
                continue
            n = len(d) if isinstance(d, list) else len(d.get("jobs", []) or [])
            if n:
                return board, slug
    return None


def harvest(feeds=None, pages: int = 3) -> list:
    """Every posting from every feed, in one normalized list."""
    names = list(feeds or FEEDS)
    out = []
    with ThreadPoolExecutor(len(names)) as ex:
        for res in ex.map(lambda n: fetch_feed(n, pages), names):
            out += res
    return out


def expand(conn, cfg: dict, config_path: str, matches_fn, pages: int = 3) -> dict:
    """Aggregator sweep: surface matching roles at companies nobody listed, then
    promote those companies to the watchlist so the next run polls their board
    directly and gets the real posting date."""
    stats = {"harvested": 0, "matched": 0, "companies_new": 0, "boards_added": 0}
    jobs = harvest(pages=pages)
    stats["harvested"] = len(jobs)
    hits = [j for j in jobs if matches_fn(j, cfg["titles"], cfg["locations"],
                                          cfg.get("exclude_titles", ()))]
    stats["matched"] = len(hits)

    known = {w["company"].lower() for w in cfg["watchlist"]}
    have = {(w["board"], w["slug"]) for w in cfg["watchlist"]}
    fresh = {(j.get("company") or "").strip() for j in hits}
    fresh = sorted(c for c in fresh if c and c.lower() not in known)
    stats["companies_new"] = len(fresh)

    added = []
    with ThreadPoolExecutor(8) as ex:
        for company, hit in zip(fresh, ex.map(resolve_slug, fresh)):
            if hit and (hit[0], hit[1]) not in have:
                added.append({"company": company, "board": hit[0], "slug": hit[1]})
    if added:
        cfg["watchlist"] += added
        with open(config_path, "w") as f:
            json.dump(cfg, f, indent=2)
    stats["boards_added"] = len(added)
    stats["added"] = [f'{a["company"]}({a["board"]}:{a["slug"]})' for a in added]
    return stats
