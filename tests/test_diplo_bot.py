import datetime as dt
import unittest

from diplo_bot import Target, month_url, day_url, parse_date, parse_day_from_url, target_accepts


class PureLogicTests(unittest.TestCase):
    def setUp(self):
        self.target = Target(
            location_code="lond",
            realm_id="1614",
            category_id="4019",
            start_date=dt.date(2026, 8, 15),
            end_date=dt.date(2026, 9, 30),
        )

    def test_parse_supported_dates(self):
        self.assertEqual(parse_date("2026-08-21"), dt.date(2026, 8, 21))
        self.assertEqual(parse_date("21.08.2026"), dt.date(2026, 8, 21))
        self.assertEqual(parse_date("21/08/2026"), dt.date(2026, 8, 21))

    def test_month_url_keeps_route_ids(self):
        url = month_url(self.target, dt.date(2026, 8, 1))
        self.assertIn("locationCode=lond", url)
        self.assertIn("realmId=1614", url)
        self.assertIn("categoryId=4019", url)
        self.assertIn("dateStr=01.08.2026", url)

    def test_day_url_round_trip(self):
        day = dt.date(2026, 8, 21)
        url = day_url(self.target, day)
        self.assertEqual(parse_day_from_url(url), day)

    def test_date_window(self):
        self.assertFalse(target_accepts(self.target, dt.date(2026, 8, 14)))
        self.assertTrue(target_accepts(self.target, dt.date(2026, 8, 15)))
        self.assertTrue(target_accepts(self.target, dt.date(2026, 9, 30)))
        self.assertFalse(target_accepts(self.target, dt.date(2026, 10, 1)))


if __name__ == "__main__":
    unittest.main()
