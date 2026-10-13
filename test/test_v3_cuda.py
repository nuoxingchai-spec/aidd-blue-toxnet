"""CPU-only acceptance-tool tests. Fixtures are never CUDA benchmark evidence."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import torch

from scripts import verify_v3_cuda as verifier


class FakeCuda:
    """Deterministic bookkeeping fixture; no real device or molecular model."""
    OutOfMemoryError = torch.cuda.OutOfMemoryError

    @staticmethod
    def is_available():
        return False

    @staticmethod
    def empty_cache():
        pass

    @staticmethod
    def synchronize():
        pass

    @staticmethod
    def reset_peak_memory_stats():
        pass

    @staticmethod
    def memory_allocated():
        return 10

    @staticmethod
    def memory_reserved():
        return 20

    @staticmethod
    def max_memory_allocated():
        return 30

    @staticmethod
    def max_memory_reserved():
        return 40


class CudaVerificationTests(unittest.TestCase):
    def test_cpu_wheel_is_distinguished_from_unusable_cuda_build(self):
        for build, kind in ((None, "cpu"), ("12.6", "cuda")):
            fake = SimpleNamespace(__version__="fixture-only", version=SimpleNamespace(cuda=build), cuda=FakeCuda())
            with patch.object(verifier, "driver_information", return_value={"status": "fixture-only"}):
                environment = verifier.environment_information(fake)
            self.assertEqual(environment["cudaBuildVersion"], build)
            self.assertEqual(environment["pytorchWheelKind"], kind)
            self.assertFalse(environment["cudaAvailable"])
            self.assertEqual(environment["gpuDevices"], [])

    def test_missing_torch_dependency_does_not_claim_device_available(self):
        with patch.object(verifier, "driver_information", return_value={"status": "fixture-only"}), patch.object(
            verifier.importlib, "import_module", side_effect=ImportError("fixture missing torch")
        ):
            environment = verifier.environment_information()
        self.assertFalse(environment["cudaAvailable"])
        self.assertIsNone(environment["torchVersion"])
        self.assertEqual(environment["environmentError"]["type"], "ImportError")

    def test_no_cuda_never_imports_or_substitutes_molecular_weights(self):
        environment = {"cudaAvailable": False, "pytorchWheelKind": "cpu", "fixtureOnly": True}
        with patch.object(verifier, "environment_information", return_value=environment), patch.object(
            verifier, "artifact_files", side_effect=AssertionError("must not load fake artifacts")
        ):
            report = verifier.run_verification(encoder_dir=Path("absent"), model_dir=Path("absent"), data_dir=Path("absent"))
        self.assertEqual(report["status"], "not-tested-no-cuda-device")
        self.assertEqual(report["reason"], "CPU-only PyTorch wheel")
        self.assertEqual(report["encoderBatches"], [])
        self.assertEqual(report["headTrainingBatches"], [])
        self.assertIsNone(report["maximumSuccessfulCombinedBatchSize"])
        self.assertFalse(report["formalAvailability"])

    def test_requested_sizes_are_explicit_bounded_and_nonduplicated(self):
        self.assertEqual(verifier.validate_batch_sizes([8, 1, 4, 2]), (1, 2, 4, 8))
        for values in ([], [0], [9], [1, 1], [True], [1.0]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                verifier.validate_batch_sizes(values)

    def test_bad_threads_or_tolerance_rejected_before_environment_loading(self):
        for extra in ({"threads": 0}, {"threads": True}, {"atol": 0}, {"rtol": float("nan")}, {"atol": 1}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                verifier.run_verification(encoder_dir=Path("x"), model_dir=Path("x"), data_dir=Path("x"), **extra)

    def test_wrapped_genuine_oom_recognized_without_gpu(self):
        wrapped = ValueError("encoder failure")
        wrapped.__cause__ = torch.cuda.OutOfMemoryError("unit fixture, not a real OOM")
        self.assertTrue(verifier.is_cuda_oom(wrapped, torch))
        self.assertFalse(verifier.is_cuda_oom(ValueError("CUDA memory insufficient text alone"), torch))

    def test_phase_measurement_reports_success_and_oom_separately(self):
        fake = SimpleNamespace(cuda=FakeCuda())
        passed, value = verifier.measure_cuda_phase(fake, lambda: {"fixtureOnly": True})
        self.assertEqual(value, {"fixtureOnly": True})
        self.assertEqual(passed["status"], "passed")
        self.assertFalse(passed["oom"])
        self.assertEqual(passed["peakAdditionalAllocatedBytes"], 20)
        self.assertEqual(passed["peakAdditionalReservedBytes"], 20)
        def fail():
            raise torch.cuda.OutOfMemoryError("unit fixture only")
        failed, value = verifier.measure_cuda_phase(fake, fail)
        self.assertIsNone(value)
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["oom"])
        self.assertGreaterEqual(failed["executionSeconds"], 0)

    def test_training_probe_rejects_validation_or_missing_rows_before_cuda(self):
        for samples in ([], [{"split": "validation"}], [{"split": "train"}, {"split": "test"}]):
            with self.subTest(samples=samples), self.assertRaisesRegex(ValueError, "train rows"):
                verifier.train_head_probe(torch, None, None, None, samples, None)

    def test_tensor_state_digest_detects_mutation_without_modifying_model(self):
        model = torch.nn.Linear(3, 2)
        before = verifier.state_digest(model)
        self.assertEqual(before, verifier.state_digest(copy.deepcopy(model)))
        with torch.no_grad():
            model.weight.add_(1)
        self.assertNotEqual(before, verifier.state_digest(model))

    def test_model_and_data_binding_rejects_mismatched_or_unfinished_artifact(self):
        tasks = ["fixture-task-" + str(index) for index in range(16)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "manifest.json").write_text('{"fixtureOnly":true}', encoding="utf-8")
            data = {"datasetVersion": "fixture-only", "splitSha256": "a" * 64}
            manifest = {"formalAvailability": False, "status": "trained-completed", "tasks": tasks,
                        "architecture": {"featureBranches": ["molformer"], "headType": "shared-mlp"},
                        "dataset": {**data, "manifestSha256": verifier.digest(path / "manifest.json")},
                        "training": {"endpointStatus": {task: "trained" for task in tasks}}}
            verifier.verify_development_binding(data, manifest, path, tasks)
            for section, field, value in ((None, "status", "pending"), (None, "formalAvailability", True),
                                          ("dataset", "splitSha256", "b" * 64), ("architecture", "headType", "random-model")):
                altered = copy.deepcopy(manifest)
                (altered if section is None else altered[section])[field] = value
                with self.assertRaisesRegex(ValueError, "actual fully trained"):
                    verifier.verify_development_binding(data, altered, path, tasks)

    def test_report_output_preserves_history_and_cannot_write_into_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = {"fixtureOnly": True, "status": "not-tested-no-cuda-device"}
            options = {"encoder_dir": root / "encoder", "model_dir": root / "head", "data_dir": root / "dataset"}
            output = root / "results" / "verification.json"
            verifier.save_report(output, report, **options)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)
            with self.assertRaisesRegex(ValueError, "exists"):
                verifier.save_report(output, report, **options)
            for output in (root / "encoder" / "new.json", root / "head" / "weights.pt", root / "dataset" / "manifest.json"):
                with self.assertRaisesRegex(ValueError, "separate"):
                    verifier.save_report(output, report, **options)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
