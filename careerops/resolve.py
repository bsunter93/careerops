"""Drain the review queue.

Stage 1 is deterministic: strip ATS boilerplate off company names that were
scraped out of subject lines, then merge the duplicates that creates.
Stage 2 asks the model to read the stored bodies for the rest, batched.
Nothing is deleted -- companies and roles are merged, events are re-pointed.
"""
import json, re, sqlite3
from typing import Optional
from . import db, fit

# Subject-line shapes that wrap a real company name.
STRIP = [
    r"^thank(?:s| you)?\s+(?:you\s+)?for\s+apply(?:ing)?\s+(?:to|at)\s+(?:the\s+)?(?P<c>.+)$",
    r"^thank(?:s| you)?\s+for\s+your\s+(?:interest|application)\s+(?:in|to|at)\s+(?P<c>.+)$",
    r"^thank\s+you\s+from\s+(?P<c>.+)$",
    r"^your\s+application\s+(?:to|for|at)\s+(?P<c>.+)$",
    r"^(?P<c>.+?)\s+application\s+(?:follow[- ]?up|update|status|received)$",
    r"^(?P<c>.+?)\s+was\s+received\b.*$",
    r"^(?P<c>.+?)\s*[-|–]\s*next\s+steps.*$",
    r"^(?P<c>.+?)\s*\|\s*.+$",
    r"^.+\s+(?:at|with)\s+(?P<c>[A-Z][\w&.'\- ]{1,28})$",
    r"^we\s+received\s+your\s+application.*$",
    r"^your\s+workday\s+account$",
    r"^thank\s+you\s+for\s+applying$",
]
# Junk that carries no company at all.
NULLISH = re.compile(r"^(we received your application|thank you for applying|your workday account|"
                     r"thank you for applying to|application received)$", re.I)

CANON = {
    "includedhealth": "Included Health", "datadoghq": "Datadog", "anthropic": "Anthropic",
    "doordash": "DoorDash", "openai": "OpenAI", "servicenow": "ServiceNow", "gitlab": "GitLab",
    "ustechsolutions": "US Tech Solutions", "paloaltonetworks": "Palo Alto Networks",
    "scaleai": "Scale AI", "nvidia": "NVIDIA", "linkedin": "LinkedIn", "sondermind": "SonderMind",
    "maybellquantumindustries": "Maybell Quantum", "finitestate": "Finite State",
    "greenhousemail": None, "sourcehire": None, "brighthire": None, "workday": None,
}


def normalize_company(name: str) -> Optional[str]:
    """Return a clean company name, or None when the string carries none."""
    n = (name or "").strip()
    if not n or NULLISH.match(n):
        return None
    for pat in STRIP:
        m = re.match(pat, n, re.I)
        if m:
            n = (m.groupdict().get("c") or "").strip() or n
            break
    n = re.sub(r"\s*[-–|,(].*$", "", n).strip()          # trailing role/req fragments
    n = re.sub(r"[!.,:;]+$", "", n).strip()
    n = re.sub(r"^(the|your|our)\s+", "", n, flags=re.I).strip()
    if not n or len(n) < 2 or len(n.split()) > 4:
        return None
    key = re.sub(r"[^a-z0-9]", "", n.lower())
    if key in CANON:
        return CANON[key]
    return n if n[:1].isupper() else n.title()


def merge_companies(conn, dry=False) -> dict:
    """Normalize every company, then fold duplicates into one row each."""
    stats = {"renamed": 0, "merged": 0, "orphaned": 0}
    rows = conn.execute("SELECT id, name FROM companies").fetchall()
    canon = {}
    for r in rows:
        new = normalize_company(r["name"])
        # Aliases were applied only in get_or_create_company, so they prevented the next
        # duplicate but never healed the one already stored. Headway's recruiter mailed
        # from findheadway.com, which had created a "Findheadway" row before the alias
        # existed, and the alias alone could not fold it. Apply the map here too, where
        # it is retroactive and idempotent.
        if new is not None:
            new = db._aliases().get(new.lower(), new)
        if new is None:
            stats["orphaned"] += 1
            continue
        canon.setdefault(new.lower(), []).append((r["id"], r["name"], new))
    if dry:
        for k, v in canon.items():
            if len(v) > 1 or v[0][1] != v[0][2]:
                stats["renamed"] += 1
        return stats
    for key, group in canon.items():
        # Prefer a row that already carries the canonical name, so the rename is a no-op.
        group.sort(key=lambda g: (g[1] != g[2], g[0]))
        keep_id, keep_name, new_name = group[0]
        for cid, _, _ in group[1:]:
            for r in conn.execute("SELECT id, title FROM roles WHERE company_id=?", (cid,)).fetchall():
                clash = conn.execute(
                    "SELECT id FROM roles WHERE company_id=? AND title=? COLLATE NOCASE",
                    (keep_id, r["title"])).fetchone()
                if clash:
                    _fold_role(conn, r["id"], clash["id"])      # same job, two rows
                else:
                    conn.execute("UPDATE roles SET company_id=? WHERE id=?", (keep_id, r["id"]))
            conn.execute("DELETE FROM companies WHERE id=?", (cid,))
            stats["merged"] += 1
        # Losers are gone, so the name is free.
        if keep_name != new_name:
            conn.execute("UPDATE companies SET name=? WHERE id=?", (new_name, keep_id))
            stats["renamed"] += 1
    conn.commit()
    return stats


def _fold_role(conn, src_id: int, keep_id: int) -> None:
    """Fold one role row into another: events follow, the duplicate application goes."""
    src = conn.execute("SELECT id FROM applications WHERE role_id=?", (src_id,)).fetchone()
    dst = conn.execute("SELECT id FROM applications WHERE role_id=?", (keep_id,)).fetchone()
    if src and dst:
        conn.execute("UPDATE events SET application_id=? WHERE application_id=?", (dst["id"], src["id"]))
        conn.execute("DELETE FROM applications WHERE id=?", (src["id"],))
    elif src:
        conn.execute("UPDATE applications SET role_id=? WHERE id=?", (keep_id, src["id"]))
    conn.execute("DELETE FROM roles WHERE id=?", (src_id,))


def dedupe_roles(conn) -> int:
    """One role per (company, title). Events and applications follow the survivor."""
    merged = 0
    dupes = conn.execute("""SELECT company_id, LOWER(title) t, COUNT(*) n
                            FROM roles GROUP BY company_id, LOWER(title) HAVING n > 1""").fetchall()
    for d in dupes:
        rs = conn.execute("""SELECT r.id, (SELECT COUNT(*) FROM applications a
                               JOIN events e ON e.application_id=a.id WHERE a.role_id=r.id) ev,
                               LENGTH(COALESCE(r.jd_text,'')) jd
                             FROM roles r WHERE r.company_id=? AND LOWER(r.title)=?
                             ORDER BY ev DESC, jd DESC""", (d["company_id"], d["t"])).fetchall()
        keep = rs[0]["id"]
        ka = conn.execute("SELECT id FROM applications WHERE role_id=?", (keep,)).fetchone()
        for r in rs[1:]:
            a = conn.execute("SELECT id FROM applications WHERE role_id=?", (r["id"],)).fetchone()
            if a and ka:
                conn.execute("UPDATE events SET application_id=? WHERE application_id=?", (ka["id"], a["id"]))
                conn.execute("DELETE FROM applications WHERE id=?", (a["id"],))
            elif a:
                conn.execute("UPDATE applications SET role_id=? WHERE id=?", (keep, a["id"]))
            conn.execute("DELETE FROM roles WHERE id=?", (r["id"],))
            merged += 1
    conn.commit()
    return merged


BATCH = """For each numbered email below, extract the hiring company and the exact job title.

Rules:
- company: the employer, never the ATS vendor (Greenhouse, Workday, Lever, Ashby) and never
  a fragment of the subject line.
- role: the job title as written. Use null if the email genuinely names no role.
- If the email is not about a job application at all, set both to null.

{items}

Return ONLY a JSON array, one object per item, no prose or fences:
[{{"i": <number>, "company": "<name or null>", "role": "<title or null>"}}]"""


def _batch(items) -> list:
    txt = "\n\n".join(
        f"[{i}] subject: {it['subject']}\n    from: {it['sender']}\n    body: {(it['body'] or '')[:700]}"
        for i, it in enumerate(items))
    raw = fit._call_claude(BATCH.format(items=txt))
    if not raw:
        return []
    m = re.search(r"\[.*\]", raw, re.S)
    try:
        return json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        return []


def set_identity(conn, app_id: int, company: Optional[str], role: Optional[str]) -> bool:
    """Point an application at the right company/role, folding into an existing row on collision."""
    cur = conn.execute("""SELECT a.role_id, r.company_id, r.title, c.name company
                          FROM applications a JOIN roles r ON r.id=a.role_id
                          JOIN companies c ON c.id=r.company_id WHERE a.id=?""", (app_id,)).fetchone()
    if not cur:
        return False
    company = normalize_company(company) if company else None
    company = company or cur["company"]
    role = (role or "").strip() or cur["title"]
    if company == cur["company"] and role == cur["title"]:
        return False
    cid = db.get_or_create_company(conn, company)
    twin = conn.execute("SELECT id FROM roles WHERE company_id=? AND title=? COLLATE NOCASE",
                        (cid, role)).fetchone()
    if twin and twin["id"] != cur["role_id"]:
        other = conn.execute("SELECT id FROM applications WHERE role_id=?", (twin["id"],)).fetchone()
        if other and other["id"] != app_id:
            conn.execute("UPDATE events SET application_id=? WHERE application_id=?", (other["id"], app_id))
            conn.execute("DELETE FROM applications WHERE id=?", (app_id,))
        else:
            conn.execute("UPDATE applications SET role_id=? WHERE id=?", (twin["id"], app_id))
        conn.execute("DELETE FROM roles WHERE id=? AND id NOT IN (SELECT role_id FROM applications)",
                     (cur["role_id"],))
    else:
        shared = conn.execute("SELECT COUNT(*) n FROM applications WHERE role_id=?",
                              (cur["role_id"],)).fetchone()["n"]
        if shared > 1:
            # Renaming a role row that several applications point at would relabel all of
            # them. Give this application its own role instead.
            new_rid = db.get_or_create_role(conn, cid, role)
            conn.execute("UPDATE applications SET role_id=? WHERE id=?", (new_rid, app_id))
        else:
            conn.execute("UPDATE roles SET company_id=?, title=? WHERE id=?",
                         (cid, role, cur["role_id"]))
    return True


def drain(conn, batch_size: int = 10, limit: int = 400, progress=print) -> dict:
    """Resolve queued events. Only items that still lack a company or role cost a model call."""
    rows = conn.execute("""SELECT rq.id rq, e.id ev, e.application_id app, e.subject, e.sender, e.body,
                                  r.title, c.name company
                           FROM review_queue rq JOIN events e ON e.id=rq.event_id
                           LEFT JOIN applications a ON a.id=e.application_id
                           LEFT JOIN roles r ON r.id=a.role_id
                           LEFT JOIN companies c ON c.id=r.company_id
                           WHERE rq.resolved=0 LIMIT ?""", (limit,)).fetchall()
    JUNK = {"thank you for applying", "we received your application", "your workday account"}
    need, auto = [], []
    for r in rows:
        bad_co = (r["company"] or "").lower() in JUNK or not r["company"]
        bad_role = (r["title"] or "").lower() in ("unknown role", "")
        (need if (r["app"] and (bad_co or bad_role) and r["body"]) else auto).append(r)

    st = {"queued": len(rows), "auto_resolved": len(auto), "sent": len(need),
          "identified": 0, "batches": 0, "failed": 0}
    for r in auto:
        conn.execute("UPDATE review_queue SET resolved=1 WHERE id=?", (r["rq"],))
    conn.commit()

    for i in range(0, len(need), batch_size):
        chunk = need[i:i + batch_size]
        out = _batch([dict(subject=c["subject"], sender=c["sender"], body=c["body"]) for c in chunk])
        st["batches"] += 1
        if not out:
            st["failed"] += len(chunk)
            continue
        by = {o.get("i"): o for o in out if isinstance(o, dict)}
        for j, c in enumerate(chunk):
            o = by.get(j)
            if o and (o.get("company") or o.get("role")):
                if set_identity(conn, c["app"], o.get("company"), o.get("role")):
                    st["identified"] += 1
            conn.execute("UPDATE review_queue SET resolved=1 WHERE id=?", (c["rq"],))
        conn.commit()
        progress(f"  batch {st['batches']}: {min(i+batch_size,len(need))}/{len(need)} "
                 f"· {st['identified']} identified")
    db.recompute_all(conn)
    return st


SPLIT = """Each numbered email below was sent by the same company but may concern
different job applications. For each, name the exact role it refers to.

Rules:
- role: the job title named in the email. Use null when the email names no role.
- Do not invent a role. Two emails naming the same role must return the identical string.

{items}

Return ONLY a JSON array, no prose or fences:
[{{"i": <number>, "role": "<title or null>"}}]"""


def split_application(conn, app_id: int, dry=False) -> dict:
    """One application row can accumulate events from several distinct applications at
    the same company (generic 'Thanks for applying!' acks). Regroup by the role each
    email names, so an old interview loop stops masquerading as a live one."""
    info = conn.execute("""SELECT r.company_id, c.name company, r.title
                           FROM applications a JOIN roles r ON r.id=a.role_id
                           JOIN companies c ON c.id=r.company_id WHERE a.id=?""", (app_id,)).fetchone()
    evs = conn.execute("""SELECT id, occurred_at, type, subject, body FROM events
                          WHERE application_id=? AND type!='noise' ORDER BY occurred_at""",
                       (app_id,)).fetchall()
    if not info or len(evs) < 3:
        return {"split": 0}

    items = "\n\n".join(
        f"[{i}] date: {e['occurred_at'][:10]}\n    subject: {e['subject']}\n    body: {(e['body'] or '')[:600]}"
        for i, e in enumerate(evs))
    raw = fit._call_claude(SPLIT.format(items=items))
    if not raw:
        return {"split": 0, "error": "no response"}
    m = re.search(r"\[.*\]", raw, re.S)
    try:
        got = json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        return {"split": 0, "error": "unparseable"}

    buckets = {}
    for o in got:
        if not isinstance(o, dict):
            continue
        i, role = o.get("i"), (o.get("role") or "").strip()
        if i is None or i >= len(evs) or not role:
            continue
        buckets.setdefault(role, []).append(evs[i]["id"])
    if len(buckets) < 2:
        return {"split": 0, "roles": list(buckets)}
    if dry:
        return {"split": len(buckets) - 1, "roles": {k: len(v) for k, v in buckets.items()}}

    # the biggest bucket keeps the existing row; the rest get their own
    order = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    keep_role, _ = order[0]
    conn.execute("UPDATE roles SET title=? WHERE id=(SELECT role_id FROM applications WHERE id=?)",
                 (keep_role, app_id))
    made = 0
    for role, eids in order[1:]:
        rid = db.get_or_create_role(conn, info["company_id"], role)
        nid = db.get_or_create_application(conn, rid, channel="split")
        for eid in eids:
            conn.execute("UPDATE events SET application_id=? WHERE id=?", (nid, eid))
        db.recompute_status(conn, nid)
        made += 1
    conn.commit()
    db.recompute_status(conn, app_id)
    return {"split": made, "roles": {k: len(v) for k, v in order}}


def split_collapsed(conn, min_events: int = 3, progress=print) -> dict:
    """Find every application carrying several events and try to regroup it."""
    rows = conn.execute("""SELECT a.id, c.name, COUNT(e.id) n
                           FROM applications a JOIN roles r ON r.id=a.role_id
                           JOIN companies c ON c.id=r.company_id
                           JOIN events e ON e.application_id=a.id AND e.type!='noise'
                           GROUP BY a.id HAVING n >= ? ORDER BY n DESC""", (min_events,)).fetchall()
    st = {"examined": len(rows), "split": 0, "new_apps": 0}
    for r in rows:
        out = split_application(conn, r["id"])
        if out.get("split"):
            st["split"] += 1
            st["new_apps"] += out["split"]
            progress(f"  {r['name']}: {r['n']} events -> {out['roles']}")
    db.recompute_all(conn)
    return st


def reidentify(conn, min_conf: float = 0.80, dry: bool = False) -> dict:
    """Re-derive each event's company and role from its stored subject + body and move
    it to the right application. Deterministic and free: this is what should have
    happened at ingestion, and it makes the LLM split pass unnecessary going forward."""
    from .classify import classify
    rows = conn.execute("""SELECT e.id, e.subject, e.sender, e.body, e.application_id,
                                  r.title, c.name company, r.company_id
                           FROM events e
                           LEFT JOIN applications a ON a.id = e.application_id
                           LEFT JOIN roles r ON r.id = a.role_id
                           LEFT JOIN companies c ON c.id = r.company_id
                           WHERE e.type != 'noise' AND e.body IS NOT NULL
                           ORDER BY e.occurred_at""").fetchall()
    st = {"examined": len(rows), "moved": 0, "role_filled": 0, "skipped": 0}
    for e in rows:
        c = classify(e["subject"] or "", e["sender"] or "", e["body"] or "")
        from .classify import creates_application
        if not creates_application(c) or c.confidence < min_conf or not c.company or not c.role:
            st["skipped"] += 1
            continue
        same_co = (e["company"] or "").lower() == c.company.lower()
        same_rl = (e["title"] or "").lower() == c.role.lower()
        if same_co and same_rl:
            continue
        if dry:
            st["moved"] += 1
            continue
        cid = db.get_or_create_company(conn, c.company)
        rid = db.get_or_create_role(conn, cid, c.role)
        ev_time = conn.execute("SELECT occurred_at FROM events WHERE id=?", (e["id"],)).fetchone()
        aid = db.get_or_create_application(conn, rid, applied_on=(ev_time["occurred_at"] or "")[:10],
                                           submitted_at=ev_time["occurred_at"],
                                           is_ack=(c.event_type == "ack"), channel="reidentified")
        if aid != e["application_id"]:
            conn.execute("UPDATE events SET application_id=? WHERE id=?", (aid, e["id"]))
            st["moved"] += 1
        if (e["title"] or "").lower() in ("unknown role", ""):
            st["role_filled"] += 1
    if not dry:
        # applications left holding nothing are artefacts of the move
        conn.execute("""DELETE FROM applications WHERE channel IN ('gmail','csv','reidentified','split')
                        AND status != 'prospect'
                        AND id NOT IN (SELECT DISTINCT application_id FROM events
                                       WHERE application_id IS NOT NULL)""")
        conn.execute("DELETE FROM roles WHERE id NOT IN (SELECT role_id FROM applications)")
        conn.execute("DELETE FROM companies WHERE id NOT IN (SELECT company_id FROM roles)")
        conn.commit()
        db.recompute_all(conn)
    return st


def split_resubmissions(conn, gap_hours: int = db.RESUBMIT_HOURS, progress=print) -> dict:
    """One row per submission. An application holding acks far enough apart is really
    several applications to the same role; later events follow the nearest preceding ack."""
    st = {"examined": 0, "split": 0, "new_apps": 0}
    rows = conn.execute("""SELECT a.id, r.id role_id, c.name company, r.title
                           FROM applications a JOIN roles r ON r.id=a.role_id
                           JOIN companies c ON c.id=r.company_id
                           WHERE a.status != 'prospect'""").fetchall()
    for a in rows:
        acks = conn.execute("""SELECT id, occurred_at FROM events
                               WHERE application_id=? AND type='ack' ORDER BY occurred_at""",
                            (a["id"],)).fetchall()
        if len(acks) < 2:
            continue
        st["examined"] += 1
        groups = [[acks[0]]]
        for e in acks[1:]:
            prev = groups[-1][-1]["occurred_at"]
            gap = conn.execute("SELECT (julianday(?) - julianday(?)) * 24 AS h",
                               (e["occurred_at"], prev)).fetchone()["h"]
            (groups[-1] if gap is not None and gap < gap_hours else groups.append([]) or groups[-1]).append(e)
        if len(groups) < 2:
            continue
        others = conn.execute("""SELECT id, occurred_at FROM events
                                 WHERE application_id=? AND type NOT IN ('ack','noise')
                                 ORDER BY occurred_at""", (a["id"],)).fetchall()
        starts = [g[0]["occurred_at"] for g in groups]
        conn.execute("UPDATE applications SET submitted_at=?, applied_on=? WHERE id=?",
                     (starts[0], starts[0][:10], a["id"]))
        made = 0
        for gi, g in enumerate(groups[1:], start=1):
            nid = db.get_or_create_application(conn, a["role_id"], applied_on=starts[gi][:10],
                                               submitted_at=starts[gi], is_ack=True, channel="resubmit")
            for e in g:
                conn.execute("UPDATE events SET application_id=? WHERE id=?", (nid, e["id"]))
            made += 1
        # each non-ack event belongs to the submission it followed
        for e in others:
            idx = max([i for i, s0 in enumerate(starts) if s0 <= e["occurred_at"]] or [0])
            if idx == 0:
                continue
            tgt = conn.execute("""SELECT id FROM applications WHERE role_id=? AND submitted_at=?""",
                               (a["role_id"], starts[idx])).fetchone()
            if tgt:
                conn.execute("UPDATE events SET application_id=? WHERE id=?", (tgt["id"], e["id"]))
        st["split"] += 1; st["new_apps"] += made
        progress(f"  {a['company']} / {a['title'][:38]}: {len(acks)} acks -> {len(groups)} submissions")
    conn.commit(); db.recompute_all(conn)
    return st


JUNK_TITLE = re.compile(
    r"(thank you|thanks for|applying to|here is a link|manage your application|"
    r"interview for this|^your |^our |^we )", re.I)

ROLE_WORDS = re.compile(
    r"\b(manager|lead|director|head|principal|associate|analyst|engineer|specialist|"
    r"consultant|architect|officer|chief|coordinator|partner|strategist)\b", re.I)


def clean_identities(conn, progress=print) -> dict:
    """Repair company/role cross-contamination that individual parsers let through."""
    from .classify import _clean_role
    st = {"prefix_stripped": 0, "role_was_company": 0, "junk_title": 0, "swapped": 0}
    for r in conn.execute("""SELECT rr.id, rr.title, co.name company, co.id cid
                             FROM roles rr JOIN companies co ON co.id = rr.company_id""").fetchall():
        title, comp = (r["title"] or "").strip(), (r["company"] or "").strip()
        new = title

        # "DoorDash's Manager, Sales Strategy" -> "Manager, Sales Strategy"
        m = re.match(r"^" + re.escape(comp) + r"(?:'s|\u2019s)?\s+(?=\S)", new, re.I)
        if m:
            new = new[m.end():].strip()
            st["prefix_stripped"] += 1

        # role that is just the company name carries no information
        if new.lower() == comp.lower():
            new = "Unknown"
            st["role_was_company"] += 1

        if JUNK_TITLE.search(new):
            new = "Unknown"
            st["junk_title"] += 1

        # company that reads like a job title, with a role that does not: they are swapped
        if ROLE_WORDS.search(comp) and not ROLE_WORDS.search(new) and new != "Unknown":
            st["swapped"] += 1
            progress(f"  swapped-looking: company={comp!r} role={new!r}")

        if new != title:
            cleaned = _clean_role(new) if new != "Unknown" else "Unknown"
            cleaned = cleaned or "Unknown"
            twin = conn.execute("""SELECT id FROM roles WHERE company_id=? AND title=? COLLATE NOCASE
                                   AND id!=?""", (r["cid"], cleaned, r["id"])).fetchone()
            if twin:
                _fold_role(conn, r["id"], twin["id"])
            else:
                conn.execute("UPDATE roles SET title=? WHERE id=?", (cleaned, r["id"]))
    conn.commit()
    return st


def merge_same_day(conn, progress=print) -> int:
    """Fold applications to one role whose submissions land on the same calendar day."""
    merged = 0
    for r in conn.execute("""SELECT role_id, substr(COALESCE(submitted_at, applied_on),1,10) d,
                                    COUNT(*) n FROM applications WHERE status!='prospect'
                             GROUP BY role_id, d HAVING n > 1""").fetchall():
        rows = conn.execute("""SELECT a.id, (SELECT COUNT(*) FROM events e
                                 WHERE e.application_id=a.id) ev FROM applications a
                               WHERE a.role_id=? AND substr(COALESCE(a.submitted_at,a.applied_on),1,10)=?
                               ORDER BY ev DESC, a.id""", (r["role_id"], r["d"])).fetchall()
        keep = rows[0]["id"]
        for x in rows[1:]:
            conn.execute("UPDATE events SET application_id=? WHERE application_id=?", (keep, x["id"]))
            conn.execute("DELETE FROM applications WHERE id=?", (x["id"],))
            merged += 1
    conn.commit()
    return merged


def demote_outreach_only(conn) -> dict:
    """Retroactively apply the rule that inbound outreach cannot create an application.

    Making the rule apply only to new mail would leave every row already built from a
    recruiter email standing, which is how company aliases and comp bands behaved before
    today. Two "A Googler recently referred you!" notes had each created a second row
    beside an application already tracked, and one of them reported in_process over a
    role Google had already rejected.

    An application whose entire event history is outreach is not evidence of a
    submission. Fold it into a compatible application at the same company if one exists;
    otherwise release the events and drop the row. Events are never deleted.
    """
    stats = {"folded": 0, "released": 0}
    rows = conn.execute("""SELECT a.id, a.role_id, r.title, r.company_id, c.name
                           FROM applications a
                           JOIN roles r ON r.id = a.role_id
                           JOIN companies c ON c.id = r.company_id
                           WHERE a.status != 'prospect'
                             AND EXISTS (SELECT 1 FROM events e
                                         WHERE e.application_id = a.id
                                           AND e.type = 'recruiter_outreach')
                             AND NOT EXISTS (SELECT 1 FROM events e
                                             WHERE e.application_id = a.id
                                               AND e.type != 'recruiter_outreach')""").fetchall()
    for r in rows:
        target = None
        for cand in conn.execute(
                """SELECT a.id, ro.title FROM applications a
                   JOIN roles ro ON ro.id = a.role_id
                   WHERE ro.company_id = ? AND a.id != ? AND a.status != 'prospect'
                   ORDER BY a.status IN ('rejected','withdrawn'),
                            COALESCE(a.submitted_at, a.applied_on) DESC""",
                (r["company_id"], r["id"])).fetchall():
            if _compatible(r["title"] or "", cand["title"] or "", r["name"] or ""):
                target = cand["id"]
                break
        if target:
            conn.execute("UPDATE events SET application_id=? WHERE application_id=?",
                         (target, r["id"]))
            stats["folded"] += 1
        else:
            conn.execute("UPDATE events SET application_id=NULL WHERE application_id=?",
                         (r["id"],))
            stats["released"] += 1
        conn.execute("DELETE FROM applications WHERE id=?", (r["id"],))
    conn.commit()
    return stats


def merge_orphan_outcomes(conn, window_days: int = 120, progress=print) -> int:
    """An application holding only an outcome (rejection, interview) and no ack is the
    tail of an earlier submission to the same role, not a separate application."""
    merged = 0
    rows = conn.execute("""SELECT a.id, a.role_id, a.submitted_at, c.name, r.title
                           FROM applications a JOIN roles r ON r.id=a.role_id
                           JOIN companies c ON c.id=r.company_id
                           WHERE a.status!='prospect'
                             AND NOT EXISTS (SELECT 1 FROM events e
                                             WHERE e.application_id=a.id AND e.type='ack')
                             AND EXISTS (SELECT 1 FROM events e
                                         WHERE e.application_id=a.id AND e.type!='noise')""").fetchall()
    for r in rows:
        host = conn.execute("""SELECT a.id FROM applications a
                               WHERE a.role_id=? AND a.id!=?
                                 AND EXISTS (SELECT 1 FROM events e
                                             WHERE e.application_id=a.id AND e.type='ack')
                                 AND julianday(?) - julianday(a.submitted_at) BETWEEN 0 AND ?
                               ORDER BY a.submitted_at DESC LIMIT 1""",
                            (r["role_id"], r["id"], r["submitted_at"], window_days)).fetchone()
        if not host:
            continue
        conn.execute("UPDATE events SET application_id=? WHERE application_id=?", (host["id"], r["id"]))
        conn.execute("DELETE FROM applications WHERE id=?", (r["id"],))
        merged += 1
        progress(f"  {r['name'][:16]} / {(r['title'] or '')[:34]}: outcome folded into its ack")
    conn.commit()
    db.recompute_all(conn)
    return merged


def drop_csv_ghosts(conn, window_days: int = 3, progress=print) -> dict:
    """The v1 CSV tracker was derived from this same inbox, so every CSV row has a
    Gmail original that carries a body, a real subject and a parsed role. Where the
    Gmail event exists, the CSV event is a strictly worse duplicate: drop it and let
    the empty application go with it."""
    st = {"examined": 0, "dropped": 0, "kept": 0, "apps_removed": 0}
    rows = conn.execute("""SELECT e.id, e.occurred_at, c.id cid, c.name
                           FROM events e JOIN applications a ON a.id = e.application_id
                           JOIN roles r ON r.id = a.role_id JOIN companies c ON c.id = r.company_id
                           WHERE e.source='csv' AND e.type!='noise'""").fetchall()
    for e in rows:
        st["examined"] += 1
        twin = conn.execute("""SELECT ev.id FROM events ev
                               JOIN applications a2 ON a2.id = ev.application_id
                               JOIN roles r2 ON r2.id = a2.role_id
                               WHERE ev.source='gmail' AND r2.company_id = ?
                                 AND ABS(julianday(ev.occurred_at) - julianday(?)) <= ?
                               LIMIT 1""", (e["cid"], e["occurred_at"], window_days)).fetchone()
        if twin:
            conn.execute("DELETE FROM events WHERE id=?", (e["id"],))
            st["dropped"] += 1
        else:
            st["kept"] += 1
            progress(f"  kept (no gmail original): {e['name'][:20]} {e['occurred_at'][:10]}")
    st["apps_removed"] = conn.execute("""DELETE FROM applications WHERE status!='prospect'
        AND id NOT IN (SELECT DISTINCT application_id FROM events
                       WHERE application_id IS NOT NULL)""").rowcount
    conn.execute("DELETE FROM roles WHERE id NOT IN (SELECT role_id FROM applications)")
    conn.execute("DELETE FROM companies WHERE id NOT IN (SELECT company_id FROM roles)")
    conn.commit(); db.recompute_all(conn)
    return st


def drop_noise_only(conn) -> int:
    """An application whose every event is noise was never an application.

    Same family as drop_csv_ghosts and creates_application(): a row exists only
    because some event once looked real. When reclassification later demotes all of
    them (contract body-shop mail, aggregator drip), the row is left behind asserting
    that an application was submitted. Discovered prospects legitimately have no
    events at all, so they are excluded by requiring at least one.
    """
    rows = conn.execute("""
        SELECT a.id FROM applications a
        WHERE a.status != 'prospect'
          AND EXISTS (SELECT 1 FROM events e WHERE e.application_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM events e
                          WHERE e.application_id = a.id AND e.type != 'noise')
    """).fetchall()
    ids = [r["id"] for r in rows]
    for aid in ids:
        conn.execute("UPDATE events SET application_id=NULL WHERE application_id=?", (aid,))
        conn.execute("DELETE FROM applications WHERE id=?", (aid,))
    conn.execute("""DELETE FROM roles WHERE id IN (SELECT r.id FROM roles r
        LEFT JOIN applications a ON a.role_id=r.id WHERE a.id IS NULL)""")
    conn.execute("""DELETE FROM companies WHERE id IN (SELECT c.id FROM companies c
        LEFT JOIN roles r ON r.company_id=c.id WHERE r.id IS NULL)""")
    conn.commit()
    return len(ids)


ABBREV = {"pgm": "program manager", "pm": "program manager", "tpm": "technical program manager",
          "ops": "operations", "sr": "senior", "mgr": "manager", "s&o": "strategy operations",
          "gtm": "go to market", "bizops": "business operations"}
GENERIC_TITLE = {"program manager", "manager", "unknown", "unknown role", "project manager",
                 "senior program manager", "operations"}


def _norm_title(t: str) -> set:
    t = re.sub(r"[^a-z0-9& ]+", " ", (t or "").lower())
    out = []
    for w in t.split():
        out.extend(ABBREV.get(w, w).split())
    return {w for w in out if w not in {"the", "a", "of", "and", "for", "at"}}


def _strip_company(t: str, company: str) -> str:
    if not company:
        return t
    return re.sub(rf"\b{re.escape(company)}(?:'s|s')?\b", " ", t or "", flags=re.I)


def _compatible(t1: str, t2: str, company: str = "") -> bool:
    """Two titles in one Gmail thread may be the same role written differently, or two
    genuinely different applications that Gmail threaded because their boilerplate
    subjects matched. Only the first may be folded."""
    t1, t2 = _strip_company(t1, company), _strip_company(t2, company)
    n1, n2 = _norm_title(t1), _norm_title(t2)
    if not n1 or not n2:
        return True
    if n1 == n2:
        return True
    if " ".join(sorted(n1)) in GENERIC_TITLE or " ".join(sorted(n2)) in GENERIC_TITLE:
        return True
    if (t1 or "").strip().lower() in GENERIC_TITLE or (t2 or "").strip().lower() in GENERIC_TITLE:
        return True
    if n1 <= n2 or n2 <= n1:
        return True
    return len(n1 & n2) / len(n1 | n2) >= 0.6


def _title_score(title: str, company: str) -> tuple:
    """Rank candidate titles for a merged application. Prefer a specific, properly
    capitalised title over a generic or company-prefixed one: an interview loop that
    split produced 'Launch PgM', 'program manager' and "Stripe's Program Manager"
    for the same role."""
    t = (title or "").strip()
    low = t.lower()
    generic = low in {"program manager", "unknown", "unknown role", "manager"}
    has_company = (company or "").lower() in low
    capitalised = t[:1].isupper() and t != low
    return (not generic, not has_company, capitalised, len(t))


def merge_threads(conn, dry: bool = True) -> dict:
    """Fold applications that share a Gmail thread AND a compatible role title.

    A thread is usually one conversation, so when later messages resolve a slightly
    different title from their body, one interview loop became several advances. But
    Gmail also threads on identical subjects, so two separate applications sharing
    "Thanks for applying to Stripe!" land in one thread and must not be folded.
    Events move per thread, never per application: one application can bridge two
    threads, and each of its events belongs with its own conversation.
    """
    rows = conn.execute("""
        SELECT thread_id, GROUP_CONCAT(DISTINCT application_id) ids
        FROM events
        WHERE thread_id IS NOT NULL AND application_id IS NOT NULL
        GROUP BY thread_id HAVING COUNT(DISTINCT application_id) > 1""").fetchall()
    plan, moved, skipped = [], 0, []
    for r in rows:
        ids = sorted(int(x) for x in r["ids"].split(","))
        apps = conn.execute(f"""
            SELECT a.id, a.status, a.applied_on, a.submitted_at, a.fit_score, a.referral,
                   r.title, c.name company
            FROM applications a JOIN roles r ON r.id = a.role_id
            JOIN companies c ON c.id = r.company_id
            WHERE a.id IN ({','.join('?' * len(ids))})""", ids).fetchall()
        if len(apps) < 2:
            continue
        co = apps[0]["company"]
        parent = {a["id"]: a["id"] for a in apps}
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]; x = parent[x]
            return x
        for i, a in enumerate(apps):
            for b in apps[i + 1:]:
                if _compatible(a["title"], b["title"], co):
                    parent[find(a["id"])] = find(b["id"])
        clusters = {}
        for a in apps:
            clusters.setdefault(find(a["id"]), []).append(a)
        group = max(clusters.values(), key=len)
        rest = [a for a in apps if a not in group]
        if rest:
            skipped.append({"company": co, "thread": r["thread_id"],
                            "kept_apart": [(a["id"], a["title"]) for a in rest]})
        if len(group) < 2:
            continue
        keeper = min(group, key=lambda a: (a["submitted_at"] or a["applied_on"] or "9999", a["id"]))
        best = max(group, key=lambda a: _title_score(a["title"], a["company"]))
        losers = [a for a in group if a["id"] != keeper["id"]]
        plan.append({"company": keeper["company"], "keep": keeper["id"], "title": best["title"],
                     "drop": [(a["id"], a["title"], a["status"]) for a in losers]})
        if dry:
            continue
        for a in losers:
            conn.execute("""UPDATE events SET application_id=?
                            WHERE application_id=? AND thread_id=?""",
                         (keeper["id"], a["id"], r["thread_id"]))
            if a["referral"]:
                conn.execute("UPDATE applications SET referral=1 WHERE id=?", (keeper["id"],))
            moved += 1
        if best["title"] != keeper["title"]:
            set_identity(conn, keeper["id"], company=None, role=best["title"])
    if not dry:
        # an application with no events left was only ever a fragment of a thread
        conn.execute("""DELETE FROM applications WHERE status != 'prospect'
                        AND NOT EXISTS (SELECT 1 FROM events e WHERE e.application_id = applications.id)""")
        conn.execute("""DELETE FROM roles WHERE id IN (SELECT r.id FROM roles r
            LEFT JOIN applications a ON a.role_id=r.id WHERE a.id IS NULL)""")
        conn.execute("""DELETE FROM companies WHERE id IN (SELECT c.id FROM companies c
            LEFT JOIN roles r ON r.company_id=c.id WHERE r.id IS NULL)""")
        db.recompute_all(conn)
        conn.commit()
    return {"threads": len(plan), "events_moved": moved, "plan": plan, "kept_apart": skipped}
