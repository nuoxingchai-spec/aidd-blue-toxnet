import copy
import json
from pathlib import Path
import tempfile
import unittest
from backend.research_evidence import read_external_evidence


class ResearchEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "evidence.json"
        self.info = {"configured": True, "version": "test-only-v2", "weightsSha256": "a" * 64, "artifactSha256": "b" * 64}
        metrics = {"count": 20, "positives": 10, "auroc": 0.6, "averagePrecision": 0.6, "brier": 0.3, "ece10Bins": 0.2}
        self.record = {"schema": "aidd.external-herg-evaluation.v1", "status": "completed", "modelVersion": self.info["version"],
                       "weightsSha256": self.info["weightsSha256"], "modelManifestSha256": self.info["artifactSha256"],
                       "modelUpdated": False, "calibrationRefit": False, "thresholdSelected": False, "prospective": False,
                       "neuralModel": dict(metrics), "fingerprintBaseline": dict(metrics), "poolSha256": "c" * 64, "candidateVersion": "test-only-candidates"}

    def write(self, record=None):
        self.path.write_text(json.dumps(record or self.record), encoding="utf-8")

    def test_matching_evidence_is_separate_and_cannot_claim_prospective_validation(self):
        self.record["neuralModel"]["privateField"] = "must not be exposed"
        self.write()
        result = read_external_evidence(self.info, self.path)
        self.assertEqual(result["neuralModel"]["auroc"], 0.6)
        self.assertNotIn("privateField", result["neuralModel"])
        self.assertFalse(result["sourcePopulationIndependenceVerified"])
        self.assertFalse(result["prospective"])
        self.assertEqual(len(result["evidenceSha256"]), 64)

    def test_other_weights_or_model_manifest_cannot_attach_evidence(self):
        self.write()
        for field in ("version", "weightsSha256", "artifactSha256"):
            info = {**self.info, field: "different"}
            self.assertIsNone(read_external_evidence(info, self.path))

    def test_refitted_updated_or_prospective_record_is_not_frozen_evidence(self):
        for field in ("modelUpdated", "calibrationRefit", "thresholdSelected", "prospective"):
            record = {**self.record, field: True}
            self.write(record)
            self.assertIsNone(read_external_evidence(self.info, self.path))

    def test_invalid_metrics_or_different_population_counts_are_not_shown(self):
        for field, value in (("auroc", float("nan")), ("brier", 2), ("count", 0), ("positives", True)):
            record = copy.deepcopy(self.record)
            record["neuralModel"][field] = value
            self.write(record)
            self.assertIsNone(read_external_evidence(self.info, self.path))
        record = copy.deepcopy(self.record)
        record["fingerprintBaseline"]["count"] += 1
        self.write(record)
        self.assertIsNone(read_external_evidence(self.info, self.path))

    def test_missing_or_malformed_evidence_keeps_model_info_available(self):
        self.assertIsNone(read_external_evidence(self.info, self.path))
        self.path.write_text("not-json", encoding="utf-8")
        self.assertIsNone(read_external_evidence(self.info, self.path))


if __name__ == "__main__":
    unittest.main()
