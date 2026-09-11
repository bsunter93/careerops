import unittest

from careerops.discover import comp_tiers, extract_comp


class TieredPayBands(unittest.TestCase):
    """A posting with pay zones has no single range, and min/max across all of them
    invents one nobody is eligible for.

    Databricks publishes four zones from $130,300 to $223,950. Scanning for the
    smallest and largest number stored exactly that span, which is a band no candidate
    can be offered: the floor belongs to the cheapest zone and the ceiling to the most
    expensive. It was a harmless inaccuracy while nothing read the field, and stopped
    being harmless the moment the fit scorer started reading it.

    The stored band is now the mean of the zone floors and the mean of the zone
    ceilings, so its midpoint is the midpoint of the zone midpoints.
    """

    STRIPPED = ("Zone 1 Pay Range$162,900—$223,950 USD"
                "Zone 2 Pay Range$146,600—$201,500 USD"
                "Zone 3 Pay Range$138,500—$190,400 USD"
                "Zone 4 Pay Range$130,300—$179,200 USD")
    MARKUP = ('<div class="title">Zone 1 Pay Range</div><div class="pay-range">'
              '<span>$181,100</span><span class="divider">&mdash;</span><span>$249,050 USD</span></div>'
              '<div class="title">Zone 2 Pay Range</div><div class="pay-range">'
              '<span>$163,000</span><span class="divider">&mdash;</span><span>$224,200 USD</span></div>')
    PROSE = ("We offer a base compensation range that varies by location tier: "
             "- Tier 1: approximately $175,000 - $233,000 per year "
             "- Tier 2: approximately $166,250 - $221,350 per year "
             "- Tier 3: approximately $157,500 - $209,700 per year")
    PLAIN = "The salary range for this position is $180,000 - $220,000 annually."
    NATIONAL = ("Expected Pay Range: The U.S. pay range for this position is "
                "$159,900 -- $325,900 annually. In California, the pay range is "
                "$225,100 - $325,900.")

    def test_finds_every_zone(self):
        self.assertEqual(len(comp_tiers(self.STRIPPED)), 4)

    def test_finds_zones_through_markup(self):
        self.assertEqual(comp_tiers(self.MARKUP), [(181100, 249050), (163000, 224200)])

    def test_finds_tiers_written_as_prose(self):
        self.assertEqual(len(comp_tiers(self.PROSE)), 3)

    def test_midpoint_is_the_midpoint_of_the_zone_midpoints(self):
        lo, hi = extract_comp(self.STRIPPED)
        mids = [(a + b) / 2 for a, b in comp_tiers(self.STRIPPED)]
        self.assertAlmostEqual((lo + hi) / 2, sum(mids) / len(mids), delta=1)
        # 795,050 / 4 lands on .5 exactly; round() breaks that tie to even
        self.assertEqual((lo, hi), (144575, 198762))

    def test_stored_band_no_longer_spans_all_zones(self):
        lo, hi = extract_comp(self.STRIPPED)
        self.assertGreater(lo, 130300, "floor should not come from the cheapest zone")
        self.assertLess(hi, 223950, "ceiling should not come from the priciest zone")

    def test_untiered_posting_is_untouched(self):
        self.assertEqual(comp_tiers(self.PLAIN), [])
        self.assertEqual(extract_comp(self.PLAIN), (180000, 220000))

    def test_one_national_range_with_a_state_carve_out_is_not_tiered(self):
        """Adobe states a US range then a California one. That is not a zone table,
        and averaging it would understate the headline range the employer published."""
        self.assertEqual(comp_tiers(self.NATIONAL), [])
