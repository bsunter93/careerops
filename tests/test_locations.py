import unittest, json, pathlib
from careerops.discover import _loc_hit

ROOT = pathlib.Path(__file__).resolve().parent.parent


class LocationPatterns(unittest.TestCase):
    """`us` on a word boundary cannot reach `usa`, and nobody noticed for a month.

    Matching locations on word boundaries was itself a fix: a plain substring test found
    `us` inside Austin, Houston, Columbus and Tuscaloosa and admitted every role in those
    cities as home market. The boundary closed that and opened a quieter hole, because a
    posting that says only "USA" then matched nothing at all. Three roles in one
    aggregator sweep were dropped that way, and a dropped role looks exactly like a role
    that was never posted.
    """

    LOCS = ["denver", "boulder", "colorado", "remote", "united states", "us", "usa", "u.s."]

    def test_usa_forms_are_home_market(self):
        for s in ("usa", "anywhere (usa)", "u.s.", "united states", "remote", "denver, co",
                  "boulder, colorado", "remote - us", "us-remote"):
            self.assertTrue(_loc_hit(s, self.LOCS), s)

    def test_cities_containing_us_are_not(self):
        for s in ("austin, tx", "houston, texas", "columbus, ohio", "tuscaloosa, al"):
            self.assertFalse(_loc_hit(s, self.LOCS), s)

    def test_abroad_is_not(self):
        for s in ("london", "paris office", "brügge", "shanghai, china", "berlin"):
            self.assertFalse(_loc_hit(s, self.LOCS), s)

    def test_shipped_config_carries_the_usa_forms(self):
        cfg = ROOT / "config.json"
        if not cfg.exists():
            self.skipTest("config.json is gitignored; present only on a configured machine")
        locs = [l.lower() for l in json.loads(cfg.read_text()).get("locations", [])]
        self.assertIn("usa", locs, "a posting whose location is only 'USA' will be dropped")
