"""A frozen set of real emails, and the label each one should get.

Every classifier change so far has been checked by hand, one email at a time, which
does not scale and does not catch anything that was already wrong. This turns
"some new permutation will break it" into a number you can read in a second.

Three states per record, because freezing today's output as truth would enshrine
today's bugs:

  verified=false            we have not looked. A change here is drift, reported
                            but not a failure. This is most of the corpus.
  verified=true             a human decided this label. A change here fails.
  verified=true, known_bad  a human decided this label AND the classifier does not
                            produce it yet. A mismatch is expected and reported as
                            still-open; a match is reported as FIXED, and the flag
                            should then be cleared.

The file holds real mail, so it lives outside the repo. See .gitignore.
"""
import json, pathlib
from typing import Optional

DEFAULT_PATH = "corpus/events.jsonl"
FIELDS = ("event_id", "external_id", "subject", "sender", "body",
          "expect", "expect_company", "verified", "known_bad", "note")


def _load(path: str) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            r = json.loads(line)
            out[r["external_id"]] = r
    return out


def _dump(path: str, records: dict) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for k in sorted(records, key=lambda k: records[k].get("event_id") or 0):
            r = {k2: records[k].get(k2) for k2 in FIELDS}
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def export(conn, path: str = DEFAULT_PATH) -> dict:
    """Add every stored event to the corpus, preserving human judgements.

    A re-export must never overwrite `expect`, `verified`, `known_bad` or `note` on a
    record someone has reviewed, or the corpus decays into a mirror of the classifier
    it is supposed to be checking.
    """
    existing = _load(path)
    stats = {"kept": len(existing), "added": 0, "verified": 0}
    rows = conn.execute("""SELECT e.id, e.external_id, e.subject, e.sender, e.body, e.type,
                                  c.name AS company
                           FROM events e
                           LEFT JOIN applications a ON a.id = e.application_id
                           LEFT JOIN roles r ON r.id = a.role_id
                           LEFT JOIN companies c ON c.id = r.company_id
                           WHERE e.external_id IS NOT NULL""").fetchall()
    for r in rows:
        ext = r["external_id"]
        if ext in existing:
            # Refresh only the immutable source text, never the judgement.
            existing[ext]["subject"] = r["subject"]
            existing[ext]["sender"] = r["sender"]
            existing[ext]["body"] = r["body"]
            existing[ext]["event_id"] = r["id"]
            continue
        existing[ext] = {"event_id": r["id"], "external_id": ext, "subject": r["subject"],
                         "sender": r["sender"], "body": r["body"], "expect": r["type"],
                         "expect_company": r["company"], "verified": False,
                         "known_bad": False, "note": None}
        stats["added"] += 1
    stats["verified"] = sum(1 for r in existing.values() if r.get("verified"))
    _dump(path, existing)
    stats["total"] = len(existing)
    return stats


def check(path: str = DEFAULT_PATH) -> dict:
    """Re-classify every record from its stored text and compare to `expect`.

    Runs against the text in the corpus, not the database, so the result does not move
    when the database does.
    """
    from .classify import classify
    records = _load(path)
    out = {"total": len(records), "agree": 0, "drift": [], "regressions": [],
           "still_open": 0, "fixed": []}
    for r in records.values():
        got = classify(r.get("subject") or "", r.get("sender") or "",
                       r.get("body") or "").event_type
        want = r.get("expect")
        label = (r.get("subject") or "")[:56]
        if r.get("known_bad"):
            if got == want:
                out["fixed"].append((label, want))
            else:
                out["still_open"] += 1
            continue
        if got == want:
            out["agree"] += 1
        elif r.get("verified"):
            out["regressions"].append((label, want, got))
        else:
            out["drift"].append((label, want, got))
    return out


def mark(path: str, external_id: str, expect: str, verified: bool = True,
         known_bad: bool = False, note: Optional[str] = None) -> bool:
    records = _load(path)
    if external_id not in records:
        return False
    records[external_id].update(expect=expect, verified=verified,
                                known_bad=known_bad, note=note)
    _dump(path, records)
    return True
