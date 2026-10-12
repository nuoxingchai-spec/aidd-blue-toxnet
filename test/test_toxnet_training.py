"""Representation and training-boundary regressions on explicit synthetic data."""
import contextlib
import csv
import hashlib
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch
from rdkit import Chem

from backend.chemistry import canonicalize_smiles
from backend.toxnet import ATOM_CARDINALITIES, DEFAULT_ARCHITECTURE, build_model, collate_features, featurize_smiles, predict_logits
from backend.validation import ENDPOINTS, scaffold_split
from scripts import train_toxnet as trainer


SMALL_ARCHITECTURE = {**DEFAULT_ARCHITECTURE, "hiddenSize": 12, "layers": 1, "heads": 3, "feedForward": 24, "fingerprintSize": 64, "dropout": 0.0}


class MolecularRepresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)

    def test_legal_equivalent_smiles_produce_identical_complete_features(self):
        for first, second in (("CCO", "OCC"), ("c1ccccc1", "C1=CC=CC=C1"), ("[Na+].[Cl-]", "[Cl-].[Na+]"), ("[CH3:7][CH2:1][OH:9]", "CCO")):
            features1 = featurize_smiles(first, DEFAULT_ARCHITECTURE)
            features2 = featurize_smiles(second, DEFAULT_ARCHITECTURE)
            for key in features1:
                self.assertTrue(torch.equal(features1[key], features2[key]), (first, second, key))

    def test_isotope_mass_has_a_distinct_encoded_feature(self):
        ordinary = featurize_smiles("CCO", DEFAULT_ARCHITECTURE)
        isotope = featurize_smiles("[13CH3]CO", DEFAULT_ARCHITECTURE)
        self.assertFalse(torch.equal(ordinary["atoms"], isotope["atoms"]))
        self.assertEqual(isotope["atoms"].shape[-1], len(ATOM_CARDINALITIES))
        self.assertIn(13, isotope["atoms"][0, 1:, -1].tolist())
        with self.assertRaisesRegex(ValueError, "300"):
            featurize_smiles("[301CH4]", DEFAULT_ARCHITECTURE)

    def test_chirality_and_double_bond_geometry_survive_encoding(self):
        left = featurize_smiles("C[C@H](O)C(=O)O", DEFAULT_ARCHITECTURE)
        right = featurize_smiles("C[C@@H](O)C(=O)O", DEFAULT_ARCHITECTURE)
        self.assertFalse(torch.equal(left["atoms"], right["atoms"]))
        # Bond geometry is currently represented through chirality-aware Morgan.
        trans = featurize_smiles("F/C=C/F", DEFAULT_ARCHITECTURE)
        cis = featurize_smiles("F/C=C\\F", DEFAULT_ARCHITECTURE)
        self.assertFalse(torch.equal(trans["fingerprint"], cis["fingerprint"]))

    def test_disconnected_fragments_and_triple_bonds_keep_graph_information(self):
        salt = featurize_smiles("[Na+].[Cl-]", DEFAULT_ARCHITECTURE)
        self.assertEqual(salt["atoms"].shape[1], 3)
        self.assertEqual(salt["distances"][0, 1, 2].item(), 9)
        self.assertEqual(salt["bonds"][0, 1, 2].item(), 0)
        nitrile = featurize_smiles("CC#N", DEFAULT_ARCHITECTURE)
        self.assertEqual(int((nitrile["bonds"][0] == 3).sum()), 2)
        for index, cardinality in enumerate(ATOM_CARDINALITIES):
            self.assertTrue(bool((nitrile["atoms"][..., index] >= 0).all()))
            self.assertTrue(bool((nitrile["atoms"][..., index] < cardinality).all()))

    def test_graph_model_is_invariant_to_atom_permutation(self):
        torch.manual_seed(2048)
        model = build_model(SMALL_ARCHITECTURE).eval()
        features = featurize_smiles("CC(=O)Oc1ccccc1C(=O)O", SMALL_ARCHITECTURE)
        length = features["atoms"].shape[1]
        order = torch.tensor([0, *range(length - 1, 0, -1)])
        permuted = {**features, "atoms": features["atoms"][:, order], "mask": features["mask"][:, order], "distances": features["distances"][:, order][:, :, order], "bonds": features["bonds"][:, order][:, :, order]}
        with torch.inference_mode():
            first = predict_logits(model, features)
            second = predict_logits(model, permuted)
        torch.testing.assert_close(first, second, atol=2e-6, rtol=2e-5)

    def test_padding_does_not_change_predictions_or_leak_other_molecule_atoms(self):
        torch.manual_seed(4096)
        model = build_model(SMALL_ARCHITECTURE).eval()
        small = featurize_smiles("CCO", SMALL_ARCHITECTURE)
        large = featurize_smiles("CC(=O)Oc1ccccc1C(=O)O", SMALL_ARCHITECTURE)
        packed = collate_features([small, large])
        with torch.inference_mode():
            alone = predict_logits(model, small)[0]
            together = predict_logits(model, packed)[0]
            # Even nonzero, chemically unrelated padding must remain invisible.
            packed["atoms"][0, small["atoms"].shape[1]:, 0] = 118
            changed_padding = predict_logits(model, packed)[0]
        torch.testing.assert_close(alone, together, atol=2e-6, rtol=2e-5)
        torch.testing.assert_close(alone, changed_padding, atol=2e-6, rtol=2e-5)


class TrainingHelperTests(unittest.TestCase):
    def test_observed_tasks_merge_without_imputing_missing_labels(self):
        records = trainer.molecular_records([
            {"canonicalSmiles": "CCO", "endpoint": "herg", "label": 1},
            {"canonicalSmiles": "CCO", "endpoint": "ames", "label": 0},
            {"canonicalSmiles": "CCN", "endpoint": "dili", "label": 1},
        ])
        indexed = {record["smiles"]: record for record in records}
        np.testing.assert_array_equal(indexed["CCO"]["mask"], [1, 0, 1, 0])
        np.testing.assert_array_equal(indexed["CCO"]["labels"], [1, 0, 0, 0])
        np.testing.assert_array_equal(indexed["CCN"]["mask"], [0, 1, 0, 0])

    def test_missing_validation_labels_do_not_change_checkpoint_score(self):
        records = [{"labels": np.array([0, 1, 1, 1]), "mask": np.array([1, 0, 0, 0])}, {"labels": np.array([1, 0, 0, 0]), "mask": np.array([1, 0, 0, 0])}]
        logits = np.array([[-1.0, 100.0, -100.0, 0.0], [1.0, -100.0, 100.0, 0.0]])
        self.assertEqual(trainer.validation_auc(records, logits), 1.0)
        for record in records:
            record["labels"][1:] = 1 - record["labels"][1:]
        self.assertEqual(trainer.validation_auc(records, logits), 1.0)

    def test_calibration_uses_only_observed_endpoint_labels(self):
        records = [{"labels": np.array([i % 2, 1, 0, 1], dtype=float), "mask": np.array([1, 0, 0, 0])} for i in range(12)]
        logits = np.array([[(-1.0 if i % 2 == 0 else 1.0), 100.0, -100.0, 0.0] for i in range(12)])
        first = trainer.calibrate(records, logits)
        self.assertEqual(first["fitStatus"]["herg"], "fitted")
        for endpoint in ENDPOINTS[1:]:
            self.assertEqual(first["fitStatus"][endpoint], "not-fitted-insufficient-classes")
            self.assertEqual(first["temperatures"][endpoint], 1.0)
            self.assertEqual(first["biases"][endpoint], 0.0)
        for record in records:
            record["labels"][1:] = 1 - record["labels"][1:]
        second = trainer.calibrate(records, logits)
        self.assertEqual(first, second)
        self.assertTrue(all(math.isfinite(value) and value > 0 for value in first["temperatures"].values()))
        self.assertEqual(first["fitSplit"], "validation")


class TrainingBoundaryIntegrationTests(unittest.TestCase):
    def test_real_training_masks_checkpoint_and_calibration_before_held_out_test(self):
        # Every ring scaffold has positive/negative examples for all tasks, plus
        # sparsely labelled examples. These are synthetic labels, never a benchmark.
        rings = ("c1ccccc1", "c1ccncc1", "c1ccoc1", "c1ccsc1", "C1CCCCC1", "C1CCCC1", "C1CCC1", "C1CC1", "c1ncncc1", "c1cncnc1", "c1ccc2ccccc2c1", "C1CCNCC1")
        rows = []
        for ring in rings:
            for variant in range(4):
                canonical = canonicalize_smiles("C" * (variant + 1) + ring)
                endpoints = ENDPOINTS if variant < 2 else ("herg", "ames") if variant == 2 else ("dili", "clintox")
                rows.extend({"smiles": canonical, "endpoint": endpoint, "label": variant % 2, "source_id": "synthetic-regression-only", "source_row": len(rows) + 2} for endpoint in endpoints)
        split = scaffold_split(rows, seed=2027)
        self.assertFalse(split["warnings"])
        ownership = {name: {row["canonicalSmiles"] for row in members} for name, members in split["splits"].items()}
        self.assertFalse(ownership["train"] & ownership["validation"])
        self.assertFalse(ownership["train"] & ownership["test"])
        masks = {record["smiles"]: record["mask"] for record in trainer.molecular_records([dict(row, canonicalSmiles=row["smiles"]) for row in rows])}
        features_to_mask, current_masks, gradient_checks = {}, [], []
        validation_snapshots, initial_state = [], {}
        boundary = {"calibrated": False, "testCalls": 0}
        original_features, original_collate = trainer.featurize_smiles, trainer.collate_features
        original_loss, original_predict = trainer.F.binary_cross_entropy_with_logits, trainer.predict_records
        original_calibrate, original_build = trainer.calibrate, trainer.build_model

        def capture_features(smiles, architecture):
            features = original_features(smiles, architecture)
            features_to_mask[id(features)] = masks[smiles]
            return features

        def capture_collation(features):
            current_masks[:] = [features_to_mask[id(feature)] for feature in features]
            return original_collate(features)

        def capture_loss(*args, **kwargs):
            result = original_loss(*args, **kwargs)
            observed = torch.tensor(np.stack(current_masks), dtype=torch.bool)
            def verify_gradient(gradient):
                self.assertTrue(bool((gradient[~observed] == 0).all()))
                self.assertTrue(bool((gradient[observed] > 0).all()))
                if bool((~observed).any()):
                    gradient_checks.append(True)
                return gradient
            result.register_hook(verify_gradient)
            return result

        def capture_build(architecture):
            model = original_build(architecture)
            initial_state.update({key: value.detach().clone() for key, value in model.state_dict().items()})
            return model

        def capture_predictions(model, records, device, batch_size):
            identities = {record["smiles"] for record in records}
            snapshot = {key: value.detach().clone() for key, value in model.state_dict().items()}
            if identities == ownership["validation"]:
                validation_snapshots.append(snapshot)
            elif identities == ownership["test"]:
                self.assertTrue(boundary["calibrated"])
                boundary["testCalls"] += 1
                for key, value in snapshot.items():
                    self.assertTrue(torch.equal(value, validation_snapshots[1][key]), key)
            else:
                self.fail("Unexpected data subset used for checkpoint selection or evaluation")
            return original_predict(model, records, device, batch_size)

        def capture_calibration(records, logits):
            self.assertEqual({record["smiles"] for record in records}, ownership["validation"])
            for key, value in validation_snapshots[-1].items():
                self.assertTrue(torch.equal(value, validation_snapshots[1][key]), key)
            boundary["calibrated"] = True
            return original_calibrate(records, logits)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            data, provenance, output = directory / "labels.csv", directory / "data-manifest.json", directory / "trained-fixture"
            with data.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, ["smiles", "endpoint", "label", "source_id", "source_row"])
                writer.writeheader()
                writer.writerows(rows)
            provenance.write_text(json.dumps({"datasetVersion": "synthetic-regression-only-v1", "sources": [], "output": {"sha256": hashlib.sha256(data.read_bytes()).hexdigest()}}), encoding="utf-8")
            arguments = ["train_toxnet.py", "--data", str(data), "--data-manifest", str(provenance), "--output", str(output), "--epochs", "3", "--patience", "3", "--batch-size", "8", "--threads", "1", "--seed", "2027", "--device", "cpu"]
            with patch.object(sys, "argv", arguments), patch.object(trainer, "DEFAULT_ARCHITECTURE", SMALL_ARCHITECTURE), patch.object(trainer, "featurize_smiles", side_effect=capture_features), patch.object(trainer, "collate_features", side_effect=capture_collation), patch.object(trainer.F, "binary_cross_entropy_with_logits", side_effect=capture_loss), patch.object(trainer, "build_model", side_effect=capture_build), patch.object(trainer, "predict_records", side_effect=capture_predictions), patch.object(trainer, "calibrate", side_effect=capture_calibration), patch.object(trainer, "validation_auc", side_effect=[0.7, 0.8, 0.75]), patch.object(trainer, "add_uncertainty"), contextlib.redirect_stdout(io.StringIO()):
                trainer.main()
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["training"]["bestEpoch"], 2)
            self.assertEqual(manifest["training"]["batchSize"], 8)
            self.assertFalse(manifest["training"]["testUsedForSelection"])
            self.assertEqual(boundary["testCalls"], 1)
            self.assertTrue(gradient_checks, "Sparse labels must have been exercised by actual training")
            self.assertTrue(any(not torch.equal(value, validation_snapshots[1][key]) for key, value in initial_state.items()), "Training must have updated actual model weights")
            references = set(json.loads((output / "training-references.json").read_text(encoding="utf-8")))
            self.assertEqual(references, ownership["train"])
            test_predictions = json.loads((output / "test-predictions.json").read_text(encoding="utf-8"))
            self.assertEqual({record["smiles"] for record in test_predictions}, ownership["test"])
            self.assertEqual(len(test_predictions), split["actualCounts"]["test"])


if __name__ == "__main__":
    unittest.main()
