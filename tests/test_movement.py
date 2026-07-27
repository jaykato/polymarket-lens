import unittest

from polymarket_app.movement import DAY_SECONDS, summarize_movement


def series(values: list[tuple[int, float]]) -> list[dict[str, object]]:
    return [{"timestamp_utc": t, "price": str(p)} for t, p in values]


class MovementTest(unittest.TestCase):
    def test_too_few_points_report_unavailable(self) -> None:
        self.assertFalse(summarize_movement([])["available"])
        self.assertFalse(summarize_movement(series([(0, 0.5)]))["available"])

    def test_change_high_and_low_come_from_the_observed_series(self) -> None:
        result = summarize_movement(
            series([(0, 0.20), (3600, 0.35), (7200, 0.10), (10800, 0.25)])
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["observations"], 4)
        self.assertEqual(result["first_percent"], 20.0)
        self.assertEqual(result["last_percent"], 25.0)
        self.assertEqual(result["change_points"], 5.0)
        self.assertEqual(result["high"], {"percent": 35.0, "timestamp_utc": 3600})
        self.assertEqual(result["low"], {"percent": 10.0, "timestamp_utc": 7200})
        self.assertEqual(result["range_points"], 25.0)

    def test_unordered_input_is_sorted_before_analysis(self) -> None:
        result = summarize_movement(
            series([(7200, 0.30), (0, 0.10), (3600, 0.20)])
        )
        self.assertEqual(result["first_percent"], 10.0)
        self.assertEqual(result["last_percent"], 30.0)
        self.assertEqual(result["change_points"], 20.0)

    def test_24h_change_needs_a_point_that_far_back(self) -> None:
        short = summarize_movement(series([(0, 0.40), (3600, 0.45)]))
        self.assertIsNone(short["change_24h_points"])

        spanning = summarize_movement(
            series([(0, 0.40), (DAY_SECONDS, 0.50), (DAY_SECONDS + 3600, 0.55)])
        )
        # 24時間前ちょうどではなく、それ以前で最も新しい点を基準にする。
        self.assertEqual(spanning["change_24h_points"], 15.0)

    def test_largest_move_reports_direction_and_duration(self) -> None:
        result = summarize_movement(
            series([(0, 0.50), (3600, 0.52), (7200, 0.40), (10800, 0.41)])
        )
        self.assertEqual(result["largest_move"]["points"], -12.0)
        self.assertEqual(result["largest_move"]["from_utc"], 3600)
        self.assertEqual(result["largest_move"]["to_utc"], 7200)
        self.assertEqual(result["largest_move"]["hours"], 1.0)

    def test_a_flat_series_is_steady_and_a_swinging_one_is_volatile(self) -> None:
        hour = 3600
        flat = summarize_movement(series([(i * hour, 0.50) for i in range(24)]))
        self.assertEqual(flat["daily_swing_points"], 0.0)
        self.assertEqual(flat["stability"]["level"], "steady")

        swinging = summarize_movement(
            series([(i * hour, 0.20 + 0.25 * (i % 2)) for i in range(24)])
        )
        self.assertGreater(swinging["daily_swing_points"], 6.0)
        self.assertEqual(swinging["stability"]["level"], "volatile")

    def test_swing_needs_several_intervals_before_it_is_reported(self) -> None:
        result = summarize_movement(series([(0, 0.40), (3600, 0.45)]))
        self.assertIsNone(result["daily_swing_points"])
        self.assertEqual(result["stability"]["level"], "unknown")

    def test_repeated_timestamps_do_not_divide_by_zero(self) -> None:
        result = summarize_movement(
            series([(0, 0.40), (0, 0.42), (3600, 0.44), (7200, 0.46), (10800, 0.48)])
        )
        self.assertTrue(result["available"])
        self.assertIsNotNone(result["daily_swing_points"])

    def test_unparsable_prices_do_not_raise(self) -> None:
        history = [
            {"timestamp_utc": 0, "price": "0.4"},
            {"timestamp_utc": 3600, "price": None},
            {"timestamp_utc": 7200, "price": "not a number"},
        ]
        result = summarize_movement(history)
        self.assertTrue(result["available"])
        self.assertEqual(result["low"]["percent"], 0.0)


if __name__ == "__main__":
    unittest.main()
