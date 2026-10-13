"""CPU regressions only. Synthetic labels/features here are never benchmarks."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch

from backend.chemistry import canonicalize_smiles
from backend.validation import ENDPOINTS
from backend import v3_data
from backend import v3_experiments as experiments


def fixture_samples():
    # Pure helper-regression inputs. The full dataset loader independently
    # verifies real structural groups, rights and legacy/exposed exclusions.
    molecules = ("Cc1ccccc1", "CCc1ccccc1", "Cc1ccncc1", "CCc1ccncc1", "Cc1ccoc1", "CCc1ccoc1", "CC1CCCC1", "CCC1CCCC1", "CC1CCC1", "CCC1CCC1")
    samples = []
    for index, smiles in enumerate(molecules):
        labels = {endpoint: index % 2 for endpoint in ENDPOINTS}
        if index == 2:
            del labels["ames"]
        samples.append({"smiles": canonicalize_smiles(smiles), "labels": labels,
                        "split": "train" if index < 6 else "validation", "groupId": f"fixture-{index // 2}"})
    return samples


class V3FeatureAndMetricTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)

    def test_morgan_dimension_equivalence_and_order(self):
        features = experiments.morgan_features(["CCO", "OCC", "CCN"])
        self.assertEqual(features.shape, (3, 2048))
        self.assertEqual(features.dtype, np.float32)
        np.testing.assert_array_equal(features[0], features[1])
        self.assertFalse(np.array_equal(features[0], features[2]))
        np.testing.assert_array_equal(experiments.morgan_features(["CCN", "CCO"]), features[[2, 0]])

    def test_invalid_smiles_rejects_rather_than_imputes_features(self):
        for invalid in ("", "garbage", "C(", "[CH5]"):
            with self.assertRaises(ValueError):
                experiments.morgan_features([invalid])

    def test_morgan_stereo_salt_and_documented_isotope_alias(self):
        features = experiments.morgan_features(["C[C@H](O)C(=O)O", "C[C@@H](O)C(=O)O", "CCO", "[13CH3]CO", "[Na+].[Cl-]", "[Cl-].[Na+]"])
        self.assertFalse(np.array_equal(features[0], features[1]))
        # RDKit's standard Morgan atom invariants do not encode isotope mass.
        # Keep full chemical identity distinct and disclose B0's alias instead
        # of pretending the reference fingerprint can distinguish isotopes.
        self.assertNotEqual(canonicalize_smiles("CCO"), canonicalize_smiles("[13CH3]CO"))
        np.testing.assert_array_equal(features[2], features[3])
        np.testing.assert_array_equal(features[4], features[5])

    def test_missing_label_mask_and_observed_label_contract(self):
        labels, mask = experiments.label_arrays([{"labels": {"herg": 1, "ames": 0}}])
        np.testing.assert_array_equal(labels, [[1, 0, 0, 0]])
        np.testing.assert_array_equal(mask, [[1, 0, 1, 0]])
        for labels in ({"newtox": 0}, {"herg": True}, {"herg": 2}, {"herg": None}):
            with self.assertRaises(ValueError):
                experiments.label_arrays([{"labels": labels}])

    def test_masked_loss_zero_gradient_even_nan_unknown_labels(self):
        logits = torch.tensor([[0.2, 10.0, -20.0, 0.5]], requires_grad=True)
        labels = torch.tensor([[1.0, float("nan"), 0.0, 1.0]])
        mask = torch.tensor([[1.0, 0.0, 0.0, 1.0]])
        loss = experiments.masked_binary_loss(logits, labels, mask)
        self.assertTrue(bool(torch.isfinite(loss)))
        loss.backward()
        self.assertEqual(logits.grad[0, 1].item(), 0)
        self.assertEqual(logits.grad[0, 2].item(), 0)
        changed = labels.clone()
        changed[0, 1:3] = torch.tensor([-1e8, 1e8])
        torch.testing.assert_close(loss.detach(), experiments.masked_binary_loss(logits.detach(), changed, mask))

    def test_standardizer_cannot_see_validation_distribution(self):
        first = np.array([[1., 5.], [3., 5.], [1000., -1000.]], dtype=np.float32)
        train = np.array([True, True, False])
        transformed, state = experiments.standardize_train_only(first, train, True)
        second = first.copy()
        second[-1] = [-1e8, 1e8]
        _, state_second = experiments.standardize_train_only(second, train, True)
        np.testing.assert_array_equal(state["mean"], [2., 5.])
        np.testing.assert_array_equal(state["scale"], [1., 1.])
        np.testing.assert_array_equal(transformed[:2], [[-1., 0.], [1., 0.]])
        np.testing.assert_array_equal(state["mean"], state_second["mean"])
        np.testing.assert_array_equal(state["scale"], state_second["scale"])
        identity, unchanged = experiments.standardize_train_only(first, train, False)
        np.testing.assert_array_equal(identity, first)
        np.testing.assert_array_equal(unchanged["mean"], [0., 0.])

    def test_single_class_and_no_data_metrics_have_explicit_nulls(self):
        labels = np.array([[0, 1, 0, 1], [1, 1, 1, 0]], dtype=float)
        masks = np.array([[1, 1, 0, 0], [1, 1, 0, 0]], dtype=float)
        probabilities = np.array([[.1, .8, .9, .2], [.9, .8, .1, .8]])
        metrics = experiments.evaluate_probabilities(labels, masks, probabilities)
        self.assertEqual(metrics["endpoints"]["herg"]["auroc"], 1.)
        self.assertEqual(metrics["endpoints"]["dili"]["positives"], 2)
        self.assertIsNone(metrics["endpoints"]["dili"]["auroc"])
        self.assertIsNone(metrics["endpoints"]["dili"]["averagePrecision"])
        self.assertAlmostEqual(metrics["endpoints"]["dili"]["brier"], .04)
        self.assertAlmostEqual(metrics["endpoints"]["dili"]["ece10Bins"], .2)
        self.assertIsNone(metrics["endpoints"]["ames"]["brier"])
        self.assertEqual(metrics["endpoints"]["ames"]["status"], "no-data")
        self.assertFalse(metrics["independentTest"])

    def test_invalid_score_is_not_metric_evidence(self):
        for invalid in (float("nan"), 1.01, -.1):
            with self.assertRaises(ValueError):
                experiments.evaluate_probabilities(np.zeros((2, 4)), np.ones((2, 4)), np.full((2, 4), invalid))

    def test_fusion_ablation_dimensions_and_missing_branch_reject(self):
        morgan, embedding = np.ones((2, 2048)), np.zeros((2, 768))
        self.assertEqual(experiments.assemble_features(("molformer", "morgan"), morgan, embedding).shape, (2, 2816))
        self.assertEqual(experiments.select_feature_branches("F1", ("morgan",)), ("morgan",))
        self.assertEqual(experiments.select_feature_branches("F1", ("morgan", "molformer")), ("molformer", "morgan"))
        with self.assertRaises(ValueError):
            experiments.assemble_features(("molformer",), morgan, None)
        with self.assertRaises(ValueError):
            experiments.select_feature_branches("F1", ("morgan", "morgan"))


class V3BoundaryAndTrainingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)

    def test_old_test_duplicate_and_cross_split_groups_rejected(self):
        for mutation in ("test", "duplicate", "group"):
            samples = fixture_samples()
            if mutation == "test":
                samples[-1]["split"] = "test"
            elif mutation == "duplicate":
                samples[-1]["smiles"] = samples[0]["smiles"]
            else:
                samples[-1]["groupId"] = samples[0]["groupId"]
            with self.assertRaises(ValueError):
                experiments.validate_development_samples(samples)

    def test_missing_training_endpoint_does_not_make_fake_predictor(self):
        samples = fixture_samples()
        for sample in samples:
            if sample["split"] == "train":
                sample["labels"]["clintox"] = 0
        with self.assertRaisesRegex(ValueError, "train/clintox"):
            experiments.validate_development_samples(samples)

    def test_single_class_validation_has_no_selection_key(self):
        samples = fixture_samples()
        for sample in samples:
            if sample["split"] == "validation":
                sample["labels"] = {endpoint: 1 for endpoint in ENDPOINTS}
        with self.assertRaisesRegex(ValueError, "Validation"):
            experiments.validate_development_samples(samples)

    def test_formal_model_path_and_existing_output_are_protected(self):
        with self.assertRaises(ValueError):
            experiments.assert_fresh_output(experiments.ROOT / "models/aidd-toxnet")
        with self.assertRaises(ValueError):
            experiments.assert_fresh_output(experiments.ROOT / "models/aidd-toxnet/new-v3")
        with self.assertRaises(ValueError):
            experiments.assert_fresh_output(experiments.ROOT / "models")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileExistsError):
                experiments.assert_fresh_output(Path(directory))

    def test_explicit_missing_cuda_and_baselines_not_skipped(self):
        with patch.object(experiments.torch.cuda, "is_available", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "CUDA"):
                experiments.resolve_device("cuda")
            self.assertEqual(experiments.resolve_device("auto"), "cpu")
        with self.assertRaisesRegex(ValueError, "must not skip"):
            experiments.run_experiments(data_dir=Path("not-used"), output=Path("not-used"), models=("F1",))

    def test_production_data_module_is_importable_and_missing_data_rejects(self):
        self.assertTrue(callable(v3_data.load_v3_data))
        self.assertEqual(v3_data.DATA_SCHEMA, "aidd.toxnet.development-data.v3")
        # Exercise run_experiments' real deferred import, without installing a
        # virtual sys.modules entry that can hide a misspelled module name.
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "not-trained"
            with self.assertRaisesRegex(ValueError, "Missing verified v3 dependency"):
                experiments.run_experiments(data_dir=Path(directory) / "missing-data", output=output, models=("B0",), threads=1)
            self.assertFalse(output.exists())

    def test_actual_lr_fits_only_observed_train_labels(self):
        samples = fixture_samples()
        labels, mask = experiments.validate_development_samples(samples)
        train = np.array([sample["split"] == "train" for sample in samples])
        features = experiments.morgan_features([sample["smiles"] for sample in samples])
        head = experiments.fit_logistic_head(features, labels, mask, train, 42)
        self.assertEqual(head["coefficients"].shape, (4, 2048))
        changed = labels.copy()
        changed[~train] = 1 - changed[~train]
        changed[mask == 0] = 1e7
        other = experiments.fit_logistic_head(features, changed, mask, train, 42)
        np.testing.assert_array_equal(head["coefficients"], other["coefficients"])
        np.testing.assert_array_equal(head["intercepts"], other["intercepts"])
        probabilities = experiments.predict_feature_head(head, features)
        self.assertEqual(probabilities.shape, (len(samples), 4))
        self.assertTrue(np.isfinite(probabilities).all())

    def test_mlp_updates_real_weights_and_restores_validation_checkpoint(self):
        samples = fixture_samples()
        labels, mask = experiments.validate_development_samples(samples)
        train = np.array([sample["split"] == "train" for sample in samples])
        features = np.arange(len(samples) * 4, dtype=np.float32).reshape(-1, 4) / 20
        initial, snapshots = {}, []
        original_build, original_predict = experiments.FrozenFeatureMLP, experiments.predict_feature_head
        def capture_build(*args, **kwargs):
            model = original_build(*args, **kwargs)
            initial.update({name: value.detach().clone() for name, value in model.state_dict().items()})
            return model
        def capture_predict(model, x, device, batch_size):
            snapshots.append({name: value.detach().clone() for name, value in model.state_dict().items()})
            self.assertEqual(len(x), int((~train).sum()))
            return original_predict(model, x, device, batch_size)
        with patch.object(experiments, "FrozenFeatureMLP", side_effect=capture_build), patch.object(experiments, "predict_feature_head", side_effect=capture_predict), patch.object(experiments, "evaluate_probabilities", side_effect=[{"macroAUROC": .6, "macroAP": .7}, {"macroAUROC": .8, "macroAP": .8}, {"macroAUROC": .7, "macroAP": .9}]):
            model, history, best = experiments.fit_mlp_head(features, labels, mask, train, 42, epochs=3, patience=3, batch_size=3, hidden_size=8)
        self.assertEqual(best, 2)
        self.assertEqual(len(history), 3)
        for name, value in model.state_dict().items():
            torch.testing.assert_close(value, snapshots[1][name])
        self.assertTrue(any(not torch.equal(value, initial[name]) for name, value in model.state_dict().items()))
        self.assertEqual(sum(parameter.numel() for parameter in model.parameters()), 76)

    def test_sha256_changes_on_file_modification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "weights.npz"
            path.write_bytes(b"real-byte-regression-fixture")
            first = experiments.sha256_file(path)
            self.assertEqual(first, hashlib.sha256(path.read_bytes()).hexdigest())
            path.write_bytes(b"corrupted-fixture")
            self.assertNotEqual(first, experiments.sha256_file(path))

    def test_real_b0_three_seed_artifacts_and_incomplete_run_boundary(self):
        samples = fixture_samples()
        manifest = {"schema": "aidd.toxnet.development-data.v3", "datasetVersion": "synthetic-unit-fixture-only",
                    "taskContractVersion": "synthetic-unit-fixture-task", "splitSha256": "a" * 64,
                    "trainingPermission": {"status": "unit-fixture-never-a-benchmark"}}
        with tempfile.TemporaryDirectory() as directory, patch.object(v3_data, "load_v3_data", return_value=(manifest, samples)), patch.object(experiments, "resolve_device", return_value="cuda"):
            data = Path(directory) / "data"
            data.mkdir()
            (data / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            output = Path(directory) / "diagnostic-b0"
            result = experiments.run_experiments(data_dir=data, output=output, models=("B0",), threads=1)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["seeds"], [42, 43, 44])
            self.assertEqual(len(result["runs"]), 3)
            for seed in result["seeds"]:
                run = output / f"B0-seed-{seed}"
                record = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(record["status"], "trained-completed")
                self.assertEqual(record["parameterCount"], 8196)
                # A CUDA request must not mislabel actual sklearn CPU fitting.
                self.assertEqual(record["training"]["device"], "cpu")
                self.assertIsNone(record["training"]["featureExtractionDevice"])
                self.assertEqual(record["training"]["splits"], {"train": 6, "validation": 4})
                self.assertEqual(record["training"]["endpointStatus"], {endpoint: "trained" for endpoint in ENDPOINTS})
                self.assertEqual(record["training"]["featureTransformFitSplit"], "train")
                self.assertFalse(record["training"]["previouslyExposedUsedForSelection"])
                self.assertFalse(record["formalAvailability"])
                self.assertIn("backend/v3_data.py", record["training"]["sourceHashes"])
                self.assertIn("backend/pretrained_encoder.py", record["training"]["sourceHashes"])
                self.assertEqual(record["evaluationStatus"], "evaluated-validation-only")
                for artifact in record["artifacts"].values():
                    self.assertEqual(experiments.sha256_file(run / artifact["file"]), artifact["sha256"])
                with np.load(run / "weights.npz", allow_pickle=False) as weights:
                    self.assertEqual(weights["coefficients"].shape, (4, 2048))
                predictions = json.loads((run / "validation-predictions.json").read_text(encoding="utf-8"))
                self.assertEqual({row["smiles"] for row in predictions}, {sample["smiles"] for sample in samples if sample["split"] == "validation"})
            failed = Path(directory) / "failed-b0"
            with patch.object(experiments, "fit_logistic_head", side_effect=RuntimeError("deliberate-unit-fixture-failure")):
                with self.assertRaisesRegex(RuntimeError, "deliberate-unit"):
                    experiments.run_experiments(data_dir=data, output=failed, models=("B0",), threads=1)
            self.assertEqual(json.loads((failed / "experiment.json").read_text(encoding="utf-8"))["status"], "incomplete")
            self.assertFalse((failed / "B0-seed-42/manifest.json").exists())

    def test_missing_real_encoder_does_not_generate_completed_outputs(self):
        samples = fixture_samples()
        manifest = {"datasetVersion": "synthetic-unit-fixture-only", "taskContractVersion": "fixture-task", "splitSha256": "a" * 64}
        with tempfile.TemporaryDirectory() as directory, patch.object(v3_data, "load_v3_data", return_value=(manifest, samples)):
            data = Path(directory) / "data"
            data.mkdir()
            (data / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            output = Path(directory) / "not-trained"
            with self.assertRaisesRegex(FileNotFoundError, "locally audited"):
                experiments.run_experiments(data_dir=data, output=output, models=("B0", "B1", "B2"), threads=1)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
