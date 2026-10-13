"""Artifact-boundary tests with explicit test-only tensors, never benchmark data."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch
from rdkit import rdBase

from backend.chemistry import CHEMISTRY_VERSION, canonicalize_smiles
from backend.local_model import LocalModelClient
from backend.model_client import ENDPOINT_IDS, ModelAPIError
from backend.versioned_local_model import V3_ARCHITECTURE, V3_SCHEMA, V3ArtifactClient, load_versioned_local_model
from backend.v3_data import TASK_CONTRACT_VERSION
from backend.v3_experiments import evaluate_probabilities, sigmoid


def write_fixture(directory):
    """Controlled artifact fixture; not evidence of a trained toxicity model."""
    np.savez(directory / "weights.npz", coefficients=np.zeros((4, 2048)), intercepts=np.array([0., 1., -1., 2.]))
    np.savez(directory / "preprocessing.npz", mean=np.zeros(2048), scale=np.ones(2048))
    # These are computed toy-tensor boundary metrics, never experimental model
    # performance. A completed-schema fixture still needs coherent evaluation.
    evaluation = evaluate_probabilities(np.array([[0.] * 4, [1.] * 4]), np.ones((2, 4)),
                                        np.tile(sigmoid(np.array([0., 1., -1., 2.])), (2, 1)))
    evaluation.update(schema="aidd.toxnet.development-evaluation.v3", status="evaluated-validation-only",
                      datasetVersion="test-only-data", splitSha256="a" * 64, model="B0", seed=42)
    (directory / "evaluation.json").write_text(json.dumps(evaluation), encoding="utf-8")
    (directory / "training-history.json").write_text(json.dumps([{"fixtureOnly": True, "method": "test tensor boundary"}]), encoding="utf-8")
    manifest = {"schema": V3_SCHEMA, "status": "trained-completed", "formalAvailability": False,
                "evaluationStatus": "evaluated-validation-only",
                "chemistryVersion": CHEMISTRY_VERSION, "rdkitVersion": rdBase.rdkitVersion,
                "modelName": "test-only-v3-tensor-fixture-NOT-A-TRAINED-MODEL", "modelVersion": "test-only-v3",
                "taskContractVersion": TASK_CONTRACT_VERSION, "endpoints": list(ENDPOINT_IDS), "parameterCount": 4 * 2049,
                "trainableParameterCount": 4 * 2049, "frozenEncoderParameterCount": 0, "encoder": None,
                "architecture": {"id": V3_ARCHITECTURE, "headType": "logistic-regression", "inputDim": 2048,
                                 "outputDim": 4, "endpointOrder": list(ENDPOINT_IDS), "featureBranches": ["morgan"],
                                 "fingerprint": {"radius": 2, "bits": 2048, "includeChirality": True}},
                "training": {"status": "completed", "optimizationSplit": "train", "selectionSplit": "validation",
                             "seed": 42,
                             "testUsedForSelection": False, "previouslyExposedUsedForSelection": False,
                             "featureTransformFitSplit": "train", "splits": {"train": 2, "validation": 2},
                             "endpointStatus": {endpoint: "trained" for endpoint in ENDPOINT_IDS}},
                "dataset": {"datasetVersion": "test-only-data", "splitSha256": "a" * 64},
                "calibration": {"status": "not-fitted"}, "applicability": {"validated": False},
                "artifacts": {name: {"file": filename, "sha256": hashlib.sha256((directory / filename).read_bytes()).hexdigest()}
                              for name, filename in (("weights", "weights.npz"), ("preprocessing", "preprocessing.npz"),
                                                     ("evaluation", "evaluation.json"), ("trainingHistory", "training-history.json"))}}
    persist(directory, manifest)
    return manifest


def persist(directory, manifest):
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def structure_id(smiles):
    return hashlib.sha256(canonicalize_smiles(smiles).encode()).hexdigest()


class VersionedArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def client(self, **kwargs):
        return V3ArtifactClient(self.directory, allow_research=True, **kwargs)

    def test_v2_dispatch_retains_the_original_loader_without_loading_v3(self):
        persist(self.directory, {"schema": "aidd.toxnet.artifact.v1"})
        self.assertIsInstance(load_versioned_local_model(self.directory), LocalModelClient)

    def test_v3_dispatch_requires_explicit_research_opt_in(self):
        write_fixture(self.directory)
        client = load_versioned_local_model(self.directory)
        self.assertEqual(client.get_public_config()["errorCode"], "V3_MODEL_RESEARCH_OPT_IN_REQUIRED")

    def test_pending_or_unevaluated_artifact_is_never_available(self):
        for status in ("pending", "not-evaluated", "training", "completed"):
            manifest = write_fixture(self.directory)
            manifest["status"] = status
            persist(self.directory, manifest)
            self.assertEqual(self.client().get_public_config()["errorCode"], "V3_MODEL_NOT_READY")

    def test_untrained_endpoint_cannot_generate_four_predictions(self):
        manifest = write_fixture(self.directory)
        manifest["training"]["endpointStatus"]["dili"] = "not-trainable"
        persist(self.directory, manifest)
        self.assertEqual(self.client().get_public_config()["errorCode"], "V3_MODEL_NOT_READY")

    def test_test_selection_or_validation_preprocessing_is_rejected(self):
        for key, value in (("selectionSplit", "test"), ("previouslyExposedUsedForSelection", True),
                           ("testUsedForSelection", True), ("featureTransformFitSplit", "validation")):
            manifest = write_fixture(self.directory)
            manifest["training"][key] = value
            persist(self.directory, manifest)
            self.assertFalse(self.client().get_public_config()["configured"])

    def test_checksum_failure_is_detected_before_tensor_loading(self):
        write_fixture(self.directory)
        (self.directory / "weights.npz").write_bytes(b"tampered")
        self.assertEqual(self.client().get_public_config()["errorCode"], "V3_MODEL_CHECKSUM_MISMATCH")

    def test_morgan_only_model_rejects_changed_or_missing_chemical_feature_version(self):
        for field in ("chemistryVersion", "rdkitVersion"):
            for value in (None, "unreviewed-future-version"):
                with self.subTest(field=field, value=value):
                    manifest = write_fixture(self.directory)
                    if value is None:
                        manifest.pop(field)
                    else:
                        manifest[field] = value
                    persist(self.directory, manifest)
                    public = self.client().get_public_config()
                    self.assertFalse(public["configured"])
                    self.assertEqual(public["errorCode"], "V3_CHEMISTRY_VERSION_MISMATCH")

    def test_evaluation_hash_and_validation_role_are_required(self):
        manifest = write_fixture(self.directory)
        manifest["artifacts"].pop("evaluation")
        persist(self.directory, manifest)
        self.assertFalse(self.client().get_public_config()["configured"])

    def rewrite_evaluation(self, manifest, change):
        path = self.directory / "evaluation.json"
        evaluation = json.loads(path.read_text(encoding="utf-8"))
        change(evaluation)
        path.write_text(json.dumps(evaluation), encoding="utf-8")
        manifest["artifacts"]["evaluation"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        persist(self.directory, manifest)

    def test_rehashed_evaluation_from_other_data_split_seed_or_role_is_rejected(self):
        for field, value in (("datasetVersion", "different-data"), ("splitSha256", "b" * 64),
                             ("seed", 43), ("schema", "unknown"), ("status", "not-evaluated"),
                             ("evaluationType", "blind-test"), ("independentTest", True),
                             ("calibrationStatus", "fitted"), ("split", "test")):
            with self.subTest(field=field):
                manifest = write_fixture(self.directory)
                self.rewrite_evaluation(manifest, lambda value_dict: value_dict.update({field: value}))
                self.assertFalse(self.client().get_public_config()["configured"])

    def test_two_class_evaluation_cannot_claim_completion_with_missing_metrics(self):
        for metric in ("auroc", "averagePrecision", "brier", "ece10Bins"):
            manifest = write_fixture(self.directory)
            self.rewrite_evaluation(manifest, lambda e: e["endpoints"]["herg"].update({metric: None}))
            self.assertEqual(self.client().get_public_config()["errorCode"], "V3_MODEL_NOT_READY")

    def test_single_class_validation_is_legal_with_null_ranking_metrics(self):
        manifest = write_fixture(self.directory)
        self.rewrite_evaluation(manifest, lambda e: e["endpoints"]["herg"].update(
            positives=0, negatives=2, status="single-class", rankingMetricReason="both-classes-required",
            auroc=None, averagePrecision=None))
        self.assertTrue(self.client().get_public_config()["configured"])
        self.rewrite_evaluation(manifest, lambda e: e["endpoints"]["herg"].update(auroc=.5))
        self.assertFalse(self.client().get_public_config()["configured"])

    def test_unobserved_validation_has_no_fabricated_metrics(self):
        manifest = write_fixture(self.directory)
        self.rewrite_evaluation(manifest, lambda e: e["endpoints"]["herg"].update(
            count=0, positives=0, negatives=0, status="no-data", rankingMetricReason="no-observed-labels",
            auroc=None, averagePrecision=None, brier=None, ece10Bins=None))
        self.assertTrue(self.client().get_public_config()["configured"])
        self.rewrite_evaluation(manifest, lambda e: e["endpoints"]["herg"].update(brier=.2))
        self.assertFalse(self.client().get_public_config()["configured"])

    def test_rehashed_evaluation_class_counts_and_status_must_agree(self):
        for changes in ({"negatives": 0}, {"status": "single-class"}, {"rankingMetricReason": "both-classes-required"}):
            manifest = write_fixture(self.directory)
            self.rewrite_evaluation(manifest, lambda e: e["endpoints"]["herg"].update(changes))
            self.assertFalse(self.client().get_public_config()["configured"])

    def test_paths_cannot_escape_the_model_directory(self):
        manifest = write_fixture(self.directory)
        manifest["artifacts"]["weights"]["file"] = "../weights.npz"
        persist(self.directory, manifest)
        self.assertFalse(self.client().get_public_config()["configured"])

    def test_declared_feature_dimension_and_parameter_count_must_match(self):
        for key, value in (("parameterCount", 123), ("architecture", {"id": "unsupported"})):
            manifest = write_fixture(self.directory)
            manifest[key] = value
            persist(self.directory, manifest)
            self.assertFalse(self.client().get_public_config()["configured"])

    def test_explicit_missing_cuda_is_not_silently_converted_to_cpu(self):
        write_fixture(self.directory)
        with patch("torch.cuda.is_available", return_value=False):
            self.assertEqual(self.client(device="cuda").get_public_config()["errorCode"], "V3_CUDA_UNAVAILABLE")

    def test_actual_tensor_fixture_predictions_preserve_input_identity(self):
        write_fixture(self.directory)
        client = self.client()
        self.assertEqual(client.get_public_config()["status"], "research-ready")
        first = client.predict("CCO", structure_id("CCO"))
        second = client.predict("OCC", structure_id("OCC"))
        self.assertEqual(first["endpoints"], second["endpoints"])
        self.assertEqual([item["id"] for item in first["endpoints"]], list(ENDPOINT_IDS))
        self.assertFalse(first["provenance"]["formalAvailability"])
        self.assertEqual(first["calibration"]["status"], "not-fitted")

    def test_modified_loaded_artifact_invalidates_the_snapshot(self):
        write_fixture(self.directory)
        client = self.client()
        self.assertTrue(client.get_public_config()["configured"])
        (self.directory / "weights.npz").write_bytes(b"changed after loading")
        self.assertFalse(client.get_public_config()["configured"])

    def test_molformer_model_requires_its_local_snapshot(self):
        manifest = write_fixture(self.directory)
        manifest["architecture"]["featureBranches"] = ["molformer"]
        persist(self.directory, manifest)
        self.assertEqual(self.client().get_public_config()["errorCode"], "V3_ENCODER_WEIGHTS_MISSING")

    def test_illegal_smiles_or_wrong_structure_id_never_predict(self):
        write_fixture(self.directory)
        for smiles, identity in (("not-smiles", "bad"), ("CCO", "bad")):
            with self.assertRaises(ModelAPIError) as caught:
                self.client().predict(smiles, identity)
            self.assertEqual(caught.exception.status_code, 422)

    def test_malformed_metadata_fails_cleanly(self):
        for key in ("training", "architecture", "dataset", "calibration", "applicability"):
            manifest = write_fixture(self.directory)
            manifest[key] = []
            persist(self.directory, manifest)
            self.assertFalse(self.client().get_public_config()["configured"])

    def test_trainable_and_frozen_provenance_cannot_be_misrepresented(self):
        for key, value in (("trainableParameterCount", 99), ("frozenEncoderParameterCount", 123), ("encoder", {"source": "claimed-but-unused"})):
            manifest = write_fixture(self.directory)
            manifest[key] = value
            persist(self.directory, manifest)
            self.assertFalse(self.client().get_public_config()["configured"])

    def test_malformed_mlp_nested_values_are_rejected_without_allocating_a_head(self):
        for hidden, dropout in ((None, 0.1), ([], 0.1), (10_000_000, 0.1), (128, "bad")):
            manifest = write_fixture(self.directory)
            manifest["architecture"].update(headType="mlp", hiddenSize=hidden, dropout=dropout)
            persist(self.directory, manifest)
            self.assertFalse(self.client().get_public_config()["configured"])


if __name__ == "__main__":
    unittest.main()
