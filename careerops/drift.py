"""Does the stored data still agree with the code that produced it?

Seven bugs in one day shared a shape: a rule was fixed, the fix worked on anything
arriving afterwards, and every row already stored kept the old answer. Nothing errored.
The dashboard looked healthy. Company aliases healed no existing company, comp bands were
read only for new roles, applications built from recruiter mail stood after the rule that
built them was removed, reclassification could correct a verdict but never retract one,
a prefix hash could not see a description grow, and prose titles survived the gate added
to stop them.

The invariant they all break is one sentence: **a derived value in the database should
equal what the current code would derive from the same source.** When it does not, either
the code changed and the data needs healing, or there is a bug. Both are worth knowing and
neither announces itself.

The second family is narrower and just as quiet: a caller hands a function fewer inputs
than it reads, and the function has no way to tell. `score_pending` passed None for the
comp band and posting date because its query never selected them, which flattened two of
seven pre-rank components to a constant for every row. `degenerate()` catches that by
looking for a signal that has stopped varying.

Read-only. It reports; healing is `resolve`'s job.
"""
import json
import sqlite3
from typing import Optional


def _rows(conn, sql, args=()):
    cur = conn.execute(sql, args)
    cur.row_factory = sqlite3.Row
    return cur.fetchall()


def classification_drift(conn, limit: int = 0) -> dict:
    """Re-classify every stored message and compare to what is stored.

    Event type is compared per event, because each event carries its own verdict.

    Role is compared only against the *earliest* event of an application, because that is
    the one that established its identity. The first version of this check compared every
    event to its application's title and reported 16 findings of which 13 were nonsense:
    a Stripe application accumulated eight events whose bodies each name a different
    requisition, and a rejection mentioning a sibling posting is not evidence the stored
    title is wrong. A check that cries wolf is worse than no check, because the next real
    finding is read as noise.
    """
    from .classify import classify
    bad_type, bad_role = [], []
    first_of = {}
    for r in _rows(conn, """SELECT e.id, e.application_id, e.subject, e.sender, e.body,
                                   e.type, ro.title, e.occurred_at
                            FROM events e
                            LEFT JOIN applications a ON a.id = e.application_id
                            LEFT JOIN roles ro ON ro.id = a.role_id
                            WHERE e.source = 'gmail' AND e.subject IS NOT NULL
                            ORDER BY e.application_id, e.occurred_at"""):
        got = classify(r["subject"] or "", r["sender"] or "", r["body"] or "")
        if got.event_type != r["type"]:
            bad_type.append((r["id"], r["type"], got.event_type, (r["subject"] or "")[:56]))
        aid = r["application_id"]
        if aid is None or aid in first_of:
            continue
        first_of[aid] = True
        stored, now = (r["title"] or "").strip(), (got.role or "").strip()
        if not now or not stored or "unknown" in stored.lower():
            continue
        if now.lower() == stored.lower():
            continue
        # The code merely adding the employer's name, at either end, is a worse answer
        # rather than drift. Guarding only the suffix left "Motive Senior Product
        # Operations Manager" reported against the correct stored title.
        if stored.lower() in now.lower():
            continue
        bad_role.append((r["id"], stored, now))
    return {"type_mismatch": bad_type, "role_mismatch": bad_role}


def comp_drift(conn) -> list:
    """A pay band sitting unread in a description the parser can already handle."""
    from .discover import extract_comp
    out = []
    for r in _rows(conn, """SELECT id, title, jd_text, comp_min, comp_max FROM roles
                            WHERE jd_text IS NOT NULL AND LENGTH(jd_text) > 200"""):
        lo, hi = extract_comp(r["jd_text"])
        if (lo or hi) and not (r["comp_min"] or r["comp_max"]):
            out.append((r["id"], (r["title"] or "")[:44], lo, hi))
    return out


def alias_drift(conn) -> list:
    """A stored company that an alias says should be called something else."""
    from . import db
    al = db._aliases()
    return [(r["id"], r["name"], al[r["name"].lower()])
            for r in _rows(conn, "SELECT id, name FROM companies")
            if r["name"].lower() in al and al[r["name"].lower()] != r["name"]]


def title_drift(conn) -> list:
    """Stored titles that name no role: prose that reached the title column."""
    from .classify import ROLE_NOUN
    return [(r["id"], r["name"], r["title"])
            for r in _rows(conn, """SELECT ro.id, ro.title, co.name FROM roles ro
                                    JOIN companies co ON co.id = ro.company_id
                                    JOIN applications a ON a.role_id = ro.id
                                    WHERE a.status != 'prospect'""")
            if r["title"] and "unknown" not in r["title"].lower()
            and not ROLE_NOUN.search(r["title"])]


def evidence_drift(conn) -> list:
    """Applications whose entire event history is inbound outreach, which is a lead."""
    return [(r["id"], r["name"], (r["title"] or "")[:40])
            for r in _rows(conn, """SELECT a.id, co.name, ro.title FROM applications a
                JOIN roles ro ON ro.id = a.role_id JOIN companies co ON co.id = ro.company_id
                WHERE a.status != 'prospect'
                  AND EXISTS (SELECT 1 FROM events e WHERE e.application_id = a.id
                              AND e.type = 'recruiter_outreach')
                  AND NOT EXISTS (SELECT 1 FROM events e WHERE e.application_id = a.id
                                  AND e.type != 'recruiter_outreach')""")]


def status_drift(conn) -> list:
    """Stored status against what the event log now derives."""
    from . import db
    out = []
    for r in _rows(conn, "SELECT id, status FROM applications WHERE status != 'prospect'"):
        want = db.derive_status(conn, r["id"]) if hasattr(db, "derive_status") else None
        if want and want != r["status"]:
            out.append((r["id"], r["status"], want))
    return out


def degenerate(conn, floor: float = 0.02) -> list:
    """A scoring signal that has stopped varying is a signal that is switched off.

    Catches the failure where a caller supplies fewer inputs than the scorer reads: the
    affected components collapse to their unknown-value constant and the score keeps
    looking reasonable. Runs the pre-rank the way the pipeline does and reports any
    component whose spread across real roles has gone flat.
    """
    from .prerank import prerank
    import statistics as st
    rows = _rows(conn, """SELECT title, jd_text, comp_min, comp_max, posted_at FROM roles
                          WHERE jd_text IS NOT NULL AND LENGTH(jd_text) > 200""")
    if len(rows) < 20:
        return []
    parts = [prerank(r["title"], r["jd_text"], r["comp_min"], r["comp_max"],
                     r["posted_at"])["parts"] for r in rows]
    out = []
    for k in parts[0]:
        vals = [p[k] for p in parts]
        sd = st.pstdev(vals)
        if sd < floor:
            out.append((k, round(sd, 4), round(st.mean(vals), 3), len(set(vals))))
    return out


def run(conn) -> dict:
    c = classification_drift(conn)
    return {
        "event type": c["type_mismatch"],
        "event role": c["role_mismatch"],
        "unread pay band": comp_drift(conn),
        "unapplied alias": alias_drift(conn),
        "title without a role": title_drift(conn),
        "outreach-only application": evidence_drift(conn),
        "flat scoring signal": degenerate(conn),
    }
