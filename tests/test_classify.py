import sys, pathlib, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from careerops.classify import classify, _clean_role, _clean_company

class TestClassify(unittest.TestCase):
    def test_noise_is_not_an_application(self):
        for s in ["Security code for your application to Affirm",
                  "Verify your candidate account",
                  "Reset your password for your candidate account"]:
            self.assertEqual(classify(s, "no-reply@us.greenhouse-mail.io").event_type, "noise", s)

    def test_ats_vendor_is_never_the_company(self):
        c = classify("Databricks!", "no-reply@us.greenhouse-mail.io")
        self.assertEqual(c.company, "Databricks")

    def test_subject_extraction(self):
        c = classify("We've received your application for Operations Enablement Manager at Affirm",
                     "no-reply@us.greenhouse-mail.io")
        self.assertEqual(c.company, "Affirm")
        self.assertEqual(c.role, "Operations Enablement Manager")
        self.assertEqual(c.event_type, "ack")
        self.assertGreaterEqual(c.confidence, 0.9)

    def test_interview_beats_ack(self):
        c = classify("Anthropic invitation to interview for Fellow program", "x@anthropic.com")
        self.assertEqual(c.event_type, "interview_invite")

    def test_rejection_detected_with_trigger(self):
        c = classify("Update on your application", "x@stripe.com",
                     "Unfortunately we have decided to move forward with other candidates.")
        self.assertEqual(c.event_type, "rejection")
        self.assertIsNotNone(c.trigger)

    def test_low_confidence_goes_to_review(self):
        self.assertTrue(classify("for Job #26", "HumanResources@ustechsolutions.com").needs_review)

    def test_blacklist_scoped_to_subject_not_body(self):
        # ATS footers say "subscription"; that must not discard a real ack
        c = classify("We've received your application for Ops Lead at Ramp", "no-reply@ashbyhq.com",
                     "Manage your subscription preferences. Payment info never requested.")
        self.assertEqual(c.event_type, "ack")

class TestStatus(unittest.TestCase):
    def test_status_never_regresses(self):
        from careerops import db
        conn = db.connect(":memory:"); db.init(conn)
        cid = db.get_or_create_company(conn, "Acme")
        rid = db.get_or_create_role(conn, cid, "Ops Lead")
        aid = db.get_or_create_application(conn, rid, applied_on="2026-01-01")
        db.add_event(conn, aid, "2026-01-02", "interview_invite", "test")
        db.add_event(conn, aid, "2026-01-03", "ack", "test")   # late ack must not downgrade
        self.assertEqual(db.recompute_status(conn, aid), "interview")

    def test_sibling_roles_are_independent(self):
        from careerops import db
        conn = db.connect(":memory:"); db.init(conn)
        cid = db.get_or_create_company(conn, "Affirm")
        a1 = db.get_or_create_application(conn, db.get_or_create_role(conn, cid, "GTM Ops Lead"))
        a2 = db.get_or_create_application(conn, db.get_or_create_role(conn, cid, "Ops Enablement Mgr"))
        db.add_event(conn, a1, "2026-02-01", "rejection", "test")
        self.assertEqual(db.recompute_status(conn, a1), "rejected")
        self.assertEqual(db.recompute_status(conn, a2), "applied")  # unaffected

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSubmissions(unittest.TestCase):
    """Re-applying to the same role is a separate submission, keyed by timestamp."""

    def _fixture(self):
        from careerops import db
        conn = db.connect(":memory:"); db.init(conn)
        cid = db.get_or_create_company(conn, "Stripe")
        rid = db.get_or_create_role(conn, cid, "Company Strategy & Operations")
        return conn, rid

    def test_reapply_creates_a_second_application(self):
        from careerops import db
        conn, rid = self._fixture()
        a1 = db.get_or_create_application(conn, rid, submitted_at="2026-09-01 20:33", is_ack=True)
        a2 = db.get_or_create_application(conn, rid, submitted_at="2026-09-05 21:40", is_ack=True)
        self.assertNotEqual(a1, a2)

    def test_duplicate_ack_for_one_submission_does_not(self):
        from careerops import db
        conn, rid = self._fixture()
        a1 = db.get_or_create_application(conn, rid, submitted_at="2026-09-01 20:33", is_ack=True)
        a2 = db.get_or_create_application(conn, rid, submitted_at="2026-09-01 20:41", is_ack=True)
        self.assertEqual(a1, a2)

    def test_later_events_attach_to_the_newest_submission(self):
        from careerops import db
        conn, rid = self._fixture()
        db.get_or_create_application(conn, rid, submitted_at="2026-09-01 20:33", is_ack=True)
        a2 = db.get_or_create_application(conn, rid, submitted_at="2026-09-05 21:40", is_ack=True)
        landed = db.get_or_create_application(conn, rid)          # e.g. a rejection
        self.assertEqual(landed, a2)

    def test_submissions_carry_independent_status(self):
        from careerops import db
        conn, rid = self._fixture()
        a1 = db.get_or_create_application(conn, rid, submitted_at="2026-09-01 20:33", is_ack=True)
        a2 = db.get_or_create_application(conn, rid, submitted_at="2026-09-05 21:40", is_ack=True)
        db.add_event(conn, a1, "2026-09-02", "rejection", "test")
        db.add_event(conn, a2, "2026-09-05", "ack", "test")
        self.assertEqual(db.recompute_status(conn, a1), "rejected")
        self.assertEqual(db.recompute_status(conn, a2), "acked")


class TestSharedRoleAssignment(unittest.TestCase):
    """Assigning one application must never relabel its siblings."""

    def test_assign_does_not_rename_a_shared_role(self):
        from careerops import db
        from careerops.resolve import set_identity
        conn = db.connect(":memory:"); db.init(conn)
        cid = db.get_or_create_company(conn, "Google")
        rid = db.get_or_create_role(conn, cid, "Unknown")
        a1 = db.get_or_create_application(conn, rid, submitted_at="2026-09-01", is_ack=True)
        a2 = db.get_or_create_application(conn, rid, submitted_at="2026-09-05", is_ack=True)
        set_identity(conn, a2, "Google", "Startups Performance Lead")
        t1 = conn.execute("""SELECT r.title FROM applications a JOIN roles r ON r.id=a.role_id
                             WHERE a.id=?""", (a1,)).fetchone()["title"]
        t2 = conn.execute("""SELECT r.title FROM applications a JOIN roles r ON r.id=a.role_id
                             WHERE a.id=?""", (a2,)).fetchone()["title"]
        self.assertEqual(t1, "Unknown")                       # sibling untouched
        self.assertEqual(t2, "Startups Performance Lead")


class TestDormancyPolicy(unittest.TestCase):
    """A company's stated review window overrides the default dormancy threshold."""

    def test_default_threshold_applies(self):
        from careerops import db
        conn = db.connect(":memory:"); db.init(conn)
        cid = db.get_or_create_company(conn, "SomeStartup")
        rid = db.get_or_create_role(conn, cid, "Ops Lead")
        aid = db.get_or_create_application(conn, rid, applied_on="2026-07-01")
        db.add_event(conn, aid, "2026-07-01", "ack", "test")
        db.recompute_status(conn, aid)
        self.assertEqual(conn.execute("SELECT activity FROM applications WHERE id=?",
                                      (aid,)).fetchone()["activity"], "dormant")

    def test_google_gets_its_longer_window(self):
        from careerops import db
        import datetime as dt
        conn = db.connect(":memory:"); db.init(conn)
        cid = db.get_or_create_company(conn, "Google")
        rid = db.get_or_create_role(conn, cid, "Strategic Programs Senior Manager, Cloud GTM")
        recent = (dt.date.today() - dt.timedelta(days=31)).isoformat()
        aid = db.get_or_create_application(conn, rid, applied_on=recent)
        db.add_event(conn, aid, recent, "ack", "test")
        db.recompute_status(conn, aid)
        # 31 days is dormant by the 21-day default but live inside Google's 8-week window
        self.assertEqual(conn.execute("SELECT activity FROM applications WHERE id=?",
                                      (aid,)).fetchone()["activity"], "active")


class TestApplicationCreation(unittest.TestCase):
    """A resolvable company name is not evidence of an application."""

    def test_product_notification_creates_no_application(self):
        from careerops.classify import classify, creates_application
        c = classify("Here is your app Master Job Application Pipeline",
                     "AppSheet <notify@appsheet.com>",
                     "'Master Job Application Pipeline' is ready. Install it on your device.")
        self.assertFalse(creates_application(c))

    def test_real_ack_does(self):
        from careerops.classify import classify, creates_application
        c = classify("Thanks for applying to Stripe!", "no-reply@stripe.com",
                     "submitting your application for the Program Manager, GTM Planning role!")
        self.assertTrue(creates_application(c))

    def test_low_confidence_ack_does_not(self):
        from careerops.classify import classify, creates_application, MIN_APPLICATION_CONF
        c = classify("Acme", "x@acme.com", "")
        if c.confidence < MIN_APPLICATION_CONF:
            self.assertFalse(creates_application(c))


class TestSenderIdentity(unittest.TestCase):
    """The From display name is the most reliable company signal on ATS mail."""

    def test_display_name_beats_ats_domain(self):
        from careerops.classify import classify
        c = classify("Thanks for applying to Scaled Operations Program Manager (Trust and Safety)!",
                     "Match Group <no-reply@hire.lever.co>",
                     "We're excited to have your application for Scaled Operations Program Manager.")
        self.assertEqual(c.company, "Match Group")
        self.assertIn("Scaled Operations Program Manager", c.role or "")

    def test_vendor_display_names_are_ignored(self):
        from careerops.classify import sender_name
        for s in ["Greenhouse <no-reply@greenhouse.io>", "no-reply@stripe.com",
                  "Talent Acquisition <x@acme.com>"]:
            self.assertIn(sender_name(s), (None, "Greenhouse", ""))

    def test_display_name_strips_careers_suffix(self):
        from careerops.classify import sender_name
        self.assertEqual(sender_name("Google Careers <no-reply@google.com>"), "Google")


class TestStaffingAndIdentity(unittest.TestCase):
    """Regressions from widening the Gmail query. Broadening the net let in a class
    of mail that uses real interview language but represents no application."""

    def test_contract_body_shop_is_noise(self):
        c = classify(
            "Interview next week/ IT Program Manager: Mountain View, CA-Hybrid-3x.week",
            "Rhythm Arora <rhythm.arora@agamasolutions.com>",
            "urgent requirement as below. W2 or C2C-if you have own corp. Duration: 18+ months Contract")
        self.assertEqual(c.event_type, "noise")

    def test_right_to_represent_is_noise(self):
        c = classify("RTR :: Position with client Ebay for the Business Operation Analyst",
                     "Venkata <venkata@dewsoftware.com>",
                     "Please confirm the below RTR. Hourly Rate: $50")
        self.assertEqual(c.event_type, "noise")

    def test_real_interview_survives_staffing_filter(self):
        """The filter must not eat genuine invites; that is the expensive direction."""
        for subj, snd in [
            ("You're invited to interview with VSCO!", "VSCO <no-reply@ashbyhq.com>"),
            ("Interview Availability: Candidate | Staff, Technology Operations | Walmart",
             "Walmart Recruiting <noreply@walmart.com>"),
        ]:
            self.assertEqual(classify(subj, snd, "Please pick a time.").event_type,
                             "interview_invite", subj)

    def test_gem_is_a_vendor_not_an_employer(self):
        """An Anthropic rejection came from appreview.gem.com and was filed under 'Gem'."""
        c = classify("Anthropic Follow-Up for [Pipeline] Product Manager, Monetization",
                     "Anthropic Recruiting <no-reply@appreview.gem.com>",
                     "we have decided not to move forward with your application")
        self.assertEqual(c.company, "Anthropic")
        self.assertEqual(c.event_type, "rejection")

    def test_bracket_tag_stripped_from_role(self):
        """'[Pipeline] Product Manager' forked one application into two."""
        self.assertEqual(_clean_role("[Pipeline] Product Manager, Monetization"),
                         "Product Manager, Monetization")

    def test_confidential_is_not_a_company(self):
        for name in ("Confidential", "confidential", "Undisclosed", "Stealth"):
            self.assertIsNone(_clean_company(name), name)


class TestAckBlocksPromotion(unittest.TestCase):
    """A definitive acknowledgement subject blocks promotion past 'acked'.

    ATS acks routinely contain "we will be reaching out to candidates" and "our
    recruiter will review your application". Trusting those in a body promoted five
    acknowledgements to in_process and inflated the advance rate.
    """

    def test_ack_subject_blocks_recruiter_outreach(self):
        for subj, body in [
            ("Thank you for applying to Cresta",
             "we will be reaching out to candidates whose qualifications best match"),
            ("\U0001f44b Thank you for applying to Whatnot!",
             "our recruiter will review your application shortly"),
            ("Thank You for Applying to the Program Manager role",
             "a recruiter will be in touch if there is a match"),
        ]:
            self.assertEqual(classify(subj, "no-reply@greenhouse-mail.io", body).event_type,
                             "ack", subj)

    def test_genuine_recruiter_outreach_survives(self):
        c = classify("Included Health - Intro to Recruiter", "hm@includedhealth.com",
                     "I wanted to connect you with our recruiter for this role")
        self.assertEqual(c.event_type, "recruiter_outreach")

    def test_ack_subject_still_blocks_interviews(self):
        c = classify("Thanks for applying to DoorDash", "no-reply@greenhouse-mail.io",
                     "we will be in touch about next steps and your availability")
        self.assertEqual(c.event_type, "ack")


class TestClosedRequisitions(unittest.TestCase):
    """A cancelled or filled req is a terminal outcome, not silence and not progress.

    Six of these sat as 'unresolved' and one, NVIDIA, had been promoted to
    recruiter_outreach because "we are reaching out to inform you that we are no longer
    recruiting" tripped a recruiter pattern. A closed req read as forward movement.
    """

    def test_closure_language_is_a_rejection(self):
        for subj, body in [
            ("Thank you from NVIDIA",
             "We are reaching out to inform you that we are no longer recruiting for the role."),
            ("Update on Your Application at Harvey", "the position has been filled"),
            ("Cloudflare Application Update", "we are no longer hiring for this role"),
            ("Update on your application", "this requisition was cancelled"),
        ]:
            self.assertEqual(classify(subj, "no-reply@greenhouse-mail.io", body).event_type,
                             "rejection", subj)

    def test_reaching_out_alone_is_not_recruiter_outreach(self):
        """Ordinary English, and it appears inside closure and rejection mail."""
        c = classify("Update on your application", "no-reply@greenhouse-mail.io",
                     "We are reaching out to inform you of a change to this role.")
        self.assertNotEqual(c.event_type, "recruiter_outreach")

    def test_real_sourcing_still_reads_as_outreach(self):
        c = classify("Quick question about a role", "jane@acme.com",
                     "I came across your profile and would love to connect about an opening")
        self.assertEqual(c.event_type, "recruiter_outreach")
