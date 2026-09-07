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
