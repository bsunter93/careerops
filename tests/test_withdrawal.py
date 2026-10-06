import unittest

from careerops import db


class WithdrawalTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:"); db.init(self.conn)
        cid = db.get_or_create_company(self.conn, "Acme")
        rid = db.get_or_create_role(self.conn, cid, "Ops Manager")
        self.aid = db.get_or_create_application(self.conn, rid, applied_on="2026-09-01", channel="manual")

    def test_withdrawal_closes_an_interview(self):
        db.add_event(self.conn, self.aid, "2026-09-10", "interview_invite", "manual")
        db.add_event(self.conn, self.aid, "2026-10-06", "withdrawal", "manual")
        self.assertEqual(db.recompute_status(self.conn, self.aid), "withdrawn")

    def test_late_ack_does_not_reopen_a_withdrawal(self):
        db.add_event(self.conn, self.aid, "2026-10-06", "withdrawal", "manual")
        db.add_event(self.conn, self.aid, "2026-10-07", "ack", "manual")
        self.assertEqual(db.recompute_status(self.conn, self.aid), "withdrawn")


if __name__ == "__main__":
    unittest.main()
