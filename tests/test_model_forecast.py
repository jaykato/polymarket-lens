import unittest

from polymarket_app.model_forecast import infer_interval_seconds, regularize


class ModelForecastTest(unittest.TestCase):
    def test_regularize_uses_only_current_or_earlier_observations(self) -> None:
        points = [
            {"timestamp_utc": 100, "price": "0.2"},
            {"timestamp_utc": 250, "price": "0.4"},
            {"timestamp_utc": 370, "price": "0.6"},
        ]
        result = regularize(points, interval_seconds=60, max_points=6)
        self.assertEqual([item["timestamp_utc"] for item in result], [120, 180, 240, 300, 360])
        self.assertEqual([item["target"] for item in result], [0.2, 0.2, 0.2, 0.4, 0.4])

    def test_regularize_discards_invalid_prices(self) -> None:
        result = regularize(
            [{"timestamp_utc": 60, "price": "bad"}, {"timestamp_utc": 120, "price": "1.2"}],
            60, 8,
        )
        self.assertEqual(result, [])

    def test_interval_is_inferred_from_observation_spacing(self) -> None:
        points = [
            {"timestamp_utc": 0, "price": "0.2"},
            {"timestamp_utc": 305, "price": "0.3"},
            {"timestamp_utc": 610, "price": "0.4"},
        ]
        self.assertEqual(infer_interval_seconds(points), 300)


if __name__ == "__main__":
    unittest.main()
