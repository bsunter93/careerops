"""Generate a role-tailored resume from a scored application.

The fit analysis already says what to lead with; this turns that into an ordered,
trimmed document. Bullet selection is deterministic (tag + token overlap against
`emphasize`, the role title, and the JD) so it is inspectable and repeatable.
Only the two prose lines -- tagline and profile -- go through the model.
"""
import json, re, subprocess, pathlib, shutil
from typing import Optional
from . import fit

ROOT = pathlib.Path(__file__).resolve().parent.parent
RESUME = ROOT / "resume"
MASTER = RESUME / "master.json"
MAX_BULLETS = 14
MIN_PER_GROUP = 2

STOP = set("""a an the and or of for to in on with by at from as is are was were be been being this that
these those it its his her their our your you we they i he she them us me my not no so than then when
where which who whom what how why all any both each few more most other some such only own same too very
can will just should now role team work working experience years year across into over under about""".split())


def _tokens(s: str):
    return {w for w in re.findall(r"[a-z0-9$%+]{3,}", (s or "").lower()) if w not in STOP}


def _load_master():
    return json.loads(MASTER.read_text())


def _all_bullets(m):
    out = []
    for i, e in enumerate(m["experience"]):
        if e.get("roles"):
            for j, r in enumerate(e["roles"]):
                for b in r["bullets"]:
                    out.append((f"{i}.{j}", b))
        else:
            for b in e["bullets"]:
                out.append((str(i), b))
    return out


def rank_bullets(m, emphasize, title, jd) -> list:
    """Score every bullet against the role. Emphasis is weighted heaviest."""
    emph = _tokens(" ".join(emphasize or []))
    ttl = _tokens(title)
    jdt = _tokens((jd or "")[:6000])
    scored = []
    for group, b in _all_bullets(m):
        bt = _tokens(b["text"] + " " + b["label"])
        tags = set(b["tags"])
        s = (6 * len(bt & emph) + 4 * len(tags & emph) +
             3 * len(bt & ttl) + 3 * len(tags & ttl) +
             1 * len(bt & jdt) + 1.5 * len(tags & jdt))
        scored.append({"id": b["id"], "group": group, "score": round(s, 1),
                       "label": b["label"], "tags": b["tags"]})
    scored.sort(key=lambda x: -x["score"])
    return scored


def select(scored, cap=MAX_BULLETS, floor=MIN_PER_GROUP) -> list:
    """Keep the best `cap` bullets, but never strip a job below `floor`."""
    groups = {}
    for b in scored:
        groups.setdefault(b["group"], []).append(b)
    keep = []
    for g, bs in groups.items():
        keep += bs[:floor]
    rest = [b for b in scored if b not in keep]
    keep += rest[: max(0, cap - len(keep))]
    order = {b["id"]: i for i, b in enumerate(scored)}
    return sorted([b["id"] for b in keep], key=lambda i: order[i])


def pick_skills(m, emphasize, title, jd, n=7):
    want = _tokens(" ".join(emphasize or []) + " " + title + " " + (jd or "")[:6000])
    out = {}
    for k in ("strategy", "data", "leadership"):
        items = m["skills"][k]["items"]
        ranked = sorted(items, key=lambda it: -(len(set(it["tags"]) & want) * 3 + len(_tokens(it["t"]) & want)))
        cap = n if k != "leadership" else 4
        chosen = ranked[:cap]
        keep = [it["t"] for it in items if it in chosen]      # keep the master's order
        out[k] = keep
    return out


PROSE = """Write two lines for a resume tailored to one job.

<candidate>{profile}</candidate>
<job>Company: {company}
Title: {title}
Description: {jd}</job>
<lead_with>{emph}</lead_with>

Return ONLY JSON, no prose or fences:
{{"tagline": "<3-6 words, uppercase, the role's own framing, no company name>",
  "profile": "<3 sentences, max 62 words. Sentence 1: a noun phrase naming the kind of operator he is. Sentence 2: the two most relevant concrete achievements from lead_with, with their numbers. Sentence 3: how he works plus the tools this job names. No em dashes. No adjectives like 'proven' or 'passionate'. Use no pronouns and no name: write 'Senior operations leader who ran...', never 'Benjamin Sunter is...' and never 'I ran...'.>"}}"""


# Resume summaries carry an implied subject. The prompt asked for "who he is" while also
# forbidding first person, which left third person as the only reading, and the model
# duly produced "Benjamin Sunter is a senior operations leader. He authored...". Prompt
# wording alone is not a guarantee, so the shape is enforced after the fact too.
_LEAD_NAME = re.compile(r"^[A-Z][a-z]+(?: [A-Z][a-z.]+){0,2} (?:is|was) (?:an?|the) ")
_SENT_PRONOUN = re.compile(r"(?<=[.!?] )(?:He|She|They) ")


def _impersonal(text: str) -> str:
    """Drop the subject so the summary reads in resume register."""
    t = _LEAD_NAME.sub("", text or "").strip()
    t = _SENT_PRONOUN.sub("", t)
    # removing a subject leaves the verb lowercase mid-paragraph
    t = re.sub(r"(?<=[.!?] )([a-z])", lambda m: m.group(1).upper(), t)
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    return re.sub(r"\s{2,}", " ", t)


def prose(company, title, jd, emphasize) -> Optional[dict]:
    raw = fit._call_claude(PROSE.format(
        profile=fit.PROFILE.read_text()[:3500], company=company, title=title,
        jd=(jd or "")[:6000], emph="; ".join(emphasize or [])))
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        d = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None
    if not d or not d.get("tagline") or not d.get("profile"):
        return None
    d["tagline"] = d["tagline"].upper().replace("—", "").strip()
    d["profile"] = _impersonal(d["profile"].replace("—", ",").strip())
    return d


def build(conn, app_id: int, out: Optional[str] = None, cap: int = MAX_BULLETS) -> dict:
    row = conn.execute("""SELECT a.id, c.name company, r.title, r.jd_text, a.fit_reasoning, a.fit_score
                          FROM applications a JOIN roles r ON r.id=a.role_id
                          JOIN companies c ON c.id=r.company_id WHERE a.id=?""", (app_id,)).fetchone()
    if not row:
        raise SystemExit(f"no application {app_id}")
    try:
        fr = json.loads(row["fit_reasoning"]) if row["fit_reasoning"] else {}
    except Exception:
        fr = {}
    emph = fr.get("emphasize") or []
    if not emph:
        raise SystemExit(f"application {app_id} has no fit analysis yet - run: careerops fit")

    m = _load_master()
    scored = rank_bullets(m, emph, row["title"], row["jd_text"])
    chosen = select(scored, cap=cap)
    pr = prose(row["company"], row["title"], row["jd_text"], emph)
    if not pr:
        raise SystemExit("prose generation failed (LLM backend unavailable)")

    safe = re.sub(r"[^A-Za-z0-9]+", "", row["company"])[:16]
    who = re.sub(r"[^A-Za-z]+", "", (m.get("header", {}).get("name", "Resume").split() or ["Resume"])[-1])
    out = out or str(ROOT / f"{who or 'Resume'}_{safe}_{app_id}.docx")
    variant = {"out": out, "tagline": pr["tagline"], "profile": pr["profile"],
               "skills": pick_skills(m, emph, row["title"], row["jd_text"]),
               "bullets": chosen}
    vpath = RESUME / f".variant_{app_id}.json"
    vpath.write_text(json.dumps(variant, indent=2))
    r = subprocess.run(["node", str(RESUME / "generate.js"), str(MASTER), str(vpath)],
                       capture_output=True, text=True, cwd=str(RESUME))
    if r.returncode != 0:
        raise SystemExit("generate.js failed:\n" + (r.stderr or r.stdout))
    return {"out": out, "tagline": pr["tagline"], "profile": pr["profile"],
            "bullets": chosen, "dropped": [b["id"] for b in scored if b["id"] not in chosen],
            "top": scored[:5], "company": row["company"], "title": row["title"],
            "score": row["fit_score"]}


def page_count(docx_path: str, keep: bool = False) -> "Optional[int]":
    """Render through real Word and count pages. Quick Look substitutes fonts (Calibri
    ships inside Office, not system-wide) and reports one page for a document Word sets
    as two, so this is the only trustworthy check.

    The PDF is written beside the .docx on purpose: Word is sandboxed and prompts for
    access to unfamiliar directories, and a prompt for a hidden folder like /tmp hangs
    the AppleEvent with no visible cause.
    """
    import re, subprocess, pathlib
    src = pathlib.Path(docx_path).resolve()
    # Keeping the render is the point once it is verified: the PDF is what gets sent,
    # because the one-page break depends on Calibri metrics the recipient may not have.
    out = src.with_suffix(".pdf") if keep else src.with_name("._pagecheck.pdf")
    if out.exists():
        out.unlink()
    script = f'''
    with timeout of 150 seconds
    tell application "Microsoft Word"
        open POSIX file "{src}"
        delay 2
        save as active document file name "{out}" file format format PDF
        delay 2
        close active document saving no
    end tell
    end timeout'''
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=180)
        data = out.read_bytes()
        return len(re.findall(rb"/Type\s*/Page[^s]", data))
    except Exception:
        return None
    finally:
        if out.exists() and not keep:
            out.unlink()
