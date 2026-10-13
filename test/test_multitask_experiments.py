"""CPU sixteen-task regressions; synthetic fixtures never benchmark results."""
import copy
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch

from backend.chemistry import canonicalize_smiles
from backend import multitask_experiments as experiment


def fixture_samples(count=60):
    samples = []
    for index in range(count):
        split = "train" if index < 40 else "validation"
        ring = "c1ccccc1" if split == "train" else "c1ccncc1"
        labels = {task: index % 2 for task in experiment.ALL_ENDPOINTS}
        if index % 5 == 0:
            for task in experiment.FOUR_ENDPOINTS:
                labels.pop(task)
        elif index % 7 == 0:
            labels.pop("clintox")
        samples.append({"smiles": canonicalize_smiles("C" * (index + 1) + ring), "labels": labels,
                        "split": split, "groupId": f"synthetic-{split}-scaffold"})
    return samples


class MultitaskFeatureAndLossTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)

    def test_sixteen_contract_sparse_masks_keep_original_and_assay_labels(self):
        task = experiment.ALL_ENDPOINTS[-1]
        labels, mask = experiment.labels_and_masks([{"labels": {"herg": 1, task: 0}}])
        self.assertEqual(labels.shape, (1, 16))
        self.assertEqual(mask.sum(), 2)
        self.assertEqual(mask[0, 0], 1)
        self.assertEqual(mask[0, -1], 1)
        self.assertEqual(labels[0, 0], 1)
        self.assertEqual(labels[0, -1], 0)
        for invalid in ({"tox21_fake": 1}, {"herg": True}, {task: None}, {task: 2}):
            with self.assertRaises(ValueError):
                experiment.labels_and_masks([{"labels": invalid}])

    def test_nan_missing_labels_and_logits_have_zero_gradient(self):
        logits = torch.tensor([[0.2, float("nan"), float("nan"), 10.]], requires_grad=True)
        labels = torch.tensor([[1., float("nan"), 0., float("nan")]])
        mask = torch.tensor([[1., 0., 1., 0.]])
        weights = torch.tensor([1., 1., 0., 1.])
        loss = experiment.weighted_masked_bce(logits, labels, mask, weights=weights, positive_weights=torch.ones(4))
        self.assertTrue(bool(torch.isfinite(loss)))
        loss.backward()
        self.assertNotEqual(logits.grad[0, 0].item(), 0)
        torch.testing.assert_close(logits.grad[0, 1:], torch.zeros(3))

    def test_zero_observation_batch_returns_none_before_optimizer_step(self):
        logits = torch.full((3, 16), float("nan"), requires_grad=True)
        loss = experiment.weighted_masked_bce(logits, logits, torch.zeros_like(logits), weights=torch.ones(16), positive_weights=torch.ones(16))
        self.assertIsNone(loss)
        self.assertIsNone(logits.grad)

    def test_bad_positive_weights_and_mask_reject(self):
        for bad in (float("nan"), -1., 0.):
            with self.assertRaises(ValueError):
                experiment.weighted_masked_bce(torch.zeros(2, 4), torch.zeros(2, 4), torch.ones(2, 4), weights=torch.ones(4), positive_weights=torch.full((4,), bad))
        with self.assertRaises(ValueError):
            experiment.weighted_masked_bce(torch.zeros(2, 4), torch.zeros(2, 4), torch.full((2, 4), 2.), weights=torch.ones(4), positive_weights=torch.ones(4))

    def test_loss_normalizes_observed_tasks_before_fixed_weights(self):
        logits, labels = torch.zeros(3, 4), torch.zeros(3, 4)
        mask = torch.tensor([[1., 1., 0., 0.], [1., 0., 0., 0.], [1., 0., 0., 0.]])
        loss = experiment.weighted_masked_bce(logits, labels, mask, weights=torch.tensor([1., 3., 0., 0.]), positive_weights=torch.ones(4))
        self.assertAlmostEqual(loss.item(), np.log(2), places=6)

    def test_train_positive_weights_cannot_use_validation_labels(self):
        labels = np.zeros((6, 16), dtype=np.float32)
        labels[0] = 1
        mask = np.ones_like(labels)
        active = np.array([True, True, True, True, False, False])
        first = experiment.positive_weights_train_only(labels, mask, active)
        labels[~active] = 1
        second = experiment.positive_weights_train_only(labels, mask, active)
        np.testing.assert_array_equal(first, second)
        np.testing.assert_allclose(first, np.sqrt(3))

    def test_separate_heads_order_dimension_and_actual_parameter_count(self):
        head = experiment.MultiTaskMLP(768, 16)
        self.assertEqual(sum(parameter.numel() for parameter in head.parameters()), 100496)
        self.assertEqual(sum(parameter.numel() for parameter in experiment.MultiTaskMLP(768, 4).parameters()), 98948)
        with torch.no_grad():
            for index, output in enumerate(head.heads):
                output.weight.zero_()
                output.bias.fill_(index)
        torch.testing.assert_close(head(torch.zeros(2, 768)), torch.arange(16, dtype=torch.float32).repeat(2, 1))
        self.assertEqual(experiment.predict_head(head, np.zeros((2, 768), dtype=np.float32)).shape, (2, 16))
        with self.assertRaises(ValueError):
            experiment.MultiTaskMLP(768, 12)

    def test_task_weights_explicit_controls_and_validation(self):
        weights = experiment.task_weights(experiment.ALL_ENDPOINTS, tox21_weight=.25)
        np.testing.assert_array_equal(weights[:4], np.ones(4))
        np.testing.assert_array_equal(weights[4:], np.full(12, .25))
        for invalid in ({"herg": 0}, {"herg": "1"}, {"herg": True}, {"fake": 1}, {"tox21_nr_ar": float("nan")}):
            with self.assertRaises(ValueError):
                experiment.task_weights(experiment.ALL_ENDPOINTS, overrides=invalid)


class MultitaskMetricsAndBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)

    def test_invalid_smiles_and_cross_split_groups_reject(self):
        for mutation in ("invalid", "test", "group", "duplicate"):
            samples = fixture_samples()
            if mutation == "invalid":
                samples[-1]["smiles"] = "C("
            elif mutation == "test":
                samples[-1]["split"] = "test"
            elif mutation == "group":
                samples[-1]["groupId"] = samples[0]["groupId"]
            else:
                samples[-1]["smiles"] = samples[0]["smiles"]
            with self.assertRaises(ValueError):
                experiment.validate_samples(samples)

    def test_single_class_training_marks_auxiliary_task_untrainable(self):
        samples = fixture_samples()
        labels, mask, train, _ = experiment.validate_samples(samples)
        labels[train, -1] = 0
        status, counts = experiment.training_task_status(labels, mask, train, experiment.ALL_ENDPOINTS, np.ones(16))
        self.assertEqual(status[experiment.ALL_ENDPOINTS[-1]], "not-trainable")
        self.assertEqual(counts[experiment.ALL_ENDPOINTS[-1]]["positive"], 0)
        metrics = experiment.evaluate_tasks(labels[~train], mask[~train], np.full((20, 16), .5), experiment.ALL_ENDPOINTS, status)
        value = metrics["endpoints"][experiment.ALL_ENDPOINTS[-1]]
        self.assertEqual(value["count"], 20)
        self.assertIsNone(value["auroc"])
        self.assertIsNone(value["brier"])
        self.assertEqual(value["scoreCount"], 0)

    def test_selection_ignores_twelve_assays_and_protects_sparse_original_task(self):
        labels, mask, train, _ = experiment.validate_samples(fixture_samples())
        protocol = experiment.eligible_selection_tasks(labels, mask, ~train, minimum_per_class=5)
        self.assertEqual(protocol["eligible"], list(experiment.FOUR_ENDPOINTS))
        metrics = {"endpoints": {task: {"auroc": .7, "averagePrecision": .8} for task in experiment.ALL_ENDPOINTS}}
        first = experiment.selection_key(metrics, protocol)
        for task in experiment.ALL_ENDPOINTS[4:]:
            metrics["endpoints"][task] = {"auroc": 1., "averagePrecision": 1.}
        self.assertEqual(first, experiment.selection_key(metrics, protocol))
        mask[~train, 3] = 0
        sparse = experiment.eligible_selection_tasks(labels, mask, ~train, minimum_per_class=5)
        self.assertNotIn("clintox", sparse["eligible"])
        self.assertIn("clintox", sparse["excluded"])
        with self.assertRaises(ValueError):
            experiment.eligible_selection_tasks(labels, mask, ~train, minimum_per_class=100)

    def test_missing_and_single_class_metrics_remain_null(self):
        labels, mask = np.zeros((4, 16)), np.zeros((4, 16))
        mask[:, 0] = 1
        labels[:, 0] = 1
        metrics = experiment.evaluate_tasks(labels, mask, np.full((4, 16), .8), experiment.ALL_ENDPOINTS,
                                            {endpoint: "trained" for endpoint in experiment.ALL_ENDPOINTS})
        self.assertIsNone(metrics["endpoints"]["herg"]["auroc"])
        self.assertAlmostEqual(metrics["endpoints"]["herg"]["brier"], .04)
        self.assertIsNone(metrics["endpoints"]["dili"]["brier"])
        self.assertEqual(metrics["endpoints"]["dili"]["count"], 0)
        self.assertEqual(set(metrics["endpoints"]), set(experiment.ALL_ENDPOINTS))

    def test_previous_artifact_paths_and_ablation_without_baselines_reject(self):
        for path in (experiment.ROOT / "models/aidd-toxnet", experiment.ROOT / "artifacts/v3/new", experiment.ROOT / "artifacts"):
            with self.assertRaises(ValueError):
                experiment.fresh_output(path)
        with self.assertRaisesRegex(ValueError, "retain"):
            experiment.run_multitask(data_dir=Path("unused"), output=Path("unused"), models=("B16_noTox21",))

    def test_training_checkpoint_and_zero_tox21_control_are_reproducible(self):
        samples = fixture_samples()
        labels, masks, train, four_members = experiment.validate_samples(samples)
        features = np.arange(len(samples) * 8, dtype=np.float32).reshape(-1, 8) / 500
        active = train & four_members
        protocol = experiment.eligible_selection_tasks(labels, masks, ~train, minimum_per_class=5)
        a_weights, b_weights = np.ones(4, dtype=np.float32), np.r_[np.ones(4), np.zeros(12)].astype(np.float32)
        a_status, _ = experiment.training_task_status(labels[:, :4], masks[:, :4], active, experiment.FOUR_ENDPOINTS, a_weights)
        b_status, _ = experiment.training_task_status(labels, masks, active, experiment.ALL_ENDPOINTS, b_weights)
        a, history_a, epoch_a, _ = experiment.fit_mlp(features, labels[:, :4], masks[:, :4], active, ~train, experiment.FOUR_ENDPOINTS, a_status, a_weights, protocol, 42, epochs=3, patience=3, batch_size=7, hidden_size=8)
        b, history_b, epoch_b, _ = experiment.fit_mlp(features, labels, masks, active, ~train, experiment.ALL_ENDPOINTS, b_status, b_weights, protocol, 42, epochs=3, patience=3, batch_size=7, hidden_size=8)
        self.assertEqual(epoch_a, epoch_b)
        for key, value in a.state_dict().items():
            torch.testing.assert_close(value, b.state_dict()[key], rtol=0, atol=0)
        for row_a, row_b in zip(history_a, history_b):
            self.assertAlmostEqual(row_a["trainLoss"], row_b["trainLoss"], places=7)
        self.assertTrue(all(not parameter.requires_grad for head in b.heads[4:] for parameter in head.parameters()))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "weights.pt"
            torch.save(b.state_dict(), path)
            restored = experiment.MultiTaskMLP(8, 16, hidden_size=8)
            restored.load_state_dict(torch.load(path, weights_only=True, map_location="cpu"), strict=True)
            torch.testing.assert_close(restored.eval()(torch.from_numpy(features)), b.eval()(torch.from_numpy(features)))

    def test_lr_uses_train_only_and_never_missing_assay_labels(self):
        samples = fixture_samples()
        labels, mask, train, _ = experiment.validate_samples(samples)
        features = np.arange(len(samples) * 8, dtype=np.float32).reshape(-1, 8) / 500
        status, _ = experiment.training_task_status(labels, mask, train, experiment.ALL_ENDPOINTS, np.ones(16))
        first, trace = experiment.fit_morgan(features, labels, mask, train, experiment.ALL_ENDPOINTS, status, 42)
        changed = labels.copy()
        changed[~train] = 1 - changed[~train]
        changed[mask == 0] = 999
        second, _ = experiment.fit_morgan(features, changed, mask, train, experiment.ALL_ENDPOINTS, status, 42)
        np.testing.assert_array_equal(first["coefficients"], second["coefficients"])
        np.testing.assert_array_equal(first["intercepts"], second["intercepts"])
        self.assertEqual(len(trace[0]["endpoints"]), 16)

    def test_paired_negative_transfer_checks_membership_and_reports_each_seed(self):
        def evaluation(value):
            return {"endpoints": {task: {"auroc": value, "averagePrecision": value, "brier": value, "ece10Bins": value} for task in experiment.FOUR_ENDPOINTS}}
        runs = [{"model": model, "seed": seed, "fourTaskValidationMembershipSha256": "same", "evaluation": evaluation(.8 if model == "A4" else .7)}
                for seed in (42, 43, 44) for model in ("A4", "B16")]
        delta = experiment.paired_deltas(runs, (42, 43, 44))["endpoints"]["herg"]["auroc"]
        self.assertEqual(delta["definedSeeds"], 3)
        self.assertEqual([pair["seed"] for pair in delta["pairs"]], [42, 43, 44])
        self.assertAlmostEqual(delta["meanDelta"], -.1)
        runs[-1]["fourTaskValidationMembershipSha256"] = "different"
        with self.assertRaises(ValueError):
            experiment.paired_deltas(runs, (42, 43, 44))

    def test_original_four_label_membership_hash_ignores_auxiliary_labels(self):
        samples = fixture_samples()
        _, _, train, four = experiment.validate_samples(samples)
        first = experiment.membership_hash(samples, ~train & four)
        changed = copy.deepcopy(samples)
        for sample in changed:
            for task in experiment.ALL_ENDPOINTS[4:]:
                if task in sample["labels"]:
                    sample["labels"][task] = 1 - sample["labels"][task]
        self.assertEqual(first, experiment.membership_hash(changed, ~train & four))
        changed[-1]["labels"]["herg"] = 1 - changed[-1]["labels"]["herg"]
        self.assertNotEqual(first, experiment.membership_hash(changed, ~train & four))

    def test_actual_data_module_contract_and_missing_dependencies(self):
        data = importlib.import_module("backend.multitask_data")
        self.assertEqual(data.ALL_ENDPOINTS, experiment.ALL_ENDPOINTS)
        self.assertEqual(data.TASK_CONTRACT_VERSION, experiment.TASK_CONTRACT_VERSION)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "not-trained"
            with self.assertRaisesRegex(ValueError, "Missing verified v3 dependency"):
                experiment.run_multitask(data_dir=Path(directory) / "missing-data", output=output, models=("C_Morgan",), threads=1)
            self.assertFalse(output.exists())

    def test_actual_morgan_three_seed_artifacts_and_partial_task_rejection(self):
        data = importlib.import_module("backend.multitask_data")
        samples = fixture_samples()
        for sample in samples:
            if sample["split"] == "train":
                sample["labels"][experiment.ALL_ENDPOINTS[-1]] = 0
        manifest = {"schema": data.DATA_SCHEMA, "datasetVersion": "synthetic-regression-only-not-experimental-results",
                    "taskContractVersion": data.TASK_CONTRACT_VERSION, "endpoints": list(data.ALL_ENDPOINTS),
                    "splitSha256": "b" * 64, "taskContracts": data.task_contracts(),
                    "trainingPermission": {"status": "synthetic-unit-fixture-never-a-benchmark"}}
        with tempfile.TemporaryDirectory() as directory, patch.object(data, "load_multitask_data", return_value=(manifest, samples)):
            data_dir = Path(directory) / "data"
            data_dir.mkdir()
            (data_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            output = Path(directory) / "regression-only"
            result = experiment.run_multitask(data_dir=data_dir, output=output, models=("C_Morgan",), threads=1, minimum_selection_per_class=5)
            self.assertEqual(result["status"], "completed-with-partial-models")
            self.assertEqual(len(result["runs"]), 3)
            self.assertEqual(result["seeds"], [42, 43, 44])
            for seed in result["seeds"]:
                run = output / f"C_Morgan-seed-{seed}"
                record = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(record["status"], "trained-partial")
                self.assertEqual(record["training"]["status"], "partial")
                self.assertEqual(record["training"]["endpointStatus"][experiment.ALL_ENDPOINTS[-1]], "not-trainable")
                self.assertEqual(record["parameterCount"], 32784)
                self.assertEqual(record["trainableParameterCount"], 30735)
                self.assertEqual(record["taskContracts"], data.task_contracts())
                self.assertFalse(record["training"]["legacyHeldoutAndKarimUsedForSelection"])
                self.assertTrue(record["training"]["priorV3DevelopmentReuseAllowed"])
                for value in record["artifacts"].values():
                    self.assertEqual(experiment.sha256_file(run / value["file"]), value["sha256"])
                with np.load(run / "weights.npz", allow_pickle=False) as coefficients:
                    self.assertEqual(coefficients["coefficients"].shape, (16, 2048))
                predictions = json.loads((run / "validation-predictions.json").read_text(encoding="utf-8"))
                unavailable = [row for row in predictions if row["endpoint"] == experiment.ALL_ENDPOINTS[-1]]
                self.assertEqual(len(unavailable), 20)
                self.assertTrue(all(row["score"] is None and row["predictionStatus"] == "not-trainable" for row in unavailable))
            failed = Path(directory) / "incomplete"
            with patch.object(experiment, "fit_morgan", side_effect=RuntimeError("deliberate-fixture-failure")):
                with self.assertRaisesRegex(RuntimeError, "deliberate-fixture-failure"):
                    experiment.run_multitask(data_dir=data_dir, output=failed, models=("C_Morgan",), threads=1, minimum_selection_per_class=5)
            self.assertEqual(json.loads((failed / "experiment.json").read_text(encoding="utf-8"))["status"], "incomplete")
            self.assertFalse((failed / "C_Morgan-seed-42/manifest.json").exists())

    def test_missing_pretrained_weights_do_not_generate_simulated_artifacts(self):
        data = importlib.import_module("backend.multitask_data")
        samples = fixture_samples()
        manifest = {"datasetVersion": "synthetic-fixture-only", "taskContractVersion": data.TASK_CONTRACT_VERSION,
                    "endpoints": list(data.ALL_ENDPOINTS), "splitSha256": "b" * 64, "taskContracts": data.task_contracts(),
                    "trainingPermission": {"status": "synthetic-fixture-only"}}
        with tempfile.TemporaryDirectory() as directory, patch.object(data, "load_multitask_data", return_value=(manifest, samples)):
            data_dir = Path(directory) / "data"
            data_dir.mkdir()
            (data_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            output = Path(directory) / "not-trained"
            with self.assertRaisesRegex(FileNotFoundError, "real verified frozen"):
                experiment.run_multitask(data_dir=data_dir, output=output, models=("A4", "B16", "C_Morgan"), minimum_selection_per_class=5)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
