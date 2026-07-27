import unittest

from polymarket_app.analysis import analyze_market, spread_score


class AnalysisTest(unittest.TestCase):
    def test_good_conditions_do_not_claim_profitability(self) -> None:
        result = analyze_market(
            {
                "spread": "0.01",
                "liquidity": "150000",
                "volume": "2000000",
                "best_bid": "0.69",
                "best_ask": "0.70",
            }
        )
        self.assertEqual(result["tone"], "good")
        self.assertEqual(result["headline"], "Favorable trading conditions")
        self.assertIn("not whether", result["disclaimer"])
        self.assertNotIn("buy", result["headline"].lower())
        self.assertNotIn("profit", result["headline"].lower())

    def test_high_friction_conditions_are_flagged(self) -> None:
        result = analyze_market(
            {
                "spread": "0.18",
                "liquidity": "120",
                "volume": "300",
                "best_bid": "0.20",
                "best_ask": "0.38",
            }
        )
        self.assertEqual(result["tone"], "risk")
        self.assertEqual(result["headline"], "High-friction market")
        self.assertEqual(result["metrics"][0]["status"], "Wide price gap")

    def test_direction_describes_crowd_without_recommendation(self) -> None:
        yes = analyze_market({"best_bid": "0.80", "best_ask": "0.84"})
        divided = analyze_market({"best_bid": "0.48", "best_ask": "0.52"})
        no = analyze_market({"best_bid": "0.15", "best_ask": "0.20"})
        self.assertEqual(yes["direction"]["status"], "Crowd leans YES")
        self.assertEqual(divided["direction"]["status"], "Crowd is divided")
        self.assertEqual(no["direction"]["status"], "Crowd leans NO")

    def test_spread_score_declines_as_gap_widens(self) -> None:
        self.assertGreater(spread_score(0.01), spread_score(0.05))
        self.assertGreater(spread_score(0.05), spread_score(0.15))


if __name__ == "__main__":
    unittest.main()
