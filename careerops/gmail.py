"""Gmail ingestion. Same event path as the CSV backfill, but with message bodies,
which is what lets the classifier resolve roles the subject line alone can't.

Idempotent: events are keyed on the Gmail message id, so re-running is safe.
"""
import base64, os, pathlib, random, re, time
from typing import Optional

from . import db
from .classify import classify, creates_application

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
ROOT = pathlib.Path(__file__).resolve().parent.parent
def _find(name: str) -> pathlib.Path:
    """Accept the file at the project root or inside the package dir --
    the careerops/careerops nesting is an easy mistake to make."""
    for p in (ROOT / name, ROOT / "careerops" / name):
        if p.exists():
            return p
    return ROOT / name


CREDS = _find("credentials.json")
TOKEN = _find("token.json")

# Narrow enough to stay out of personal mail, broad enough to catch ATS traffic.
THROTTLE = float(os.environ.get("CAREEROPS_GMAIL_THROTTLE", "0.35"))  # seconds between message fetches

DEFAULT_QUERY = (
    # Two arms, either is enough. Subject phrases catch employers who mail direct;
    # the vendor list catches ATS mail whose subject says nothing recognizable.
    # An Anthropic rejection was missed for a year because it failed BOTH arms:
    # its subject read "Anthropic Follow-Up for [Pipeline] Product Manager,
    # Monetization" and it came from gem.com, which was not on the list. Under-fetching
    # is the expensive failure here, because a message never fetched cannot be
    # reclassified later. Over-fetching only costs a trip through the classifier,
    # which already has a blacklist and a review queue.
    '(subject:(application OR applying OR applied OR candidate OR interview OR recruiter '
    'OR "thank you for your interest" OR "follow-up for" OR "follow up for" '
    'OR "your application" OR "moving forward" OR "not moving forward" '
    'OR "your candidacy" OR "your submission" OR "position" OR "opening") '
    'OR from:(greenhouse-mail.io OR greenhouse.io OR myworkday.com OR workday.com '
    'OR ashbyhq.com OR lever.co OR smartrecruiters.com OR icims.com OR jobvite.com '
    'OR workablemail.com OR gem.com OR taleo.net OR avature.net OR breezy.hr '
    'OR teamtailor.com OR recruitee.com OR eightfold.ai OR phenompeople.com '
    'OR successfactors.com OR bamboohr.com OR applytojob.com OR rippling.com '
    'OR ripplingats.com OR dayforcehcm.com OR ultipro.com OR paylocity.com '
    'OR hire.lever.co OR oraclecloud.com OR myworkdayjobs.com)) '
    '-in:chats'
)


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS.exists():
                raise SystemExit(f"Missing {CREDS}. See README (Gmail setup).")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS), SCOPES)
            # Fixed port + explicit URL: auto-launch is unreliable, and a random
            # port makes the loopback redirect hard to diagnose when it fails.
            port = int(os.environ.get("CAREEROPS_OAUTH_PORT", "8765"))
            print("\n>>> OPEN THIS URL IN YOUR BROWSER:\n", flush=True)
            creds = flow.run_local_server(port=port, open_browser=True,
                                          authorization_prompt_message="AUTH_URL: {url}\n",
                                          success_message="Authorized. You can close this tab.")
        TOKEN.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _retry(fn, *, tries: int = 6, base: float = 2.0):
    """Gmail enforces a per-minute quota; 403 rateLimitExceeded and 429 are
    transient. Exponential backoff with jitter, re-raising anything else."""
    from googleapiclient.errors import HttpError
    for attempt in range(tries):
        try:
            return fn()
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            transient = status in (403, 429, 500, 503) and (
                b"ateLimit" in e.content or b"uota" in e.content or status in (500, 503))
            if not transient or attempt == tries - 1:
                raise
            wait = base ** attempt + random.uniform(0, 1)
            print(f"  rate limited; backing off {wait:.1f}s", flush=True)
            time.sleep(wait)
    return None


def _header(payload, name) -> str:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _html_to_text(html: str) -> str:
    """Strip markup to readable text. Style/script blocks must go BEFORE tag stripping:
    a <style> block of @font-face rules can be thousands of characters and will otherwise
    fill the whole stored body, burying the actual message."""
    h = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?is)<!--.*?-->", " ", h)
    h = re.sub(r"(?i)<(br|/p|/div|/tr|/h[1-6])[^>]*>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = _unescape(h)
    h = re.sub(r"[ \t\xa0]{2,}", " ", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip()


def _unescape(s: str) -> str:
    import html as _h
    return _h.unescape(s)


def _normalize(t: str) -> str:
    """Decode entities and non-breaking spaces. Without this a role parses as
    '&nbsp;Technical Program Manager' and splits into its own application."""
    t = _unescape(_unescape(t or ""))          # twice: some senders double-encode
    t = t.replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"[ \t]{2,}", " ", t)


def _looks_like_css(t: str) -> bool:
    return bool(re.search(r"@font-face|@media|border-collapse|\{[^}]*:[^}]*\}", t[:400]))


def _body_text(payload, limit=4000) -> str:
    """Prefer a real text/plain part; otherwise convert the richest HTML part."""
    stack, plains, htmls = [payload], [], []
    while stack:
        p = stack.pop(0)
        mime = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data:
            try:
                text = base64.urlsafe_b64decode(data).decode("utf-8", "replace")
            except Exception:
                text = ""
            if mime == "text/plain":
                plains.append(text)
            elif mime == "text/html":
                htmls.append(text)
        stack.extend(p.get("parts", []) or [])
    for t in plains:
        t = _normalize(t)
        if t.strip() and not _looks_like_css(t):
            return t[:limit]
    for h in sorted(htmls, key=len, reverse=True):
        t = _normalize(_html_to_text(h))
        if t.strip() and not _looks_like_css(t):
            return t[:limit]
    return _normalize(plains[0] if plains else _html_to_text(htmls[0]) if htmls else "")[:limit]


def sync(conn, query: Optional[str] = None, max_results: int = 400, newer_than: str = "1y",
         refetch: bool = False) -> dict:
    svc = _service()
    q = f"{query or DEFAULT_QUERY} newer_than:{newer_than}"
    stats = {"seen": 0, "new": 0, "noise": 0, "review": 0, "apps": 0, "skipped": 0, "refetched": 0}

    page, fetched = None, 0
    while fetched < max_results:
        resp = _retry(lambda: svc.users().messages().list(
            userId="me", q=q, pageToken=page,
            maxResults=min(100, max_results - fetched)).execute())
        msgs = resp.get("messages", [])
        if not msgs:
            break
        for m in msgs:
            fetched += 1
            stats["seen"] += 1
            ext = "gmail:" + m["id"]
            known = conn.execute("SELECT id, body, thread_id FROM events WHERE external_id=?",
                                 (ext,)).fetchone()
            # Refetch when anything we now store is missing, not only the body. Adding
            # thread_id without widening this left the backfill a no-op on 686 events.
            stale = known and (not known["body"] or _looks_like_css(known["body"])
                               or known["thread_id"] is None)
            if known and not (refetch and stale):
                stats["skipped"] += 1
                continue
            full = _retry(lambda: svc.users().messages()
                          .get(userId="me", id=m["id"], format="full").execute())
            time.sleep(THROTTLE)
            payload = full.get("payload", {})
            subject = _header(payload, "Subject")
            sender = _header(payload, "From")
            body = _body_text(payload)
            when = _header(payload, "Date")
            iso = _to_iso(when, full.get("internalDate"))

            tid = full.get("threadId")
            if known:                       # refetch path: repair the body, backfill the thread
                conn.execute("UPDATE events SET body=?, thread_id=? WHERE id=?",
                             (body[:2000], tid, known["id"]))
                stats["refetched"] += 1
                continue

            c = classify(subject, sender, body)
            if c.event_type == "noise":
                db.add_event(conn, None, iso, "noise", "gmail", confidence=c.confidence,
                             external_id=ext, subject=subject, sender=sender, body=body[:2000],
                             thread_id=tid)
                stats["noise"] += 1
                continue

            app_id = None
            prior = conn.execute("""SELECT application_id FROM events
                                    WHERE thread_id = ? AND application_id IS NOT NULL
                                    ORDER BY occurred_at LIMIT 1""", (tid,)).fetchone() if tid else None
            if prior:
                app_id = prior["application_id"]          # same conversation, same application
            elif c.company and creates_application(c):
                cid = db.get_or_create_company(conn, c.company)
                rid = db.get_or_create_role(conn, cid, c.role or "Unknown role", source="gmail")
                existed = conn.execute("SELECT 1 FROM applications WHERE role_id=?", (rid,)).fetchone()
                app_id = db.get_or_create_application(conn, rid, applied_on=iso[:10],
                                                      submitted_at=iso, is_ack=(c.event_type == "ack"),
                                                      channel="gmail")
                if not existed:
                    stats["apps"] += 1

            eid = db.add_event(conn, app_id, iso, c.event_type, "gmail", confidence=c.confidence,
                               external_id=ext, subject=subject, sender=sender,
                               raw="; ".join(c.reasons), body=body[:2000], thread_id=tid,
                               held=c.held)
            if eid:
                stats["new"] += 1
                if c.held:
                    db.queue_review(conn, eid, c.held_reason)
                    stats["held"] = stats.get("held", 0) + 1
                    stats["review"] += 1
                elif c.needs_review:
                    db.queue_review(conn, eid, f"conf={c.confidence} company={c.company!r} role={c.role!r}")
                    stats["review"] += 1
            if stats["seen"] % 25 == 0:
                conn.commit()
                print(f"  ...{stats['seen']} seen, {stats['new']} new", flush=True)
        page = resp.get("nextPageToken")
        if not page:
            break
    conn.commit()
    db.recompute_all(conn)
    return stats


def _to_iso(date_header: str, internal_ms: Optional[str]) -> str:
    from email.utils import parsedate_to_datetime
    import datetime as dt
    try:
        return parsedate_to_datetime(date_header).astimezone(dt.timezone.utc).isoformat(" ")[:19]
    except Exception:
        pass
    if internal_ms:
        return dt.datetime.utcfromtimestamp(int(internal_ms) / 1000).isoformat(" ")[:19]
    return dt.datetime.utcnow().isoformat(" ")[:19]
