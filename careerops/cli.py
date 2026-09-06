"""careerops <command>"""
import sys, argparse, pathlib
from . import db
from .ingest import ingest_legacy_csv


def cmd_init(a):
    conn = db.connect(a.db); db.init(conn); print(f"initialized {a.db or db.DEFAULT_DB}")


def cmd_ingest_csv(a):
    conn = db.connect(a.db); db.init(conn)
    s = ingest_legacy_csv(conn, a.path)
    print("  ".join(f"{k}={v}" for k, v in s.items()))


def cmd_pipeline(a):
    conn = db.connect(a.db)
    q = "SELECT * FROM v_pipeline"
    if not a.all:
        q += " WHERE status NOT IN ('rejected','withdrawn')"
    q += " ORDER BY (applied_on IS NULL), applied_on DESC"
    rows = conn.execute(q).fetchall()
    if not rows:
        print("no applications"); return
    w = max(len(r["company"] or "") for r in rows) + 2
    print(f"{'COMPANY':<{w}}{'ROLE':<44}{'APPLIED':<12}{'STATUS':<12}QUIET")
    print("-" * (w + 76))
    for r in rows:
        role = (r["role"] or "")[:42]
        print(f"{(r['company'] or '')[:w-2]:<{w}}{role:<44}{(r['applied_on'] or '')[:10]:<12}"
              f"{r['status']:<12}{r['days_quiet'] if r['days_quiet'] is not None else ''}")
    print(f"\n{len(rows)} applications")


def cmd_why(a):
    """Audit trail: every event behind an application's current status."""
    conn = db.connect(a.db)
    rows = conn.execute("""SELECT c.name company, r.title role, a.status
                           FROM applications a JOIN roles r ON r.id=a.role_id
                           JOIN companies c ON c.id=r.company_id WHERE a.id=?""", (a.id,)).fetchone()
    if not rows:
        print(f"no application {a.id}"); return
    print(f"{rows['company']} - {rows['role']}  [{rows['status']}]\n")
    for e in conn.execute("""SELECT occurred_at, type, confidence, subject, sender, raw
                             FROM events WHERE application_id=? ORDER BY occurred_at""", (a.id,)):
        print(f"  {e['occurred_at'][:10]}  {e['type']:<18} conf={e['confidence']}")
        print(f"      {e['subject']}")
        print(f"      why: {e['raw']}\n")


def cmd_event(a):
    """Record an event by hand (portal/verbal news that never hits email)."""
    import datetime
    conn = db.connect(a.db); db.init(conn)
    cid = db.get_or_create_company(conn, a.company)
    rid = db.get_or_create_role(conn, cid, a.role)
    aid = db.get_or_create_application(conn, rid, applied_on=a.applied, channel="manual")
    when = a.date or datetime.date.today().isoformat()
    db.add_event(conn, aid, when, a.type, "manual", confidence=1.0,
                 subject=a.note or f"manual: {a.type}", raw="entered by hand")
    conn.commit()
    print(f"[{aid}] {a.company} - {a.role} -> {db.recompute_status(conn, aid)}")


def cmd_reclassify(a):
    """Re-run classification over stored events after changing classify.py."""
    from .classify import classify
    conn = db.connect(a.db)
    rows = conn.execute("SELECT id, subject, sender, type, body FROM events").fetchall()
    changed = {}
    for r in rows:
        c = classify(r["subject"] or "", r["sender"] or "", r["body"] or "")
        if c.event_type != r["type"] and c.event_type != "unresolved":
            conn.execute("UPDATE events SET type=?, confidence=? WHERE id=?",
                         (c.event_type, c.confidence, r["id"]))
            changed[(r["type"], c.event_type)] = changed.get((r["type"], c.event_type), 0) + 1
    conn.commit()
    n = db.recompute_all(conn)
    for (old, new), k in sorted(changed.items(), key=lambda kv: -kv[1]):
        print(f"  {old:<18} -> {new:<18} {k}")
    print(f"\n{sum(changed.values())} events reclassified; {n} statuses recomputed")


def cmd_resolve(a):
    from .resolve import merge_companies, dedupe_roles, drain, drop_noise_only
    conn = db.connect(a.db)
    m = merge_companies(conn); d = dedupe_roles(conn); g = drop_noise_only(conn)
    print(f"companies: renamed {m['renamed']}, merged {m['merged']}; "
          f"duplicate roles folded {d}; noise-only applications dropped {g}")
    if not a.clean_only:
        s = drain(conn, limit=a.limit)
        print("  ".join(f"{k}={v}" for k, v in s.items()))
    db.recompute_all(conn)


def cmd_assign(a):
    """Attach an application to a known role by hand (portal-sourced titles)."""
    from .resolve import set_identity
    conn = db.connect(a.db)
    ok = set_identity(conn, a.id, a.company, a.role)
    conn.commit(); db.recompute_all(conn)
    print(("assigned" if ok else "no change") + f": [{a.id}] -> {a.company or ''} / {a.role}")


def cmd_unassigned(a):
    """Applications whose role could not be recovered, with any portal roles to pick from."""
    conn = db.connect(a.db)
    rows = conn.execute("""SELECT a.id, c.name company, a.submitted_at, a.applied_on, a.status
                           FROM applications a JOIN roles r ON r.id=a.role_id
                           JOIN companies c ON c.id=r.company_id
                           WHERE r.title IN ('Unknown','Unknown role')
                             AND (? IS NULL OR c.name = ?)
                           ORDER BY c.name, COALESCE(a.submitted_at, a.applied_on)""",
                        (a.company, a.company)).fetchall()
    for r in rows:
        print(f"  [{r['id']:>4}] {r['company'][:18]:<20}{(r['submitted_at'] or r['applied_on'] or '')[:16]:<18}{r['status']}")
    print(f"\n{len(rows)} unassigned")
    if a.company:
        p = conn.execute("""SELECT role, portal_status, portal_seen FROM portal_snapshot
                            WHERE company=? ORDER BY role""", (a.company,)).fetchall()
        if p:
            print(f"\nknown {a.company} roles from the portal snapshot (status shown is the")
            print("portal's own and is NOT used for tracking):")
            for x in p:
                print(f"    {x['role'][:58]:<60}{x['portal_status']} ({x['portal_seen']})")


def cmd_stats(a):
    conn = db.connect(a.db)
    print("BY STATUS")
    for r in conn.execute("SELECT status, COUNT(*) n FROM applications GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']:<14}{r['n']}")
    print("\nEVENTS")
    for r in conn.execute("SELECT type, COUNT(*) n FROM events GROUP BY type ORDER BY n DESC"):
        print(f"  {r['type']:<20}{r['n']}")
    n = conn.execute("SELECT COUNT(*) n FROM review_queue WHERE resolved=0").fetchone()["n"]
    print(f"\nreview queue: {n}")


def cmd_review(a):
    conn = db.connect(a.db)
    rows = conn.execute("""SELECT rq.id, rq.reason, e.subject, e.sender, e.type, e.occurred_at
                           FROM review_queue rq JOIN events e ON e.id = rq.event_id
                           WHERE rq.resolved = 0 ORDER BY rq.id""").fetchall()
    if not rows:
        print("review queue empty"); return
    for r in rows:
        print(f"[{r['id']:>3}] {r['occurred_at'][:10]}  type={r['type']}")
        print(f"      subject: {r['subject']}")
        print(f"      from:    {r['sender']}")
        print(f"      why:     {r['reason']}\n")
    print(f"{len(rows)} awaiting review")


def cmd_sync(a):
    from .gmail import sync
    conn = db.connect(a.db); db.init(conn)
    s = sync(conn, newer_than=a.since, max_results=a.max, refetch=a.refetch)
    print("  ".join(f"{k}={v}" for k, v in s.items()))


def location_verdict(loc: str, home: dict) -> str:
    """'remote' | 'local' | 'ambiguous' | 'elsewhere', per config home_locations.
    Ambiguous national postings pass with a flag: under-filtering beats hiding a
    role you could actually take behind a vague 'United States'."""
    import re
    l = (loc or "").lower()
    if any(k in l for k in home.get("remote", [])):
        return "remote"
    for k in home.get("colorado", []):
        if k == ", co":
            if re.search(r",\s*co\b", l):
                return "colorado"
        elif k in l:
            return "colorado"
    # A named non-CO city/state makes it concrete, not ambiguous:
    # "Chicago, IL, USA" is elsewhere; a bare "United States" is genuinely open.
    if re.search(r",\s*[a-z]{2}\b", l) and not re.search(r",\s*co\b", l):
        return "elsewhere"
    if any(re.search(r"\b" + re.escape(k) + r"\b", l) for k in home.get("ambiguous", [])):
        return "ambiguous"
    return "elsewhere"


def _config():
    import json, pathlib
    p = pathlib.Path(__file__).resolve().parent.parent / "config.json"
    return json.loads(p.read_text())


def cmd_discover(a):
    from .discover import discover
    conn = db.connect(a.db); db.init(conn)
    cfg = _config()
    s = discover(conn, cfg["watchlist"], cfg["titles"], cfg["locations"],
                 cfg.get("exclude_titles", []), cfg.get("comp_floor", 0))
    failed = s.pop("failed", [])
    print("  ".join(f"{k}={v}" for k, v in s.items()))
    if failed:
        print("unreachable: " + ", ".join(failed))


def cmd_fit(a):
    from .fit import score_pending, backend_status
    b = backend_status()
    if b == "none":
        print("no LLM backend. Set ANTHROPIC_API_KEY, or run `claude` once and /login."); return 1
    conn = db.connect(a.db); db.init(conn)
    print(f"backend={b}")
    print("  ".join(f"{k}={v}" for k, v in score_pending(conn, limit=a.limit, rescore=a.rescore).items()))


def cmd_prospects(a):
    conn = db.connect(a.db)
    rows = conn.execute("""SELECT a.id, c.name company, r.title, r.location, a.fit_score,
                                  json_extract(a.fit_reasoning,'$.one_line') one_line
                           FROM applications a JOIN roles r ON r.id=a.role_id
                           JOIN companies c ON c.id=r.company_id
                           WHERE a.status='prospect' AND a.fit_score >= ?
                           ORDER BY a.fit_score DESC LIMIT ?""",
                        (a.min if a.min is not None else _config().get("min_fit_to_surface", 0),
                         a.limit * 4)).fetchall()
    if not rows:
        print("no scored prospects. run: discover, then fit"); return
    pol = _config().get("company_policy", {})
    kept, suppressed = [], []
    for r in rows:
        rule = next((v for k, v in pol.items() if k.lower() in (r["company"] or "").lower()), None)
        if rule:
            loc = (r["location"] or "").lower()
            if r["fit_score"] < rule.get("min_score", 0):
                suppressed.append((r, f"below {rule['min_score']} threshold")); continue
            if rule.get("require_location"):
                v = location_verdict(r["location"], _config().get("home_locations", {}))
                if v == "elsewhere":
                    suppressed.append((r, f"location: {(r['location'] or '?')[:34]}")); continue
                r = dict(r); r["_loc_note"] = "verify location eligibility" if v == "ambiguous" else ""
        kept.append(r)
    rows = kept[:a.limit]
    for r in rows:
        print(f"[{r['id']:>3}] {r['fit_score']:>3}  {r['company']} - {r['title'][:52]}")
        print(f"          {r['location'] or ''}")
        rule = next((v for k, v in pol.items() if k.lower() in (r["company"] or "").lower()), None)
        if rule:
            print(f"          [!] {r['company']}: conditional - {rule.get('rule','')[:140]}")
            if r.get("_loc_note"):
                print(f"          [?] {r['_loc_note']}")
        if r["one_line"]:
            print(f"          {r['one_line']}")
    _print_suppressed(suppressed)


def _print_suppressed(suppressed):
    if suppressed:
        print(f"\n-- suppressed by company policy ({len(suppressed)}) --")
        for r, why in suppressed[:8]:
            print(f"   {r['fit_score']:>3}  {r['company']} - {r['title'][:44]}  ({why})")


def cmd_intel(a):
    """Public employee sentiment. Reference only: never feeds fit or status."""
    import os, json
    from .intel import run
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("intel needs ANTHROPIC_API_KEY (web search runs through the Messages API)."); return 1
    conn = db.connect(a.db); db.init(conn)
    if a.show:
        rows = conn.execute("""SELECT * FROM company_intel
                               ORDER BY (rating IS NULL), rating DESC""").fetchall()
        if not rows:
            print("no intel yet. run: careerops intel"); return 0
        for r in rows:
            rt = f"{r['rating']:.1f}" if r["rating"] is not None else "?"
            w  = f"{r['wlb']:.1f}"    if r["wlb"]    is not None else "?"
            n  = f"{r['reviews_n']:,}" if r["reviews_n"] else "?"
            rec = f"{r['recommend_pct']}% recommend  " if r["recommend_pct"] is not None else ""
            print(f"\n{r['company']}  {rt}/5  (wlb {w}/5, {n} reviews)  {rec}[{r['confidence']}]")
            if r["entity_note"]: print(f"  entity: {r['entity_note']}")
            for pro in json.loads(r["pros"] or "[]"):  print(f"  +  {pro}")
            for con in json.loads(r["cons"] or "[]"):  print(f"  -  {con}")
            if r["wlb_summary"]: print(f"  wlb: {r['wlb_summary']}")
            for s in json.loads(r["sources"] or "[]")[:3]: print(f"  src: {s.get('url','')}")
        return 0
    print("  ".join(f"{k}={v}" for k, v in
          run(conn, limit=a.limit, refresh=a.refresh, company=a.company).items()))


def cmd_demo(a):
    """Seed a synthetic pipeline in its own database. Never touches your real one."""
    from .demo import seed, DEMO_DB
    from .dashboard import collect, render
    st = seed()
    print("  ".join(f"{k}={v}" for k, v in st.items()))
    conn = db.connect(DEMO_DB)
    out = a.out or str(pathlib.Path(DEMO_DB).parent / "demo-dashboard.html")
    pathlib.Path(out).write_text(render(collect(conn), artifact=a.artifact))
    print(out)
    if a.open:
        import subprocess; subprocess.run(["open", out])


def cmd_dashboard(a):
    from .dashboard import write
    import subprocess, pathlib
    out = a.out or str(pathlib.Path(__file__).resolve().parent.parent / "dashboard.html")
    p = write(db.connect(a.db), out, artifact=a.artifact)
    print(p)
    if a.open:
        subprocess.run(["open", p])


def cmd_resume(a):
    from .resume import build
    r = build(db.connect(a.db), a.id, a.out)
    print(f"{r['company']} - {r['title']}  (fit {r['score']})")
    print(f"  tagline: {r['tagline']}")
    print(f"  profile: {r['profile'][:150]}...")
    print(f"  bullets: {len(r['bullets'])} kept, {len(r['dropped'])} dropped")
    print("  top-ranked:")
    for b in r["top"]:
        print(f"    {b['score']:>5}  {b['label']}")
    print(f"  -> {r['out']}")


def cmd_analytics(a):
    from .analytics import report
    print(report(db.connect(a.db)))


def cmd_doctor(a):
    import pathlib, importlib, os
    root = pathlib.Path(__file__).resolve().parent.parent
    ok = lambda b: "OK " if b else "-- "
    print(ok((root/"careerops.db").exists()), "database")
    print(ok((root/"profile.md").exists()), "profile.md")
    print(ok((root/"config.json").exists()), "config.json")
    print(ok((root/"credentials.json").exists()), "credentials.json (Gmail OAuth)")
    print(ok((root/"token.json").exists()), "token.json (Gmail authorized)")
    for m in ("googleapiclient", "google_auth_oauthlib"):
        try:
            importlib.import_module(m); print("OK ", m)
        except ImportError:
            print("-- ", m, "(pip install -r requirements.txt)")
    from .fit import backend_status
    b = backend_status()
    print(ok(b != "none"), f"LLM backend ({b})")


def cmd_validate(a):
    from .validate import run
    return run()


def main(argv=None):
    p = argparse.ArgumentParser(prog="careerops")
    p.add_argument("--db", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    ic = sub.add_parser("ingest-csv"); ic.add_argument("path"); ic.set_defaults(fn=cmd_ingest_csv)
    pl = sub.add_parser("pipeline"); pl.add_argument("--all", action="store_true"); pl.set_defaults(fn=cmd_pipeline)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    asg = sub.add_parser("assign"); asg.add_argument("id", type=int); asg.add_argument("role")
    asg.add_argument("--company"); asg.set_defaults(fn=cmd_assign)
    un = sub.add_parser("unassigned"); un.add_argument("--company"); un.set_defaults(fn=cmd_unassigned)
    sub.add_parser("reclassify").set_defaults(fn=cmd_reclassify)
    ri = sub.add_parser("reidentify"); ri.add_argument("--dry", action="store_true")
    ri.set_defaults(fn=lambda a: print("  ".join(f"{k}={v}" for k, v in
        __import__("careerops.resolve", fromlist=["reidentify"]).reidentify(db.connect(a.db), dry=a.dry).items())))
    rv = sub.add_parser("resolve"); rv.add_argument("--limit", type=int, default=400)
    rv.add_argument("--clean-only", action="store_true"); rv.set_defaults(fn=cmd_resolve)
    ev = sub.add_parser("event")
    ev.add_argument("company"); ev.add_argument("role")
    ev.add_argument("--type", required=True,
                    choices=["ack","rejection","interview_invite","assessment","recruiter_outreach","offer"])
    ev.add_argument("--date"); ev.add_argument("--applied"); ev.add_argument("--note")
    ev.set_defaults(fn=cmd_event)
    wy = sub.add_parser("why"); wy.add_argument("id", type=int); wy.set_defaults(fn=cmd_why)
    sub.add_parser("review").set_defaults(fn=cmd_review)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    sub.add_parser("discover").set_defaults(fn=cmd_discover)
    ft = sub.add_parser("fit"); ft.add_argument("--limit", type=int, default=10)
    ft.add_argument("--rescore", action="store_true"); ft.set_defaults(fn=cmd_fit)
    pr = sub.add_parser("prospects"); pr.add_argument("--limit", type=int, default=20)
    pr.add_argument("--min", type=int, default=None); pr.set_defaults(fn=cmd_prospects)
    it = sub.add_parser("intel"); it.add_argument("--limit", type=int, default=10)
    it.add_argument("--refresh", action="store_true"); it.add_argument("--company")
    it.add_argument("--show", action="store_true"); it.set_defaults(fn=cmd_intel)
    sub.add_parser("analytics").set_defaults(fn=cmd_analytics)
    rs = sub.add_parser("resume"); rs.add_argument("id", type=int); rs.add_argument("--out")
    rs.set_defaults(fn=cmd_resume)
    dm = sub.add_parser("demo"); dm.add_argument("--out"); dm.add_argument("--open", action="store_true")
    dm.add_argument("--artifact", action="store_true"); dm.set_defaults(fn=cmd_demo)
    dh = sub.add_parser("dashboard"); dh.add_argument("--out"); dh.add_argument("--open", action="store_true")
    dh.add_argument("--artifact", action="store_true")
    dh.set_defaults(fn=cmd_dashboard)
    sy = sub.add_parser("sync"); sy.add_argument("--since", default="1y"); sy.add_argument("--max", type=int, default=400)
    sy.add_argument("--refetch", action="store_true")
    sy.set_defaults(fn=cmd_sync)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
