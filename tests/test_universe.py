import unittest
from unittest import mock

from careerops import db, discover as dsc, universe


TITLES = ["strategy and operations", "program manager"]
EXCLUDES = ["intern"]


def _job(title, location="Remote, US", jd="x" * 300):
    return {"title": title, "location": location, "url": "https://example.test/j",
            "jd_text": jd, "external_id": title, "posted_at": "2026-10-01T00:00:00",
            "comp_min": None, "comp_max": None}


class SlugTest(unittest.TestCase):
    def test_first_path_segment_is_the_company(self):
        self.assertEqual(universe.slug_from_url("ashby", "https://jobs.ashbyhq.com/Ramp/3f2a"), "ramp")
        self.assertEqual(universe.slug_from_url("lever", "https://jobs.lever.co/plaid/abc-123"), "plaid")
        self.assertEqual(universe.slug_from_url(
            "greenhouse", "https://job-boards.greenhouse.io/figma/jobs/55"), "figma")

    def test_greenhouse_embed_names_the_board_in_the_query(self):
        self.assertEqual(universe.slug_from_url(
            "greenhouse", "https://boards.greenhouse.io/embed/job_board?for=airtable"), "airtable")

    def test_a_board_name_with_a_space_is_kept_encoded(self):
        self.assertEqual(universe.slug_from_url(
            "ashby", "https://jobs.ashbyhq.com/Flock%20Safety/77"), "flock%20safety")
        self.assertEqual(universe.name_from_slug("flock%20safety"), "Flock Safety")
        self.assertIsNone(universe.slug_from_url("ashby", "https://jobs.ashbyhq.com/a<b>"))

    def test_board_software_paths_and_unsafe_slugs_are_dropped(self):
        self.assertIsNone(universe.slug_from_url("greenhouse", "https://boards.greenhouse.io/embed/job_app"))
        self.assertIsNone(universe.slug_from_url("ashby", "https://jobs.ashbyhq.com/robots.txt"))
        self.assertIsNone(universe.slug_from_url("ashby", "https://jobs.ashbyhq.com/"))


class BoardsTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:"); db.init(self.conn)

    def test_store_is_idempotent(self):
        self.assertEqual(universe.store(self.conn, {"ashby": {"ramp", "plaid"}}, "commoncrawl:x"), 2)
        self.assertEqual(universe.store(self.conn, {"ashby": {"ramp"}}, "commoncrawl:x"), 0)

    def test_watchlist_boards_are_not_polled_twice(self):
        universe.store(self.conn, {"ashby": {"ramp", "plaid"}}, "commoncrawl:x")
        wl = [{"company": "Ramp", "board": "ashby", "slug": "Ramp"}]
        out = universe.boards_to_poll(self.conn, wl)
        self.assertEqual([(w["board"], w["slug"]) for w in out], [("ashby", "Ramp"), ("ashby", "plaid")])
        self.assertTrue(out[1]["universe"])
        self.assertEqual(out[1]["company"], "Plaid")

    def test_a_board_that_keeps_coming_back_empty_is_rested(self):
        universe.store(self.conn, {"lever": {"quiet"}}, "commoncrawl:x")
        self.conn.execute("UPDATE boards SET misses = ?, last_checked = datetime('now')",
                          (universe.MISS_LIMIT,))
        self.assertEqual(universe.boards_to_poll(self.conn, []), [])
        self.conn.execute("UPDATE boards SET last_checked = datetime('now', '-30 days')")
        self.assertEqual(len(universe.boards_to_poll(self.conn, [])), 1)


class PrefetchTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:"); db.init(self.conn)

    def test_greenhouse_with_no_matching_title_never_fetches_descriptions(self):
        universe.store(self.conn, {"greenhouse": {"acme"}}, "commoncrawl:x")
        boards = universe.boards_to_poll(self.conn, [])
        with mock.patch.object(dsc, "_get", return_value={"jobs": [{"title": "Software Engineer"}]}), \
             mock.patch.object(dsc, "fetch") as full:
            got, stats = universe.prefetch(self.conn, boards, TITLES, EXCLUDES, workers=1)
        full.assert_not_called()
        self.assertEqual(got[("greenhouse", "acme")], [])
        row = self.conn.execute("SELECT last_jobs, misses FROM boards").fetchone()
        self.assertEqual((row["last_jobs"], row["misses"]), (1, 0))

    def test_only_titles_the_gate_would_pass_are_kept(self):
        universe.store(self.conn, {"ashby": {"acme"}}, "commoncrawl:x")
        boards = universe.boards_to_poll(self.conn, [])
        jobs = [_job("Strategy & Operations Lead"), _job("Strategy and Operations Lead"),
                _job("Program Manager Intern"), _job("Account Executive")]
        with mock.patch.object(dsc, "fetch", return_value=jobs):
            got, stats = universe.prefetch(self.conn, boards, TITLES, EXCLUDES, workers=1)
        self.assertEqual([j["title"] for j in got[("ashby", "acme")]], ["Strategy and Operations Lead"])
        self.assertEqual((stats["listed"], stats["title_hits"]), (4, 1))

    def test_an_empty_board_counts_a_miss_and_a_crash_does_not_stop_the_sweep(self):
        universe.store(self.conn, {"ashby": {"empty", "broken"}}, "commoncrawl:x")
        boards = universe.boards_to_poll(self.conn, [])

        def fake(board, slug, titles=()):
            if slug == "broken":
                raise ValueError("bad url")
            return []
        with mock.patch.object(dsc, "fetch", side_effect=fake):
            got, stats = universe.prefetch(self.conn, boards, TITLES, EXCLUDES, workers=2)
        self.assertEqual(stats["empty"], 2)
        misses = dict(self.conn.execute("SELECT slug, misses FROM boards").fetchall())
        self.assertEqual(misses, {"broken": 1, "empty": 1})

    def test_greenhouse_reports_its_company_name(self):
        universe.store(self.conn, {"greenhouse": {"acmeco"}}, "commoncrawl:x")
        boards = universe.boards_to_poll(self.conn, [])

        def get(url):
            return {"name": "Acme Co"} if url.endswith("/acmeco") else {"jobs": [{"title": "Program Manager"}]}
        with mock.patch.object(dsc, "_get", side_effect=get), \
             mock.patch.object(dsc, "fetch", return_value=[_job("Program Manager")]):
            universe.prefetch(self.conn, boards, TITLES, EXCLUDES, workers=1)
        self.assertEqual(boards[0]["company"], "Acme Co")
        self.assertEqual(self.conn.execute("SELECT company FROM boards").fetchone()[0], "Acme Co")


class DiscoverPrefetchedTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:"); db.init(self.conn)

    def test_prefetched_boards_skip_fetch_and_empty_ones_are_not_unreachable(self):
        boards = [{"company": "Acme", "board": "ashby", "slug": "acme", "universe": True},
                  {"company": "Quiet", "board": "ashby", "slug": "quiet", "universe": True}]
        pre = {("ashby", "acme"): [_job("Program Manager")], ("ashby", "quiet"): []}
        with mock.patch.object(dsc, "fetch") as f:
            s = dsc.discover(self.conn, boards, TITLES, ["remote"], prefetched=pre)
        f.assert_not_called()
        self.assertEqual((s["new"], s["failed"]), (1, []))

    def test_watchlist_boards_still_fetch_and_report_failures(self):
        with mock.patch.object(dsc, "fetch", return_value=[]):
            s = dsc.discover(self.conn, [{"company": "Gone", "board": "lever", "slug": "gone"}],
                             TITLES, ["remote"], prefetched={})
        self.assertEqual(s["failed"], ["Gone(lever:gone)"])


if __name__ == "__main__":
    unittest.main()
