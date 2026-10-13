"""CPU-only research service tests. All numerical fixtures are synthetic tests.

Fixtures are written exclusively to TemporaryDirectory, never into actual
model, dataset, or published evaluation directories. No network/GPU is used.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import numpy as np
from rdkit import rdBase

from backend.chemistry import CHEMISTRY_VERSION
from backend.candidate_data import TOX21_ENDPOINTS
from backend.model_client import ENDPOINT_IDS, ModelAPIError
from backend.multitask_model import (ALL_TASKS, ARCHITECTURE_ID, ARTIFACT_SCHEMA,
                                     FINGERPRINT, TASK_CONTRACT_VERSION, MultitaskModelClient)
from backend.research_routes import create_research_router
from backend.multitask_data import task_contracts, training_permission, source_usage, TOX21_PROCESSED_SHA256
from backend import v3_data


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def source_ledger():
    # Only metadata fixtures; these fixed source files are never opened in this test.
    required = {
        "frozen-v2-model-manifest": v3_data.V2_MANIFEST_SHA256, "frozen-v2-split": v3_data.V2_SPLIT_SHA256,
        "previously-exposed-herg-evidence": v3_data.EXTERNAL_EVIDENCE_SHA256,
        "previously-exposed-herg-pool": v3_data.EXPOSED_POOL_SHA256,
        "verified-experimental-labels": "75e5f2c8766efe24cf6cbca57ce1e72bc9028705303b29fff8ad6016c6b0bd6b",
        "clintox-label-verification": v3_data.CLINTOX_REFERENCE_SHA256,
        **{"experimental-source-" + spec["endpoint"]: spec["sha256"] for spec in v3_data.DATASETS},
        **{"audit-only-source-" + spec["id"]: spec["sha256"] for spec in v3_data.SOURCES},
        "audit-only-tox21-clean-structures": TOX21_PROCESSED_SHA256,
    }
    return [{"role": role, "pathFromData": "synthetic-never-read-" + str(index), "sha256": sha,
             "useInThisProtocol": source_usage(role)} for index, (role, sha) in enumerate(required.items())]


def fixture(directory, tasks=ALL_TASKS, disabled=False):
    """Minimal numerical artifact fixture; not evidence of a trained real model."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    np.savez(directory / "weights.npz", coefficients=np.zeros((len(tasks), 2048), np.float32), intercepts=np.linspace(-1, 1, len(tasks), dtype=np.float32))
    np.savez(directory / "preprocessing.npz", mean=np.zeros(2048, np.float32), scale=np.ones(2048, np.float32))
    write_json(directory / "evaluation.json", {"schema": "aidd.toxnet.multitask-evaluation.v1", "split": "validation",
        "datasetVersion": "synthetic-unit-test-only", "splitSha256": "b" * 64, "endpoints": {
        task: {"count": 2, "positives": 1, "negatives": 1,
               "auroc": None if disabled and task in TOX21_ENDPOINTS else 0.5,
               "averagePrecision": None if disabled and task in TOX21_ENDPOINTS else 0.5,
               "brier": None if disabled and task in TOX21_ENDPOINTS else 0.25,
               "ece10Bins": None if disabled and task in TOX21_ENDPOINTS else 0.0} for task in tasks}})
    write_json(directory / "training-history.json", [{"fixture": "synthetic-unit-test-only"}])
    write_json(directory / "validation-predictions.json", [{"fixture": "synthetic-unit-test-only"}])
    write_json(directory / "training-references.json", [{"fixture": "synthetic-unit-test-only"}])
    records = {key: {"file": name, "sha256": digest(directory / name)} for key, name in (
        ("weights", "weights.npz"), ("preprocessing", "preprocessing.npz"), ("evaluation", "evaluation.json"),
        ("trainingHistory", "training-history.json"), ("validationPredictions", "validation-predictions.json"),
        ("trainingReferences", "training-references.json"))}
    manifest = {
        "schema": ARTIFACT_SCHEMA, "status": "trained-ablation" if disabled else "trained-completed", "formalAvailability": False,
        "modelName": "synthetic-unit-test-only", "modelVersion": "synthetic-service-fixture-v1",
        "tasks": list(tasks), "taskContractVersion": TASK_CONTRACT_VERSION,
        "taskContracts": {task: task_contracts()[task] for task in tasks},
        "chemistryVersion": CHEMISTRY_VERSION, "rdkitVersion": rdBase.rdkitVersion,
        "parameterCount": len(tasks) * 2049, "trainableParameterCount": (4 if disabled else len(tasks)) * 2049,
        "frozenEncoderParameterCount": 0, "encoder": None,
        "architecture": {"id": ARCHITECTURE_ID, "headType": "logistic-regression", "inputDim": 2048,
                         "outputDim": len(tasks), "taskOrder": list(tasks), "featureBranches": ["morgan"], "fingerprint": FINGERPRINT},
        "training": {"status": "completed-ablation" if disabled else "completed", "optimizationSplit": "train", "selectionSplit": "validation",
                     "testUsedForSelection": False, "legacyHeldoutAndKarimUsedForSelection": False,
                     "featureTransformFitSplit": "train", "splits": {"train": 2, "validation": 2},
                     "taskWeights": {task: 0 if disabled and task in TOX21_ENDPOINTS else 1 for task in tasks},
                     "classCounts": {task: {"positive": 1, "negative": 1, "count": 2} for task in tasks},
                     "sourceHashes": {"test/test_multitask_service.py": digest(Path(__file__))},
                     "endpointStatus": {task: "disabled" if disabled and task in TOX21_ENDPOINTS else "trained" for task in tasks}},
        "dataset": {"datasetVersion": "synthetic-unit-test-only", "manifestSha256": "a" * 64, "splitSha256": "b" * 64,
                    "trainingPermission": training_permission(), "sourceHashes": source_ledger()},
        "calibration": {"status": "not-fitted"}, "applicability": {"validated": False}, "artifacts": records,
    }
    write_json(directory / "manifest.json", manifest)
    return manifest


def test_head_module():
    """Small test-only implementation substitutes optional trainer import.

    The loader's actual LR coefficients and preprocessing are still loaded and
    dimension/finite-value/SHA checked. No synthetic production predictor exists.
    """
    def features(smiles):
        return np.zeros((len(smiles), 2048), dtype=np.float32)
    def predict(head, features, device="cpu", batch_size=1):
        if isinstance(head, dict):
            return 1 / (1 + np.exp(-np.clip(features @ head["coefficients"].T + head["intercepts"], -80, 80)))
        import torch
        with torch.inference_mode():
            return torch.sigmoid(head(torch.as_tensor(features, dtype=torch.float32))).numpy()
    from backend.multitask_experiments import MultiTaskMLP
    return SimpleNamespace(morgan_features=features, predict_head=predict, MultiTaskMLP=MultiTaskMLP)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "synthetic-fixture"
        self.manifest = fixture(self.directory)
        original_import = importlib.import_module
        def import_test_module(name, *args, **kwargs):
            return test_head_module() if name == "backend.multitask_experiments" else original_import(name, *args, **kwargs)
        self.import_patch = patch("backend.multitask_model.importlib.import_module", side_effect=import_test_module)
        self.import_patch.start()
        self.model = MultitaskModelClient(self.directory, allow_research=True)

    def tearDown(self):
        self.import_patch.stop()
        self.temporary.cleanup()

    def app_client(self, model=None, env=None):
        app = FastAPI()
        app.include_router(create_research_router(client=model, env=env))
        return TestClient(app)

    def mutate(self, callback):
        callback(self.manifest)
        write_json(self.directory / "manifest.json", self.manifest)

    def test_default_status_unavailable_and_prediction_503(self):
        client = self.app_client(env={})
        status = client.get("/api/research/tox21/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["status"], "unavailable")
        self.assertFalse(status.json()["formalAvailability"])
        result = client.post("/api/research/tox21", json={"smiles": "CCO"})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()["error"]["code"], "MULTITASK_RESEARCH_DISABLED")

    def test_explicit_enable_without_model_503(self):
        client = self.app_client(env={"TOXNET_MULTITASK_ENABLE": "true"})
        response = client.post("/api/research/tox21", json={"smiles": "CCO"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "MULTITASK_MODEL_UNCONFIGURED")

    def test_success_returns_exact_twelve_pathways_and_no_composite(self):
        client = self.app_client(self.model)
        self.assertTrue(client.get("/api/research/tox21/status").json()["configured"])
        response = client.post("/api/research/tox21", json={"smiles": "OCC"})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["canonicalSmiles"], "CCO")
        self.assertEqual([p["id"] for p in result["pathways"]], list(TOX21_ENDPOINTS))
        self.assertNotIn("overall", result)
        self.assertNotIn("endpoints", result)
        self.assertFalse(result["semantics"]["clinicalToxicityProbability"])
        self.assertFalse(result["semantics"]["aggregateRiskProvided"])
        self.assertEqual(result["calibration"]["status"], "not-fitted")
        self.assertEqual(result["provenance"]["weightsSha256"], digest(self.directory / "weights.npz"))

    def test_invalid_smiles_422_even_when_unconfigured(self):
        client = self.app_client(env={})
        for smiles in ("C1CC", "C#", "*", "CCO label"):
            with self.subTest(smiles=smiles):
                result = client.post("/api/research/tox21", json={"smiles": smiles})
                self.assertEqual(result.status_code, 422)

    def test_strict_request_contract(self):
        client = self.app_client(self.model)
        for value in ({"smiles": True}, {"smiles": "CCO", "model": "untrusted"}, {"smiles": ""}, {"smiles": "C" * 4097}):
            self.assertEqual(client.post("/api/research/tox21", json=value).status_code, 422)

    def test_incomplete_model_is_refused(self):
        self.mutate(lambda m: m.update(status="not-trained"))
        self.assertEqual(self.model.get_status()["errorCode"], "MULTITASK_MODEL_NOT_READY")

    def test_weights_sha_failure(self):
        (self.directory / "weights.npz").write_bytes(b"corrupted")
        self.assertEqual(self.model.get_status()["errorCode"], "MULTITASK_CHECKSUM_MISMATCH")

    def test_missing_artifact(self):
        (self.directory / "training-history.json").unlink()
        self.assertEqual(self.model.get_status()["errorCode"], "MULTITASK_MODEL_NOT_READY")

    def test_source_and_split_hash_metadata_required(self):
        self.mutate(lambda m: m["dataset"].update(splitSha256="bad"))
        self.assertFalse(self.model.get_status()["configured"])
        self.mutate(lambda m: m["dataset"].update(splitSha256="b" * 64))
        self.mutate(lambda m: m["training"].update(sourceHashes={}))
        self.assertFalse(self.model.get_status()["configured"])

    def test_source_roles_usage_and_fixed_hashes_checked(self):
        original = deepcopy(self.manifest["dataset"]["sourceHashes"])
        for field, bad_value in (("sha256", "d" * 64), ("useInThisProtocol", "training-on-heldout")):
            self.mutate(lambda m: m["dataset"]["sourceHashes"][0].update({field: bad_value}))
            self.assertFalse(self.model.get_model_info()["configured"])
            self.mutate(lambda m: m["dataset"].update(sourceHashes=deepcopy(original)))
        self.mutate(lambda m: m["dataset"]["sourceHashes"].append(deepcopy(m["dataset"]["sourceHashes"][0])))
        self.assertFalse(self.model.get_model_info()["configured"])

    def test_train_counts_and_validation_identity_checked(self):
        self.mutate(lambda m: m["training"]["classCounts"][TOX21_ENDPOINTS[0]].update(positive=0, negative=2, count=2))
        self.assertFalse(self.model.get_model_info()["configured"])
        self.mutate(lambda m: m["training"]["classCounts"][TOX21_ENDPOINTS[0]].update(positive=1, negative=1, count=2))
        path = self.directory / "evaluation.json"
        evaluation = json.loads(path.read_text(encoding="utf-8"))
        evaluation["splitSha256"] = "e" * 64
        write_json(path, evaluation)
        self.mutate(lambda m: m["artifacts"]["evaluation"].update(sha256=digest(path)))
        self.assertFalse(self.model.get_model_info()["configured"])

    def test_parameter_counts_and_dimensions_checked(self):
        for field in ("parameterCount", "trainableParameterCount"):
            original = self.manifest[field]
            self.mutate(lambda m: m.update({field: original + 1}))
            self.assertFalse(self.model.get_status()["configured"])
            self.mutate(lambda m: m.update({field: original}))
        self.mutate(lambda m: m["architecture"].update(inputDim=768))
        self.assertFalse(self.model.get_status()["configured"])

    def test_task_order_and_contract_mismatch(self):
        self.mutate(lambda m: m["architecture"].update(taskOrder=list(reversed(ALL_TASKS))))
        self.assertFalse(self.model.get_status()["configured"])
        self.mutate(lambda m: m["architecture"].update(taskOrder=list(ALL_TASKS)))
        self.mutate(lambda m: m.update(taskContractVersion="different"))
        self.assertFalse(self.model.get_status()["configured"])

    def test_a4_loads_for_verification_but_not_tox21(self):
        fixture(self.directory, tasks=ENDPOINT_IDS)
        self.assertTrue(self.model.get_model_info()["configured"])
        self.assertEqual(len(self.model.predict_all("CCO")["endpoints"]), 4)
        self.assertEqual(self.model.get_status()["errorCode"], "MULTITASK_TASKS_UNSUPPORTED")
        with self.assertRaises(ModelAPIError):
            self.model.predict_tox21("CCO")

    def test_disabled_pathway_never_published_as_prediction(self):
        fixture(self.directory, disabled=True)
        self.assertTrue(self.model.get_model_info()["configured"])
        self.assertEqual([value["id"] for value in self.model.predict_all("CCO")["endpoints"]], list(ENDPOINT_IDS))
        self.assertEqual(self.model.get_status()["errorCode"], "MULTITASK_TASKS_UNSUPPORTED")

    def test_shared_mlp_ablation_actual_trainable_count(self):
        import torch
        from backend.multitask_experiments import MultiTaskMLP
        from backend import pretrained_encoder
        self.manifest = fixture(self.directory, disabled=True)
        head = MultiTaskMLP(768, 16)
        torch.save(head.state_dict(), self.directory / "weights.pt")
        np.savez(self.directory / "preprocessing.npz", mean=np.zeros(768), scale=np.ones(768))
        provenance = {"source": pretrained_encoder.MODEL_SOURCE, "revision": pretrained_encoder.MODEL_REVISION,
                      "weightsSha256": pretrained_encoder.WEIGHTS_SHA256, "dimension": 768,
                      "chemistryVersion": CHEMISTRY_VERSION, "rdkitVersion": rdBase.rdkitVersion,
                      "encoderVersion": pretrained_encoder.ENCODER_VERSION, "precision": "float32",
                      "files": {}, "pooling": "test-only", "maxTokens": 202, "truncation": "reject",
                      "deterministicEval": True, "frozenParameterCount": 44375040, "trainableParameterCount": 0}
        self.manifest["architecture"].update(headType="shared-mlp", featureBranches=["molformer"], inputDim=768, hiddenSize=128, dropout=0.1)
        self.manifest.update(parameterCount=100496, trainableParameterCount=98948, frozenEncoderParameterCount=44375040, encoder=provenance)
        self.manifest["artifacts"]["weights"] = {"file": "weights.pt", "sha256": digest(self.directory / "weights.pt")}
        self.manifest["artifacts"]["preprocessing"]["sha256"] = digest(self.directory / "preprocessing.npz")
        write_json(self.directory / "manifest.json", self.manifest)
        stub = SimpleNamespace(provenance=provenance, encode=lambda smiles, batch_size=1: np.zeros((len(smiles), 768), np.float32))
        with patch.object(pretrained_encoder, "PretrainedEncoder", return_value=stub):
            model = MultitaskModelClient(self.directory, encoder_dir=self.directory, allow_research=True)
            info = model.get_model_info()
            self.assertTrue(info["configured"], info)
            self.assertEqual(info["parameterCount"], 100496)
            self.assertEqual(info["trainableParameterCount"], 98948)
            self.assertEqual(len(model.predict_all("CCO")["endpoints"]), 4)
            self.assertEqual(model.get_status()["errorCode"], "MULTITASK_TASKS_UNSUPPORTED")

    def test_disabled_loss_weight_must_be_zero(self):
        self.manifest = fixture(self.directory, disabled=True)
        self.mutate(lambda m: m["training"]["taskWeights"].update({TOX21_ENDPOINTS[0]: 0.25}))
        self.assertFalse(self.model.get_model_info()["configured"])

    def test_task_semantics_and_permission_are_verified(self):
        self.mutate(lambda m: m["taskContracts"][TOX21_ENDPOINTS[0]].update(positiveClass="clinical toxicity"))
        self.assertFalse(self.model.get_model_info()["configured"])
        self.mutate(lambda m: m.update(taskContracts=task_contracts()))
        self.mutate(lambda m: m["dataset"]["trainingPermission"].update(externalOrHeldoutLabelsAllowed=True))
        self.assertFalse(self.model.get_model_info()["configured"])

    def test_single_class_auc_and_ap_cannot_be_published(self):
        path = self.directory / "evaluation.json"
        evaluation = json.loads(path.read_text(encoding="utf-8"))
        evaluation["endpoints"][TOX21_ENDPOINTS[0]].update(positives=0, negatives=2, auroc=None, averagePrecision=0.5)
        write_json(path, evaluation)
        self.mutate(lambda m: m["artifacts"]["evaluation"].update(sha256=digest(path)))
        self.assertFalse(self.model.get_model_info()["configured"])

    def test_cuda_missing_is_503(self):
        import torch
        with patch.object(torch.cuda, "is_available", return_value=False):
            model = MultitaskModelClient(self.directory, device="cuda", allow_research=True)
            self.assertEqual(model.get_status()["errorCode"], "MULTITASK_CUDA_UNAVAILABLE")

    def test_missing_encoder_is_503(self):
        self.mutate(lambda m: m["architecture"].update(featureBranches=["molformer"], inputDim=768))
        # Update preprocessing to the declared width, so encoder is the missing dependency.
        np.savez(self.directory / "preprocessing.npz", mean=np.zeros(768), scale=np.ones(768))
        self.mutate(lambda m: m["artifacts"]["preprocessing"].update(sha256=digest(self.directory / "preprocessing.npz")))
        self.assertEqual(self.model.get_status()["errorCode"], "MULTITASK_ENCODER_MISSING")

    def test_malformed_metadata_is_unavailable(self):
        self.mutate(lambda m: m.update(training=[]))
        self.assertFalse(self.model.get_status()["configured"])

    def test_legacy_leakage_claim_is_rejected(self):
        self.mutate(lambda m: m["training"].update(legacyHeldoutAndKarimUsedForSelection=True))
        self.assertFalse(self.model.get_status()["configured"])

    def test_manifest_paths_cannot_escape(self):
        self.mutate(lambda m: m["artifacts"]["weights"].update(file="../weights.npz"))
        self.assertFalse(self.model.get_status()["configured"])


if __name__ == "__main__":
    unittest.main()
