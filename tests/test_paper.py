import json
import tempfile
import unittest
from pathlib import Path

from polymarket_app.calibration import corrected_probability
from polymarket_app.database import Database
from polymarket_app.paper import scan


class PaperPositionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.initialize()
        self.market = {
            "conditionId": "paper", "question": "Paper market", "active": True,
            "closed": False, "endDate": "2030-01-01T00:00:00Z",
            "outcomes": json.dumps(["Yes", "No"]), "outcomePrices": json.dumps(["0.5", "0.5"]),
            "clobTokenIds": json.dumps(["paper-yes", "paper-no"]), "feeSchedule": {"rate": 0},
        }
        self.database.upsert_market(self.market)
        self.database.save_order_book("paper-yes", {"bids": [], "asks": [{"price": "0.50", "size": "1000"}]})

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_correction_smooths_a_price_bin(self) -> None:
        estimate = corrected_probability(
            [{"price": "0.5", "outcome_index": 0}] * 3,
            0.5,
        )
        self.assertEqual(estimate["samples"], 3)
        self.assertAlmostEqual(estimate["probability"], 0.8)

    def test_paper_scan_needs_enough_training_examples(self) -> None:
        report = scan(self.database, stake=100, min_edge=0.01, min_samples=1)
        self.assertEqual(report["created"], 0)
        self.assertEqual(report["positions"], 0)

    def test_resolved_paper_position_is_settled(self) -> None:
        saved = self.database.save_paper_position(
            {"condition_id": "paper", "token_id": "paper-yes", "side_index": 0,
             "model_name": "test", "predicted_probability": 0.6, "entry_cost": 0.5,
             "shares": 20, "stake": 10, "edge": 0.1}
        )
        self.assertTrue(saved)
        resolved = dict(self.market, active=False, closed=True, outcomePrices=json.dumps(["1", "0"]))
        self.database.upsert_market(resolved)
        self.assertEqual(self.database.settle_paper_positions(), 1)
        summary = self.database.paper_position_summary()
        self.assertEqual(summary["resolved_positions"], 1)
        self.assertAlmostEqual(summary["profit"], 10)


if __name__ == "__main__":
    unittest.main()
