import sys, pathlib, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from careerops.aggregators import slug_candidates, fetch_feed
from careerops.discover import matches


class TestSlugResolution(unittest.TestCase):
    def test_slugs_strip_punctuation_and_suffixes(self):
        self.assertIn("cleanharbors", slug_candidates("Clean Harbors"))
        self.assertIn("instridehealth", slug_candidates("InStride Health"))
        self.assertIn("modern-treasury", slug_candidates("Modern Treasury"))
        self.assertIn("juullabs", slug_candidates("JUUL Labs"))

    def test_short_and_empty_names_yield_nothing(self):
        """A two-character slug matches half the internet's test boards."""
        self.assertEqual(slug_candidates(""), [])
        self.assertEqual(slug_candidates("HP"), [])

    def test_candidates_are_deduplicated(self):
        c = slug_candidates("Asana")
        self.assertEqual(len(c), len(set(c)))


class TestFeedNormalization(unittest.TestCase):
    """Every feed must produce the shape discover.matches() already consumes.
    A feed returning {"location": {...}} instead of a string silently breaks
    the location filter, which fails open and floods the board."""

    REQUIRED = {"title", "company", "location", "url", "jd_text", "external_id", "posted_at"}

    def test_shape_is_uniform_across_feeds(self):
        for name in ("arbeitnow", "remotive"):
            with self.subTest(feed=name):
                jobs = fetch_feed(name)
                if not jobs:
                    self.skipTest(f"{name} unreachable")
                j = jobs[0]
                self.assertTrue(self.REQUIRED <= set(j), f"{name} missing {self.REQUIRED - set(j)}")
                for k in ("title", "location"):
                    self.assertIsInstance(j[k] or "", str, f"{name}.{k} must be a string")

    def test_normalized_jobs_pass_through_the_real_matcher(self):
        jobs = fetch_feed("arbeitnow")
        if not jobs:
            self.skipTest("arbeitnow unreachable")
        matches(jobs[0], ["operations"], ["remote"], ["intern"])   # must not raise


if __name__ == "__main__":
    unittest.main()


class TestScoringBacklog(unittest.TestCase):
    """discover must report roles that fit has not reached. An unscored role has no
    application row, so it shows up in no count anywhere and stalls silently."""

    def setUp(self):
        from careerops import db
        self.conn = db.connect(":memory:"); db.init(self.conn)
        self.db = db
        self.cid = db.get_or_create_company(self.conn, "Contoso")

    def _role(self, title, jd):
        return self.db.get_or_create_role(self.conn, self.cid, title, jd_text=jd)

    def test_counts_roles_awaiting_fit(self):
        from careerops.discover import scoring_backlog
        self._role("Business Operations Manager", "x" * 500)
        self._role("Strategy and Operations Lead", "y" * 500)
        self.assertEqual(scoring_backlog(self.conn)["unscored"], 2)

    def test_scored_roles_drop_out_of_the_backlog(self):
        from careerops.discover import scoring_backlog
        rid = self._role("Business Operations Manager", "x" * 500)
        aid = self.db.get_or_create_application(self.conn, rid, channel="discovered")
        self.conn.execute("UPDATE applications SET fit_score=80 WHERE id=?", (aid,))
        self.assertEqual(scoring_backlog(self.conn)["unscored"], 0)

    def test_applied_roles_are_neither_unscored_nor_unscorable(self):
        """Roles reached through Gmail have no JD and need no score. Counting them
        reported a 319-role backlog that no amount of scoring would ever clear."""
        from careerops.discover import scoring_backlog
        rid = self._role("Business Operations Manager", "")
        aid = self.db.get_or_create_application(self.conn, rid, channel="gmail")
        self.conn.execute("UPDATE applications SET status='acked' WHERE id=?", (aid,))
        b = scoring_backlog(self.conn)
        self.assertEqual((b["unscored"], b["unscorable"]), (0, 0))

    def test_a_role_with_no_jd_is_unscorable_not_unscored(self):
        """fit skips these every run, so counting them as pending would report a
        backlog that never clears no matter how often fit is run."""
        from careerops.discover import scoring_backlog
        self._role("Chief of Staff", "too short")
        b = scoring_backlog(self.conn)
        self.assertEqual(b["unscored"], 0)
        self.assertEqual(b["unscorable"], 1)

    def test_backlog_matches_what_fit_would_actually_pick_up(self):
        """The count is only useful if it mirrors score_pending's own predicate."""
        from careerops.discover import scoring_backlog
        self._role("Business Operations Manager", "x" * 500)
        self._role("Revenue Operations Manager", "y" * 500)
        self._role("Chief of Staff", "short")
        rows = self.conn.execute("""
            SELECT COUNT(*) n FROM roles r LEFT JOIN applications a ON a.role_id = r.id
            WHERE r.jd_text IS NOT NULL AND LENGTH(r.jd_text) > 200
              AND (a.id IS NULL OR a.fit_score IS NULL)""").fetchone()["n"]
        self.assertEqual(scoring_backlog(self.conn)["unscored"], rows)


class TestFetchIsFailureTolerant(unittest.TestCase):
    def test_a_socket_timeout_does_not_abort_the_sweep(self):
        """On Python 3.9 socket.timeout is not TimeoutError, so naming TimeoutError
        in the handler let one slow board raise through and kill all 87."""
        import socket
        from unittest import mock
        from careerops import discover as D
        with mock.patch.object(D.urllib.request, "urlopen", side_effect=socket.timeout("timed out")):
            self.assertIsNone(D._get("https://example.invalid/board"))
            self.assertEqual(D.fetch("greenhouse", "anything"), [])


class TestTitleNormalization(unittest.TestCase):
    def test_a_trailing_space_does_not_create_a_phantom_new_role(self):
        """Ashby titles arrive as "Product Designer ". get_or_create_role strips
        before inserting, so an unstripped lookup misses forever: discover reports
        it new on every run and never refreshes its JD or posting date."""
        from careerops import db
        from careerops.discover import discover
        from unittest import mock
        conn = db.connect(":memory:"); db.init(conn)
        job = {"title": "Business Operations Manager ", "location": "Remote, US",
               "url": "https://example.test/1", "jd_text": "x" * 500,
               "external_id": "1", "posted_at": "2026-09-01T00:00:00"}
        wl = [{"company": "Contoso", "board": "ashby", "slug": "contoso"}]
        with mock.patch("careerops.discover.fetch", return_value=[dict(job)]):
            first = discover(conn, wl, ["business operations"], ["remote"])
            second = discover(conn, wl, ["business operations"], ["remote"])
        self.assertEqual(first["new"], 1)
        self.assertEqual(second["new"], 0, "same posting counted new twice")
        self.assertEqual(conn.execute("SELECT COUNT(*) n FROM roles").fetchone()["n"], 1)


class TestResumeRegister(unittest.TestCase):
    """A resume summary carries an implied subject. The prompt asked for "who he is"
    while forbidding first person, so the model wrote "Benjamin Sunter is... He
    authored...". Prompt wording is not a guarantee; the shape is enforced after."""

    def test_third_person_subject_is_removed(self):
        from careerops.resume import _impersonal
        got = _impersonal("Benjamin Sunter is a senior operations leader with governance "
                          "experience. He authored the OKR architecture adopted into FY27.")
        self.assertTrue(got.startswith("Senior operations leader"), got)
        self.assertNotIn(" He ", " " + got)
        self.assertIn("Authored the OKR", got)

    def test_already_impersonal_text_is_untouched(self):
        from careerops.resume import _impersonal
        src = ("Senior operations leader with experience running board cadences. "
               "Built an $18M operating cadence in seven days.")
        self.assertEqual(_impersonal(src), src)


class TestResumeServer(unittest.TestCase):
    """The button takes an application id, never a path. A dashboard is a web page,
    and the worst a bad request should manage is building a resume for the wrong role."""

    def test_filenames_are_derived_not_accepted(self):
        from careerops.server import _slug
        self.assertEqual(_slug("Chief of Staff, Payer"), "ChiefOfStaffPayer")
        self.assertEqual(_slug("../../etc/passwd"), "EtcPasswd")
        self.assertEqual(_slug(""), "Role")

    def test_out_path_lands_in_the_resume_dir(self):
        import tempfile, pathlib
        from careerops import db
        from careerops.server import out_path
        conn = db.connect(":memory:"); db.init(conn)
        cid = db.get_or_create_company(conn, "Fivetran")
        rid = db.get_or_create_role(conn, cid, "Lead Company Operations Manager")
        aid = db.get_or_create_application(conn, rid, channel="discovered")
        with tempfile.TemporaryDirectory() as d:
            out, company, title = out_path(conn, aid, d)
            self.assertEqual(pathlib.Path(out).parent, pathlib.Path(d))
            self.assertTrue(pathlib.Path(out).name.endswith(".docx"))
            self.assertEqual(company, "Fivetran")

    def test_unknown_application_is_rejected(self):
        from careerops import db
        from careerops.server import out_path
        conn = db.connect(":memory:"); db.init(conn)
        self.assertEqual(out_path(conn, 999999, "/tmp")[0], None)


class TestCompanyAliases(unittest.TestCase):
    """One employer, one row. Companies rebrand and merge, and the mail follows the new
    name before the job board does: Fivetran's ack arrived from "Fivetran + dbt Labs"
    while its board still published as "Fivetran", so it created a second company and a
    phantom "Unknown role" application beside the one already applied to."""

    def test_an_alias_resolves_to_the_existing_company(self):
        from unittest import mock
        from careerops import db
        conn = db.connect(":memory:"); db.init(conn)
        with mock.patch.object(db, "_ALIASES", {"fivetran + dbt labs": "Fivetran"}):
            a = db.get_or_create_company(conn, "Fivetran")
            b = db.get_or_create_company(conn, "Fivetran + dbt Labs")
        self.assertEqual(a, b)
        self.assertEqual(conn.execute("SELECT COUNT(*) n FROM companies").fetchone()["n"], 1)

    def test_matching_is_case_insensitive(self):
        from unittest import mock
        from careerops import db
        conn = db.connect(":memory:"); db.init(conn)
        with mock.patch.object(db, "_ALIASES", {"fivetran + dbt labs": "Fivetran"}):
            a = db.get_or_create_company(conn, "Fivetran")
            b = db.get_or_create_company(conn, "FIVETRAN + DBT LABS")
        self.assertEqual(a, b)

    def test_unaliased_companies_are_untouched(self):
        from unittest import mock
        from careerops import db
        conn = db.connect(":memory:"); db.init(conn)
        with mock.patch.object(db, "_ALIASES", {"fivetran + dbt labs": "Fivetran"}):
            a = db.get_or_create_company(conn, "Arcadia")
            b = db.get_or_create_company(conn, "InStride Health")
        self.assertNotEqual(a, b)


class TestAckAdoptsThePendingProspect(unittest.TestCase):
    """Applying on a company's site means the acknowledgement usually arrives before
    `careerops apply` is run. The prospect row has no submitted_at, so the near-match
    cannot see it, and the ack used to open a second application beside it: one role,
    two rows, two different statuses, listed twice in Recent activity."""

    def _setup(self):
        from careerops import db
        conn = db.connect(":memory:"); db.init(conn)
        cid = db.get_or_create_company(conn, "Airbnb")
        rid = db.get_or_create_role(conn, cid, "Senior Programs & Business Operations Lead")
        return db, conn, rid

    def test_ack_adopts_the_prospect_instead_of_opening_a_second_row(self):
        db, conn, rid = self._setup()
        pid = db.get_or_create_application(conn, rid, channel="discovered")
        conn.execute("UPDATE applications SET status='prospect' WHERE id=?", (pid,))
        aid = db.get_or_create_application(conn, rid, submitted_at="2026-09-07T04:12:05",
                                           is_ack=True, channel="gmail")
        self.assertEqual(aid, pid, "ack opened a second application")
        self.assertEqual(conn.execute("SELECT COUNT(*) n FROM applications").fetchone()["n"], 1)
        row = conn.execute("SELECT submitted_at, applied_on FROM applications WHERE id=?",
                           (pid,)).fetchone()
        self.assertEqual(row["applied_on"], "2026-09-07")

    def test_a_real_resubmission_still_opens_its_own_row(self):
        """Re-applying months later is a second submission, not the same one."""
        db, conn, rid = self._setup()
        first = db.get_or_create_application(conn, rid, submitted_at="2026-01-05T09:00:00",
                                             is_ack=True, channel="gmail")
        second = db.get_or_create_application(conn, rid, submitted_at="2026-09-07T04:12:05",
                                              is_ack=True, channel="gmail")
        self.assertNotEqual(first, second)

    def test_a_duplicate_ack_for_one_submission_does_not(self):
        db, conn, rid = self._setup()
        a = db.get_or_create_application(conn, rid, submitted_at="2026-09-07T04:12:05",
                                         is_ack=True, channel="gmail")
        b = db.get_or_create_application(conn, rid, submitted_at="2026-09-07T05:30:00",
                                         is_ack=True, channel="gmail")
        self.assertEqual(a, b)


class TestAtsTenantAddress(unittest.TestCase):
    """Workday hosts every employer on one domain and signs its mail with a product
    name. "AutoNotification workday <autodesk@myworkday.com>" reached the board as a
    company literally called "AutoNotification workday", holding two real Autodesk
    rejections. The tenant in the local part is the only place the employer appears."""

    def test_the_tenant_in_the_address_is_the_employer(self):
        from careerops.classify import classify
        c = classify("Update from Autodesk on 26WD100753 Senior Principal Program Manager",
                     "AutoNotification workday <autodesk@myworkday.com>",
                     "We have decided to move forward with other candidates.")
        self.assertEqual(c.company, "Autodesk")
        self.assertEqual(c.event_type, "rejection")

    def test_the_vendors_own_name_is_never_the_company(self):
        from careerops.classify import sender_name
        for n in ("AutoNotification workday <x@myworkday.com>",
                  "Workday AutoNotification <x@myworkday.com>",
                  "myworkday <x@myworkday.com>"):
            self.assertIsNone(sender_name(n), n)

    def test_a_generic_mailbox_is_not_a_tenant(self):
        from careerops.classify import company_from_ats_address
        for a in ("noreply@myworkday.com", "info@myworkday.com", "jobs@myworkday.com"):
            self.assertIsNone(company_from_ats_address(a), a)

    def test_non_workday_addresses_are_left_alone(self):
        from careerops.classify import company_from_ats_address
        self.assertIsNone(company_from_ats_address("no-reply@us.greenhouse-mail.io"))


class TestRoleTitleKeepsItsOwnWords(unittest.TestCase):
    def test_a_named_company_suffix_is_stripped(self):
        from careerops.classify import strip_company_suffix as f
        self.assertEqual(f("Senior Principal Program Manager, GTM PMO at Autodesk", "Autodesk"),
                         "Senior Principal Program Manager, GTM PMO")

    def test_titles_that_merely_end_in_at_something_survive(self):
        """A general "at <Capitalized Words>" rule quietly eats real titles."""
        from careerops.classify import strip_company_suffix as f
        for t in ("Program Manager, Analytics at Scale", "Engineering Manager, Trust at Work",
                  "Director, Data at Rest"):
            self.assertEqual(f(t, "Autodesk"), t)
            self.assertEqual(f(t, None), t)


class TestRelocation(unittest.TestCase):
    """Outside the home market, all three conditions must hold: right coast, senior
    title, and pay far enough above the local floor to cover moving a family."""

    T = ["business operations", "revenue operations", "program manager"]
    L = ["denver", "colorado", "remote", "united states", "us"]
    R = {"locations": ["california", "washington", "oregon", ", ca", "san francisco", "seattle"],
         "comp_floor": 250000,
         "seniority": ["director", "head of", "principal", "staff", "senior manager"]}

    def _m(self, title, loc, comp):
        from careerops.discover import matches
        return matches({"title": title, "location": loc}, self.T, self.L, (), self.R, comp)

    def test_senior_west_coast_role_above_the_bar_passes(self):
        self.assertTrue(self._m("Director, Business Operations", "San Francisco, CA", 310000))

    def test_pay_below_the_relocation_bar_is_not_worth_moving_for(self):
        self.assertFalse(self._m("Director, Business Operations", "San Francisco, CA", 190000))

    def test_an_unpublished_range_does_not_qualify(self):
        """CA and WA require pay ranges in postings, so a missing one is a real signal.
        Relocating on an unverified guess is exactly the wrong trade."""
        self.assertFalse(self._m("Director, Business Operations", "San Francisco, CA", None))

    def test_a_non_senior_title_does_not_qualify_however_well_paid(self):
        self.assertFalse(self._m("Business Operations Manager", "San Francisco, CA", 310000))

    def test_the_wrong_coast_never_qualifies(self):
        self.assertFalse(self._m("Director, Business Operations", "Austin, Texas", 310000))
        self.assertFalse(self._m("Director, Business Operations", "New York, NY", 400000))

    def test_home_market_and_remote_are_unaffected(self):
        self.assertTrue(self._m("Business Operations Manager", "Denver, CO", 180000))
        self.assertTrue(self._m("Business Operations Manager", "Remote, US", None))


class TestLocationWordBoundaries(unittest.TestCase):
    def test_us_does_not_match_inside_a_city_name(self):
        """"us" is the catch-all for nationwide postings. As a raw substring it hides
        inside Austin, Houston, Columbus and Tuscaloosa, admitting those as home market."""
        from careerops.discover import _loc_hit
        for city in ("austin, texas", "houston, tx", "columbus, oh", "tuscaloosa, al"):
            self.assertFalse(_loc_hit(city, ["us", "remote", "denver"]), city)

    def test_real_nationwide_and_home_postings_still_match(self):
        from careerops.discover import _loc_hit
        for loc in ("united states", "remote, us", "us - remote", "denver, co"):
            self.assertTrue(_loc_hit(loc, ["us", "united states", "remote", "denver"]), loc)

    def test_punctuated_patterns_keep_their_literal_form(self):
        from careerops.discover import _loc_hit
        self.assertTrue(_loc_hit("san mateo, ca", [", ca"]))
        self.assertFalse(_loc_hit("chicago, il", [", ca"]))


class TestAckWithNoRoleNamed(unittest.TestCase):
    """"Thank you for applying to InStride Health" names no role. Creating an "Unknown
    role" opens a phantom application beside the submission it acknowledges, leaving one
    row reading applied and another acked for the same thing."""

    def _co(self):
        from careerops import db
        conn = db.connect(":memory:"); db.init(conn)
        return db, conn, db.get_or_create_company(conn, "InStride Health")

    def test_the_single_pending_submission_is_found(self):
        db, conn, cid = self._co()
        rid = db.get_or_create_role(conn, cid, "Chief of Staff")
        aid = db.get_or_create_application(conn, rid, applied_on="2026-09-07",
                                           submitted_at="2026-09-07T12:00:00")
        conn.execute("UPDATE applications SET status='applied' WHERE id=?", (aid,))
        self.assertEqual(db.awaiting_ack(conn, cid, "2026-09-07T17:36:00"), rid)

    def test_two_pending_submissions_are_ambiguous_so_it_declines(self):
        """Attaching to the wrong one is worse than an Unknown row: it marks the wrong
        submission live and leaves the real one looking ignored."""
        db, conn, cid = self._co()
        for t in ("Chief of Staff", "Head of Operations"):
            rid = db.get_or_create_role(conn, cid, t)
            aid = db.get_or_create_application(conn, rid, applied_on="2026-09-07",
                                               submitted_at="2026-09-07T12:00:00")
            conn.execute("UPDATE applications SET status='applied' WHERE id=?", (aid,))
        self.assertIsNone(db.awaiting_ack(conn, cid, "2026-09-07T17:36:00"))

    def test_a_submission_that_already_has_its_ack_is_not_a_candidate(self):
        db, conn, cid = self._co()
        rid = db.get_or_create_role(conn, cid, "Chief of Staff")
        aid = db.get_or_create_application(conn, rid, applied_on="2026-09-07",
                                           submitted_at="2026-09-07T12:00:00")
        conn.execute("UPDATE applications SET status='applied' WHERE id=?", (aid,))
        db.add_event(conn, aid, "2026-09-07T13:00:00", "ack", "gmail", external_id="x1")
        self.assertIsNone(db.awaiting_ack(conn, cid, "2026-09-07T17:36:00"))

    def test_an_old_submission_is_out_of_the_window(self):
        db, conn, cid = self._co()
        rid = db.get_or_create_role(conn, cid, "Chief of Staff")
        aid = db.get_or_create_application(conn, rid, applied_on="2026-01-02",
                                           submitted_at="2026-01-02T12:00:00")
        conn.execute("UPDATE applications SET status='applied' WHERE id=?", (aid,))
        self.assertIsNone(db.awaiting_ack(conn, cid, "2026-09-07T17:36:00"))
