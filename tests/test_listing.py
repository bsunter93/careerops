"""Stripe's listing page outranks its Greenhouse feed for location and pay.

Fixtures copy the shape of real stripe.com/jobs/listing pages: the rendered facts are a
<dt><h3>label</h3></dt><dd>value</dd> pair, the band is a sentence under "Pay and
benefits", and a schema.org JobPosting block carries the same facts as JSON. The
postings themselves are invented.
"""
import json
import sqlite3
import unittest
from unittest import mock

from careerops import db, discover as dsc, drift, listing

def _fact(label, value):
    return (f'<div class="careers-listing-details__fact"><dt class="x"><span><svg></svg></span>'
            f'<h3 class="hds-heading hds-heading--xxs">{label}</h3></dt>'
            f'<dd class="hds-text careers-listing-details__fact-value">{value}</dd></div>')


def _ld(**kw):
    d = {"@context": "https://schema.org", "@type": "JobPosting", "title": "x"}
    d.update(kw)
    return f'<script type="application/ld+json">{json.dumps(d)}</script>'


def _salary(lo, hi):
    return (f"<p>The annual US base salary range for this role is ${lo:,} - ${hi:,}. "
            "For sales roles, the range provided is the role&#x27;s On Target Earnings.</p>")


REMOTE = (_ld(jobLocationType="TELECOMMUTE",
              applicantLocationRequirements=[{"@type": "Country", "name": "United States"}],
              jobLocation=[{"address": {"addressLocality": "Chicago"}},
                           {"address": {"addressLocality": "South San Francisco HQ"}}],
              baseSalary={"currency": "USD", "value": {"minValue": 189400, "maxValue": 284000,
                                                       "unitText": "YEAR"}})
          + _salary(189400, 284000)
          + _fact("Office locations", "Chicago, South San Francisco HQ")
          + _fact("Remote location", "Remote in United States"))

OFFICE_ONLY = (_ld(jobLocation=[{"address": {"addressLocality": "South San Francisco HQ"}}],
                   baseSalary={"currency": "USD", "value": {"minValue": 155800,
                                                            "maxValue": 233600, "unitText": "YEAR"}})
               + _salary(155800, 233600)
               + _fact("Office location", "South San Francisco HQ"))

CANADA = (_ld(jobLocationType="TELECOMMUTE",
              applicantLocationRequirements=[{"@type": "Country", "name": "Canada"}],
              baseSalary={"currency": "CAD", "value": {"minValue": 153700, "maxValue": 230500,
                                                       "unitText": "YEAR"}})
          + _fact("Office location", "Toronto") + _fact("Remote location", "Remote in Canada"))


def _pages(by_jid):
    """A fetcher serving fixtures by posting id; anything else is a 404 (None)."""
    calls = []

    def get(url):
        calls.append(url)
        return by_jid.get(url.rsplit("/", 1)[1])
    get.calls = calls
    return get


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init(conn)
    if "closed_at" not in {r[1] for r in conn.execute("PRAGMA table_info(roles)")}:
        conn.execute("ALTER TABLE roles ADD COLUMN closed_at TEXT")    # liveness's column
    return conn


def _role(conn, title, location, jid, company="Stripe", **kw):
    cid = db.get_or_create_company(conn, company)
    return db.get_or_create_role(conn, cid, title, location=location, source="greenhouse",
                                 url=f"https://stripe.com/jobs/search?gh_jid={jid}",
                                 jd_text="x" * 400, **kw)


class ParsePage(unittest.TestCase):

    def test_remote_role_reads_remote_offices_and_band(self):
        f = listing.parse_stripe(REMOTE)
        self.assertEqual(f, {"remote": "Remote in United States",
                             "offices": "Chicago, South San Francisco HQ",
                             "comp_min": 189400, "comp_max": 284000})
        self.assertEqual(listing.location(f),
                         "Remote in United States (offices: Chicago, South San Francisco HQ)")

    def test_office_only_role_never_says_remote(self):
        f = listing.parse_stripe(OFFICE_ONLY)
        self.assertIsNone(f["remote"])
        loc = listing.location(f)
        self.assertEqual(loc, "Offices: South San Francisco HQ")
        self.assertNotIn("remote", loc.lower())

    def test_a_non_usd_band_is_not_a_floor_comparison(self):
        f = listing.parse_stripe(CANADA)
        self.assertEqual((f["comp_min"], f["comp_max"]), (None, None))
        self.assertEqual(f["remote"], "Remote in Canada")

    def test_schema_block_alone_is_enough(self):
        ld_only = REMOTE.split('<p>')[0]
        f = listing.parse_stripe(ld_only)
        self.assertEqual(f["remote"], "Remote in United States")
        self.assertEqual(f["offices"], "Chicago, South San Francisco HQ")
        self.assertEqual((f["comp_min"], f["comp_max"]), (189400, 284000))

    def test_rendered_page_alone_is_enough(self):
        rendered = REMOTE.split("</script>", 1)[1]
        f = listing.parse_stripe(rendered)
        self.assertEqual(f["remote"], "Remote in United States")
        self.assertEqual((f["comp_min"], f["comp_max"]), (189400, 284000))

    def test_a_page_with_no_fields_changes_nothing(self):
        self.assertIsNone(listing.parse_stripe("<html><body>Stripe careers</body></html>"))
        job = {"external_id": "1", "location": "NYC, SF, US"}
        self.assertFalse(listing.enrich(job, _pages({"1": "<html></html>"})))
        self.assertFalse(listing.enrich(job, _pages({})))           # 404
        self.assertEqual(job, {"external_id": "1", "location": "NYC, SF, US"})

    def test_jid_comes_from_the_stored_url(self):
        self.assertEqual(listing.stripe_jid("https://stripe.com/jobs/search?gh_jid=8214620"),
                         "8214620")
        self.assertIsNone(listing.stripe_jid("https://boards.greenhouse.io/x/jobs/1"))
        self.assertIsNone(listing.stripe_jid(None))


class Discover(unittest.TestCase):
    """The page is read before the location gate, so the gate judges the real fields."""

    TITLES = ["strategy & operations", "program manager"]
    LOCATIONS = ["denver", "remote", "united states", "us"]

    def _run(self, conn, jobs, board="greenhouse", slug="stripe", pages=None):
        get = pages or _pages({})
        with mock.patch.object(dsc, "fetch", return_value=jobs), \
             mock.patch.object(listing, "fetch_page", get):
            dsc.discover(conn, [{"company": "Stripe", "board": board, "slug": slug}],
                         self.TITLES, self.LOCATIONS)
        return get

    def _job(self, jid, title, location):
        return {"title": title, "location": location, "external_id": jid, "jd_text": "x" * 400,
                "url": f"https://stripe.com/jobs/search?gh_jid={jid}"}

    def test_a_remote_role_the_feed_calls_onsite_is_stored_remote_with_its_band(self):
        conn = _conn()
        self._run(conn, [self._job("1", "Strategy & Operations Partner", "NYC, SF, Chicago")],
                  pages=_pages({"1": REMOTE}))
        r = conn.execute("SELECT location, remote, comp_min, comp_max FROM roles").fetchone()
        self.assertEqual(tuple(r), ("Remote in United States (offices: Chicago, South San Francisco HQ)",
                                    1, 189400, 284000))

    def test_an_office_role_the_feed_calls_remote_fails_the_gate(self):
        conn = _conn()
        self._run(conn, [self._job("2", "Strategy & Operations, Infra", "Remote US")],
                  pages=_pages({"2": OFFICE_ONLY}))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0], 0)

    def test_a_stored_role_is_corrected_on_the_next_sweep(self):
        conn = _conn()
        job = self._job("1", "Strategy & Operations Partner", "NYC, SF, Chicago, Seattle, US")
        self._run(conn, [dict(job)])                       # page down: feed values stored
        self.assertEqual(conn.execute("SELECT remote FROM roles").fetchone()[0], None)
        self._run(conn, [dict(job)], pages=_pages({"1": REMOTE}))
        r = conn.execute("SELECT location, remote, comp_max FROM roles").fetchone()
        self.assertTrue(r["location"].startswith("Remote in United States"))
        self.assertEqual((r["remote"], r["comp_max"]), (1, 284000))

    def test_a_same_title_posting_elsewhere_does_not_overwrite_the_row(self):
        conn = _conn()
        us = self._job("1", "Program Manager, Risk", "US")
        self._run(conn, [dict(us)], pages=_pages({"1": REMOTE}))
        before = tuple(conn.execute("SELECT location, comp_max, url FROM roles").fetchone())
        abroad = dict(us, external_id="9", url="https://stripe.com/jobs/search?gh_jid=9")
        self._run(conn, [abroad], pages=_pages({"9": CANADA}))
        self.assertEqual(tuple(conn.execute("SELECT location, comp_max, url FROM roles").fetchone()),
                         before)

    def test_other_boards_and_unmatched_titles_cost_no_request(self):
        conn = _conn()
        get = self._run(conn, [self._job("1", "Program Manager", "Remote")], slug="notstripe")
        self.assertEqual(get.calls, [])
        get = self._run(conn, [self._job("2", "Account Executive", "Remote")])
        self.assertEqual(get.calls, [])


class Backfill(unittest.TestCase):
    """The retroactive half reaches roles stored before the rule, whatever the gate says."""

    def setUp(self):
        self.conn = _conn()
        self.remote = _role(self.conn, "S&O Partner", "NYC, SF, Chicago, Seattle, US", "1")
        self.office = _role(self.conn, "S&O, Infra", "Remote US", "2")
        self.gone = _role(self.conn, "Program Manager, Tax", "US Remote", "3")
        self.pages = _pages({"1": REMOTE, "2": OFFICE_ONLY})

    def _loc(self, rid):
        return tuple(self.conn.execute(
            "SELECT location, remote, comp_min, comp_max FROM roles WHERE id=?", (rid,)).fetchone())

    def test_dry_run_reports_and_writes_nothing(self):
        b = listing.backfill(self.conn, self.pages, write=False)
        self.assertEqual([c[0] for c in b["changed"]], [self.remote, self.office])
        self.assertEqual(b["unanswered"], [self.gone])
        self.assertEqual(self._loc(self.remote), ("NYC, SF, Chicago, Seattle, US", None, None, None))

    def test_writes_page_fields_and_leaves_a_404_alone(self):
        listing.backfill(self.conn, self.pages)
        self.assertEqual(self._loc(self.office),
                         ("Offices: South San Francisco HQ", 0, 155800, 233600))
        self.assertEqual(self._loc(self.gone), ("US Remote", None, None, None))
        self.assertEqual(listing.backfill(self.conn, self.pages)["changed"], [])

    def test_a_page_without_a_band_keeps_the_stored_one(self):
        self.conn.execute("UPDATE roles SET comp_min=1, comp_max=2 WHERE id=?", (self.remote,))
        listing.backfill(self.conn, _pages({"1": _fact("Remote location", "Remote in United States")}))
        self.assertEqual(self._loc(self.remote)[2:], (1, 2))

    def test_only_unlisted_skips_roles_a_page_already_answered(self):
        listing.backfill(self.conn, self.pages)
        get = _pages({"1": REMOTE, "2": OFFICE_ONLY})
        listing.backfill(self.conn, get, only_unlisted=True)
        self.assertEqual(get.calls, [listing.STRIPE_LISTING.format(jid="3")])

    def test_drift_names_the_unread_roles_until_the_backfill_runs(self):
        self.assertEqual({r[0] for r in drift.listing_drift(self.conn)},
                         {self.remote, self.office, self.gone})
        listing.backfill(self.conn, self.pages)
        self.assertEqual([r[0] for r in drift.listing_drift(self.conn)], [self.gone])
        self.conn.execute("UPDATE roles SET closed_at='2026-10-09' WHERE id=?", (self.gone,))
        self.assertEqual(drift.listing_drift(self.conn), [])


if __name__ == "__main__":
    unittest.main()
