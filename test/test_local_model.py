"""Local provider contracts; tensor fixtures are explicitly test-only models."""
import hashlib
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.chemistry import canonicalize_smiles
from backend.local_model import ARTIFACT_SCHEMA, LocalModelClient
from backend.model_client import ENDPOINT_IDS, ModelAPIError


def identity(smiles):
    return hashlib.sha256(canonicalize_smiles(smiles).encode()).hexdigest()


def write_manifest(directory, overrides=None):
    weights = directory / "weights.pt"
    if not weights.exists():
        weights.write_bytes(b"not-a-network; dependency/error test only")
    (directory / "training-references.json").write_text(json.dumps(["CCO", "c1ccccc1"]), encoding="utf-8")
    evaluation = {"split": "test", "heldOutStatus": "manifest-membership-and-scaffold-separation-verified", "scoreKind": "probability", "endpoints": {endpoint: {"count": 2, "positives": 1, "auroc": 0.5, "averagePrecision": 0.5, "brier": 0.25, "ece10Bins": 0.0} for endpoint in ENDPOINT_IDS}}
    (directory / "evaluation.json").write_text(json.dumps(evaluation), encoding="utf-8")
    manifest = {"schema": ARTIFACT_SCHEMA, "modelName": "tensor-test-fixture-not-a-real-trained-model", "modelVersion": "test-only-1", "datasetVersion": "test-only-source-v1", "endpoints": list(ENDPOINT_IDS), "architecture": {"id": "linear-tensor-test-fixture"}, "training": {"status": "completed", "optimizationSplit": "train", "selectionSplit": "validation", "testUsedForSelection": False, "splits": {"train": 2, "validation": 2, "test": 2}}, "datasetProvenance": [], "calibration": {"status": "measured-not-certified", "method": "temperature-scaling", "temperatures": dict(zip(ENDPOINT_IDS, (1.0, 2.0, 1.0, 2.0))), "fitSplit": "validation"}, "applicability": {"threshold": 0.5}, "artifacts": {name: {"file": filename, "sha256": hashlib.sha256((directory / filename).read_bytes()).hexdigest()} for name, filename in (("weights", "weights.pt"), ("trainingReferences", "training-references.json"), ("evaluation", "evaluation.json"))}}
    if overrides:
        manifest.update(overrides)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


class LocalModelArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_untrained_model_is_explicitly_unavailable(self):
        client = LocalModelClient(self.directory)
        self.assertFalse(client.get_public_config()["configured"])
        self.assertEqual(client.get_model_info()["errorCode"], "LOCAL_MODEL_NOT_READY")
        with self.assertRaises(ModelAPIError) as caught:
            client.predict("CCO", identity("CCO"))
        self.assertEqual(caught.exception.status_code, 503)

    def test_optional_pytorch_dependency_never_breaks_other_providers(self):
        write_manifest(self.directory)
        with patch("backend.local_model.importlib.import_module", side_effect=ImportError("secret installation path")):
            info = LocalModelClient(self.directory).get_model_info()
        self.assertEqual(info["errorCode"], "LOCAL_MODEL_DEPENDENCY_MISSING")
        self.assertNotIn("secret", json.dumps(info))

    def test_modified_artifact_fails_its_hash_without_loading_weights(self):
        write_manifest(self.directory)
        (self.directory / "weights.pt").write_bytes(b"changed")
        info = LocalModelClient(self.directory).get_public_config()
        self.assertEqual(info["errorCode"], "LOCAL_MODEL_CHECKSUM_MISMATCH")

    def test_paths_cannot_escape_model_directory(self):
        manifest = write_manifest(self.directory)
        manifest["artifacts"]["weights"]["file"] = "../weights.pt"
        (self.directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        info = LocalModelClient(self.directory).get_public_config()
        self.assertEqual(info["errorCode"], "LOCAL_MODEL_INVALID_ARTIFACT")

    def test_missing_endpoint_or_evaluation_cannot_become_ready(self):
        for override in ({"endpoints": ["herg", "dili", "ames"]}, {"artifacts": {}}):
            write_manifest(self.directory, override)
            self.assertEqual(LocalModelClient(self.directory).get_public_config()["errorCode"], "LOCAL_MODEL_INVALID_ARTIFACT")

    def test_structure_id_is_required_to_match_canonical_input(self):
        with self.assertRaises(ModelAPIError) as caught:
            LocalModelClient(self.directory).predict("CCO", "wrong-id")
        self.assertEqual(caught.exception.code, "LOCAL_MODEL_INPUT_ERROR")
        self.assertEqual(caught.exception.status_code, 422)

    def test_untrained_or_test_selected_weights_cannot_be_used(self):
        for changes in ({"status": "pending"}, {"testUsedForSelection": True}, {"splits": {"train": 0}}):
            manifest = write_manifest(self.directory)
            manifest["training"].update(changes)
            (self.directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertFalse(LocalModelClient(self.directory).get_public_config()["configured"])


try:
    _torch = importlib.import_module("torch")
except ImportError:
    _torch = None


@unittest.skipIf(_torch is None, "PyTorch model dependency is optional until installed")
class LocalModelTensorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        torch = _torch

        class TensorFixtureNetwork:
            @staticmethod
            def build_model(architecture):
                return torch.nn.Linear(4, 4)

            @staticmethod
            def featurize_smiles(canonical, architecture):
                return {"features": torch.zeros((1, 4))}

            @staticmethod
            def predict_logits(model, features):
                return model(features["features"])

        self.network = TensorFixtureNetwork
        model = self.network.build_model({})
        with torch.no_grad():
            model.weight.zero_()
            model.bias.copy_(torch.tensor([-1.0, 0.0, 1.0, 2.0]))
        torch.save(model.state_dict(), self.directory / "weights.pt")
        write_manifest(self.directory)
        original_import = importlib.import_module
        self.import_patch = patch("backend.local_model.importlib.import_module", side_effect=lambda name: self.network if name == "backend.toxnet" else original_import(name))
        self.import_patch.start()

    def tearDown(self):
        self.import_patch.stop()
        self.temporary.cleanup()

    def test_scores_come_from_cpu_tensors_and_calibration_not_structure_rules(self):
        result = LocalModelClient(self.directory).predict("CCO", identity("CCO"))
        expected = _torch.sigmoid(_torch.tensor([-1.0, 0.0, 1.0, 1.0])).tolist()
        self.assertEqual([item["id"] for item in result["endpoints"]], list(ENDPOINT_IDS))
        for actual, value in zip(result["endpoints"], expected):
            self.assertAlmostEqual(actual["score"], value)
            self.assertEqual(actual["drivers"], [])
        self.assertEqual(result["scoreKind"], "probability")
        self.assertEqual(result["provenance"]["device"], "cpu")
        self.assertEqual(result["provenance"]["explanationMethod"], "none")
        self.assertEqual(result["calibration"]["status"], "measured-not-certified")
        self.assertFalse(result["calibration"]["certified"])
        self.assertEqual(result["applicability"]["maximumTanimoto"], 1.0)
        self.assertFalse(result["applicability"]["validated"])

    def test_equivalent_smiles_have_identical_model_scores_and_domain_assessment(self):
        client = LocalModelClient(self.directory)
        first = client.predict("c1ccccc1", identity("c1ccccc1"))
        second = client.predict("C1=CC=CC=C1", identity("C1=CC=CC=C1"))
        self.assertEqual(first["endpoints"], second["endpoints"])
        self.assertEqual(first["applicability"], second["applicability"])

    def test_ready_public_info_keeps_training_structures_and_paths_private(self):
        info = LocalModelClient(self.directory).get_model_info()
        self.assertTrue(info["configured"])
        self.assertEqual(info["parameterCount"], 20)
        self.assertEqual(info["applicability"]["referenceCount"], 2)
        self.assertNotIn(str(self.directory), json.dumps(info))
        self.assertNotIn("trainingReferences", info)

    def test_changing_a_loaded_weight_file_invalidates_the_snapshot(self):
        client = LocalModelClient(self.directory)
        self.assertTrue(client.get_public_config()["configured"])
        (self.directory / "weights.pt").write_bytes(b"modified after loading")
        with self.assertRaises(ModelAPIError) as caught:
            client.predict("CCO", identity("CCO"))
        self.assertEqual(caught.exception.code, "LOCAL_MODEL_CHECKSUM_MISMATCH")

    def test_test_split_calibration_or_nonfinite_temperatures_are_rejected(self):
        for changes in ({"fitSplit": "test"}, {"temperatures": {endpoint: float("nan") for endpoint in ENDPOINT_IDS}}):
            manifest = write_manifest(self.directory)
            manifest["calibration"].update(changes)
            (self.directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(LocalModelClient(self.directory).get_public_config()["errorCode"], "LOCAL_MODEL_INVALID_ARTIFACT")

    def test_validation_calibration_bias_is_applied_to_tensor_logits(self):
        manifest = write_manifest(self.directory)
        manifest["calibration"]["biases"] = dict(zip(ENDPOINT_IDS, (1.0, -1.0, 0.5, -0.5)))
        (self.directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        result = LocalModelClient(self.directory).predict("CCO", identity("CCO"))
        expected = _torch.sigmoid(_torch.tensor([0.0, -1.0, 1.5, 0.5])).tolist()
        self.assertEqual([item["score"] for item in result["endpoints"]], expected)

    def test_nonfinite_weights_and_outputs_are_never_reported_as_predictions(self):
        model = self.network.build_model({})
        with _torch.no_grad():
            model.weight.fill_(float("nan"))
        _torch.save(model.state_dict(), self.directory / "weights.pt")
        write_manifest(self.directory)
        self.assertEqual(LocalModelClient(self.directory).get_public_config()["errorCode"], "LOCAL_MODEL_INVALID_WEIGHTS")


@unittest.skipIf(_torch is None, "PyTorch model dependency is optional until installed")
class LocalGraphTransformerIntegrationTests(unittest.TestCase):
    def test_actual_graph_transformer_artifact_inference_and_equivalence(self):
        # This randomly initialized tiny fixture checks the production architecture
        # contract only; it is never written to the real model directory.
        network = importlib.import_module("backend.toxnet")
        architecture = {**network.DEFAULT_ARCHITECTURE, "hiddenSize": 24, "layers": 1, "heads": 4, "feedForward": 48, "fingerprintSize": 64, "dropout": 0.0}
        model = network.build_model(architecture)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _torch.save(model.state_dict(), directory / "weights.pt")
            write_manifest(directory, {"architecture": architecture})
            client = LocalModelClient(directory)
            first = client.predict("c1ccccc1", identity("c1ccccc1"))
            second = client.predict("C1=CC=CC=C1", identity("C1=CC=CC=C1"))
            self.assertEqual(first["endpoints"], second["endpoints"])
            self.assertTrue(all(0 <= endpoint["score"] <= 1 for endpoint in first["endpoints"]))
            self.assertEqual(client.get_model_info()["parameterCount"], sum(parameter.numel() for parameter in model.parameters()))


if __name__ == "__main__":
    unittest.main()
