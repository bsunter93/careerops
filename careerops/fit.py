"""Fit analysis: JD vs profile, producing a score AND the reasoning behind it.

Backends, in order: the Anthropic Messages API (key from a gitignored .env or the
environment), then the local `claude` CLI as a fallback. Output is strict JSON;
anything unparseable returns None rather than a fabricated number.

The key is read from .env and never logged, printed, or written back to disk.
"""
import json, os, subprocess, pathlib, re, urllib.request, urllib.error
from typing import Optional

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROFILE = ROOT / "profile.md"
CONFIG = ROOT / "config.json"


def company_policy(company: str) -> dict:
    """Per-company constraints (history, minimum score, remote requirement)."""
    try:
        pol = json.loads(CONFIG.read_text()).get("company_policy", {})
    except Exception:
        return {}
    for name, rules in pol.items():
        if name.lower() in (company or "").lower():
            return rules
    return {}
ENV_FILE = ROOT / ".env"


def _load_env() -> None:
    """Read KEY=value from a gitignored .env. Never logged, never echoed.
    Real environment variables win, so CI/cron can override."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_env()

MODEL = os.environ.get("CAREEROPS_MODEL", "claude-sonnet-5")
# Only honor a custom base URL if a key is present for it; otherwise use the public API.
API_BASE = (os.environ.get("CAREEROPS_API_BASE") or "https://api.anthropic.com").rstrip("/")

PROMPT = """You are scoring how well one candidate fits one job description.

Be skeptical and calibrated. Most applications are mediocre fits; reserve
scores above 80 for genuinely strong matches. Penalize missing hard requirements.
Never invent candidate experience that is not in the profile.

Score CAPABILITY fit and PREFERENCE fit together. A role the candidate could do well
but does not want is not a good fit. Apply the "Preferences and dealbreakers" section
strictly: dealbreakers cap the score below 40 regardless of skill match.

<candidate_profile>
{profile}
</candidate_profile>

{policy}<job>
Company: {company}
Title: {title}
Location: {location}
Posted pay band: {comp}
Description:
{jd}
</job>

Return ONLY a JSON object, no prose, no code fence:
{{
  "score": <int 0-100>,
  "verdict": "<strong|plausible|stretch|poor>",
  "meets": ["hard requirements the candidate clearly meets"],
  "gaps": ["hard requirements the candidate does not meet"],
  "emphasize": ["specific bullets/facts from the profile to lead with"],
  "level_read": "<is this above, at, or below the candidate's level>",
  "preference_flags": ["any preference mismatch or dealbreaker, e.g. requires managing a team, comp below floor, onsite elsewhere"],
  "one_line": "<one sentence on whether to apply and why>"
}}"""


def _call_api(prompt: str, timeout: int = 180) -> Optional[str]:
    """Direct Messages API. Preferred: works headlessly and in cron."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    body = json.dumps({"model": MODEL, "max_tokens": 2500,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        API_BASE + "/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        return "".join(b.get("text", "") for b in data.get("content", []))
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None


def _call_cli(prompt: str, timeout: int = 180) -> Optional[str]:
    """Fallback: local `claude` CLI. Requires an interactive `claude /login` first."""
    try:
        p = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    out = (p.stdout or "").strip()
    if p.returncode != 0 or "Invalid API key" in out or "/login" in out:
        return None
    return out


def _call_claude(prompt: str, timeout: int = 180) -> Optional[str]:
    return _call_api(prompt, timeout) or _call_cli(prompt, timeout)


def backend_status() -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    if _call_cli('Reply with exactly: ok', timeout=60) == "ok":
        return "cli"
    return "none"


def _parse(raw: str) -> Optional[dict]:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)          # tolerate stray prose/fences
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(d.get("score"), int) or not 0 <= d["score"] <= 100:
        return None
    return d


def score_role(company: str, title: str, location: str, jd: str,
               profile: Optional[str] = None,
               comp_min: Optional[int] = None, comp_max: Optional[int] = None) -> Optional[dict]:
    """Score one role. The pay band is an argument because it is often not in the text.

    Ashby and Lever carry compensation in a structured field, which `extract_comp` reads
    into comp_min/comp_max. Those columns were selected for the pre-rank sort and then
    dropped on the floor before the model saw anything, so every posting whose band lives
    outside the description body was scored as though its pay were unknown, and judged
    against the comp and relocation rules on that basis. A $293-325K remote role came back
    at 28 with "no published salary range to verify it clears the bar" in its own gaps
    list. Same shape as the bug this function's own comment already describes one level
    down: a value selected, then not handed to the thing that needs it.
    """
    if not jd or len(jd) < 200:
        return None                              # no JD, no score. Never guess.
    prof = profile if profile is not None else PROFILE.read_text()
    pol = company_policy(company)
    block = ""
    if pol:
        block = ("<company_policy>\nThis candidate has history with this company. Weigh it.\n"
                 f"History: {pol.get('history','')}\n"
                 f"Rule: {pol.get('rule','')}\n</company_policy>\n\n")
    if comp_min and comp_max:
        comp = f"${comp_min:,} to ${comp_max:,} base, from the posting"
    elif comp_min or comp_max:
        comp = f"${(comp_min or comp_max):,} base, from the posting (one end only)"
    else:
        comp = "not published in a structured field; read the description for it"
    return _parse(_call_claude(PROMPT.format(
        profile=prof, policy=block, company=company, title=title,
        location=location or "unspecified", comp=comp, jd=jd[:12000])))


def score_pending(conn, limit: int = 10, rescore: bool = False) -> dict:
    """Score discovered roles that have a JD. With rescore=True, re-evaluate
    already-scored roles (use after changing profile.md or the prompt)."""
    cond = "" if rescore else "AND (a.id IS NULL OR a.fit_score IS NULL)"
    rows = conn.execute(f"""
        SELECT r.id, r.title, r.location, r.jd_text, c.name company,
               r.comp_min, r.comp_max, r.posted_at
        FROM roles r JOIN companies c ON c.id = r.company_id
        LEFT JOIN applications a ON a.role_id = r.id
        WHERE r.jd_text IS NOT NULL AND LENGTH(r.jd_text) > 200 {cond}""").fetchall()
    # Spend the model budget on the top of the deterministic pre-rank rather than on
    # whatever was discovered most recently. Discovery order has nothing to do with
    # relevance, and once the watchlist widened past the curated 87 it stopped being a
    # harmless default: a payer publishing 19,443 postings can fill a whole run.
    #
    # The comp band and the posting date have to be selected for this, not passed as
    # None. Two of the seven pre-rank components read them, and handing over nulls
    # flattens both to their unknown-value constant for every row, which is a third of
    # the ranking signal silently switched off.
    from .prerank import prerank
    rows = sorted(rows, key=lambda r: -prerank(r["title"], r["jd_text"], r["comp_min"],
                                               r["comp_max"], r["posted_at"])["score"]
                  )[:limit]
    stats = {"scored": 0, "skipped": 0}
    prof = PROFILE.read_text()
    for r in rows:
        res = score_role(r["company"], r["title"], r["location"], r["jd_text"], prof,
                         r["comp_min"], r["comp_max"])
        if not res:
            stats["skipped"] += 1
            continue
        app = conn.execute("SELECT id FROM applications WHERE role_id=?", (r["id"],)).fetchone()
        aid = app["id"] if app else None
        if aid is None:
            from . import db
            aid = db.get_or_create_application(conn, r["id"], channel="discovered")
            conn.execute("UPDATE applications SET status='prospect' WHERE id=?", (aid,))
        conn.execute("UPDATE applications SET fit_score=?, fit_reasoning=? WHERE id=?",
                     (res["score"], json.dumps(res, indent=2), aid))
        conn.commit()          # commit per row: a long run survives interruption
        stats["scored"] += 1
    conn.commit()
    return stats
