"""Every public ATS board that can be found, so discovery stops depending on a hand-kept list.

The watchlist decided where the tool looked. 252 companies picked by hand, each polled on
one board, produced 0 to 7 new roles a day, and on 2026-10-06 the open prospects scoring
70 or better numbered 14, two of them under three weeks old. Fit was never the constraint.
Reach was.

Common Crawl indexes the public job board pages of Ashby, Greenhouse and Lever, and the
company slug is the first path segment of every one of those URLs. One page of the Ashby
index named 1,724 companies against the watchlist's 87. Harvesting the slugs gives a
board universe that grows with the crawl, and every board in it is polled through the
same public API and the same gates as the watchlist. Which company a role belongs to
plays no part in whether it surfaces; the fit score decides that.

Workday has no equivalent. A tenant lives on its own hostname with a site name the URL
does not reveal, so the Workday entries stay on the watchlist.
"""
import json, re, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

from . import discover as dsc

CC_COLLINFO = "https://index.commoncrawl.org/collinfo.json"
CC_INDEX = "https://index.commoncrawl.org/{index}-index"
HOSTS = {
    "ashby":      ("jobs.ashbyhq.com",),
    "greenhouse": ("boards.greenhouse.io", "job-boards.greenhouse.io"),
    "lever":      ("jobs.lever.co",),
}
GREENHOUSE_LIST = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
GREENHOUSE_META = "https://boards-api.greenhouse.io/v1/boards/{slug}"

# First path segments that belong to the board software rather than to a company.
RESERVED = {"embed", "api", "v1", "jobs", "job_app", "static", "assets", "favicon.ico",
            "robots.txt", "sitemap.xml", "careers", "search"}
# Ashby allows board names with spaces ("Flock Safety", "Hippocratic AI"), and a raw
# space reaches urllib as an invalid URL, which raises ValueError rather than OSError and
# would escape the fetcher's handler. Slugs are therefore stored percent-encoded, which
# the APIs accept as is: api.ashbyhq.com/posting-api/job-board/flock%20safety answers 200.
SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9._%-]{0,119}$")

WORKERS = 8          # concurrent board requests; the three APIs are built for this
MISS_LIMIT = 3       # consecutive empty polls before a board is rested
RECHECK_DAYS = 14    # how long a rested board waits before it is tried again


def slug_from_url(board: str, url: str) -> Optional[str]:
    p = urllib.parse.urlsplit(url)
    if board == "greenhouse" and p.path.startswith("/embed/"):
        seg = (urllib.parse.parse_qs(p.query).get("for") or [""])[0]
    else:
        seg = p.path.strip("/").split("/")[0]
    seg = urllib.parse.unquote(seg).strip().lower()
    if not seg or seg in RESERVED:
        return None
    seg = urllib.parse.quote(seg, safe="")
    return seg if SLUG_OK.match(seg) else None


def name_from_slug(slug: str) -> str:
    words = re.split(r"[-_.\s]+", urllib.parse.unquote(slug))
    return " ".join(w.capitalize() for w in words if w) or slug


def _get_text(url: str, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": dsc.UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def latest_indexes(n: int = 3) -> list:
    return [c["id"] for c in json.loads(_get_text(CC_COLLINFO))[:n]]


def harvest(indexes: Iterable[str], hosts: dict = HOSTS, max_pages: Optional[int] = None,
            pause: float = 1.0) -> dict:
    """Company slugs per board, read from the Common Crawl URL index.

    Any error on a host stops that host for the rest of the run. The index is a shared
    free service; a 502 means it is busy, and retrying into it helps nobody.
    """
    found = {b: set() for b in hosts}
    stopped = []
    for index in indexes:
        for board, names in hosts.items():
            for host in names:
                # The wildcard goes in literally. Encoded as %2A by urlencode, the same
                # query returned 608 Ashby slugs where the literal form had returned
                # 1,689 from a single page.
                base = CC_INDEX.format(index=index) + f"?url={host}/*&output=json&fl=url"
                try:
                    pages = json.loads(_get_text(base + "&showNumPages=true"))["pages"]
                except (OSError, ValueError, KeyError) as e:
                    stopped.append(f"{index} {host}: {e}")
                    continue
                for page in range(pages if max_pages is None else min(pages, max_pages)):
                    try:
                        body = _get_text(f"{base}&page={page}")
                    except OSError as e:
                        stopped.append(f"{index} {host} page {page}: {e}")
                        break
                    for line in body.splitlines():
                        try:
                            s = slug_from_url(board, json.loads(line)["url"])
                        except (ValueError, KeyError):
                            continue
                        if s:
                            found[board].add(s)
                    time.sleep(pause)
    return {"found": found, "stopped": stopped}


def store(conn, found: dict, source: str) -> int:
    n = 0
    for board, slugs in found.items():
        for s in sorted(slugs):
            n += conn.execute("INSERT OR IGNORE INTO boards (board, slug, source) VALUES (?, ?, ?)",
                              (board, s, source)).rowcount
    conn.commit()
    return n


def boards_to_poll(conn, watchlist: list) -> list:
    """The watchlist as it stands, then every harvested board it does not already cover.

    A board that came back empty MISS_LIMIT times in a row is rested for RECHECK_DAYS.
    Most of the crawl is companies that are not hiring this month, and polling all of
    them every run would spend most of the sweep on empty boards.
    """
    out = list(watchlist)
    seen = {(w["board"], (w["slug"] or "").lower()) for w in watchlist}
    rows = conn.execute(
        """SELECT board, slug, company FROM boards
           WHERE misses < ? OR last_checked IS NULL
              OR julianday('now') - julianday(last_checked) >= ?
           ORDER BY board, slug""", (MISS_LIMIT, RECHECK_DAYS)).fetchall()
    for r in rows:
        if (r["board"], r["slug"]) in seen:
            continue
        out.append({"company": r["company"] or name_from_slug(r["slug"]),
                    "board": r["board"], "slug": r["slug"], "universe": True})
    return out


def _title_hit(title: str, titles: Iterable[str], excludes: Iterable[str]) -> bool:
    """The title half of discover.matches, so the prefilter can only drop what it would."""
    t = (title or "").lower()
    if any(x.lower() in t for x in excludes):
        return False
    return any(dsc._kw(k).search(t) for k in titles)


def _poll(w: dict, titles: list, excludes: list):
    """(jobs worth gating, postings listed, company name) for one board.

    Greenhouse is the only board with a cheap listing: the titles come without the
    descriptions, so the full board is fetched only when at least one title could match.
    Ashby and Lever return everything in one response either way.
    """
    board, slug = w["board"], w["slug"]
    name = None
    if board == "greenhouse":
        listed = dsc._get(GREENHOUSE_LIST.format(slug=slug))
        if listed is None:
            return None, 0, None
        n = len(listed.get("jobs", []))
        if not any(_title_hit(j.get("title"), titles, excludes) for j in listed.get("jobs", [])):
            return [], n, None
        name = (dsc._get(GREENHOUSE_META.format(slug=slug)) or {}).get("name")
        jobs = dsc.fetch(board, slug, titles)
    else:
        jobs = dsc.fetch(board, slug, titles)
        n = len(jobs)
    # Keep only what the title gate would pass. Holding every description from a few
    # thousand boards in memory until discover runs is the alternative.
    return [j for j in jobs if _title_hit(j.get("title"), titles, excludes)], n, name


def prefetch(conn, boards: list, titles: list, excludes: list = (), workers: int = WORKERS) -> tuple:
    """Poll every universe board concurrently. Returns ({(board, slug): jobs}, stats).

    Requests run on threads; every database write stays on the calling thread.
    """
    uni = [w for w in boards if w.get("universe")]
    out, stats = {}, {"universe_boards": len(uni), "listed": 0, "title_hits": 0,
                      "hiring": 0, "empty": 0}

    def one(w):
        try:
            return w, _poll(w, titles, list(excludes))
        except Exception:
            return w, (None, 0, None)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for w, (jobs, n, name) in ex.map(one, uni):
            out[(w["board"], w["slug"])] = jobs or []
            stats["listed"] += n
            stats["title_hits"] += len(jobs or [])
            stats["hiring" if n else "empty"] += 1
            if name and name.strip():
                w["company"] = name.strip()
            conn.execute(
                """UPDATE boards SET last_checked = datetime('now'), last_jobs = ?,
                          misses = CASE WHEN ? > 0 THEN 0 ELSE misses + 1 END,
                          company = COALESCE(?, company)
                   WHERE board = ? AND slug = ?""",
                (n, n, (name or "").strip() or None, w["board"], w["slug"]))
    conn.commit()
    return out, stats


def summary(conn) -> dict:
    row = conn.execute(
        """SELECT COUNT(*) total, SUM(last_checked IS NOT NULL) polled,
                  SUM(last_jobs > 0) hiring, SUM(misses >= ?) rested
           FROM boards""", (MISS_LIMIT,)).fetchone()
    by = dict(conn.execute("SELECT board, COUNT(*) FROM boards GROUP BY board").fetchall())
    return {"total": row["total"] or 0, "polled": row["polled"] or 0, "hiring": row["hiring"] or 0,
            "rested": row["rested"] or 0, **{f"on_{k}": v for k, v in sorted(by.items())}}
