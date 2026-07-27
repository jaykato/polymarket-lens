import unittest

from polymarket_app.history import (
    DEFAULT_RANGE_KEY,
    RANGES,
    range_options,
    resolve_range,
)


class HistoryRangeTest(unittest.TestCase):
    def test_known_keys_resolve_to_their_own_resolution(self) -> None:
        self.assertEqual(resolve_range("1d").fidelity, 5)
        self.assertEqual(resolve_range("1w").fidelity, 60)
        self.assertEqual(resolve_range("max").interval, "max")

    def test_keys_are_case_and_whitespace_tolerant(self) -> None:
        self.assertEqual(resolve_range(" MAX ").key, "max")

    def test_unknown_and_missing_keys_fall_back_to_the_default(self) -> None:
        """クエリ文字列は検証なしで届くため、未知の値でも例外を出さない。"""
        for value in ("", None, "1y", "'; DROP TABLE markets; --"):
            self.assertEqual(resolve_range(value).key, DEFAULT_RANGE_KEY)

    def test_only_the_longest_range_is_unbounded(self) -> None:
        self.assertIsNone(RANGES["max"].window_seconds)
        for key in ("1d", "1w", "1m"):
            self.assertGreater(RANGES[key].window_seconds, 0)

    def test_options_expose_every_range_to_the_ui(self) -> None:
        self.assertEqual(
            [option["key"] for option in range_options()], list(RANGES)
        )


if __name__ == "__main__":
    unittest.main()
