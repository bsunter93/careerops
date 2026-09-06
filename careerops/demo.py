"""Synthetic pipeline so the dashboard has something to show on a clean clone.

Two reasons this exists. A visitor who runs `discover` without an API key gets an empty
dashboard, because `fit` is what turns a discovered role into a prospect, and an empty
dashboard is a bad first impression for a tool whose pitch is visual. And you cannot
demo a real job search on a shared screen without projecting your own rejections.

Every company here is from Microsoft's documented set of fictional companies (Contoso,
Fabrikam, Northwind and so on), chosen precisely so no real employer is attached to an
invented rejection.

It always writes to its own database file and never touches yours.
"""
import json, random, sqlite3, pathlib
from datetime import date, timedelta

from . import db

DEMO_DB = str(pathlib.Path(__file__).resolve().parent.parent / "demo.db")

FIRMS = [
    ("Contoso", "contoso.com"), ("Fabrikam", "fabrikam.com"), ("Northwind Traders", "northwind.com"),
    ("Tailspin Toys", "tailspintoys.com"), ("Wingtip Software", "wingtip.com"),
    ("Litware", "litware.com"), ("Proseware", "proseware.com"), ("Woodgrove Bank", "woodgrove.com"),
    ("Lamna Health", "lamnahealth.com"), ("Relecloud", "relecloud.com"),
    ("Adventure Works", "adventure-works.com"), ("Trey Research", "treyresearch.net"),
    ("Alpine Ski House", "alpineskihouse.com"), ("VanArsdel", "vanarsdel.com"),
    ("Margie's Travel", "margiestravel.com"), ("Fourth Coffee", "fourthcoffee.com"),
]
TITLES = [
    "Business Operations Manager", "Strategy & Operations Lead", "Revenue Operations Manager",
    "Senior Program Manager", "Business Operations Analyst", "Chief of Staff",
    "Strategic Programs Manager", "Technical Program Manager", "GTM Operations Manager",
    "Principal Program Manager",
]
# Prospects need titles that cannot collide with the history above. A repeated
# (company, title) pair resolves to the SAME role, and the prospect update then rewrites
# a submitted application's status, silently deleting history.
OPEN_TITLES = [
    "Head of Business Operations", "Director of Strategy & Ops", "Senior Manager, BizOps",
    "Principal, Revenue Strategy", "Lead Program Manager, GTM", "Manager, Sales Operations",
    "Senior Strategy Associate", "Business Planning Lead", "Operations Strategy Manager",
    "Senior Analyst, Growth Operations", "Partner Operations Lead", "Field Operations Manager",
]
LOCS = ["Remote", "Remote - US", "Denver, CO", "San Francisco, CA", "New York, NY", "United States"]


def _fit(score: int, company: str, title: str) -> str:
    v = "strong" if score >= 78 else "plausible" if score >= 65 else "stretch" if score >= 50 else "poor"
    return json.dumps({
        "score": score, "verdict": v,
        "meets": ["6+ years of relevant operating experience",
                  "Track record building processes from scratch",
                  "Strong SQL and modeling background"][: 3 if score > 60 else 2],
        "gaps": ["No domain experience in this vertical"] + ([] if score > 70 else
                ["Role leans further into finance than the profile supports"]),
        "emphasize": ["Zero-to-one launch delivered in under a month",
                      "Owned an eight-figure cost mandate end to end",
                      "Governance across 200+ stakeholders"],
        "level_read": "at" if 60 <= score < 85 else ("above" if score < 60 else "at or slightly below"),
        "preference_flags": [] if score >= 70 else ["Posted range floor sits under the stated comp floor"],
        "one_line": f"{v.capitalize()} match for the {title} role at {company}.",
    }, indent=2)


def seed(path: str = DEMO_DB, seed_value: int = 7) -> dict:
    """Build a demo database from scratch. Deterministic, so the screenshot is stable."""
    rng = random.Random(seed_value)
    p = pathlib.Path(path)
    if p.exists():
        p.unlink()
    conn = db.connect(str(p))
    db.init(conn)
    today = date.today()
    stats = {"applications": 0, "prospects": 0, "events": 0}

    # --- history: 44 submitted applications with a realistic drop after acknowledgement
    plan = ([("rejected", 26), ("acked", 12), ("in_process", 3), ("interview", 3)])
    # Company and title are cycled rather than sampled, because random pairs collide and
    # get_or_create_application correctly folds the duplicate, quietly shrinking the set.
    # Dates skew recent (mode near three weeks) so the rolling 7-day pacing card is not zero.
    i = 0
    for status, n in plan:
        for _ in range(n):
            comp, dom = FIRMS[i % len(FIRMS)]
            title = TITLES[(i // len(FIRMS) + i) % len(TITLES)]
            i += 1
            day = int(rng.triangular(0, 150, 22))
            cid = db.get_or_create_company(conn, comp, dom)
            rid = db.get_or_create_role(conn, cid, title, location=rng.choice(LOCS),
                                        source="demo", url=f"https://{dom}/careers")
            applied = (today - timedelta(days=day)).isoformat()
            aid = db.get_or_create_application(conn, rid, applied_on=applied, channel="demo")
            conn.execute("UPDATE applications SET referral=? WHERE id=?",
                         (1 if rng.random() < 0.12 else 0, aid))
            db.add_event(conn, aid, applied + " 09:00:00", "ack", "demo", confidence=0.9,
                         subject=f"Thanks for applying to {comp}", sender=f"careers@{dom}")
            stats["events"] += 1
            after = lambda d: (today - timedelta(days=max(0, day - d))).isoformat()
            if status == "rejected":
                db.add_event(conn, aid, after(rng.randint(6, 30)) + " 11:00:00", "rejection", "demo",
                             confidence=0.9, subject=f"Your application to {comp}", sender=f"careers@{dom}")
                stats["events"] += 1
            elif status in ("in_process", "interview"):
                db.add_event(conn, aid, after(rng.randint(4, 12)) + " 15:00:00", "recruiter_outreach",
                             "demo", confidence=0.85, subject=f"Quick call about {title}?",
                             sender=f"talent@{dom}")
                stats["events"] += 1
                if status == "interview":
                    db.add_event(conn, aid, after(rng.randint(13, 22)) + " 16:30:00", "interview_invite",
                                 "demo", confidence=0.9, subject=f"Schedule your interview with {comp}",
                                 sender=f"talent@{dom}")
                    stats["events"] += 1
            stats["applications"] += 1

    # --- prospects: discovered, scored, not yet applied to
    for j, score in enumerate([88, 84, 81, 79, 76, 74, 71, 68, 64, 61, 57, 53]):
        comp, dom = FIRMS[(j * 3 + 1) % len(FIRMS)]
        title = OPEN_TITLES[j % len(OPEN_TITLES)]
        cid = db.get_or_create_company(conn, comp, dom)
        rid = db.get_or_create_role(conn, cid, title, location=rng.choice(LOCS), source="demo",
                                    url=f"https://{dom}/careers",
                                    comp_min=150000 + score * 500, comp_max=190000 + score * 900,
                                    jd_text="Demo job description. " * 40)
        aid = db.get_or_create_application(conn, rid, channel="discovered")
        conn.execute("UPDATE applications SET status='prospect', fit_score=?, fit_reasoning=? WHERE id=?",
                     (score, _fit(score, comp, title), aid))
        stats["prospects"] += 1

    # --- reference data: public sentiment for a handful of them
    for comp, dom in FIRMS[:8]:
        r = round(rng.uniform(3.1, 4.6), 1)
        conn.execute("""INSERT OR REPLACE INTO company_intel
            (company, rating, reviews_n, wlb, comp_benefits, culture, career, recommend_pct,
             pros, cons, wlb_summary, confidence, entity_note, sources, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,date('now'))""",
            (comp, r, rng.randint(80, 2400), round(min(5, max(2.4, r - rng.uniform(-.3, .8))), 1),
             round(min(5, r + .2), 1), round(r, 1), round(max(2.5, r - .4), 1), int(r / 5 * 100),
             json.dumps(["Smart colleagues", "Real autonomy", "Good benefits"]),
             json.dumps(["Pace can be intense", "Career ladders are unclear"]),
             "Demo data. Balance is rated acceptable, with peaks around launches.",
             "high", "Fictional company used for demo data.",
             json.dumps([{"title": "demo", "url": f"https://{dom}"}])))

    db.recompute_all(conn)
    conn.commit()
    stats["db"] = str(p)
    return stats
