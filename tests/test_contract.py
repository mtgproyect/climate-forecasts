from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ForecastContractTests(unittest.TestCase):
    def test_reference_config(self) -> None:
        config = json.loads((ROOT / "config/pronosticos.json").read_text(encoding="utf-8"))
        self.assertEqual(config["count"], 475)
        ids = [item["forecast_reference_id"] for item in config["forecast_references"]]
        self.assertEqual(len(ids), 475)
        self.assertEqual(len(ids), len(set(ids)))

    def test_publication_files(self) -> None:
        files = list((ROOT / "docs/pronosticos").glob("*.json"))
        self.assertEqual(len(files), 475)

    def test_manifest(self) -> None:
        manifest = json.loads((ROOT / "docs/manifiesto.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["counts"]["forecast_references"], 475)
        self.assertEqual(manifest["files"]["forecasts"]["directory"], "pronosticos")
        self.assertEqual(len(manifest["stale"]["forecast_reference_ids"]), 6)


if __name__ == "__main__":
    unittest.main()
