"""Local companion server, so the dashboard can do things instead of only showing them.

The dashboard is a static file. It can render the whole pipeline but cannot run
anything, so every action has been "copy this command and go paste it". Generating a
tailored resume is the one action worth a button: it is slow, it is per-role, and the
command is easy to run against the wrong application id.

Deliberately small and deliberately local. It binds 127.0.0.1 only, exposes exactly one
verb, and takes an application id rather than a path or a template name, so the worst a
bad request can do is build a resume for the wrong role.

The published artifact cannot reach this: a page served over https may not call
http://127.0.0.1 (mixed content), and the browser blocks it before the request leaves.
That is a browser rule, not something to work around, so the dashboard falls back to
copying the command when the server is not reachable. The button does real work on the
local dashboard.html and degrades to what it always did everywhere else.
"""
import json, pathlib, re, threading, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from .db import DEFAULT_DB

from . import db

DEFAULT_PORT = 8765
_LOCK = threading.Lock()          # Word drives one document at a time


def _slug(s: str, n: int = 28) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "", (s or "").title())
    return s[:n] or "Role"


def out_path(conn, app_id: int, resume_dir: str) -> "tuple":
    row = conn.execute("""SELECT co.name company, r.title FROM applications a
                          JOIN roles r ON r.id = a.role_id
                          JOIN companies co ON co.id = r.company_id
                          WHERE a.id = ?""", (app_id,)).fetchone()
    if not row:
        return None, None, None
    d = pathlib.Path(resume_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    name = f"Sunter_{_slug(row['company'], 20)}_{_slug(row['title'])}.docx"
    return str(d / name), row["company"], row["title"]


def generate(app_id: int, resume_dir: str, db_path=None) -> dict:
    """Build, then shrink until Word confirms one page. Mirrors `careerops resume`."""
    from .resume import build, page_count, MAX_BULLETS
    conn = db.connect(db_path)
    out, company, title = out_path(conn, app_id, resume_dir)
    if not out:
        return {"ok": False, "error": f"no application {app_id}"}
    cap = MAX_BULLETS
    with _LOCK:
        for _ in range(4):
            r = build(db.connect(db_path), app_id, out, cap=cap)
            n = page_count(r["out"], keep=True)
            if n is None:
                return {"ok": True, "docx": r["out"], "pdf": None, "pages": None,
                        "company": company, "title": title,
                        "note": "Word could not be reached, so the page count is unverified"}
            if n == 1:
                return {"ok": True, "docx": r["out"], "pdf": str(pathlib.Path(out).with_suffix(".pdf")),
                        "pages": 1, "company": company, "title": title,
                        "bullets": len(r["bullets"])}
            cap = max(6, len(r["bullets"]) - 1)
    return {"ok": False, "error": "could not reach one page after 4 attempts"}


class Handler(BaseHTTPRequestHandler):
    resume_dir = "~/Desktop/resumes"
    db_path = None

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/ping":
            return self._send(200, {"ok": True, "resume_dir": self.resume_dir})
        if u.path == "/snooze":
            return self._snooze(parse_qs(u.query))
        if u.path != "/resume":
            return self._send(404, {"ok": False, "error": "unknown endpoint"})
        try:
            app_id = int(parse_qs(u.query).get("app", [""])[0])
        except ValueError:
            return self._send(400, {"ok": False, "error": "app must be an integer id"})
        print(f"  resume for application {app_id} ...", flush=True)
        try:
            res = generate(app_id, self.resume_dir, self.db_path)
        except Exception as e:
            traceback.print_exc()
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print(f"    {'ok ' + str(res.get('pdf')) if res.get('ok') else 'failed: ' + str(res.get('error'))}",
              flush=True)
        self._send(200 if res.get("ok") else 500, res)

    def _snooze(self, q):
        """Snooze from the dashboard instead of copying a command into a terminal.

        Same write as `careerops snooze`: a date on the application, nothing else. Days
        default to 30, `clear=1` lifts it. Keeping this beside /resume means the page
        already knows whether the server is up, and the copy-the-command path stays as
        the fallback for when it is not.
        """
        import sqlite3
        from datetime import date, timedelta
        try:
            app_id = int(q.get("app", [""])[0])
        except ValueError:
            return self._send(400, {"ok": False, "error": "app must be an integer id"})
        clear = q.get("clear", ["0"])[0] in ("1", "true", "yes")
        try:
            days = int(q.get("days", ["30"])[0])
        except ValueError:
            days = 30
        until = None if clear else (q.get("until", [""])[0]
                                    or (date.today() + timedelta(days=days)).isoformat())
        reason = (q.get("reason", [""])[0] or None)
        conn = sqlite3.connect(self.db_path or DEFAULT_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""SELECT a.id, c.name co, r.title FROM applications a
                              JOIN roles r ON r.id=a.role_id
                              JOIN companies c ON c.id=r.company_id WHERE a.id=?""",
                           (app_id,)).fetchone()
        if not row:
            return self._send(404, {"ok": False, "error": f"no application {app_id}"})
        conn.execute("UPDATE applications SET snoozed_until=?, snooze_reason=? WHERE id=?",
                     (until, reason, app_id))
        conn.commit()
        print(f"  snooze {app_id} {row['co']} -> {until or 'cleared'}", flush=True)
        return self._send(200, {"ok": True, "id": app_id, "company": row["co"],
                                "role": row["title"], "until": until})

    do_POST = do_GET

    def log_message(self, *a):        # the prints above are the useful log
        pass


def serve(port: int = DEFAULT_PORT, resume_dir: str = "~/Desktop/resumes", db_path=None):
    Handler.resume_dir, Handler.db_path = resume_dir, db_path
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"careerops server on http://127.0.0.1:{port}  ->  {resume_dir}")
    print("open dashboard.html locally and the Do next resume buttons will work.")
    print("ctrl-c to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
