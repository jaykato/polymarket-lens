import json
import tempfile
import unittest
from pathlib import Path

from polymarket_app.consistency import event_cost_sums
from polymarket_app.database import Database


class ConsistencyTest(unittest.TestCase):
    def test_event_sum_uses_best_ask_and_fee(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.db")
            database.initialize()
            for number, ask in ((1, "0.40"), (2, "0.50")):
                market = {
                    "conditionId": f"c{number}", "question": f"Choice {number}", "active": True,
                    "closed": False, "events": [{"id": "event"}], "feesEnabled": False,
                    "outcomes": json.dumps(["Yes", "No"]), "outcomePrices": json.dumps([ask, str(1 - float(ask))]),
                    "clobTokenIds": json.dumps([f"c{number}-yes", f"c{number}-no"]),
                }
                database.upsert_market(market)
                database.save_order_book(f"c{number}-yes", {"bids": [], "asks": [{"price": ask, "size": "100"}]})
            report = event_cost_sums(database)
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["comparison_status"], "unverified_event_membership")
        self.assertAlmostEqual(report[0]["sum_effective_cost"], 0.9)
        self.assertAlmostEqual(report[0]["deviation_points"], -10)


if __name__ == "__main__":
    unittest.main()
