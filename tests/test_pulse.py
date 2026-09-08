import json
import tempfile
import unittest
from pathlib import Path

import pulse


class FakeClient:
    def fetch(self, url, payload=None):
        if payload:
            method = payload["method"]
            values = {
                "getHealth": "ok",
                "getSlot": 123456,
                "getBlockTime": 1700000000,
                "getEpochInfo": {"epoch": 800, "slotIndex": 50, "slotsInEpoch": 100},
                "getRecentPerformanceSamples": [
                    {"samplePeriodSecs": 60, "numTransactions": 60000, "numNonVoteTransactions": 12000, "numSlots": 150},
                    {"samplePeriod": 60, "numTransactions": 57000, "numNonVoteTransactions": 11000, "numSlots": 148},
                ],
                "getVoteAccounts": {
                    "current": [
                        {"votePubkey": "vote-a", "activatedStake": 600000000000, "commission": 5, "lastVote": 10},
                        {"votePubkey": "vote-b", "activatedStake": 400000000000, "commission": 7, "lastVote": 10},
                    ],
                    "delinquent": [{"votePubkey": "vote-c", "activatedStake": 10000000000, "commission": 9}],
                },
                "getSupply": {"value": {"circulating": 500000000000000000}},
            }
            return {"jsonrpc": "2.0", "result": values[method], "id": method}
        if "v2/chains" in url:
            return [{"name": "Solana", "tvl": 9000000000}]
        if "overview/dexs" in url:
            return {"total24h": 2500000000, "change_1d": 25}
        if "stablecoincharts" in url:
            return [{"date": "1", "totalCirculatingUSD": {"peggedUSD": 10000000000}}]
        if "coingecko" in url:
            return {"solana": {"usd": 150, "usd_24h_change": -12, "last_updated_at": 1700000000}}
        raise AssertionError(url)


class PulseTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "rpc_url": "https://rpc.example",
            "performance_sample_limit": 2,
            "top_validator_limit": 10,
            "anomaly_thresholds": {
                "tps_percent_from_sample_median": 35,
                "delinquent_validator_percent": 5,
                "sol_price_24h_percent": 10,
                "dex_volume_24h_percent": 20,
            },
        }

    def test_collects_and_derives_metrics(self):
        report = pulse.Collector(self.config, FakeClient()).collect()
        self.assertEqual(report["network"]["health"], "ok")
        self.assertEqual(report["network"]["sample_count"], 2)
        self.assertEqual(report["network"]["tps"], 975)
        self.assertEqual(report["network"]["epoch_progress_percent"], 50)
        self.assertEqual(report["validators"]["active_count"], 2)
        self.assertEqual(report["validators"]["nakamoto_coefficient_33_percent"], 1)
        self.assertEqual(report["ecosystem"]["tvl_usd"], 9000000000)
        self.assertTrue(any(a["metric"] == "sol_price_change_24h_percent" for a in report["anomalies"]))
        self.assertTrue(any(a["metric"] == "dex_volume_change_24h_percent" for a in report["anomalies"]))

    def test_outputs_are_valid_and_truthful(self):
        report = pulse.Collector(self.config, FakeClient()).collect()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pulse.write_outputs(report, root)
            parsed = json.loads((root / "reports/latest.json").read_text())
            self.assertEqual(parsed["schema_version"], 1)
            markdown = (root / "reports/latest.md").read_text()
            webpage = (root / "docs/index.html").read_text()
            self.assertIn("Solana Ecosystem Pulse", markdown)
            self.assertIn("Solana DEX volume changed", markdown)
            self.assertIn("Failed sources stay unavailable", webpage)
            self.assertIn("$9B", webpage)
            self.assertNotIn("undefined", webpage)

    def test_empty_inputs_remain_unavailable(self):
        report = pulse.build_report("2026-09-07T00:00:00Z", {}, {}, self.config)
        self.assertIsNone(report["network"]["tps"])
        self.assertIsNone(report["ecosystem"]["sol_price_usd"])
        self.assertEqual(report["anomalies"], [])


if __name__ == "__main__":
    unittest.main()
