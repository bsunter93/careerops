"""SQLite access layer. The DB is the source of truth; status is always derived."""
import sqlite3, os, pathlib
from typing import Optional

DEFAULT_DB = os.environ.get("CAREEROPS_DB", str(pathlib.Path(__file__).resolve().parent.parent / "careerops.db"))
SCHEMA = pathlib.Path(__file__).resolve().parent / "schema.sql"

# Terminal states never regress; ranked so a later weak event can't downgrade a strong one.
# STAGE: how far the application got. Monotonic, historical, never decays.
STATUS_RANK = {"prospect": -1, "applied": 0, "acked": 1, "in_process": 2,
               "assessment": 2, "interview": 3, "offer": 4, "rejected": 5, "withdrawn": 5}

# ACTIVITY is separate from stage. An interview reached last October is still an
# interview that happened, but it is not a live one. Silence this long makes any
# non-terminal application dormant, whatever stage it reached. A later event revives it.
DORMANT_DAYS = 21


def _dormant_days(conn, application_id: int) -> int:
    """Companies publish different review windows. Google states eight weeks, so a
    fixed 21 days would mark a healthy application dead. Override per company."""
    import json as _j, pathlib as _p
    global _POLICY
    try:
        _POLICY
    except NameError:
        try:
            cfg = _j.loads((_p.Path(__file__).resolve().parent.parent / "config.json").read_text())
            _POLICY = (cfg.get("company_policy", {}), cfg.get("default_dormant_days", DORMANT_DAYS))
        except Exception:
            _POLICY = ({}, DORMANT_DAYS)
    policy, default = _POLICY
    row = conn.execute("""SELECT c.name FROM applications a JOIN roles r ON r.id=a.role_id
                          JOIN companies c ON c.id=r.company_id WHERE a.id=?""",
                       (application_id,)).fetchone()
    name = (row["name"] if row else "") or ""
    for k, v in policy.items():
        if k.lower() in name.lower() and v.get("dormant_days"):
            return int(v["dormant_days"])
    return int(default)

EVENT_TO_STATUS = {
    "ack": "acked",
    "recruiter_outreach": "in_process",
    "assessment": "assessment",
    "interview_invite": "interview",
    "offer": "offer",
    "rejection": "rejected",
}


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DEFAULT_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns the schema has grown. schema.sql is CREATE TABLE IF NOT EXISTS, so it
    never reaches a database that already exists."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    except sqlite3.DatabaseError:
        return
    if cols and "held" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN held INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    try:
        rcols = {r[1] for r in conn.execute("PRAGMA table_info(roles)")}
    except sqlite3.DatabaseError:
        return
    if rcols and "posted_at" not in rcols:
        conn.execute("ALTER TABLE roles ADD COLUMN posted_at TEXT")
        conn.commit()


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text())
    conn.commit()


_ALIASES = None


def _aliases() -> dict:
    """config.company_aliases, lowercased. One employer, one row.

    Companies rebrand and merge, and the mail follows the new name before the job board
    does. Fivetran's acknowledgement arrived from "Fivetran + dbt Labs" while its board
    still published as "Fivetran", so the ack created a second company and a phantom
    "Unknown role" application instead of attaching to the one already applied to.
    """
    global _ALIASES
    if _ALIASES is None:
        try:
            import json
            cfg = json.loads((pathlib.Path(__file__).resolve().parent.parent
                              / "config.json").read_text())
            _ALIASES = {k.lower(): v for k, v in (cfg.get("company_aliases") or {}).items()}
        except Exception:
            _ALIASES = {}
    return _ALIASES


def get_or_create_company(conn, name: str, domain: Optional[str] = None) -> int:
    name = name.strip()
    name = _aliases().get(name.lower(), name)
    row = conn.execute("SELECT id FROM companies WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        if domain:
            conn.execute("UPDATE companies SET domain = COALESCE(domain, ?) WHERE id = ?", (domain, row["id"]))
        return row["id"]
    cur = conn.execute("INSERT INTO companies (name, domain) VALUES (?, ?)", (name, domain))
    return cur.lastrowid


def get_or_create_role(conn, company_id: int, title: str, **kw) -> int:
    title = (title or "Unknown role").strip()
    row = conn.execute(
        "SELECT id FROM roles WHERE company_id = ? AND title = ? COLLATE NOCASE", (company_id, title)
    ).fetchone()
    if row:
        return row["id"]
    cols = {"company_id": company_id, "title": title}
    cols.update({k: v for k, v in kw.items() if v is not None})
    keys = ",".join(cols)
    marks = ",".join("?" * len(cols))
    cur = conn.execute(f"INSERT INTO roles ({keys}) VALUES ({marks})", tuple(cols.values()))
    return cur.lastrowid


# Two acks for one role this far apart are two submissions. Same-day acks are one
# submission with two emails; re-applying to the same role within a day is not a thing.
RESUBMIT_HOURS = 24


def get_or_create_application(conn, role_id: int, applied_on: Optional[str] = None,
                              submitted_at: Optional[str] = None, is_ack: bool = False, **kw) -> int:
    """A submission is the unit. Re-applying to the same role creates a new row,
    identified by its timestamp; later events attach to the most recent submission."""
    if is_ack and submitted_at:
        near = conn.execute(
            """SELECT id FROM applications WHERE role_id = ?
               AND submitted_at IS NOT NULL
               AND ABS(julianday(?) - julianday(submitted_at)) * 24 < ?
               ORDER BY submitted_at DESC LIMIT 1""",
            (role_id, submitted_at, RESUBMIT_HOURS)).fetchone()
        if near:
            return near["id"]                      # same submission, duplicate email
    else:
        row = conn.execute("""SELECT id FROM applications WHERE role_id = ?
                              ORDER BY COALESCE(submitted_at, applied_on) DESC LIMIT 1""",
                           (role_id,)).fetchone()
        if row:
            if applied_on:
                conn.execute(
                    "UPDATE applications SET applied_on = MIN(COALESCE(applied_on, ?), ?) WHERE id = ?",
                    (applied_on, applied_on, row["id"]))
            return row["id"]
    row = None
    cols = {"role_id": role_id, "applied_on": applied_on, "submitted_at": submitted_at}
    cols.update({k: v for k, v in kw.items() if v is not None})
    cols = {k: v for k, v in cols.items() if v is not None}
    keys = ",".join(cols)
    marks = ",".join("?" * len(cols))
    cur = conn.execute(f"INSERT INTO applications ({keys}) VALUES ({marks})", tuple(cols.values()))
    return cur.lastrowid


def add_event(conn, application_id, occurred_at, type_, source, *,
              confidence=1.0, external_id=None, subject=None, sender=None, raw=None,
              body=None, thread_id=None, held=False) -> Optional[int]:
    """Idempotent on external_id. Returns event id, or None if already ingested."""
    if external_id:
        row = conn.execute("SELECT id FROM events WHERE external_id = ?", (external_id,)).fetchone()
        if row:
            return None
    cur = conn.execute(
        """INSERT INTO events (application_id, occurred_at, type, confidence, source,
                               external_id, subject, sender, raw, body, thread_id, held)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (application_id, occurred_at, type_, confidence, source, external_id, subject, sender,
         raw, body, thread_id, 1 if held else 0),
    )
    return cur.lastrowid


def queue_review(conn, event_id: int, reason: str) -> None:
    conn.execute("INSERT INTO review_queue (event_id, reason) VALUES (?,?)", (event_id, reason))


def recompute_status(conn, application_id: int) -> str:
    """Status is derived from the event log, never set by hand."""
    rows = conn.execute(
        # A held event is evidence on the record, not a verdict: it must not move status.
        "SELECT type, occurred_at FROM events WHERE application_id = ? AND type != 'noise' "
        "AND held = 0 ORDER BY occurred_at",
        (application_id,),
    ).fetchall()
    cur = conn.execute("SELECT status FROM applications WHERE id = ?", (application_id,)).fetchone()
    # A discovered prospect has no events yet; it must not be relabelled "applied".
    if not rows and cur and cur["status"] == "prospect":
        return "prospect"
    status, updated = "applied", None
    for r in rows:
        cand = EVENT_TO_STATUS.get(r["type"])
        if cand and STATUS_RANK[cand] >= STATUS_RANK[status]:
            status, updated = cand, r["occurred_at"]
    if status in ("rejected", "withdrawn"):
        activity = "closed"
    elif status == "prospect":
        activity = None
    else:
        # Measure from the LAST activity of any kind, not the event that set the stage.
        row = conn.execute("""SELECT CAST(julianday('now') - julianday(COALESCE(
                                (SELECT MAX(occurred_at) FROM events e
                                   WHERE e.application_id = a.id AND e.type != 'noise'),
                                a.applied_on)) AS INT) AS d
                              FROM applications a WHERE a.id = ?""", (application_id,)).fetchone()
        d = row["d"] if row else None
        activity = "dormant" if (d is not None and d >= _dormant_days(conn, application_id)) else "active"
    conn.execute("UPDATE applications SET status = ?, activity = ?, status_updated_at = ? WHERE id = ?",
                 (status, activity, updated, application_id))
    return status


def recompute_all(conn) -> int:
    ids = [r["id"] for r in conn.execute("SELECT id FROM applications")]
    for i in ids:
        recompute_status(conn, i)
    conn.commit()
    return len(ids)
