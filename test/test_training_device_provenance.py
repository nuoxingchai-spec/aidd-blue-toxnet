"""CPU metadata regressions; fixture embeddings are never model evidence."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from backend import multitask_data, multitask_experiments, pretrained_encoder, v3_data, v3_experiments
from test.test_multitask_experiments import fixture_samples as multitask_samples
from test.test_v3_experiments import fixture_samples as fourtask_samples


class TrainingDeviceProvenanceTests(unittest.TestCase):
    def _run(self, *, multitask, cache_exists):
        samples = multitask_samples() if multitask else fourtask_samples()
        manifest = {"datasetVersion": "synthetic-device-regression-never-a-benchmark",
                    "taskContractVersion": multitask_data.TASK_CONTRACT_VERSION if multitask else "fixture-only",
                    "splitSha256": "a" * 64,
                    "trainingPermission": {"status": "synthetic-test-only"}}
        if multitask:
            manifest.update(endpoints=list(multitask_data.ALL_ENDPOINTS), taskContracts=multitask_data.task_contracts())
        fixture_encoder = SimpleNamespace(provenance={"dimension": 4, "frozenParameterCount": 8})
        vectors = np.random.default_rng(42).normal(size=(len(samples), 4)).astype(np.float32)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            data = folder / "data"
            data.mkdir()
            (data / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            cache = folder / "explicit-synthetic-cache.npz"
            if cache_exists:
                cache.write_bytes(b"mock-cache-file-not-a-real-embedding")
            loader = multitask_data if multitask else v3_data
            method = "load_multitask_data" if multitask else "load_v3_data"
            with patch.object(loader, method, return_value=(manifest, samples)), \
                 patch.object(pretrained_encoder, "PretrainedEncoder", return_value=fixture_encoder), \
                 patch.object(pretrained_encoder, "cache_embeddings", return_value=vectors):
                output = folder / "output"
                arguments = dict(data_dir=data, output=output, snapshot_dir=folder / "mock-encoder",
                                 embedding_cache=cache, seeds=(42,), threads=1)
                if multitask:
                    multitask_experiments.run_multitask(**arguments, models=("A4", "C_Morgan"),
                        device="cpu", epochs=1, patience=1, minimum_selection_per_class=5)
                    morgan_id, encoded_id, expected_device = "C_Morgan", "A4", "cpu"
                else:
                    # Both heads really fit with sklearn on CPU; this mock only
                    # selects a hypothetical CUDA encoder for field regression.
                    with patch.object(v3_experiments, "resolve_device", return_value="cuda"):
                        v3_experiments.run_experiments(**arguments, models=("B0", "B1"))
                    morgan_id, encoded_id, expected_device = "B0", "B1", "cuda"
            def training(model):
                return json.loads((output / f"{model}-seed-42/manifest.json").read_text(encoding="utf-8"))["training"]
            morgan, encoded = training(morgan_id), training(encoded_id)
            self.assertEqual(morgan["device"], "cpu")
            self.assertIsNone(morgan["encoderLoadDevice"])
            self.assertIsNone(morgan["featureExtractionDevice"])
            self.assertEqual(morgan["featureExtractionMode"], "not-used")
            self.assertEqual(encoded["encoderLoadDevice"], expected_device)
            self.assertEqual(encoded["featureExtractionMode"], "verified-cache-load" if cache_exists else "computed-this-run")
            self.assertEqual(encoded["featureExtractionDevice"], None if cache_exists else expected_device)
            if not multitask:
                self.assertEqual(encoded["device"], "cpu")

    def test_fourtask_morgan_does_not_inherit_shared_encoder_device(self):
        self._run(multitask=False, cache_exists=False)

    def test_fourtask_cache_load_does_not_claim_gpu_recomputation(self):
        self._run(multitask=False, cache_exists=True)

    def test_multitask_morgan_does_not_inherit_shared_encoder_device(self):
        self._run(multitask=True, cache_exists=False)

    def test_multitask_cache_load_does_not_claim_recomputation(self):
        self._run(multitask=True, cache_exists=True)


if __name__ == "__main__":
    unittest.main()
