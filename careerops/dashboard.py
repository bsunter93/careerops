"""Generate a self-contained HTML command center from the database.

One file, no CDN, no server, no external dependencies. Data is embedded as JSON
so the page works offline, from a phone, or published as a private Artifact.
Every status is expandable to the event log that produced it -- traceability is
the point, not a feature.
"""
import json, html, pathlib, sqlite3
from . import db

STALE_DAYS = 21
ACT_SCORE = 75


def _rows(conn, q, args=()):
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def collect(conn) -> dict:
    apps = _rows(conn, """
        SELECT a.id, c.name AS company, r.title AS role, r.location, r.url,
               a.applied_on, a.submitted_at, a.status, a.activity, a.channel, a.referral, a.fit_score,
               a.fit_reasoning, r.comp_min, r.comp_max,
               (SELECT MAX(occurred_at) FROM events e
                  WHERE e.application_id=a.id AND e.type!='noise') AS last_event,
               CAST(julianday('now') - julianday(COALESCE(
                  (SELECT MAX(occurred_at) FROM events e
                     WHERE e.application_id=a.id AND e.type!='noise'),
                  a.applied_on)) AS INT) AS days_quiet
        FROM applications a
        JOIN roles r ON r.id=a.role_id
        JOIN companies c ON c.id=r.company_id""")

    ev = {}
    for e in _rows(conn, """SELECT application_id, occurred_at, type, confidence,
                                   subject, sender, source, raw
                            FROM events WHERE application_id IS NOT NULL AND type!='noise'
                            ORDER BY occurred_at"""):
        ev.setdefault(e["application_id"], []).append(e)
    CLASSES = [("Business Ops", ["business operation","bizops","operational excellence"]),
               ("Strategy & Ops", ["strategy and operations","strategy & operations","strategic","business strategy"]),
               ("GTM / Revenue", ["gtm","go-to-market","revenue operations","sales operations","partner"]),
               ("Program Mgmt", ["program manager","program management","tpm","technical program"]),
               ("Chief of Staff", ["chief of staff"]),
               ("Product Ops", ["product operations","product ops"])]
    # Number repeat submissions to the same role. Prospects are not submissions, so
    # they must not inflate the denominator; order by the submission timestamp, which
    # is the identity, not by the coarser applied_on date.
    seen = {}
    for a in sorted([x for x in apps if x["status"] != "prospect"],
                    key=lambda x: (x["company"], (x["role"] or "").lower(),
                                   x["submitted_at"] or x["applied_on"] or "")):
        seen.setdefault((a["company"], (a["role"] or "").lower()), []).append(a)
    for group in seen.values():
        if len(group) > 1:
            dates = [(g["submitted_at"] or g["applied_on"] or "")[:10] for g in group]
            for n, a in enumerate(group, 1):
                a["attempt"] = f"{n} of {len(group)}"
                a["attempt_note"] = ("Submission " + str(n) + " of " + str(len(group)) +
                                     " to this role: " + ", ".join(dates) +
                                     ". Others may be hidden by the current filter.")
    for a in apps:
        a["events"] = ev.get(a["id"], [])
        a["event_dates"] = sorted({(e["occurred_at"] or "")[:10] for e in a["events"] if e["occurred_at"]})
        t = (a["role"] or "").lower()
        a["submitted_on"] = (a["submitted_at"] or a["applied_on"] or "")[:10]
        a["klass"] = next((n for n, kws in CLASSES if any(k in t for k in kws)), "Other")
        try:
            a["fit"] = json.loads(a["fit_reasoning"]) if a["fit_reasoning"] else None
        except Exception:
            a["fit"] = None
        a.pop("fit_reasoning", None)

    POS = ("in_process", "assessment", "interview", "offer")
    real = [a for a in apps if a["status"] != "prospect"]
    responded = [a for a in real if a["status"] != "applied"]

    # A funnel counts what an application EVER reached, not where it sits now. Status is
    # monotonic and rejection outranks interview, so counting current status erased every
    # loop that ended in a no: two Walmart interviews vanished the moment the rejection
    # landed, and the funnel reported zero advances for 2026.
    adv_ids = {r["application_id"] for r in conn.execute(
        """SELECT DISTINCT application_id FROM events
           WHERE application_id IS NOT NULL
             AND type IN ('interview_invite','assessment','offer','recruiter_outreach')""")}
    itv_ids = {r["application_id"] for r in conn.execute(
        """SELECT DISTINCT application_id FROM events
           WHERE application_id IS NOT NULL
             AND type IN ('interview_invite','offer')""")}
    ages = {r["id"]: r["age"] for r in conn.execute(
        """SELECT a.id, CAST(julianday('now') - julianday(r.posted_at) AS INT) age
           FROM applications a JOIN roles r ON r.id = a.role_id
           WHERE r.posted_at IS NOT NULL""")}
    for a in apps:
        a["posted_age"] = ages.get(a["id"])
        a["ever_advanced"] = a["id"] in adv_ids or a["status"] in POS
        a["ever_interviewed"] = a["id"] in itv_ids or a["status"] in ("interview", "offer")
    positive = [a for a in real if a["ever_advanced"]]
    interview = [a for a in real if a["ever_interviewed"]]
    open_apps = [a for a in real if a["activity"] in ("active", "dormant")]
    dormant = [a for a in real if a["activity"] == "dormant"]
    live = [a for a in real if a["activity"] == "active"]
    live_interview = [a for a in live if a["status"] in ("interview", "offer")]

    funnel = [
        {"k": "submitted", "label": "Submitted",  "n": len(real)},
        {"k": "acked",     "label": "Acknowledged","n": len(responded)},
        {"k": "positive",  "label": "Ever advanced", "n": len(positive)},
        {"k": "interview", "label": "Ever interviewed", "n": len(interview)},
    ]

    # Aging is a state, so it takes the status palette, with labels + icons.
    buckets = [("0-7d", 0, 7, "good", "\u25cf"), ("8-14d", 8, 14, "warning", "\u25d0"),
               ("15-21d", 15, 21, "serious", "\u25d1"), ("22d+", 22, 10**6, "critical", "\u25cb")]
    aging = []
    for label, lo, hi, role, icon in buckets:
        ids = [a["id"] for a in open_apps if a["days_quiet"] is not None and lo <= a["days_quiet"] <= hi]
        aging.append({"label": label, "n": len(ids), "role": role, "icon": icon, "lo": lo, "hi": hi})

    # Weekly submitted vs. replies received
    from datetime import date, timedelta
    def monday(d):
        return d - timedelta(days=d.weekday())
    def parse(x):
        try: return date.fromisoformat((x or "")[:10])
        except Exception: return None
    today = date.today(); first = monday(today) - timedelta(weeks=11)
    weeks = [first + timedelta(weeks=i) for i in range(12)]
    sub = {w: 0 for w in weeks}; rep = {w: 0 for w in weeks}
    for a in real:
        d = parse(a["applied_on"])
        if d and monday(d) in sub: sub[monday(d)] += 1
        for e in a["events"]:
            if e["type"] in ("rejection", "interview_invite", "assessment", "offer", "recruiter_outreach"):
                d2 = parse(e["occurred_at"])
                if d2 and monday(d2) in rep: rep[monday(d2)] += 1
    weekly = [{"w": w.isoformat(), "label": w.strftime("%b %-d"),
               "sub": sub[w], "rep": rep[w]} for w in weeks]

    # Pacing. Rolling 7-day windows, not calendar weeks: the current calendar week is
    # partial for six days out of seven, so comparing it to a finished one always reads
    # as a collapse in output that did not happen.
    def window(lo, hi):
        n_sub = sum(1 for a in real
                    if (d := parse(a["applied_on"])) and lo <= d < hi)
        n_rep = sum(1 for a in real for e in a["events"]
                    if e["type"] in ("rejection", "interview_invite", "assessment",
                                     "offer", "recruiter_outreach")
                    and (d := parse(e["occurred_at"])) and lo <= d < hi)
        n_adv = sum(1 for a in real for e in a["events"]
                    if e["type"] in ("interview_invite", "assessment", "offer")
                    and (d := parse(e["occurred_at"])) and lo <= d < hi)
        return {"sent": n_sub, "replies": n_rep, "advanced": n_adv}
    d0 = today + timedelta(days=1)          # inclusive of today
    cur  = window(d0 - timedelta(days=7),  d0)
    prev = window(d0 - timedelta(days=14), d0 - timedelta(days=7))
    full = [w for w in weeks[:-1]]          # completed weeks only
    pace = {"cur": cur, "prev": prev,
            "avg4": round(sum(sub[w] for w in full[-4:]) / 4, 1) if len(full) >= 4 else None,
            "delta": {k: cur[k] - prev[k] for k in cur}}

    scored = [a for a in apps if a["fit_score"] is not None]
    bands = [(50,59,"50s"),(60,69,"60s"),(70,79,"70s"),(80,100,"80+")]
    fit_hist = [{"label": lb, "lo": lo, "hi": hi,
                 "n": sum(1 for a in scored if lo <= a["fit_score"] <= hi)} for lo,hi,lb in bands]
    fit_low = sum(1 for a in scored if a["fit_score"] < 50)

    # Per company: what is still alive, what went quiet, what is closed, and whether it
    # ever advanced. "Ever advanced" alone is history; "active" alone hides that a company
    # engaged once. The bar shows current state; the ever-advanced count sits in the label.
    byco = {}
    for a in real:
        b = byco.setdefault(a["company"], {"company": a["company"], "n": 0, "resp": 0,
                                           "pos": 0, "active": 0, "dormant": 0, "closed": 0})
        b["n"] += 1
        if a["status"] != "applied": b["resp"] += 1
        if a["ever_advanced"]: b["pos"] += 1
        if a["status"] in ("rejected", "withdrawn"): b["closed"] += 1
        elif a["activity"] == "dormant":            b["dormant"] += 1
        else:                                        b["active"] += 1
    companies = sorted([b for b in byco.values() if b["n"] >= 2],
                       key=lambda b: (-b["active"], -b["pos"], -b["n"]))[:10]

    # Public sentiment, keyed by company. Reference only: it is attached to the view,
    # never merged into fit_score, so a 4.8 on Glassdoor can never quietly promote a role.
    intel = {}
    for r in conn.execute("SELECT * FROM company_intel"):
        intel[r["company"]] = {
            "rating": r["rating"], "wlb": r["wlb"], "n": r["reviews_n"],
            "rec": r["recommend_pct"], "conf": r["confidence"],
            "pros": json.loads(r["pros"] or "[]"), "cons": json.loads(r["cons"] or "[]"),
            "wlb_summary": r["wlb_summary"], "entity": r["entity_note"],
            "sources": json.loads(r["sources"] or "[]"),
            "fetched": (r["fetched_at"] or "")[:10],
        }

    # Recent activity. Answers "what has moved lately", which no chart here did: the
    # funnel is all-time, weekly is counts, and Do next only shows what has NOT happened.
    recent = [dict(r) for r in conn.execute("""
        SELECT e.occurred_at, e.type, e.source, a.id AS app_id, c.name AS company,
               r.title AS role, a.status, a.fit_score
        FROM events e
        JOIN applications a ON a.id = e.application_id
        JOIN roles r        ON r.id = a.role_id
        JOIN companies c    ON c.id = r.company_id
        WHERE e.type NOT IN ('noise', 'unresolved')
          AND e.source != 'portal'
        ORDER BY e.occurred_at DESC LIMIT 14""")]

    return {
        "apps": apps, "intel": intel, "recent": recent,
        "funnel": funnel, "aging": aging, "weekly": weekly,
        "fit_hist": fit_hist, "fit_low": fit_low, "companies": companies,
        "totals": {
            "submitted": len(real), "live": len(live), "dormant": len(dormant), "responded": len(responded),
            "positive": len(positive), "interview": len(interview), "live_interview": len(live_interview),
            "rejected": sum(1 for a in real if a["status"] == "rejected"),
            "prospects": sum(1 for a in apps if a["status"] == "prospect"),
            "review": conn.execute("SELECT COUNT(*) FROM review_queue WHERE resolved=0").fetchone()[0],
            "rate": round(len(positive) / len(real) * 100) if real else 0,
            "ackrate": round(len(responded) / len(real) * 100) if real else 0,
        },
        "stale_days": STALE_DAYS, "act_score": ACT_SCORE, "pace": pace,
    }


def render(data: dict, artifact: bool = False) -> str:
    payload = json.dumps(data).replace("</", "<\\/")
    tpl = BODY if artifact else TEMPLATE.replace("__BODY__", BODY)
    return tpl.replace("__DATA__", payload)


def write(conn, out: str, artifact: bool = False) -> str:
    p = pathlib.Path(out)
    p.write_text(render(collect(conn), artifact), encoding="utf-8")
    return str(p.resolve())


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
__BODY__
</body></html>"""

BODY = r"""<title>Career Ops Command Center</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root{
  --bg:#f7f7f5; --panel:#fff; --ink:#0f1012; --muted:#5f636b; --faint:#8b8f97;
  --line:#e4e4df; --grid:#eeeeea; --accent:#2a78d6; --soft:#eef3fb;
  --s1:#2a78d6; --s2:#eb6834;
  --seq1:#cde2fb; --seq2:#86b6ef; --seq3:#3987e5; --seq4:#1c5cab; --seq5:#0d366b;
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
  --r-ctl:3px;   /* controls and inline tokens: pills, chips, buttons, inputs */
  --sans:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --bg:#101216; --panel:#191c22; --ink:#dfe3ea; --muted:#99a0ac; --faint:#727986;
  --line:#282c34; --grid:#21242b; --accent:#4a92e8; --soft:#1a2331;
  --s1:#3987e5; --s2:#d95926;
  --seq1:#1c3050; --seq2:#1c5cab; --seq3:#2a78d6; --seq4:#5598e7; --seq5:#9ec5f4;}}
:root[data-theme=dark]{
  --bg:#101216; --panel:#191c22; --ink:#dfe3ea; --muted:#99a0ac; --faint:#727986;
  --line:#282c34; --grid:#21242b; --accent:#4a92e8; --soft:#1a2331;
  --s1:#3987e5; --s2:#d95926;
  --seq1:#1c3050; --seq2:#1c5cab; --seq3:#2a78d6; --seq4:#5598e7; --seq5:#9ec5f4;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 var(--sans)}
.wrap{max-width:1120px;margin:0 auto;padding:22px 18px 80px}
h1{font-size:20px;font-weight:600;letter-spacing:-.2px;margin:0 0 3px}
.stamp{color:var(--faint);font:400 11.5px/1.5 var(--mono);margin-bottom:20px}
.views{display:flex;gap:0;margin:0 0 16px;border-bottom:1px solid var(--line)}
.views button{background:none;border:0;border-bottom:2px solid transparent;color:var(--muted);
  padding:7px 2px;margin-right:22px;font:600 12.5px var(--sans);cursor:pointer;border-radius:0}
.views button:hover{color:var(--ink)}
.views button.on{color:var(--ink);border-bottom-color:var(--accent)}
.hero{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:8px}
.tile{background:var(--panel);border:1px solid var(--line);border-radius:0;padding:10px 13px}
.tile.lead{border-color:var(--accent);background:var(--soft)}
.tile .n{font:600 23px/1.05 var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-1px}
.tile .l{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.7px;margin-top:3px}
.tile .h{font-size:11.5px;color:var(--faint);margin-top:3px}
.dl{display:inline-block;margin-top:7px;font:600 10.5px var(--mono);padding:2px 6px;
  border-radius:var(--r-ctl);letter-spacing:.02em}
.dl.up{color:var(--good);background:color-mix(in srgb,var(--good) 12%,transparent)}
.dl.down{color:var(--critical);background:color-mix(in srgb,var(--critical) 12%,transparent)}
.dl.flat{color:var(--faint);background:var(--grid)}
.info{display:inline-flex;align-items:center;justify-content:center;width:13px;height:13px;
  border:1px solid var(--line);border-radius:50%;font:600 9px var(--mono);color:var(--faint);
  margin-left:5px;cursor:help;vertical-align:1px;text-transform:none}
.info:hover{border-color:var(--accent);color:var(--accent)}
#tip{max-width:290px;white-space:normal;line-height:1.45;font-family:var(--sans);font-size:11.5px}
h2{font-size:11px;text-transform:uppercase;letter-spacing:1.1px;color:var(--muted);font-weight:600;
   margin:22px 0 0;padding-bottom:6px;border-bottom:1px solid var(--line);
   display:flex;align-items:center;gap:8px}
h2 b{color:var(--accent);font-weight:600}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:start}
@media(max-width:700px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:0;padding:12px 14px 12px}
.panel h3{font-size:13px;font-weight:600;margin:0 0 2px}
.panel .cap{font-size:11px;color:var(--faint);margin-bottom:9px;line-height:1.45;max-width:58ch}
.sub{font:600 9.5px var(--mono);text-transform:uppercase;letter-spacing:.9px;color:var(--faint);
  margin:14px 0 6px;padding-top:11px;border-top:1px solid var(--grid)}
.sub span{text-transform:none;letter-spacing:0;font-weight:400;font-family:var(--sans);
  font-size:11px;margin-left:8px}
.legend{display:flex;gap:14px;font-size:11.5px;color:var(--muted);margin-bottom:8px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:var(--r-ctl);margin-right:5px;vertical-align:-1px}
svg{display:block;width:100%;max-width:100%;height:auto;overflow:visible}
.grid>.panel{min-width:0}
.panel>div{min-width:0}
.hit{cursor:pointer} .hit:hover{opacity:.78}
.axis{fill:var(--faint);font:500 10.5px var(--mono)}
.vlab{fill:var(--ink);font:600 11px var(--mono)}
.slab{fill:var(--muted);font:500 11px var(--sans)}
.act{display:flex;gap:9px;align-items:center;padding:0;border-bottom:0}
.act.good .bar{background:var(--good)} .act .bar{background:var(--accent)}
.act.quiet .bar{background:var(--line)}
.act .bar{width:2px;align-self:stretch;flex:none;border-radius:2px}
/* One line per row: the whole decision list has to sit in a quadrant cell, so the
   location and the score reasoning move into the drill-down where there is room. */
.act-w{border-bottom:1px solid var(--grid)} .act-w:last-child{border-bottom:0}
.act{padding:7px 6px;margin:0 -6px;cursor:pointer}
.act:hover{background:var(--soft)}
.act-t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12.5px}
.act-t .ro{color:var(--muted)}
.act.quiet .act-t b{color:var(--muted);font-weight:600}
.act-v{flex:none;font:600 14px var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.3px}
.act.quiet .act-v{color:var(--muted);font-size:12.5px}
.f-hi{color:var(--good)} .f-mid{color:var(--seq3)} .f-lo{color:var(--seq2)} .f-min{color:var(--muted)}
.age{font:600 10px var(--mono);padding:1px 4px;border-radius:var(--r-ctl);margin-left:2px}
.age.fresh{color:var(--good);background:color-mix(in srgb,var(--good) 13%,transparent)}
.age.ok{color:var(--muted);background:var(--grid)}
.age.old{color:var(--serious);background:color-mix(in srgb,var(--serious) 13%,transparent)}
.act-x{flex:none;color:var(--faint);font:400 14px var(--mono);transition:transform .14s}
.act.open .act-x{transform:rotate(90deg);color:var(--accent)}
.act-d{padding:0 0 10px 13px}
.rc{display:flex;align-items:baseline;gap:10px;padding:6px 6px;margin:0 -6px;
  border-bottom:1px solid var(--grid);cursor:pointer;font-size:12.5px}
.rc:last-child{border-bottom:0} .rc:hover{background:var(--soft)}
.rc-d{flex:none;width:38px;font:400 11px var(--mono);color:var(--faint);text-align:right}
.rc-t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rc-t .ro{color:var(--muted)}
.rc-e{flex:none;font:600 10.5px var(--mono);text-transform:uppercase;letter-spacing:.4px}
.rc-s{display:block;font-weight:400;font-size:9px;color:var(--faint);letter-spacing:.3px}
.act-g{font:600 9.5px var(--mono);text-transform:uppercase;letter-spacing:.9px;color:var(--faint);
  margin:11px 0 3px;padding:0}
.act-g:first-child{margin-top:0}
/* Never let the list blow out the quadrant; it scrolls inside its own panel. */
#actions{max-height:292px;overflow-y:scroll;margin-right:-6px;padding-right:8px;
  scrollbar-width:thin;scrollbar-color:var(--line) transparent}
#actions::-webkit-scrollbar{width:9px;-webkit-appearance:none}
#actions::-webkit-scrollbar-track{background:var(--grid);border-radius:var(--r-ctl)}
#actions::-webkit-scrollbar-thumb{background:var(--faint);border-radius:var(--r-ctl);
  border:2px solid var(--panel)}
#actions::-webkit-scrollbar-thumb:hover{background:var(--muted)}
.int-scroll{scrollbar-width:thin;scrollbar-color:var(--line) transparent}
/* Drill-downs: same shape everywhere they appear, in an action row or a table row. */
.dd{border-top:1px solid var(--grid)}
.dd:first-child{border-top:0}
.dd>summary{list-style:none;cursor:pointer;display:flex;align-items:baseline;gap:10px;
  padding:7px 2px;font-size:12px}
.dd>summary::-webkit-details-marker{display:none}
.dd>summary:before{content:"+";font:600 11px var(--mono);color:var(--faint);width:9px;flex:none}
.dd[open]>summary:before{content:"\2212";color:var(--accent)}
.dd>summary:hover .dd-k{color:var(--ink)}
.dd-k{font-weight:600;color:var(--muted);flex:none}
.dd-v{margin-left:auto;font:600 11.5px var(--mono);font-variant-numeric:tabular-nums;text-align:right}
.dd-b{padding:2px 0 12px 19px}
.tr-row{display:flex;align-items:baseline;gap:9px;padding:3px 0;font-size:12px}
.tr-t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted)}
.tr-d{font:400 11px var(--mono);flex:none}
.dd-act{display:flex;gap:14px;align-items:center;margin-top:10px;padding-left:19px}
.dd-act button,.dd-act a{background:none;border:0;padding:0;color:var(--accent);cursor:pointer;
  font:600 11.5px var(--sans);text-decoration:none}
.dd-act button:hover,.dd-act a:hover{text-decoration:underline}
.dd-act code{font:600 11px var(--mono);background:var(--grid);padding:1px 4px;border-radius:var(--r-ctl)}
.act .t{font-weight:600;font-size:13.5px} .act .d{color:var(--muted);font-size:12px;margin-top:1px}
.tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:9px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:11px;min-height:0}
.chip{display:inline-flex;align-items:center;gap:6px;background:var(--soft);border:1px solid var(--accent);
  color:var(--accent);border-radius:var(--r-ctl);padding:3px 6px 3px 11px;font:600 11.5px var(--sans)}
.chip b{font-weight:700} .chip button{all:unset;cursor:pointer;width:16px;height:16px;line-height:15px;
  text-align:center;border-radius:50%;color:var(--accent);font-size:13px}
.chip button:hover{background:var(--accent);color:var(--panel)}
.tile{cursor:pointer;transition:border-color .12s}
.tile:hover{border-color:var(--accent)}
.tile.on{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
input,select,button{background:var(--panel);color:var(--ink);border:1px solid var(--line);
  border-radius:var(--r-ctl);padding:8px 11px;font:400 13px var(--sans)}
input{flex:1;min-width:180px} input:focus,select:focus,button:focus{outline:2px solid var(--accent);outline-offset:1px}
button{cursor:pointer} .chipf{background:var(--soft);border-color:var(--accent);color:var(--accent);font-weight:600}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:0}
th{text-align:left;font:600 10.5px var(--sans);text-transform:uppercase;letter-spacing:.6px;color:var(--faint);
   padding:10px;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap;user-select:none}
th:hover{color:var(--ink)}
td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top}
tr.r{cursor:pointer} tr.r:hover{background:var(--soft)} tr:last-child td{border-bottom:0}
.attempt{cursor:help;background:transparent;border:1px solid var(--line);color:var(--muted)}
.attempt:hover{border-color:var(--accent);color:var(--accent)}
.pill{display:inline-block;padding:1.5px 8px;border-radius:var(--r-ctl);font:600 11px var(--mono);background:var(--grid);white-space:nowrap}
.s-interview,.s-offer{background:var(--good);color:#fff}
.s-rejected{color:var(--critical)} .s-acked,.s-in_process,.s-assessment{color:var(--accent)}
.s-prospect,.s-dormant{color:var(--faint)}
.s-dormant{text-decoration:line-through;text-decoration-color:var(--line)}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:12.5px}
.int-scroll{max-height:430px;overflow-y:auto;margin-right:-6px;padding-right:6px}
.int-row{display:grid;grid-template-columns:150px 62px 1fr 96px;gap:10px;align-items:center;
  padding:6px 6px;border-bottom:1px solid var(--grid);cursor:pointer}
.int-row:hover{background:var(--soft)}
.int-nm{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.int-sc{font-family:var(--mono);font-variant-numeric:tabular-nums;font-weight:600}
.int-track{height:7px;background:var(--grid);border-radius:0;overflow:hidden;position:relative}
.int-fill{height:100%;border-radius:0 2px 2px 0}
.int-wlb{font-family:var(--mono);font-size:11px;font-variant-numeric:tabular-nums;text-align:right}
.int-body{display:none;padding:8px 6px 14px;border-bottom:1px solid var(--grid)}
.int-body.on{display:block}
.int-cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:640px){.int-cols{grid-template-columns:1fr}.int-row{grid-template-columns:1fr 56px 70px}}
.int-li{font-size:12px;line-height:1.5;margin:0 0 3px;padding-left:14px;position:relative;color:var(--muted)}
.int-li:before{position:absolute;left:0;font-weight:700}
.int-pro:before{content:"+";color:var(--good)} .int-con:before{content:"\2212";color:var(--serious)}
.int-ent{font-size:11px;color:var(--faint);line-height:1.5;margin-top:9px}
.int-src a{color:var(--accent);font-size:11px;margin-right:10px}
.hit{cursor:pointer}
svg .hit:hover{opacity:.78}
svg rect.hit[fill]:hover{opacity:.85}
text.hit{text-decoration:underline;text-decoration-color:var(--line);text-underline-offset:2px}
.int-f{border-bottom:1px dashed var(--line)}
.int-f:hover{color:var(--accent);border-bottom-color:var(--accent)}
.act.hit{cursor:pointer;padding-left:8px;padding-right:8px;margin-left:-8px;margin-right:-8px}
.act.hit:hover{background:var(--soft)}
.act.hit:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.act-go{margin-left:auto;align-self:center;font:600 11px var(--mono);color:var(--faint);
  opacity:0;transition:opacity .12s;white-space:nowrap;padding-left:10px}
.act.hit:hover .act-go,.act.hit:focus-visible .act-go{opacity:1;color:var(--accent)}
#sticky{position:sticky;top:0;z-index:8;display:flex;align-items:center;gap:9px;flex-wrap:wrap;
  padding:7px 12px;margin:0 0 16px;background:var(--panel);border:1px solid var(--line);
  border-radius:0;box-shadow:0 1px 0 var(--line)}
.jump{display:flex;align-items:baseline;gap:0;flex:none}
.jump a{color:var(--muted);text-decoration:none;font:400 12px var(--sans);padding:0 2px;
  border-bottom:1px solid transparent}
.jump a+a:before{content:"\00b7";color:var(--line);padding:0 9px 0 7px}
.jump a:hover{color:var(--ink)}
.jump a.here{color:var(--ink);font-weight:600;border-bottom-color:var(--faint)}
.jump a.here:before{border-bottom-color:transparent}
#sticky .sep{width:1px;align-self:stretch;background:var(--line);margin:0 3px}
#sticky.filtered .sep,#sticky.filtered #sk-clr{display:block}
#sticky:not(.filtered) .sep,#sticky:not(.filtered) #sk-clr,
#sticky:not(.filtered) #sk-go{display:none}
/* Panels fold to their title rather than disappearing: nothing is ever off the page. */
.panel>.fold{position:absolute;right:12px;top:12px;background:none;border:0;color:var(--faint);
  font:600 15px var(--mono);cursor:pointer;padding:0 4px;line-height:1}
.panel>.fold:hover{color:var(--accent)}
.panel{position:relative}
.panel.shut>.cap,.panel.shut>div:not(.fold):not(.legend),.panel.shut>.legend{display:none}
.panel.shut{padding-bottom:12px}
.sk-lab{font:600 10.5px var(--mono);letter-spacing:.07em;text-transform:uppercase;color:var(--faint)}
.sk-n{font:600 12px var(--mono);font-variant-numeric:tabular-nums;color:var(--muted);margin-left:auto}
.sk-btn{font:600 12px var(--sans);border:1px solid var(--accent);background:var(--accent);
  color:#fff;border-radius:var(--r-ctl);padding:5px 11px;cursor:pointer}
.sk-btn.ghost{background:transparent;color:var(--muted);border-color:var(--line)}
.sk-btn:hover{filter:brightness(1.08)} .sk-btn.ghost:hover{border-color:var(--muted);color:var(--ink)}
#sticky .chips{margin:0}
.cap.sec{margin:8px 0 10px;max-width:72ch;font-size:12px;color:var(--faint)}
h2{scroll-margin-top:64px}
.q-hot{color:var(--good);font-weight:600} .q-stale{color:var(--critical);font-weight:600}
.det{background:var(--soft)} .det td{padding:15px 17px}
.ev{border-left:2px solid var(--line);padding-left:12px;margin-bottom:11px}
.ev .h{font:500 11.5px var(--mono);color:var(--accent)} .ev .m{font-size:12px;color:var(--muted);word-break:break-word}
ul.k{margin:4px 0 10px;padding-left:15px} ul.k li{font-size:12px;margin-bottom:3px;color:var(--muted)}
.flag{color:var(--serious);font-size:12px;font-weight:600;margin-bottom:3px}
.scroll{overflow-x:auto} a{color:var(--accent)} .muted{color:var(--muted)} .small{font-size:12px}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--bg);padding:6px 9px;
  border-radius:var(--r-ctl);font:500 11.5px var(--mono);opacity:0;transition:opacity .12s;z-index:9}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
<div class="wrap">
<h1>Career Ops Command Center</h1>
<div class="stamp" id="stamp"></div>
<div class="hero" id="hero"></div>

<div id="sticky" class="on">
  <span class="sk-lab">Jump to</span>
  <nav class="jump" id="jump">
    <a href="#s-overview">Overview</a><a href="#s-next">Do next</a><a href="#s-records">Records</a>
  </nav>
  <span id="stickychips" class="chips"></span>
  <span class="sep"></span>
  <span class="sk-n" id="stickycount"></span>
  <button id="sk-go" class="sk-btn">View records &darr;</button>
  <button id="sk-clr" class="sk-btn ghost">Clear</button>
</div>

<section id="s-overview">
<div class="grid">
  <div class="panel"><h3>Recent activity</h3><div class="cap">The last things that moved, newest first. Portal-recorded outcomes are excluded: they carry the date they were logged, not the date they happened. Click a row for the table.</div><div id="c-recent"></div></div>
  <div class="panel dn" id="s-next"><h3>Do next</h3><div class="cap" id="dncap"></div><div id="actions"></div></div>
  <div class="panel"><h3>Aging, open applications</h3><div class="cap">Open applications by days since last activity. 22d+ is dormant.</div><div id="c-aging"></div><div class="sub">Funnel <span>all submitted, all time &middot; click a stage to filter</span></div><div id="c-funnel"></div></div>
  <div class="panel"><h3>Weekly activity</h3><div class="cap">Last 12 weeks.<span id="wkpace"></span></div><div id="c-weekly"></div></div>
  <div class="panel wide" style="grid-column:1/-1"><h3>By company</h3><div class="cap">Companies with 2+ applications, by what is still alive. Sorted by active threads. Click any segment to filter.</div><div id="c-co"></div></div>
</div>
</section>

<section id="s-records"><h2>All records</h2>
<div class="cap sec">The full pipeline. Every chart above filters this table.</div>
<div class="tools">
  <input id="q" placeholder="Search company or role…">
  <select id="f" title="Status"><option value="live">Active</option><option value="all">All statuses</option>
    <option value="dormant">Dormant</option><option value="prospect">Prospects</option>
    <option value="interview">Interviews</option><option value="in_process">In process</option>
    <option value="acked">Acknowledged</option><option value="rejected">Rejected</option></select>
  <select id="kl" title="Role class"><option value="">All role classes</option></select>
  <select id="fit" title="Fit score"><option value="">Any fit</option>
    <option value="80">80+</option><option value="70">70+</option><option value="60">60+</option>
    <option value="50">50+</option><option value="u50">Under 50</option><option value="none">Unscored</option></select>
  <select id="act" title="Activity"><option value="">Any activity</option>
    <option value="7">Active last 7d</option><option value="14">Active last 14d</option>
    <option value="30">Active last 30d</option><option value="q21">Quiet 21d+</option></select>
  <select id="age" title="How long the req has been open">
    <option value="">Posted: any</option>
    <option value="7">Posted last 7d</option>
    <option value="21">Posted last 21d</option>
    <option value="45">Posted last 45d</option>
    <option value="o45">Older than 45d</option>
    <option value="none">No posting date</option></select>
  <select id="ref" title="Referral"><option value="">Referral: any</option>
    <option value="1">Referred</option><option value="0">Cold</option></select>
  <input type="date" id="dt" title="Show activity on a specific date" style="flex:none;min-width:0">
  <select id="dtm" title="Date meaning" style="display:none">
    <option value="event">activity on date</option><option value="applied">applied on date</option></select>
</div>
<div id="chips" class="chips"></div>
<div class="scroll"><table><thead><tr>
<th data-k="company">Company</th><th data-k="role">Role</th><th data-k="status">Status</th>
<th data-k="fit_score">Fit</th><th data-k="posted_age">Posted</th><th data-k="applied_on">Applied</th><th data-k="days_quiet">Quiet</th>
</tr></thead><tbody id="tb"></tbody></table></div>
<div class="stamp" style="margin-top:9px"><span id="count"></span> · click any row for its full event history</div>
</section>

</div>
<div id="tip"></div>
<script>
const D = __DATA__;
const esc = s => (s??'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const cv = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const T = D.totals;
document.getElementById('stamp').textContent =
  `${D.apps.length} records · ${T.submitted} submitted · generated ${new Date().toLocaleString()}`;

// ---------- exec summary ----------
// Four cards, each a headline number over a microchart of the full chart further down.
// The summary is meant to be read at a glance, so the microcharts carry no text and no
// axes: shape only. The detailed version of each stays in Analysis for the actual read.
const P=D.pace, fit80=D.apps.filter(a=>a.status==='prospect'&&a.fit_score>=80).length;
// Rolling 7 days against the 7 before it. Signed, because the direction is the point.
const sgn=n=>(n>0?'+':n<0?'\u2212':'\u00b1')+Math.abs(n);
const dchip=(n,unit)=>`<span class="dl ${n>0?'up':n<0?'down':'flat'}">${sgn(n)} ${esc(unit)}</span>`;

const HERO=[
  ['lead', T.rate+'%', 'Advance rate', `${T.positive} of ${T.submitted} \u00b7 ${T.live_interview} live interview${T.live_interview===1?'':'s'}`,
   'Applications that got past an automated acknowledgement \u2014 recruiter contact, assessment, interview or offer \u2014 divided by all applications ever submitted. Prospects are excluded.',
   dchip(P.delta.advanced,'advanced vs prior 7d')],
  ['', T.live, 'Active', `${T.dormant} dormant \u00b7 ${T.rejected} rejected`,
   `Submitted, not rejected, and with some activity in the last ${D.stale_days} days. Applications quiet longer than that are counted as dormant instead.`,
   dchip(P.delta.replies,'replies vs prior 7d')],
  ['', P.cur.sent, 'Sent, last 7 days', `${P.cur.replies} replies back`,
   `Applications submitted in the last seven days, against the seven before it (${P.prev.sent} sent, ${P.prev.replies} replies). Rolling windows, not calendar weeks: the current week is partial six days out of seven and would always look like a collapse. Your average over the last four completed weeks is ${P.avg4} per week.`,
   dchip(P.delta.sent,'vs prior 7d')],
  ['', T.prospects, 'Prospects', `${fit80} scored 80+`,
   'Roles found by the board scan and scored against your profile that you have not applied to. Never counted as applications.',
   ''],
];
const HERO_F=[
  {key:'advance',label:'Advanced',fn:a=>['in_process','assessment','interview','offer'].includes(a.status)},
  {key:'active',label:'Active',fn:a=>a.activity==='active'},
  {key:'week',label:'Active last 7d',fn:a=>a.days_quiet!=null&&a.days_quiet<=7},
  {key:'prospect',label:'Prospects',fn:a=>a.status==='prospect'},
];
document.getElementById('hero').innerHTML = HERO.map(([c,n,l,h,,dl],i)=>
  `<div class="tile ${c}" data-h="${i}" role="button" tabindex="0" title="Click to filter">
   <div class="n">${n}</div>
   <div class="l">${l}<span class="info" data-d="${i}">i</span></div>
   <div class="h">${h}</div>${dl||''}
</div>`).join('');

const CHARTS=[];
const cw=id=>{const h=document.getElementById(id);
  return h?Math.max(300,Math.round(h.clientWidth)):460;};
function drawAll(){CHARTS.forEach(f=>{try{f()}catch(e){}});}
let rz; addEventListener('resize',()=>{clearTimeout(rz);rz=setTimeout(drawAll,140);});

// ---------- tooltip ----------
const tip=document.getElementById('tip');
function bind(el,txt){
  el.classList.add('hit');
  el.addEventListener('mousemove',e=>{tip.textContent=txt;tip.style.opacity=1;
    tip.style.left=Math.min(e.clientX+12,innerWidth-tip.offsetWidth-8)+'px';tip.style.top=(e.clientY-32)+'px';});
  el.addEventListener('mouseleave',()=>tip.style.opacity=0);
}
const SVG='http://www.w3.org/2000/svg';
const mk=(t,a={})=>{const e=document.createElementNS(SVG,t);for(const k in a)e.setAttribute(k,a[k]);return e;};

document.querySelectorAll('.info').forEach(el=>bind(el, HERO[+el.dataset.d][4]));

// ---------- filter engine ----------
const Fs = {q:'', status:'live', klass:'', fit:'', act:'', ref:'', age:'', date:'', dmode:'event',
            chart:null, hero:null};
const el = id => document.getElementById(id);

// role classes, from the data
const classes=[...new Set(D.apps.map(a=>a.klass))].sort();
el('kl').innerHTML='<option value="">All role classes</option>'+
  classes.map(c=>`<option value="${c}">${c}</option>`).join('');

function match(a){
  if(Fs.q && !(a.company+' '+a.role).toLowerCase().includes(Fs.q)) return false;
  if(Fs.chart && !Fs.chart.fn(a)) return false;
  if(Fs.hero!==null && !HERO_F[Fs.hero].fn(a)) return false;   // 0 is a valid index
  if(Fs.klass && a.klass!==Fs.klass) return false;
  if(Fs.ref!=='' && String(a.referral?1:0)!==Fs.ref) return false;
  if(Fs.fit){
    if(Fs.fit==='none'){ if(a.fit_score!=null) return false; }
    else if(Fs.fit==='u50'){ if(a.fit_score==null||a.fit_score>=50) return false; }
    else if(a.fit_score==null||a.fit_score<+Fs.fit) return false;
  }
  if(Fs.age){
    if(Fs.age==='none'){ if(a.posted_age!=null) return false; }
    else if(Fs.age==='o45'){ if(a.posted_age==null||a.posted_age<=45) return false; }
    else if(a.posted_age==null||a.posted_age>+Fs.age) return false;
  }
  if(Fs.act){
    if(Fs.act==='q21'){ if(a.days_quiet==null||a.days_quiet<21) return false; }
    else if(a.days_quiet==null||a.days_quiet>+Fs.act) return false;
  }
  if(Fs.date){
    const hit = Fs.dmode==='applied'
      ? (a.applied_on||'').slice(0,10)===Fs.date
      : (a.event_dates||[]).includes(Fs.date);
    if(!hit) return false;
  }
  if(!Fs.chart && Fs.hero===null){
    if(Fs.status==='live') return a.activity==='active';
    if(Fs.status==='dormant') return a.activity==='dormant';
    if(Fs.status!=='all' && a.status!==Fs.status) return false;
  }
  return true;
}

function chips(){
  const c=[];
  if(Fs.hero!=null) c.push(['hero',HERO_F[Fs.hero].label]);
  if(Fs.chart) c.push(['chart',Fs.chart.label]);
  if(Fs.klass) c.push(['klass',Fs.klass]);
  if(Fs.fit) c.push(['fit', Fs.fit==='none'?'Unscored':Fs.fit==='u50'?'Fit under 50':'Fit '+Fs.fit+'+']);
  if(Fs.age) c.push(['age', Fs.age==='none'?'No posting date':Fs.age==='o45'?'Posted 45d+':'Posted last '+Fs.age+'d']);
  if(Fs.act) c.push(['act', Fs.act==='q21'?'Quiet 21d+':'Active last '+Fs.act+'d']);
  if(Fs.ref!=='') c.push(['ref', Fs.ref==='1'?'Referred':'Cold']);
  if(Fs.date) c.push(['date', (Fs.dmode==='applied'?'Applied ':'Activity ')+Fs.date]);
  if(Fs.q) c.push(['q','"'+Fs.q+'"']);
  const html = c.length
    ? c.map(([k,t])=>`<span class="chip"><b>${esc(t)}</b><button data-x="${k}" title="Remove">&times;</button></span>`).join('')
      + `<span class="chip" style="border-style:dashed"><b>Reset all</b><button data-x="all">&times;</button></span>`
    : '';
  el('chips').innerHTML = html;
  // The charts sit far above the table. Rather than dragging the page down on every
  // click, the sticky bar reports what the click did and offers the jump once.
  el('stickychips').innerHTML = c.length
    ? c.map(([k,t])=>`<span class="chip"><b>${esc(t)}</b><button data-x="${k}" title="Remove">&times;</button></span>`).join('')
    : '';
  el('sticky').classList.toggle('filtered', c.length>0);
  writeHash();
}
function clearAll(){
  Object.assign(Fs,{q:'',status:'live',klass:'',fit:'',act:'',ref:'',age:'',date:'',chart:null,hero:null});
  el('q').value='';el('f').value='live';el('kl').value='';el('fit').value='';
  el('act').value='';el('ref').value='';el('age').value='';el('dt').value='';el('dtm').style.display='none';
  view();
}
function onChip(e){
  const k=e.target.dataset.x; if(!k) return;
  if(k==='all'){clearAll();return;}
  else if(k==='hero')Fs.hero=null;
  else if(k==='chart')Fs.chart=null;
  else if(k==='q'){Fs.q='';el('q').value='';}
  else if(k==='klass'){Fs.klass='';el('kl').value='';}
  else if(k==='fit'){Fs.fit='';el('fit').value='';}
  else if(k==='act'){Fs.act='';el('act').value='';}
  else if(k==='age'){Fs.age='';el('age').value='';}
  else if(k==='ref'){Fs.ref='';el('ref').value='';}
  else if(k==='date'){Fs.date='';el('dt').value='';el('dtm').style.display='none';}
  view();
}
el('chips').addEventListener('click',onChip);
el('stickychips').addEventListener('click',onChip);
el('sk-clr').addEventListener('click',clearAll);
// Deep links: the filter state lives in the URL, so a view can be bookmarked or sent.
let hashLock=false;
function writeHash(){
  if(hashLock) return;
  const q={};
  if(Fs.status!=='live')q.s=Fs.status; if(Fs.klass)q.k=Fs.klass; if(Fs.fit)q.fit=Fs.fit;
  if(Fs.act)q.a=Fs.act; if(Fs.age)q.g=Fs.age; if(Fs.ref!=='')q.r=Fs.ref; if(Fs.q)q.q=Fs.q;
  if(Fs.date){q.d=Fs.date;q.dm=Fs.dmode;} if(Fs.hero!=null)q.h=Fs.hero;
  const str=Object.entries(q).map(([k,v])=>k+'='+encodeURIComponent(v)).join('&');
  history.replaceState(null,'',str?'#'+str:location.pathname+location.search);
}
function readHash(){
  const h=location.hash.slice(1); if(!h||h.startsWith('s-'))return false;
  const q=Object.fromEntries(h.split('&').map(x=>{const [k,...v]=x.split('=');
    return [k,decodeURIComponent(v.join('='))];}));
  hashLock=true;
  if(q.s){Fs.status=q.s;el('f').value=q.s;} if(q.k){Fs.klass=q.k;el('kl').value=q.k;}
  if(q.fit){Fs.fit=q.fit;el('fit').value=q.fit;} if(q.a){Fs.act=q.a;el('act').value=q.a;} if(q.g){Fs.age=q.g;el('age').value=q.g;}
  if(q.r!==undefined){Fs.ref=q.r;el('ref').value=q.r;} if(q.q){Fs.q=q.q;el('q').value=q.q;}
  if(q.d){Fs.date=q.d;el('dt').value=q.d;Fs.dmode=q.dm||'event';el('dtm').style.display='';}
  if(q.h!==undefined)Fs.hero=+q.h;
  hashLock=false; return true;
}

// Panels fold to their heading. Collapsed is still on the page and still one click away,
// which a tab is not. State persists so the layout stays how it was left.
(function(){
  let shut=[]; try{shut=JSON.parse(localStorage.getItem('co.shut')||'[]')}catch(e){}
  document.querySelectorAll('#s-analysis .panel').forEach(pn=>{
    const key=pn.querySelector('h3').textContent;
    const b=document.createElement('button');
    b.className='fold'; b.type='button';
    const set=on=>{pn.classList.toggle('shut',on);b.textContent=on?'+':'\u2212';
      b.setAttribute('aria-label',(on?'Expand ':'Collapse ')+key);b.setAttribute('aria-expanded',String(!on));};
    const DEFAULT_SHUT=['By company'];
    set(shut.length?shut.includes(key):DEFAULT_SHUT.includes(key));
    b.addEventListener('click',()=>{
      const opening=pn.classList.contains('shut');
      set(!opening);
      if(opening) drawAll();          // it had no width to measure while folded
      const now=[...document.querySelectorAll('#s-analysis .panel.shut h3')].map(h=>h.textContent);
      try{localStorage.setItem('co.shut',JSON.stringify(now))}catch(e){}});
    pn.appendChild(b);
  });
})();

// Scroll spy on the jump rail, so the rail always says where you are.
(function(){
  const links=[...document.querySelectorAll('.jump a')];
  const secs=links.map(a=>document.querySelector(a.getAttribute('href')));
  const mark=()=>{let i=0;secs.forEach((sec,n)=>{if(sec&&sec.getBoundingClientRect().top<=90)i=n;});
    links.forEach((a,n)=>a.classList.toggle('here',n===i));};
  addEventListener('scroll',mark,{passive:true});
  addEventListener('hashchange',()=>setTimeout(mark,60)); mark();
})();
el('sk-go').addEventListener('click',()=>
  el('tb').scrollIntoView({behavior:'smooth',block:'center'}));

function setFilter(fn,label){Fs.chart={fn,label};Fs.hero=null;view();}

el('q').oninput=e=>{Fs.q=e.target.value.toLowerCase();view();};
el('f').onchange=e=>{Fs.status=e.target.value;Fs.chart=null;Fs.hero=null;view();};
el('kl').onchange=e=>{Fs.klass=e.target.value;view();};
el('fit').onchange=e=>{Fs.fit=e.target.value;view();};
el('act').onchange=e=>{Fs.act=e.target.value;view();};
el('age').onchange=e=>{Fs.age=e.target.value;view();};
el('ref').onchange=e=>{Fs.ref=e.target.value;view();};
el('dt').onchange=e=>{Fs.date=e.target.value;
  el('dtm').style.display=Fs.date?'':'none';view();};
el('dtm').onchange=e=>{Fs.dmode=e.target.value;view();};
document.getElementById('hero').addEventListener('click',e=>{
  const t=e.target.closest('.tile'); if(!t) return;
  if(e.target.classList.contains('info')) return;
  const i=+t.dataset.h; Fs.hero = (Fs.hero===i?null:i); Fs.chart=null; view();});
document.getElementById('hero').addEventListener('keydown',e=>{
  if(e.key==='Enter'||e.key===' '){e.preventDefault();e.target.click();}});

// ---------- funnel infographic ----------
// A real funnel: trapezoids narrowing left to right, so the collapse after
// acknowledgement is a shape rather than four numbers to compare. Thin stages keep a
// minimum band and put their label above the silhouette, since 16 and 9 against 338
// are only a few pixels tall and would otherwise be unlabelled slivers.
CHARTS.push(function(){
  const host=document.getElementById('c-funnel'); if(!host) return;
  host.innerHTML='';
  const M={submitted:a=>a.status!=='prospect',
           acked:a=>a.status!=='prospect'&&a.status!=='applied',
           positive:a=>a.ever_advanced,
           interview:a=>a.ever_interviewed};
  const d=D.funnel, W=cw('c-funnel'), H=124, pad={t:20,b:26};
  const base=d[0].n||1, band=H-pad.t-pad.b, cy=pad.t+band/2, segW=W/d.length;
  const ramp=['--seq4','--seq3','--seq2','--seq5'];
  const hOf=n=>Math.max(5, n/base*band);
  const s2=mk('svg',{viewBox:`0 0 ${W} ${H}`,role:'img'});
  d.forEach((x,i)=>{
    const x0=i*segW, x1=x0+segW;
    const h0=hOf(x.n), h1=hOf(i+1<d.length?d[i+1].n:x.n);
    const pts=`${x0},${cy-h0/2} ${x1},${cy-h1/2} ${x1},${cy+h1/2} ${x0},${cy+h0/2}`;
    const go=()=>setFilter(M[x.k],x.label);
    // full-height hit target first, so a 5px band is still an easy click
    const hit=mk('rect',{x:x0,y:0,width:segW,height:H,fill:'transparent',class:'hit'});
    bind(hit,`${x.label}: ${x.n} of ${base} submitted (${Math.round(x.n/base*100)}%)`);
    hit.addEventListener('click',go); s2.appendChild(hit);
    const poly=mk('polygon',{points:pts,fill:cv(ramp[i]),class:'hit'});
    bind(poly,`${x.label}: ${x.n} of ${base} submitted (${Math.round(x.n/base*100)}%)`);
    poly.addEventListener('click',go); s2.appendChild(poly);
    if(i){s2.appendChild(mk('line',{x1:x0,x2:x0,y1:cy-h0/2,y2:cy+h0/2,
      stroke:cv('--panel'),'stroke-width':2}));}
    const n=mk('text',{x:x0+segW/2,y:cy-h0/2-6,class:'vlab','text-anchor':'middle'});
    n.textContent=x.n; s2.appendChild(n);
    const l=mk('text',{x:x0+segW/2,y:H-12,class:'axis','text-anchor':'middle'});
    l.textContent=x.label; s2.appendChild(l);
    const pc=mk('text',{x:x0+segW/2,y:H-2,class:'axis','text-anchor':'middle'});
    pc.setAttribute('fill',cv('--faint'));
    pc.textContent=i?`${Math.round(x.n/base*100)}%`:'100%'; s2.appendChild(pc);
  });
  host.appendChild(s2);
});

// ---------- aging: status palette + icon + label ----------
CHARTS.push(function(){
  const host=document.getElementById('c-aging'); host.innerHTML='';
  const d=D.aging,W=cw('c-aging'),rowH=27,H=d.length*rowH+8,max=Math.max(...d.map(x=>x.n),1),labW=96,barW=W-labW-46;
  const s=mk('svg',{viewBox:`0 0 ${W} ${H}`,role:'img'});
  d.forEach((x,i)=>{
    const y=i*rowH+4,w=Math.max(3,x.n/max*barW),col=cv('--'+x.role);
    const go=()=>setFilter(a=>!['prospect','rejected','withdrawn'].includes(a.status)
      && a.days_quiet>=x.lo && a.days_quiet<=x.hi, x.label+' quiet');
    const tip=`${x.label}: ${x.n} live application${x.n===1?'':'s'} \u00b7 click to filter`;
    const lb=mk('text',{x:0,y:y+16,class:'slab hit'});lb.textContent=`${x.icon} ${x.label}`;
    lb.setAttribute('fill',col);bind(lb,tip);lb.addEventListener('click',go);s.appendChild(lb);
    const bg=mk('rect',{x:labW,y:y+3,width:barW,height:14,fill:cv('--grid'),class:'hit'});
    bind(bg,tip);bg.addEventListener('click',go);s.appendChild(bg);
    const r=mk('rect',{x:labW,y:y+3,width:w,height:14,rx:2,fill:col,class:'hit'});
    bind(r,tip);r.addEventListener('click',go);
    s.appendChild(r);
    const t=mk('text',{x:labW+barW+8,y:y+17,class:'vlab'});t.textContent=x.n;s.appendChild(t);
  });
  document.getElementById('c-aging').appendChild(s);
});

// ---------- weekly: grouped bars, 2 series + legend ----------
CHARTS.push(function(){
  const d=D.weekly,W=cw('c-weekly'),H=150,pad={l:24,r:6,t:8,b:22};
  const max=Math.max(...d.map(x=>Math.max(x.sub,x.rep)),1);
  const iw=(W-pad.l-pad.r)/d.length, bw=(iw-6)/2;
  document.getElementById('c-weekly').innerHTML=
    `<div class="legend"><span><i style="background:${cv('--s1')}"></i>Sent</span>
     <span><i style="background:${cv('--s2')}"></i>Replies</span></div>`;
  const s=mk('svg',{viewBox:`0 0 ${W} ${H}`,role:'img'});
  [0,.5,1].forEach(f=>{const y=pad.t+(1-f)*(H-pad.t-pad.b);
    s.appendChild(mk('line',{x1:pad.l,x2:W-pad.r,y1:y,y2:y,stroke:cv('--grid'),'stroke-width':1}));
    const t=mk('text',{x:0,y:y+3,class:'axis'});t.textContent=Math.round(max*f);s.appendChild(t);});
  d.forEach((x,i)=>{
    const x0=pad.l+i*iw+2, base=H-pad.b;
    const lo0=x.w, hi0=new Date(Date.parse(x.w)+7*864e5).toISOString().slice(0,10);
    const col=mk('rect',{x:pad.l+i*iw,y:pad.t,width:iw,height:base-pad.t,fill:'transparent',class:'hit'});
    bind(col,`Week of ${x.label}: ${x.sub} sent, ${x.rep} replies \u00b7 click to filter`);
    col.addEventListener('click',()=>setFilter(
      a=>(a.submitted_on&&a.submitted_on>=lo0&&a.submitted_on<hi0)
         ||(a.event_dates||[]).some(d0=>d0>=lo0&&d0<hi0), `Week of ${x.label}`));
    s.appendChild(col);
    [['sub','--s1',0],['rep','--s2',bw+2]].forEach(([k,c,off])=>{
      const h=x[k]/max*(H-pad.t-pad.b);
      if(x[k]>0){const r=mk('rect',{x:x0+off,y:base-h,width:bw,height:h,rx:2,fill:cv(c),class:'hit'});
        bind(r,`${x.label}: ${x[k]} ${k==='sub'?'sent':'replies'} \u00b7 click to filter`);
        // week bounds, so 'sent' filters by submission date and 'replies' by any event date
        const lo=x.w, hi=new Date(Date.parse(x.w)+7*864e5).toISOString().slice(0,10);
        r.addEventListener('click',()=>setFilter(
          k==='sub' ? a=>a.submitted_on&&a.submitted_on>=lo&&a.submitted_on<hi
                    : a=>(a.event_dates||[]).some(d0=>d0>=lo&&d0<hi),
          `${k==='sub'?'Sent':'Replies'} week of ${x.label}`));
        s.appendChild(r);}
    });
    if(i%2===0){const t=mk('text',{x:x0+iw/2-2,y:H-9,class:'axis','text-anchor':'middle'});
      t.textContent=x.label;s.appendChild(t);}
  });
  document.getElementById('c-weekly').appendChild(s);
});

// ---------- by company ----------
// The bar is CURRENT state, stacked: active, dormant, closed. Where a thread is still
// alive is the actionable question; "ever advanced" is history, so it rides in the label
// where it informs without dominating.
CHARTS.push(function(){
  const host=document.getElementById('c-co');
  const d=D.companies;
  if(!d.length){host.innerHTML='<div class="muted small">Not enough repeat companies yet.</div>';return;}
  const W=cw('c-co'),rowH=22,H=d.length*rowH+10,max=Math.max(...d.map(x=>x.n),1),labW=150,barW=W-labW-190;
  host.innerHTML=
    `<div class="legend"><span><i style="background:${cv('--good')}"></i>Active</span>
     <span><i style="background:${cv('--seq2')}"></i>Dormant</span>
     <span><i style="background:${cv('--grid')}"></i>Closed</span></div>`;
  const s=mk('svg',{viewBox:`0 0 ${W} ${H}`,role:'img'});
  const SEG=[['active','--good','active'],['dormant','--seq2','dormant'],['closed','--grid','closed']];
  d.forEach((x,i)=>{
    const y=i*rowH+4;
    const nm=mk('text',{x:0,y:y+15,class:'slab hit'});
    nm.textContent=x.company.length>22?x.company.slice(0,21)+'\u2026':x.company;
    bind(nm,`${x.company}: click to filter`);
    nm.addEventListener('click',()=>setFilter(a=>a.company===x.company,x.company));s.appendChild(nm);
    let off=0;
    SEG.forEach(([k,col,label])=>{
      if(!x[k]) return;
      const w=Math.max(2,x[k]/max*barW);
      const r=mk('rect',{x:labW+off,y:y+4,width:Math.max(1,w-1.5),height:15,rx:2,fill:cv(col),class:'hit'});
      bind(r,`${x.company}: ${x[k]} ${label}${x.pos?` \u00b7 ${x.pos} ever advanced`:''}`);
      r.addEventListener('click',()=>{
        const f=k==='active' ? (a=>a.company===x.company&&a.activity==='active'&&!['rejected','withdrawn'].includes(a.status))
              : k==='dormant'? (a=>a.company===x.company&&a.activity==='dormant')
              :                (a=>a.company===x.company&&['rejected','withdrawn'].includes(a.status));
        setFilter(f, `${x.company} ${label}`);});
      s.appendChild(r); off+=w;
    });
    const t=mk('text',{x:labW+barW+12,y:y+16,class:'vlab'});
    t.textContent=`${x.active} active`+(x.pos?` \u00b7 ${x.pos} advanced`:'')+` \u00b7 ${x.n} sent`;
    if(!x.active) t.setAttribute('fill',cv('--faint'));
    s.appendChild(t);
  });
  host.appendChild(s);
});

// ---------- action queue ----------
// One merged decision list: what is live, what to apply to next, then housekeeping.
// A row opens in place. Making it filter the table instead meant clicking a company,
// being sent elsewhere, and clicking the same company again to see anything.
const acts=[];
const push=(o)=>acts.push(o);
const inPlay=D.apps.filter(a=>['interview','offer'].includes(a.status)&&a.activity==='active')
  .sort((a,b)=>(b.last_event||'').localeCompare(a.last_event||''));
if(inPlay.length) push({group:'In play', note:'Live conversations. Keep these moving.'});
inPlay.forEach(a=>push({a,c:'good',co:a.company,ro:a.role,val:a.days_quiet,unit:'d'}));
// Posting age is the primary driver, not fit. A hiring manager screening the first 200
// of 6,000 applicants means a 145-day-old req is closed in practice however good the
// match is. Band by age, rank by fit inside the band, and never let a stale 85 outrank
// a fresh 78.
const AGE_BAND=a=>{const d=a.posted_age;
  return d==null?3 : d<=7?0 : d<=21?1 : d<=45?2 : 3;};
const BAND_LABEL=['posted this week','posted 1-3 weeks ago','posted 3-6 weeks ago','stale or unknown'];
const nextUp=D.apps.filter(a=>a.status==='prospect'&&a.fit_score>=D.act_score)
  .sort((a,b)=>AGE_BAND(a)-AGE_BAND(b) || b.fit_score-a.fit_score).slice(0,8);
(function(){const c=document.getElementById('dncap'); if(c) c.textContent=
  `Best ${nextUp.length} of ${T.prospects} prospects, ranked by fit. Open a row for the reasoning, your record there, and sentiment.`;})();
if(nextUp.length) push({group:'Apply next'});
let lastBand=-1;
nextUp.forEach(a=>{
  const b=AGE_BAND(a);
  if(b!==lastBand){push({group:BAND_LABEL[b]}); lastBand=b;}
  push({a,c:'',co:a.company,ro:a.role,val:a.fit_score,fit:true,
        age:a.posted_age==null?null:a.posted_age});});
const stale=D.apps.filter(a=>a.activity==='dormant');
if(stale.length||T.review) push({group:'Housekeeping'});
if(stale.length)push({c:'quiet',co:'Dormant',ro:`silent ${D.stale_days}+ days`,val:stale.length,
  filter:{fn:x=>x.activity==='dormant', label:'Dormant'}});
if(T.review)push({c:'quiet',co:'Review queue',ro:'run: careerops review',val:T.review});

const fitClass=v=>v>=80?'f-hi':v>=75?'f-mid':v>=70?'f-lo':'f-min';
document.getElementById('actions').innerHTML = acts.map((o,i)=>{
  if(o.group) return `<div class="act-g"><span>${esc(o.group)}</span></div>`;
  const val=o.val!=null?`<b class="act-v ${o.fit?fitClass(o.val):''}">${esc(o.val)}${esc(o.unit||'')}</b>`:'';
  return `<div class="act-w"><div class="act ${o.c} hit" data-i="${i}" tabindex="0" role="button" aria-expanded="false"`
    +` title="${esc(o.co)} \u2014 ${esc(o.ro)}">`
    +`<div class="bar"></div><div class="act-t"><b>${esc(o.co)}</b> <span class="ro">${esc(o.ro)}</span>`
    +(o.age!=null?` <span class="age ${o.age<=7?'fresh':o.age<=21?'ok':'old'}">${o.age}d</span>`:'')+`</div>`
    +val+`<span class="act-x">\u203a</span></div><div class="act-d" hidden></div></div>`;
}).join('') || '<div class="act good"><div class="bar"></div><div class="act-t">Nothing needs attention.</div></div>';

document.getElementById('actions').addEventListener('click',e=>{
  const row=e.target.closest('.act[data-i]'); if(!row) return;
  const o=acts[+row.dataset.i], body=row.nextElementSibling;
  if(!body.dataset.built){
    body.innerHTML = o.a
      ? fitHTML(o.a)+trackHTML(o.a.company)+sentimentHTML(o.a.company)
        +`<div class="dd-act">${o.a.url?`<a href="${esc(o.a.url)}" target="_blank" rel="noopener">Open posting</a>`:''}`
        +`<button type="button" data-co="${esc(o.a.company)}">Show ${esc(o.a.company)} in the table</button>`
        +(o.a.status==='prospect'
           ? `<button type="button" class="cp" data-cmd="careerops apply ${o.a.id}">Applied? copy <code>careerops apply ${o.a.id}</code></button>`
           : '')
        +`</div>`
      : `<div class="dd-act"><button type="button" data-flt="${o.filter?1:0}">Show these in the table</button></div>`;
    body.dataset.built='1';
  }
  const open=body.hidden; body.hidden=!open; row.setAttribute('aria-expanded',String(open));
  row.classList.toggle('open',open);
});
document.getElementById('actions').addEventListener('click',e=>{
  const b=e.target.closest('.dd-act button'); if(!b) return;
  e.stopPropagation();
  const row=b.closest('.act-w').querySelector('.act'); const o=acts[+row.dataset.i];
  if(b.dataset.cmd){
    const done=()=>{const o=b.innerHTML; b.innerHTML='copied, run it in your terminal';
      setTimeout(()=>b.innerHTML=o,2200);};
    if(navigator.clipboard) navigator.clipboard.writeText(b.dataset.cmd).then(done,done);
    else done();
    return;
  }
  if(b.dataset.co) setFilter(a=>a.company===b.dataset.co, b.dataset.co);
  else if(o.filter) setFilter(o.filter.fn,o.filter.label);
  el('tb').scrollIntoView({behavior:'smooth',block:'center'});
});
document.getElementById('actions').addEventListener('keydown',e=>{
  if((e.key==='Enter'||e.key===' ')&&e.target.dataset.i!=null){e.preventDefault();e.target.click();}});

(function(){const e=document.getElementById('wkpace'); if(!e)return;
  const p=D.pace, d=p.delta.sent;
  e.innerHTML=` <b>Last 7 days: ${p.cur.sent} sent, ${p.cur.replies} replies</b> `
    +`(prior 7: ${p.prev.sent} and ${p.prev.replies}). `
    +`Four-week average ${p.avg4} per week.`;})();

// ---------- recent activity ----------
(function(){
  const host=document.getElementById('c-recent'); if(!host) return;
  const rows=D.recent||[];
  if(!rows.length){host.innerHTML='<div class="muted small">Nothing yet. Run sync, or record a submission with <code>careerops apply &lt;id&gt;</code>.</div>';return;}
  const LABEL={submitted:'applied', ack:'acknowledged', recruiter_outreach:'recruiter',
               assessment:'assessment', interview_invite:'interview', offer:'offer',
               rejection:'rejected'};
  const HUE={submitted:'accent', ack:'faint', recruiter_outreach:'seq3', assessment:'seq3',
             interview_invite:'good', offer:'good', rejection:'critical'};
  const today=new Date(); today.setHours(0,0,0,0);
  const ago=d=>{const t=new Date(d.slice(0,10)+'T00:00:00');
    const n=Math.round((today-t)/864e5);
    return n<=0?'today':n===1?'1d':n+'d';};
  host.innerHTML=rows.map(r=>`<div class="rc hit" data-id="${r.app_id}"
      title="${esc(r.company)} \u2014 ${esc(r.role)}${r.source&&r.source!=='gmail'?` \u00b7 recorded from ${esc(r.source)}, so the date is when it was logged`:``}">
    <span class="rc-d">${esc(ago(r.occurred_at))}</span>
    <span class="rc-t"><b>${esc(r.company)}</b> <span class="ro">${esc(r.role)}</span></span>
    <span class="rc-e" style="color:var(--${HUE[r.type]||'muted'})">${esc(LABEL[r.type]||r.type)}${
      r.source&&r.source!=='gmail'?`<span class="rc-s">${esc(r.source)}</span>`:''}</span>
  </div>`).join('');
  host.addEventListener('click',e=>{
    const row=e.target.closest('.rc'); if(!row) return;
    const id=+row.dataset.id, a=D.apps.find(x=>x.id===id);
    setFilter(x=>x.id===id, a?`${a.company}: ${a.role}`:'record '+id);
    el('tb').scrollIntoView({behavior:'smooth',block:'center'});});
})();

// ---------- drill-downs ----------
// Sentiment has no section of its own. It is wanted in exactly two moments: deciding
// where to apply, and reviewing somewhere already applied. Both are rows, so it lives as
// a drill-down on the row it describes rather than a list you have to cross-reference.
const RATE_HUE=v=>v>=4.2?'good':v>=3.7?'seq3':v>=3.2?'warning':'serious';

function sentimentHTML(co){
  const x=(D.intel||{})[co]; if(!x) return '';
  const r=x.rating, hue=RATE_HUE(r||0);
  const head=(r?r.toFixed(1)+'/5':'no rating')+(x.wlb!=null?` \u00b7 wlb ${x.wlb.toFixed(1)}`:'')
    +(x.n?` \u00b7 ${x.n.toLocaleString()} reviews`:'')+(x.rec!=null?` \u00b7 ${x.rec}% recommend`:'');
  return `<details class="dd"><summary><span class="dd-k">Employee sentiment</span>`
    +`<span class="dd-v" style="color:var(--${hue})">${esc(head)}</span></summary>`
    +`<div class="dd-b"><div class="int-cols">`
    +`<div>${(x.pros||[]).map(t=>`<p class="int-li int-pro">${esc(t)}</p>`).join('')||'<p class="int-li muted">none captured</p>'}</div>`
    +`<div>${(x.cons||[]).map(t=>`<p class="int-li int-con">${esc(t)}</p>`).join('')||'<p class="int-li muted">none captured</p>'}</div></div>`
    +(x.wlb_summary?`<p class="int-li" style="padding-left:0;margin-top:8px"><b>Work/life balance.</b> ${esc(x.wlb_summary)}</p>`:'')
    +`<div class="int-ent">Reference only, never moves a fit score. Entity match: ${esc(x.conf)} \u00b7 fetched ${esc(x.fetched)}<br>${esc(x.entity||'')}</div>`
    +`<div class="int-src">${(x.sources||[]).slice(0,3).map(sr=>`<a href="${esc(sr.url)}" target="_blank" rel="noopener">source</a>`).join('')}</div>`
    +`</div></details>`;
}

// A suggestion to apply somewhere means more beside what happened the last thirteen times.
function trackHTML(co){
  const mine=D.apps.filter(a=>a.company===co&&a.status!=='prospect');
  if(!mine.length) return `<details class="dd"><summary><span class="dd-k">Your track record</span>`
    +`<span class="dd-v muted">no applications yet</span></summary>`
    +`<div class="dd-b"><p class="int-li muted">This would be your first application to ${esc(co)}.</p></div></details>`;
  const adv=mine.filter(a=>['in_process','assessment','interview','offer'].includes(a.status)).length;
  const rej=mine.filter(a=>a.status==='rejected').length;
  const rate=Math.round(adv/mine.length*100);
  const hue=adv>0?'good':(rej===mine.length?'serious':'muted');
  return `<details class="dd"><summary><span class="dd-k">Your track record</span>`
    +`<span class="dd-v" style="color:var(--${hue})">${mine.length} applied \u00b7 ${adv} advanced (${rate}%)</span></summary>`
    +`<div class="dd-b">${mine.slice(0,8).map(a=>`<div class="tr-row">`
        +`<span class="pill s-${a.status}">${esc(a.status)}</span>`
        +`<span class="tr-t">${esc(a.role)}</span>`
        +`<span class="tr-d muted">${esc(a.submitted_on||(a.applied_on||'').slice(0,10)||'')}</span></div>`).join('')}`
    +(mine.length>8?`<p class="int-li muted">+${mine.length-8} more in the table below</p>`:'')
    +`<div class="int-ent">${adv} of ${mine.length} got past an acknowledgement; ${rej} rejected.</div></div></details>`;
}

function fitHTML(a){
  const f=a.fit; if(!f) return '';
  return `<details class="dd"><summary><span class="dd-k">Why this scored ${a.fit_score}</span>`
    +`<span class="dd-v muted">${esc(f.verdict||'')}</span></summary><div class="dd-b">`
    +(f.one_line?`<p class="int-li" style="padding-left:0"><b>${esc(f.one_line)}</b></p>`:'')
    +(f.preference_flags||[]).map(x=>`<div class="flag">! ${esc(x)}</div>`).join('')
    +`<div class="int-cols"><div>${(f.emphasize||[]).map(x=>`<p class="int-li int-pro">${esc(x)}</p>`).join('')}</div>`
    +`<div>${(f.gaps||[]).map(x=>`<p class="int-li int-con">${esc(x)}</p>`).join('')}</div></div>`
    +(f.level_read?`<div class="int-ent">Level read: ${esc(f.level_read)}</div>`:'')
    +`</div></details>`;
}

// Rating chip beside a company in the pipeline table. Reference only, hence muted.
function sent(co){const x=(D.intel||{})[co];
  if(!x||x.rating==null)return '';
  const c=x.rating>=4.2?'good':x.rating>=3.7?'seq3':x.rating>=3.2?'warning':'serious';
  return ` <span class="pill attempt" style="color:var(--${c})" data-t="Public rating ${x.rating.toFixed(1)}/5${x.wlb!=null?', work/life balance '+x.wlb.toFixed(1)+'/5':''}${x.n?', '+x.n.toLocaleString()+' reviews':''}. Reference only.">${x.rating.toFixed(1)}\u2605</span>`;}

let sortK='days_quiet',sortAsc=true;
const tb=document.getElementById('tb');
function view(){
  let rows=D.apps.filter(match);
  rows.sort((a,b)=>{let x=a[sortK],y=b[sortK];
    x=x??(typeof y==='number'?-1:'');y=y??(typeof x==='number'?-1:'');
    return (x<y?-1:x>y?1:0)*(sortAsc?1:-1);});
  document.querySelectorAll('.tile').forEach(t=>t.classList.toggle('on',+t.dataset.h===Fs.hero));
  chips();
  const lab = rows.length + (rows.length===1?' record':' records');
  el('count').textContent = lab;
  el('stickycount').textContent = lab;
  tb.innerHTML=rows.map(a=>{
    const q1=a.days_quiet==null?'':`<span class="${a.days_quiet>=D.stale_days?'q-stale':a.days_quiet<=7?'q-hot':''}">${a.days_quiet}d</span>`;
    return `<tr class="r" data-id="${a.id}">
      <td><b>${esc(a.company)}</b>${a.referral?' <span class="pill">ref</span>':''}${sent(a.company)}</td>
      <td>${esc(a.role)}${a.attempt?` <span class="pill attempt" data-t="${esc(a.attempt_note)}">try ${a.attempt}</span>`:''}<div class="muted small">${esc(a.klass)}${a.location?' · '+esc(a.location):''}</div></td>
      <td><span class="pill s-${a.status}">${a.status}</span>${a.activity==='dormant'?' <span class="pill s-prospect">dormant</span>':''}</td>
      <td class="num">${a.fit_score??''}</td>
      <td class="num">${a.posted_age==null?'<span class="muted">&mdash;</span>'
        :`<span class="age ${a.posted_age<=7?'fresh':a.posted_age<=21?'ok':'old'}">${a.posted_age}d</span>`}</td>
      <td class="num muted">${a.submitted_on||(a.applied_on||'').slice(0,10)}</td>
      <td class="num">${q1}</td></tr>`;}).join('')
    ||'<tr><td colspan="7" class="muted">No records match these filters.</td></tr>';
}
tb.addEventListener('mouseover',e=>{
  const el=e.target.closest('.attempt'); if(!el) return;
  tip.textContent=el.dataset.t; tip.style.opacity=1;
  tip.style.left=Math.min(e.clientX+12,innerWidth-tip.offsetWidth-8)+'px';
  tip.style.top=(e.clientY-38)+'px';});
tb.addEventListener('mouseout',e=>{ if(e.target.closest('.attempt')) tip.style.opacity=0;});
tb.addEventListener('click',e=>{
  const tr=e.target.closest('tr.r');if(!tr)return;
  const nx=tr.nextElementSibling;if(nx&&nx.classList.contains('det')){nx.remove();return;}
  const a=D.apps.find(x=>x.id==tr.dataset.id);let h='';
  if(a.url)h+=`<div class="small"><a href="${esc(a.url)}" target="_blank" rel="noopener">Open posting</a></div>`;
  if(a.comp_min)h+=`<div class="small muted">Posted comp: $${a.comp_min.toLocaleString()}–$${a.comp_max.toLocaleString()}</div>`;
  h+=fitHTML(a)+trackHTML(a.company)+sentimentHTML(a.company);
  h+=`<div class="small" style="margin-top:10px"><b>Event history (${a.events.length})</b></div>`;
  h+=a.events.length?a.events.map(e=>`<div class="ev">
      <div class="h">${(e.occurred_at||'').slice(0,10)} · ${esc(e.type)} <span class="muted">(${e.source}, conf ${e.confidence})</span></div>
      <div class="m">${esc(e.subject||'')}</div><div class="m">${esc(e.raw||'')}</div></div>`).join('')
    :'<div class="muted small">No events recorded.</div>';
  const row=document.createElement('tr');row.className='det';
  row.innerHTML=`<td colspan="7">${h}</td>`;tr.after(row);});
document.querySelectorAll('th[data-k]').forEach(th=>th.onclick=()=>{
  const k=th.dataset.k;sortAsc=(k===sortK)?!sortAsc:(k==='days_quiet'||k==='company');sortK=k;view();});
drawAll();             // charts size themselves to their panels
readHash();            // a shared link opens on the filter it encodes
view();
addEventListener('hashchange',()=>{if(readHash())view();});
</script>"""
