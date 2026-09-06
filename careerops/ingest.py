"""Ingestion. Legacy CSV backfill now; Gmail sync uses the same event path."""
import csv, hashlib
from . import db
from .classify import classify


def ingest_legacy_csv(conn, path: str, source: str = "csv") -> dict:
    """The v1 tracker put the SENDER in 'Company' and the SUBJECT in 'Role Title'."""
    stats = {"rows": 0, "noise": 0, "events": 0, "review": 0, "apps": 0, "dupes": 0}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            stats["rows"] += 1
            sender = (row.get("Company") or "").strip()
            subject = (row.get("Role Title") or "").strip()
            when = (row.get("Date Applied") or "").strip()
            if not subject:
                continue

            c = classify(subject, sender)
            ext = "csv:" + hashlib.sha1(f"{when}|{sender}|{subject}".encode()).hexdigest()[:16]

            if c.event_type == "noise":
                stats["noise"] += 1
                if db.add_event(conn, None, when, "noise", source, confidence=c.confidence,
                                external_id=ext, subject=subject, sender=sender) is None:
                    stats["dupes"] += 1
                continue

            app_id = None
            if c.company:
                cid = db.get_or_create_company(conn, c.company)
                rid = db.get_or_create_role(conn, cid, c.role or "Unknown role", source=source)
                existed = conn.execute("SELECT 1 FROM applications WHERE role_id=?", (rid,)).fetchone()
                app_id = db.get_or_create_application(conn, rid, applied_on=when or None, channel=source)
                if not existed:
                    stats["apps"] += 1

            eid = db.add_event(conn, app_id, when, c.event_type, source, confidence=c.confidence,
                               external_id=ext, subject=subject, sender=sender,
                               raw="; ".join(c.reasons))
            if eid is None:
                stats["dupes"] += 1
                continue
            stats["events"] += 1
            if c.needs_review:
                db.queue_review(conn, eid, f"conf={c.confidence} company={c.company!r} role={c.role!r}")
                stats["review"] += 1
    conn.commit()
    db.recompute_all(conn)
    return stats
