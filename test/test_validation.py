import unittest
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from backend.validation import assess_applicability, evaluate_scores, scaffold_split


class ValidationTests(unittest.TestCase):
    def test_equivalent_structures_deduplicate_and_scaffolds_do_not_leak(self):
        inputs = ["CCO", "OCC", "c1ccccc1", "Cc1ccccc1", "c1ccncc1", "C1CCCCC1", "c1ccoc1"]
        rows = [{"smiles": s, "endpoint": "ames", "label": 1} for s in inputs]
        result = scaffold_split(rows)
        self.assertEqual(result["duplicatesRemoved"], 1)
        ownership = {}
        for split, records in result["splits"].items():
            for row in records:
                self.assertEqual(ownership.setdefault(row["scaffold"], split), split)

    def test_conflicting_equivalent_labels_rejected(self):
        with self.assertRaises(ValueError):
            scaffold_split([{"smiles": "CCO", "endpoint": "ames", "label": 0}, {"smiles": "OCC", "endpoint": "ames", "label": 1}])

    def test_ranking_ties_and_probabilistic_metrics(self):
        rows = [{"smiles": s, "endpoint": "ames", "label": y, "score": p, "split": "test"} for s, y, p in [("CCO", 0, 0.1), ("CCN", 1, 0.8), ("CC", 0, 0.5), ("CCC", 1, 0.5)]]
        result = evaluate_scores(rows, "probability")["endpoints"]["ames"]
        self.assertAlmostEqual(result["auroc"], 0.875)
        self.assertAlmostEqual(result["averagePrecision"], 5 / 6)
        self.assertAlmostEqual(result["brier"], 0.1375)
        self.assertNotIn("brier", evaluate_scores(rows)["endpoints"]["ames"])

    def test_validation_data_cannot_be_reported_as_final_test(self):
        with self.assertRaises(ValueError):
            evaluate_scores([{"smiles": "CCO", "endpoint": "ames", "label": 1, "score": 0.8, "split": "validation"}])

    def test_applicability_requires_training_references(self):
        self.assertEqual(assess_applicability("CCO", [])["status"], "unknown")
        result = assess_applicability("CCO", ["OCC"])
        self.assertAlmostEqual(result["maximumTanimoto"], 1.0)
        self.assertFalse(result["validated"])

    def test_unsplittable_acyclic_dataset_is_explicit(self):
        result = scaffold_split([{"smiles": "C" * size, "endpoint": "ames", "label": size % 2} for size in range(1, 11)])
        self.assertFalse(result["completeThreeWaySplit"])
        self.assertTrue(any("empty split" in warning for warning in result["warnings"]))

    def test_logits_are_ranked_but_not_reported_as_probabilities(self):
        rows = [{"smiles": s, "endpoint": "ames", "label": y, "score": value, "split": "test"} for s, y, value in [("CCO", 0, -3), ("CCN", 1, 2)]]
        result = evaluate_scores(rows)
        self.assertEqual(result["endpoints"]["ames"]["auroc"], 1.0)
        self.assertEqual(result["heldOutStatus"], "declared-not-verified")
        with self.assertRaises(ValueError):
            evaluate_scores(rows, "probability")

    def test_nonfinite_scores_duplicates_single_class_and_no_data(self):
        row = {"smiles": "CCO", "endpoint": "ames", "label": 1, "score": 0.8, "split": "test"}
        for invalid in (float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                evaluate_scores([{**row, "score": invalid}])
        with self.assertRaises(ValueError):
            evaluate_scores([row, {**row, "smiles": "OCC"}])
        self.assertEqual(evaluate_scores([row])["endpoints"]["ames"]["status"], "single-class")
        self.assertEqual(evaluate_scores([])["endpoints"]["ames"]["status"], "no-data")

    def test_manifest_membership_and_scaffold_leakage(self):
        test_row = {"smiles": "c1ccncc1", "endpoint": "ames", "label": 1, "score": 0.8, "split": "test"}
        manifest = {"splits": {"train": [{"smiles": "c1ccccc1", "endpoint": "ames", "label": 0}], "validation": [], "test": [test_row]}}
        self.assertEqual(evaluate_scores([test_row], split_manifest=manifest)["heldOutStatus"], "manifest-membership-and-scaffold-separation-verified")
        with self.assertRaises(ValueError):
            evaluate_scores([{**test_row, "smiles": "CCO"}], split_manifest=manifest)
        manifest["splits"]["train"].append({"smiles": "Cc1ccncc1", "endpoint": "ames", "label": 0})
        with self.assertRaises(ValueError):
            evaluate_scores([test_row], split_manifest=manifest)

    def test_cli_bad_scores_missing_columns_and_input_overwrite_are_readable(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "validate_research.py"
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "source.csv", Path(directory) / "result.json"
            for content in ("smiles,endpoint,label,score,split\nCCO,ames,1,bad,test\n", "smiles,endpoint,label,split\nCCO,ames,1,test\n"):
                source.write_text(content, encoding="utf-8")
                result = subprocess.run([sys.executable, str(script), "evaluate", str(source), str(output), "--dataset-version", "test-only", "--model-version", "fixture-only"], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
            original = source.read_bytes()
            result = subprocess.run([sys.executable, str(script), "split", str(source), str(source), "--dataset-version", "test-only"], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
