-- Career Ops: applications are entities with derived state; emails are events that mutate it.
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS companies (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,
  domain        TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS roles (
  id            INTEGER PRIMARY KEY,
  company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  title         TEXT NOT NULL,
  level         TEXT,
  location      TEXT,
  remote        INTEGER,
  comp_min      INTEGER,
  comp_max      INTEGER,
  source        TEXT,              -- greenhouse|lever|ashby|workday|google|manual
  url           TEXT,
  jd_text       TEXT,
  jd_hash       TEXT,
  discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
  posted_at     TEXT,              -- when the employer published it, not when we saw it
  UNIQUE(company_id, title)
);

CREATE TABLE IF NOT EXISTS applications (
  id                INTEGER PRIMARY KEY,
  role_id           INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  applied_on        TEXT,
  submitted_at      TEXT,          -- a re-apply to the same role is a new row
  channel           TEXT,          -- how it was found/submitted
  referral          INTEGER NOT NULL DEFAULT 0,
  status            TEXT NOT NULL DEFAULT 'applied',   -- furthest stage reached
  activity          TEXT,                              -- active | dormant | closed
  status_updated_at TEXT,
  fit_score         REAL,
  fit_reasoning     TEXT,
  notes             TEXT
);

-- Append-only. Never edited; status is derived from these.
CREATE TABLE IF NOT EXISTS events (
  id             INTEGER PRIMARY KEY,
  application_id INTEGER REFERENCES applications(id) ON DELETE CASCADE,
  occurred_at    TEXT NOT NULL,
  type           TEXT NOT NULL,    -- ack|rejection|interview_invite|recruiter_outreach|assessment|offer|noise|unresolved
  confidence     REAL NOT NULL DEFAULT 1.0,
  source         TEXT NOT NULL,    -- gmail|csv|manual
  external_id    TEXT UNIQUE,      -- gmail message id -> idempotent ingestion
  subject        TEXT,
  sender         TEXT,
  raw            TEXT,
  body           TEXT,
  thread_id      TEXT,             -- Gmail threadId: the only reliable way to know
                                   -- two messages concern the same conversation
  held           INTEGER NOT NULL DEFAULT 0  -- recorded as evidence, withheld from status
);

CREATE TABLE IF NOT EXISTS artifacts (
  id           INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL,      -- resume|cover_letter|portfolio
  variant      TEXT NOT NULL,
  path         TEXT NOT NULL,
  content_hash TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(kind, variant, content_hash)
);

CREATE TABLE IF NOT EXISTS application_artifacts (
  application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  artifact_id    INTEGER NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
  PRIMARY KEY (application_id, artifact_id)
);

-- Anything the classifier isn't confident about lands here instead of being guessed at.
CREATE TABLE IF NOT EXISTS review_queue (
  id         INTEGER PRIMARY KEY,
  event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  reason     TEXT NOT NULL,
  resolved   INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_app  ON events(application_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status);

CREATE VIEW IF NOT EXISTS v_pipeline AS
SELECT a.id, c.name AS company, r.title AS role, r.location,
       a.applied_on, a.status, a.referral, a.fit_score,
       (SELECT MAX(occurred_at) FROM events e WHERE e.application_id = a.id
          AND e.type NOT IN ('noise')) AS last_event_at,
       CAST(julianday('now') - julianday(COALESCE(
          (SELECT MAX(occurred_at) FROM events e WHERE e.application_id=a.id AND e.type NOT IN ('noise')),
          a.applied_on)) AS INT) AS days_quiet
FROM applications a
JOIN roles r     ON r.id = a.role_id
JOIN companies c ON c.id = r.company_id;

-- Reference only. The portal misreports status (it shows 'Submitted' for roles
-- already rejected by email), so it never feeds derived status; it supplies role
-- identity for acks that name no role.
CREATE TABLE IF NOT EXISTS portal_snapshot (
  id            INTEGER PRIMARY KEY,
  company       TEXT NOT NULL,
  role          TEXT NOT NULL,
  portal_status TEXT,
  portal_seen   TEXT,
  observed_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(company, role)
);

-- Public employee sentiment. REFERENCE ONLY, exactly like portal_snapshot: it never
-- feeds fit_score or derived status. Third-party review aggregates are self-selected
-- and easy to confuse between same-named companies, so every row carries its own
-- confidence and sources and is shown with provenance attached.
CREATE TABLE IF NOT EXISTS company_intel (
  company        TEXT PRIMARY KEY,
  rating         REAL,      -- overall, out of 5
  reviews_n      INTEGER,
  wlb            REAL,      -- work/life balance sub-rating, out of 5
  comp_benefits  REAL,
  culture        REAL,
  career         REAL,
  recommend_pct  INTEGER,
  pros           TEXT,      -- JSON array
  cons           TEXT,      -- JSON array
  wlb_summary    TEXT,
  confidence     TEXT,      -- high | medium | low
  entity_note    TEXT,      -- which legal entity this is, when the name is ambiguous
  sources        TEXT,      -- JSON array of {title,url}
  fetched_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
