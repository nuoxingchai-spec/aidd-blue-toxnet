import tempfile
import unittest
from pathlib import Path

from backend.training_data import DATASETS, download_source, normalize_rows, read_tdc_rows


def row(smiles, label, endpoint="ames", source_row=2):
    return {"smiles": smiles, "label": label, "endpoint": endpoint, "source_id": "source", "source_row": source_row}


class TrainingDataTests(unittest.TestCase):
    def test_equivalent_structures_remove_duplicates(self):
        retained, quarantined, duplicates, counts = normalize_rows([row("CCO", "1"), row("OCC", "1.0", source_row=3)])
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["smiles"], "CCO")
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(quarantined, [])
        self.assertEqual(counts["ames"]["positiveRows"], 1)

    def test_conflicts_remove_every_row_in_group(self):
        retained, quarantined, duplicates, counts = normalize_rows([row("CCO", "0"), row("OCC", "1"), row("CCO", "0"), row("CCN", "1")])
        self.assertEqual([item["smiles"] for item in retained], ["CCN"])
        self.assertEqual(len(quarantined), 3)
        self.assertEqual(duplicates, [])
        self.assertEqual(counts["ames"]["conflictingStructureGroups"], 1)

    def test_missing_labels_are_never_imputed(self):
        retained, quarantined, _, _ = normalize_rows([row("CCO", ""), row("CCN", "nan"), row("CCC", True)])
        self.assertEqual(retained, [])
        self.assertEqual(len(quarantined), 3)

    def test_invalid_and_wildcard_structures_quarantined(self):
        retained, quarantined, _, _ = normalize_rows([row("C1CC", "1"), row("*CC", "0"), row("CCO", "0")])
        self.assertEqual(len(retained), 1)
        self.assertEqual(len(quarantined), 2)

    def test_endpoint_and_stereochemistry_remain_distinct(self):
        retained, quarantined, duplicates, _ = normalize_rows([
            row("N[C@@H](C)C(=O)O", "0"), row("N[C@H](C)C(=O)O", "1"),
            row("N[C@@H](C)C(=O)O", "1", endpoint="dili"),
        ])
        self.assertEqual(len(retained), 3)
        self.assertEqual(quarantined, [])
        self.assertEqual(duplicates, [])

    def test_hash_mismatch_and_unknown_csv_contract_reject(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "herg.csv"
            source.write_text("smiles,label\nCCO,1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                download_source(DATASETS[0], Path(directory), offline=True)
            with self.assertRaisesRegex(ValueError, "CSV contract"):
                read_tdc_rows(source, "herg")


if __name__ == "__main__":
    unittest.main()
