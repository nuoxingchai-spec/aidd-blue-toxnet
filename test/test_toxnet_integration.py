"""CPU-only integration-verifier safeguards using explicitly labelled fixtures.

No pretrained weights, GPU, external providers or experimental source rows are
needed. Fixture scores are contract examples, never research model evidence.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.verify_toxnet_integration import (
    FOUR_TASKS, TOX21_TASKS, SCHEMA, IntegrationCheckError,
    main, validate_scores, validate_report, validate_tox21,
    verify_protected_files, write_summary,
)

HASH = "a" * 64
FIXTURE_VERSION = "unit-fixture-not-a-real-trained-model"


def output_fixture(tasks):
    return [{"id": task, "score": 0.2} for task in tasks]


def explanation_fixture():
    return {
        "contractVersion": "aidd.research.explanation.api.v1",
        "modelPrediction": {"endpoints": output_fixture(FOUR_TASKS)},
        "provenance": {"weightsSha256": HASH, "modelVersion": FIXTURE_VERSION,
                       "researchOnly": True, "humanRiskProbability": False},
        "attribution": {"status": "available", "method": "unit-fixture-not-real-attribution",
                        "provenance": {"weightsSha256": HASH, "predictionBeforeAfterIdentical": True},
                        "atoms": [{"index": 0, "contribution": 0.1}],
                        "imageDataUrl": "data:image/png;base64,unit-fixture", "diagnostics": {}},
        "report": {"language": {"status": "unavailable"},
                   "claims": [{"sourceKind": kind, "sourceIds": [], "confidence": "unit fixture only", "limitations": []}
                              for kind in ("model-computation", "human-rule", "mechanism-hypothesis")],
                   "mechanismAssessment": {"status": "insufficient-evidence", "verifiedMechanisticClaims": []},
                   "sections": [{"text": "不确定；尚无充分证据。"}]},
        "structuralAlerts": {"sourceKind": "human-rule", "notNeuralAttribution": True},
    }


def tox21_fixture():
    return {"contractVersion": "aidd.research.tox21.api.v1", "pathways": output_fixture(TOX21_TASKS),
            "semantics": {"clinicalToxicityProbability": False, "aggregateRiskProvided": False},
            "provenance": {"modelVersion": FIXTURE_VERSION, "formalAvailability": False,
                           "researchOnly": True, "weightsSha256": HASH},
            "calibration": {"status": "not-fitted"}, "applicability": {"validated": False}}


class IntegrationOutputContractTests(unittest.TestCase):
    def test_labelled_fixture_scores_order_and_boolean_not_accepted(self):
        self.assertEqual(list(validate_scores(output_fixture(FOUR_TASKS), FOUR_TASKS)), list(FOUR_TASKS))
        invalid = [output_fixture(FOUR_TASKS)[::-1], output_fixture(FOUR_TASKS)[:-1]]
        for score in (True, float("nan"), float("inf"), -0.1, 1.1, "0.2"):
            packet = output_fixture(FOUR_TASKS); packet[0]["score"] = score; invalid.append(packet)
        for packet in invalid:
            with self.subTest(packet=packet), self.assertRaises(IntegrationCheckError):
                validate_scores(packet, FOUR_TASKS)

    def test_report_keeps_scores_and_uncertainty(self):
        result = validate_report(explanation_fixture(), {task: .2 for task in FOUR_TASKS}, HASH)
        self.assertTrue(result["scoresUnchanged"])
        self.assertTrue(result["mechanismUncertain"])

    def test_report_changed_scores_or_real_weight_association_rejected(self):
        for change in ("score", "prediction-sha", "attribution-sha", "prediction-changed"):
            packet = explanation_fixture()
            if change == "score": packet["modelPrediction"]["endpoints"][0]["score"] = .9
            elif change == "prediction-sha": packet["provenance"]["weightsSha256"] = "b" * 64
            elif change == "attribution-sha": packet["attribution"]["provenance"]["weightsSha256"] = "b" * 64
            else: packet["attribution"]["provenance"]["predictionBeforeAfterIdentical"] = False
            with self.subTest(change=change), self.assertRaises(IntegrationCheckError):
                validate_report(packet, {task: .2 for task in FOUR_TASKS}, HASH)

    def test_unavailable_attribution_or_unsourced_mechanism_cannot_pass(self):
        for change in ("attribution", "mechanism", "language", "rules"):
            packet = explanation_fixture()
            if change == "attribution": packet["attribution"]["status"] = "unavailable"
            elif change == "mechanism": packet["report"]["mechanismAssessment"]["verifiedMechanisticClaims"] = ["fabricated fixture claim"]
            elif change == "language": packet["report"]["language"]["status"] = "organized"
            else: packet["structuralAlerts"]["notNeuralAttribution"] = False
            with self.subTest(change=change), self.assertRaises(IntegrationCheckError):
                validate_report(packet, {task: .2 for task in FOUR_TASKS}, HASH)

    def test_twelve_pathways_and_sixteen_outputs_fixture(self):
        result = validate_tox21(tox21_fixture(), {"endpoints": output_fixture((*FOUR_TASKS, *TOX21_TASKS))}, HASH)
        self.assertEqual((result["pathwayCount"], result["taskCount"]), (12, 16))
        self.assertFalse(result["formalAvailability"])

    def test_tox21_score_mutation_and_formal_clinical_claims_rejected(self):
        for change in ("score", "formal", "clinical", "aggregate", "calibration"):
            packet = tox21_fixture()
            if change == "score": packet["pathways"][0]["score"] = .9
            elif change == "formal": packet["provenance"]["formalAvailability"] = True
            elif change == "clinical": packet["semantics"]["clinicalToxicityProbability"] = True
            elif change == "aggregate": packet["semantics"]["aggregateRiskProvided"] = True
            else: packet["calibration"]["status"] = "certified"
            with self.subTest(change=change), self.assertRaises(IntegrationCheckError):
                validate_tox21(packet, {"endpoints": output_fixture((*FOUR_TASKS, *TOX21_TASKS))}, HASH)


class IntegrationHistoryAndExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.weights = self.root / "models/aidd-toxnet/weights.pt"
        self.weights.parent.mkdir(parents=True)
        self.weights.write_bytes(b"unit fixture bytes, not real model weights")
        self.sha = hashlib.sha256(self.weights.read_bytes()).hexdigest()
        self.record = {"file": "models/aidd-toxnet/weights.pt", "beforeSha256": self.sha, "afterSha256": self.sha}
        self.ledger = self.root / "protected-unit-fixture.json"
        self.save_ledger()

    def tearDown(self):
        self.temp.cleanup()

    def save_ledger(self):
        self.ledger.write_text(json.dumps({"status": "unchanged", "files": [self.record]}), encoding="utf-8")

    def test_protected_fixture_read_only_and_sha_rejects_corruption(self):
        result = verify_protected_files(self.root, self.ledger, origin_ref=None)
        self.assertEqual(result[0]["sha256"], self.sha)
        self.weights.write_bytes(b"corruption in fixture copy")
        with self.assertRaisesRegex(IntegrationCheckError, "PROTECTED_FILE_CHANGED"):
            verify_protected_files(self.root, self.ledger, origin_ref=None)

    def test_ledger_cannot_reauthorize_changes_or_escape_root(self):
        self.record["afterSha256"] = "b" * 64; self.save_ledger()
        with self.assertRaisesRegex(IntegrationCheckError, "PROTECTED_LEDGER_CHANGED"):
            verify_protected_files(self.root, self.ledger, origin_ref=None)
        self.record["file"] = "../outside-unit-fixture"; self.save_ledger()
        with self.assertRaisesRegex(IntegrationCheckError, "PROTECTED_PATH_ESCAPE"):
            verify_protected_files(self.root, self.ledger, origin_ref=None)

    def test_independent_git_bytes_checked_not_only_rehashed_manifest(self):
        from types import SimpleNamespace
        tree = SimpleNamespace(returncode=0, stdout=b"models/aidd-toxnet/weights.pt\n")
        wrong = SimpleNamespace(returncode=0, stdout=b"changed independent git fixture")
        with patch("scripts.verify_toxnet_integration.subprocess.run", side_effect=[tree, wrong]):
            with self.assertRaisesRegex(IntegrationCheckError, "PROTECTED_GIT_BYTES_CHANGED"):
                verify_protected_files(self.root, self.ledger)

    def test_summary_refuses_overwrite_and_secret_observation_keys(self):
        target = self.root / "proof.json"
        summary = {"schema": SCHEMA, "status": "fixture-only-not-runtime-verification"}
        write_summary(target, summary)
        with self.assertRaises(FileExistsError): write_summary(target, summary)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), summary)
        for key in ("canonicalSmiles", "label", "source_row", "apiKey", "Authorization"):
            fresh = self.root / (key + ".json")
            bad = {**summary, "nested": [{key: "private fixture value"}]}
            with self.subTest(key=key), self.assertRaisesRegex(IntegrationCheckError, "SUMMARY_SENSITIVE_CONTENT_FORBIDDEN"):
                write_summary(fresh, bad)
            self.assertFalse(fresh.exists())

    def test_cli_refuses_existing_output_before_running_models(self):
        target = self.root / "existing.json"; target.write_text("preserve fixture")
        with patch("scripts.verify_toxnet_integration.verify_runtime") as runtime:
            with self.assertRaises(SystemExit):
                main(["--model-dir", ".", "--encoder-dir", ".", "--data-dir", ".", "--index-dir", ".", "--output", str(target)])
        runtime.assert_not_called(); self.assertEqual(target.read_text(), "preserve fixture")

    def test_failure_export_never_records_arbitrary_exception_detail(self):
        target = self.root / "failure.json"
        with patch("scripts.verify_toxnet_integration.verify_runtime", side_effect=ValueError("PRIVATE_API_KEY fixture value")):
            self.assertEqual(main(["--model-dir", ".", "--encoder-dir", ".", "--data-dir", ".", "--index-dir", ".", "--output", str(target)]), 1)
        result = target.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_", result)
        self.assertEqual(json.loads(result)["failureCode"], "ValueError")


if __name__ == "__main__":
    unittest.main()
