"""Public employee sentiment per company: rating out of 5, sub-ratings, pros/cons.

Reference data only. It never touches fit_score or derived status, for the same
reason portal_snapshot never writes status: it is a third-party aggregate of
self-selected reviews, and it is easy to confuse two companies that share a name.
It informs the human, it does not move a number the system computes.

How it gets the data matters. Glassdoor and Blind have no public API, sit behind
bot detection and (for Blind) a work-email gate, and their terms forbid scraping.
So nothing here scrapes them. It runs a web search through the Anthropic Messages
API's server-side search tool and reads what the public result snippets say, which
is the same thing a person gets from a search engine. Sources are stored with every
row so any figure can be traced back.

Ambiguity is the main failure mode, not availability: a search for "Headway" returns
five unrelated employers. The prompt forces the model to name the entity it landed on
and to return low confidence when it cannot tell, and low-confidence rows are labelled
as such everywhere they surface.
"""
import json, os, re, urllib.request, urllib.error, sys
from typing import Optional

from .fit import API_BASE, MODEL, _load_env

_load_env()

PROMPT = """Find current public employee sentiment for one specific employer.

Employer: {company}
{hint}
Search the public web (Glassdoor, Indeed, Comparably, Blind and similar review sites
surface this in their public result snippets). Use the numbers those snippets state.

Two rules:
1. Do not invent or estimate any number. If a figure is not stated in a source you
   actually saw, return null for it. A null is correct; a plausible guess is not.
2. Company names collide. "Headway" alone matches at least five unrelated employers.
   Identify WHICH entity you found (industry, headquarters, rough size) and say so.
   If you cannot confirm it is the employer above, set confidence to "low".

Return ONLY a JSON object, no prose, no code fence:
{{
  "rating": <overall out of 5, or null>,
  "reviews_n": <number of reviews the rating is based on, or null>,
  "wlb": <work/life balance sub-rating out of 5, or null>,
  "comp_benefits": <out of 5, or null>,
  "culture": <out of 5, or null>,
  "career": <career opportunities out of 5, or null>,
  "recommend_pct": <percent who would recommend to a friend, or null>,
  "pros": ["3-5 recurring positives, in reviewers' own terms, short"],
  "cons": ["3-5 recurring negatives, in reviewers' own terms, short"],
  "wlb_summary": "<one sentence on work/life balance specifically>",
  "confidence": "<high|medium|low>",
  "entity_note": "<which company this is: industry, HQ, size. Say plainly if uncertain.>",
  "sources": [{{"title": "...", "url": "..."}}]
}}"""


def _search_api(prompt: str, timeout: int = 240) -> tuple[Optional[str], list]:
    """Messages API with the server-side web_search tool. Returns (text, citations).

    Search runs on Anthropic's side inside this one request, so there is no local
    tool loop and nothing here fetches a review site directly.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None, []
    body = json.dumps({
        "model": MODEL, "max_tokens": 2500,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        API_BASE + "/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # Surface the reason. A silent None here looks identical to "no data found",
        # and those need very different fixes.
        print(f"  api error {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        return None, []
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        print(f"  api error: {e}", file=sys.stderr)
        return None, []
    text, cites = [], []
    for b in data.get("content", []):
        if b.get("type") == "text":
            text.append(b.get("text", ""))
        elif b.get("type") == "web_search_tool_result":
            for item in (b.get("content") or []):
                if isinstance(item, dict) and item.get("url"):
                    cites.append({"title": item.get("title", ""), "url": item["url"]})
    return "".join(text), cites


def _num(v, lo=0.0, hi=5.0) -> Optional[float]:
    """Accept a number in range, reject anything else. No coercion of prose."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if lo <= v <= hi else None


def _parse(raw: str) -> Optional[dict]:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    out = {k: _num(d.get(k)) for k in ("rating", "wlb", "comp_benefits", "culture", "career")}
    n = d.get("reviews_n")
    out["reviews_n"] = int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) and n >= 0 else None
    p = d.get("recommend_pct")
    out["recommend_pct"] = int(p) if isinstance(p, (int, float)) and not isinstance(p, bool) and 0 <= p <= 100 else None
    out["pros"] = [str(x)[:200] for x in (d.get("pros") or [])][:5]
    out["cons"] = [str(x)[:200] for x in (d.get("cons") or [])][:5]
    out["wlb_summary"] = str(d.get("wlb_summary") or "")[:500]
    conf = str(d.get("confidence") or "").lower()
    out["confidence"] = conf if conf in ("high", "medium", "low") else "low"
    out["entity_note"] = str(d.get("entity_note") or "")[:400]
    out["sources"] = [s for s in (d.get("sources") or [])
                      if isinstance(s, dict) and s.get("url")][:6]
    # A row with no overall rating and no pros/cons carries nothing. Absent beats empty.
    if out["rating"] is None and not out["pros"] and not out["cons"]:
        return None
    return out


def fetch_company(company: str, hint: str = "") -> Optional[dict]:
    h = f"Disambiguating detail: {hint}\n" if hint else ""
    raw, cites = _search_api(PROMPT.format(company=company, hint=h))
    res = _parse(raw or "")
    if res and not res["sources"]:
        res["sources"] = cites[:6]     # fall back to what the search tool actually hit
    return res


def save(conn, company: str, d: dict) -> None:
    conn.execute("""
        INSERT INTO company_intel
          (company, rating, reviews_n, wlb, comp_benefits, culture, career,
           recommend_pct, pros, cons, wlb_summary, confidence, entity_note,
           sources, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(company) DO UPDATE SET
          rating=excluded.rating, reviews_n=excluded.reviews_n, wlb=excluded.wlb,
          comp_benefits=excluded.comp_benefits, culture=excluded.culture,
          career=excluded.career, recommend_pct=excluded.recommend_pct,
          pros=excluded.pros, cons=excluded.cons, wlb_summary=excluded.wlb_summary,
          confidence=excluded.confidence, entity_note=excluded.entity_note,
          sources=excluded.sources, fetched_at=excluded.fetched_at""",
        (company, d["rating"], d["reviews_n"], d["wlb"], d["comp_benefits"],
         d["culture"], d["career"], d["recommend_pct"],
         json.dumps(d["pros"]), json.dumps(d["cons"]), d["wlb_summary"],
         d["confidence"], d["entity_note"], json.dumps(d["sources"])))
    conn.commit()


def targets(conn, limit: int, refresh: bool, stale_days: int = 30) -> list:
    """Companies worth spending a search on: live prospects and open applications
    first, ranked by best fit score. Dead ends are not worth the call."""
    cond = "" if refresh else """
        AND (ci.company IS NULL
             OR julianday('now') - julianday(ci.fetched_at) > {d})""".format(d=stale_days)
    rows = conn.execute(f"""
        SELECT c.name, MAX(a.fit_score) best, COUNT(*) n
        FROM applications a
        JOIN roles r     ON r.id = a.role_id
        JOIN companies c ON c.id = r.company_id
        LEFT JOIN company_intel ci ON ci.company = c.name
        WHERE a.status NOT IN ('rejected','withdrawn') {cond}
        GROUP BY c.name
        ORDER BY (best IS NULL), best DESC, n DESC
        LIMIT ?""", (limit,)).fetchall()
    return [r["name"] for r in rows]


def run(conn, limit: int = 10, refresh: bool = False,
        company: Optional[str] = None) -> dict:
    names = [company] if company else targets(conn, limit, refresh)
    stats = {"fetched": 0, "missed": 0, "low_confidence": 0}
    for name in names:
        d = fetch_company(name)
        if not d:
            stats["missed"] += 1
            print(f"  {name}: no usable data")
            continue
        save(conn, name, d)
        stats["fetched"] += 1
        if d["confidence"] == "low":
            stats["low_confidence"] += 1
        r = f"{d['rating']:.1f}" if d["rating"] is not None else "  ?"
        w = f"{d['wlb']:.1f}" if d["wlb"] is not None else "  ?"
        print(f"  {name:<28} {r}/5  wlb {w}  ({d['confidence']})")
    return stats
