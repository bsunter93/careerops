"""Traction analysis. Answers: what is actually converting?"""
from . import db

RESPONSE_STATES = ("acked", "in_process", "assessment", "interview", "offer")
POSITIVE_STATES = ("in_process", "assessment", "interview", "offer")


def _rate(rows, key):
    buckets = {}
    for r in rows:
        k = r[key] or "(unknown)"
        b = buckets.setdefault(k, {"n": 0, "responded": 0, "positive": 0})
        b["n"] += 1
        if r["status"] in RESPONSE_STATES:
            b["responded"] += 1
        if r["status"] in POSITIVE_STATES:
            b["positive"] += 1
    return buckets


def report(conn) -> str:
    rows = conn.execute("""
        SELECT a.status, a.referral, a.channel, c.name company, r.title, r.location,
               a.applied_on, a.fit_score
        FROM applications a JOIN roles r ON r.id=a.role_id
        JOIN companies c ON c.id=r.company_id
        WHERE a.status != 'prospect'""").fetchall()
    if not rows:
        return "no applications yet"
    n = len(rows)
    pos = sum(1 for r in rows if r["status"] in POSITIVE_STATES)
    rej = sum(1 for r in rows if r["status"] == "rejected")
    silent = sum(1 for r in rows if r["status"] == "applied")
    out = ["OVERALL", f"  applications      {n}",
           f"  positive signal   {pos} ({pos*100//n}%)",
           f"  rejected          {rej} ({rej*100//n}%)",
           f"  never acked       {silent} ({silent*100//n}%)", ""]

    for label, key in (("BY COMPANY", "company"), ("BY CHANNEL", "channel")):
        b = _rate(rows, key)
        if len(b) < 2:
            continue
        out.append(label)
        for k, v in sorted(b.items(), key=lambda kv: (-kv[1]["positive"], -kv[1]["n"]))[:12]:
            out.append(f"  {k[:28]:<30}n={v['n']:<4}acked={v['responded']:<4}positive={v['positive']}")
        out.append("")

    ref = [r for r in rows if r["referral"]]
    if ref:
        rp = sum(1 for r in ref if r["status"] in POSITIVE_STATES)
        out += ["REFERRAL EFFECT",
                f"  referred     n={len(ref):<4}positive={rp}",
                f"  cold         n={n-len(ref):<4}positive={pos-rp}", ""]

    scored = [r for r in rows if r["fit_score"] is not None]
    if scored:
        hi = [r for r in scored if r["fit_score"] >= 75]
        lo = [r for r in scored if r["fit_score"] < 75]
        out.append("FIT SCORE vs OUTCOME")
        for lbl, grp in (("fit >= 75", hi), ("fit < 75", lo)):
            if grp:
                p = sum(1 for r in grp if r["status"] in POSITIVE_STATES)
                out.append(f"  {lbl:<14}n={len(grp):<4}positive={p}")
    return "\n".join(out)
