import unittest

from polymarket_app.calibration import summarize


class CalibrationTest(unittest.TestCase):
    def test_summary_reports_brier_score_and_probability_bins(self) -> None:
        report = summarize(
            [
                {"price": "0.80", "outcome_index": 0},
                {"price": "0.80", "outcome_index": 1},
                {"price": "0.20", "outcome_index": 1},
            ]
        )
        self.assertEqual(report["observations"], 3)
        self.assertAlmostEqual(report["brier_score"], 0.24)
        high = next(item for item in report["calibration"] if item["from"] == 0.8)
        self.assertEqual(high["count"], 2)
        self.assertEqual(high["yes_rate"], 0.5)

    def test_invalid_rows_do_not_make_a_score(self) -> None:
        report = summarize([{"price": "not-a-number", "outcome_index": 0}])
        self.assertEqual(report["observations"], 0)
        self.assertIsNone(report["brier_score"])


if __name__ == "__main__":
    unittest.main()
