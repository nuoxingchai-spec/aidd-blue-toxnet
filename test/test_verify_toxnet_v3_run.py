"""CPU receipt-audit fixtures, never real training or benchmark evidence.

Model clients and data source audit are deliberately mocked. Controlled toy
labels/probabilities exist only in temporary directories; no encoder is loaded.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from scripts import verify_toxnet_v3_run as verifier


FIXTURE_PURPOSE = "synthetic-unit-fixture-not-a-completed-research-model"
HASH = "a" * 64


class FourTaskRunVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.experiment = self.root / "unit-fixture-experiment"
        self.data = self.root / "unit-fixture-data"
        self.experiment.mkdir(); self.data.mkdir()
        self.data_manifest = {"datasetVersion": FIXTURE_PURPOSE, "splitSha256": HASH, "fixtureOnly": True}
        self.write(self.data / "manifest.json", self.data_manifest)
        self.samples = [
            {"smiles": "CCO", "split": "validation", "labels": {endpoint: 0 for endpoint in verifier.ENDPOINTS}},
            {"smiles": "CCN", "split": "validation", "labels": {endpoint: 1 for endpoint in verifier.ENDPOINTS}},
        ]
        labels = np.array([[0.] * 4, [1.] * 4])
        self.measured = verifier.evaluate_probabilities(labels, np.ones_like(labels), np.array([[.2] * 4, [.8] * 4]))
        self.summary = {"status": "completed", "fixtureOnly": True, "artifactPurpose": FIXTURE_PURPOSE,
                        **self.data_manifest, "dataManifestSha256": verifier.sha256_file(self.data / "manifest.json"),
                        "seeds": [42, 43, 44], "runs": []}
        self.information = {}
        self.manifests = {}
        for model in ("B0", "B1", "B2"):
            for seed in self.summary["seeds"]:
                name = f"{model}-seed-{seed}"
                directory = self.experiment / name
                directory.mkdir()
                evaluation = {**copy.deepcopy(self.measured), "model": model, "seed": seed,
                              "fixtureOnly": True, "artifactPurpose": FIXTURE_PURPOSE}
                manifest = {"modelVersion": f"unit-fixture-not-real-{name}", "fixtureOnly": True,
                            "training": {"seed": seed, "sourceHashes": {"fixture-never-executed.py": HASH}},
                            "artifacts": {"weights": {"sha256": HASH}}}
                self.write(directory / "manifest.json", manifest)
                rows = [{"smiles": sample["smiles"], "endpoint": task, "label": label,
                         "score": .2 if index == 0 else .8, "split": "validation"}
                        for index, sample in enumerate(self.samples) for task, label in sample["labels"].items()]
                self.write(directory / "validation-predictions.json", rows)
                self.summary["runs"].append({"model": model, "seed": seed, "directory": name,
                                             "manifestSha256": verifier.sha256_file(directory / "manifest.json"),
                                             "evaluation": copy.deepcopy(evaluation)})
                self.manifests[name] = manifest
                self.information[name] = {"configured": True, "version": manifest["modelVersion"], "weightsSha256": HASH,
                                          "artifactSha256": verifier.sha256_file(directory / "manifest.json"),
                                          "dataset": self.data_manifest, "evaluation": evaluation, "parameterCount": 0,
                                          "fixtureOnly": True}
        self.write_summary()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def write(path, payload):
        path.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")

    def write_summary(self):
        self.write(self.experiment / "experiment.json", self.summary)

    def audit(self, *, training_source_ref=None):
        information = self.information

        class MockArtifactClient:
            """No model loading; explicitly controlled contract fixture."""
            def __init__(self, directory, **kwargs): self.name = Path(directory).name
            def get_model_info(self): return copy.deepcopy(information[self.name])
            def predict(self, smiles, structure_id):
                return {"endpoints": [{"id": task, "score": .2} for task in verifier.ENDPOINTS], "fixtureOnly": True}

        receipt = {"mode": "unit-fixture-source-audit", "historicalCodeExecuted": False}
        with patch.object(verifier, "load_v3_data", return_value=(self.data_manifest, self.samples)), \
             patch.object(verifier, "V3ArtifactClient", MockArtifactClient), \
             patch.object(verifier, "verify_training_sources", return_value=receipt) as source_check:
            result = verifier.verify_run(self.experiment, self.data, self.root / "no-encoder-needed",
                                         training_source_ref=training_source_ref)
            return result, source_check.call_args_list

    def test_controlled_cpu_receipts_and_immutable_source_ref_forwarded(self):
        commit = "b" * 40
        result, calls = self.audit(training_source_ref=commit)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(len(result["runs"]), 9)
        self.assertEqual(len(calls), 9)
        self.assertTrue(all(call.kwargs["commit"] == commit for call in calls))
        self.assertTrue(all(run["recordedMetricRecomputation"] == "passed" for run in result["runs"]))

    def test_actual_evaluation_mutation_cannot_hide_behind_correct_summary(self):
        for field in ("auroc", "averagePrecision", "brier", "ece10Bins", "positives"):
            before = copy.deepcopy(self.information["B0-seed-42"]["evaluation"])
            self.information["B0-seed-42"]["evaluation"]["endpoints"]["herg"][field] = 0
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "actual artifact metric"):
                self.audit()
            self.information["B0-seed-42"]["evaluation"] = before

    def test_summary_metric_mutation_cannot_hide_behind_correct_artifact(self):
        self.summary["runs"][0]["evaluation"]["endpoints"]["ames"]["auroc"] = .25
        self.write_summary()
        with self.assertRaisesRegex(ValueError, "summary metric"):
            self.audit()

    def test_actual_evaluation_model_and_seed_are_bound_to_summary_row(self):
        for field, wrong in (("model", "B1"), ("seed", 43), ("seed", True)):
            before = copy.deepcopy(self.information["B0-seed-42"]["evaluation"])
            self.information["B0-seed-42"]["evaluation"][field] = wrong
            with self.subTest(field=field, wrong=wrong), self.assertRaisesRegex(ValueError, "model, seed or checksum identity"):
                self.audit()
            self.information["B0-seed-42"]["evaluation"] = before

    def test_summary_evaluation_model_and_seed_are_bound_to_actual_artifact(self):
        for field, wrong in (("model", "B2"), ("seed", 44), ("seed", True)):
            before = copy.deepcopy(self.summary["runs"][0]["evaluation"])
            self.summary["runs"][0]["evaluation"][field] = wrong
            self.write_summary()
            with self.subTest(field=field, wrong=wrong), self.assertRaisesRegex(ValueError, "model, seed or checksum identity"):
                self.audit()
            self.summary["runs"][0]["evaluation"] = before
        self.write_summary()

    def test_manifest_seed_rehashed_in_summary_cannot_relabel_another_run(self):
        name = "B0-seed-42"
        self.manifests[name]["training"]["seed"] = 43
        self.write(self.experiment / name / "manifest.json", self.manifests[name])
        sha = verifier.sha256_file(self.experiment / name / "manifest.json")
        self.summary["runs"][0]["manifestSha256"] = sha
        self.information[name]["artifactSha256"] = sha
        self.write_summary()
        with self.assertRaisesRegex(ValueError, "model, seed or checksum identity"):
            self.audit()

    def test_loaded_manifest_or_weight_snapshot_mismatch_rejected(self):
        for field in ("version", "artifactSha256", "weightsSha256"):
            before = self.information["B0-seed-42"][field]
            self.information["B0-seed-42"][field] = "changed-unit-fixture-value"
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "model, seed or checksum identity"):
                self.audit()
            self.information["B0-seed-42"][field] = before

    def test_incomplete_null_and_boolean_metrics_rejected(self):
        for bad in (None, True):
            before = copy.deepcopy(self.information["B0-seed-42"]["evaluation"])
            self.information["B0-seed-42"]["evaluation"]["endpoints"]["dili"]["brier"] = bad
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "actual artifact metric"):
                self.audit()
            self.information["B0-seed-42"]["evaluation"] = before
        del self.information["B0-seed-42"]["evaluation"]["endpoints"]["dili"]["brier"]
        with self.assertRaisesRegex(ValueError, "incomplete observed metrics"):
            self.audit()


if __name__ == "__main__":
    unittest.main()
