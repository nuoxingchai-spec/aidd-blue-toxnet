"""Tiny 16-task integrity fixtures; no trained metrics or real-data claims."""
import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import multitask_data as data
from backend.candidate_data import ROW_FIELDS, TOX21_ASSAYS, TOX21_ENDPOINTS, normalize_candidate_rows, read_source_rows
from backend.training_data import sha256_file


def four(smiles, task="ames", label=1, number=2):
    return {"smiles": smiles, "endpoint": task, "label": label, "source_id": "unit-fixture", "source_row": number}


def tox(smiles, task=0, label=1, number=2):
    return {"smiles": smiles, "endpoint": TOX21_ENDPOINTS[task], "label": label, "source_id": "T" + str(number),
            "source_row": number, "source_assay": TOX21_ASSAYS[task], "source_dataset": "tox21"}


class MultitaskChemistryTests(unittest.TestCase):
    def test_four_and_twelve_task_contracts_remain_independent(self):
        self.assertEqual(len(data.ALL_ENDPOINTS), 16)
        contracts = data.task_contracts()
        self.assertEqual(set(contracts), set(data.ALL_ENDPOINTS))
        for endpoint, assay in zip(TOX21_ENDPOINTS, TOX21_ASSAYS):
            self.assertEqual(contracts[endpoint]["sourceAssay"], assay)
            self.assertFalse(contracts[endpoint]["clinicalToxicityInterpretation"])
        self.assertIn("CT_TOX", contracts["clintox"]["positiveClass"])

    def test_missing_labels_remain_absent_and_tasks_share_one_molecule(self):
        samples, _, _, _ = data.split_multitask_data([four("c1ccsc1", "herg", 0), tox("c1sccc1", 0, 1)], {})
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["labels"], {"herg": 0, TOX21_ENDPOINTS[0]: 1})
        self.assertEqual(len(samples[0]["provenance"]), 2)
        self.assertNotIn("dili", samples[0]["labels"])

    def test_tox21_cannot_be_relabelled_as_a_four_endpoint(self):
        changed = {**tox("c1ccsc1"), "endpoint": "herg"}
        with self.assertRaisesRegex(ValueError, "cannot supply"):
            data.split_multitask_data([changed], {})
        with self.assertRaisesRegex(ValueError, "assay/task"):
            data.split_multitask_data([{**tox("c1ccsc1"), "source_assay": "SR-p53"}], {})
        for label in (True, None, "", "1", 2):
            with self.assertRaises(ValueError):
                data.split_multitask_data([tox("c1ccsc1", label=label)], {})

    def test_cross_task_parent_scaffold_and_tautomer_grouping(self):
        rows = [four("[Cl-].C[NH2+][C@H](C)c1ccccc1"), tox("CN[C@@H](C)c1ccccc1"),
                four("O=c1cccc[nH]1", number=3), tox("Oc1ccccn1", number=3),
                four("c1ccsc1", number=4), tox("C1CCC1", number=4)]
        samples, _, _, forbidden = data.split_multitask_data(rows, {})
        grouped = {}
        for sample in samples:
            grouped.setdefault(sample["groupId"], []).append(sample)
        self.assertEqual(sorted(map(len, grouped.values())), [1, 1, 2, 2])
        changed = copy.deepcopy(samples)
        pair = next(group for group in grouped.values() if len(group) == 2)
        for sample in changed:
            if sample["smiles"] == pair[0]["smiles"]:
                sample["split"] = "validation" if pair[1]["split"] == "train" else "train"
        with self.assertRaisesRegex(ValueError, "leakage"):
            data.verify_multitask_samples(changed, forbidden)

    def test_all_tasks_are_excluded_for_old_heldout_near_identity(self):
        rows = [four("[13CH3]C1CCC1"), tox("CC1CCC1"), tox("c1ccsc1", number=3)]
        samples, excluded, audit, _ = data.split_multitask_data(rows, {"old-test": ["CC1CCC1"]})
        self.assertEqual([sample["smiles"] for sample in samples], ["c1ccsc1"])
        self.assertEqual(len(excluded), 2)
        self.assertFalse(audit["exposedHeldoutLabelsUsedForSelection"])

    def test_acyclic_component_is_never_fractured_to_make_split(self):
        samples, _, audit, _ = data.split_multitask_data([four("CCO"), tox("CCN")], {})
        self.assertEqual(len({sample["groupId"] for sample in samples}), 1)
        self.assertEqual(audit["status"], "blocked-insufficient-disjoint-groups")
        self.assertFalse(audit["calibrationCertificationAllowed"])

    def test_common_encoder_filter_is_applied_before_any_task_split(self):
        def allowed(smiles):
            if smiles == "c1ccsc1":
                raise ValueError("fixture-incompatible")
            return smiles

        samples, exclusions, _, _ = data.split_multitask_data([four("c1ccsc1"), tox("c1ccsc1"), tox("C1CCC1")], {}, label_blind_filter=allowed)
        self.assertEqual(len(samples), 1)
        self.assertEqual(len(exclusions), 2)
        self.assertEqual(samples[0]["smiles"], "C1CCC1")

    def test_old_test_label_cannot_be_authorized_by_tox21_membership(self):
        rows = [four("c1ccsc1", "herg", 1), tox("c1ccsc1")]
        samples, _, _, _ = data.split_multitask_data(rows, {})
        with self.assertRaisesRegex(ValueError, "old TRAIN/Tox21"):
            data.verify_observation_membership(samples, {"splits": {"train": [], "test": [rows[0]]}}, [rows[1]])

    def test_missing_previous_development_is_unknown_overlap_not_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            reference = data._previous_development(Path(folder) / "not-distributed", [])
        self.assertEqual(reference["role"], "previously-exposed-development")
        self.assertTrue(reference["unknownStructureReuse"])
        self.assertIsNone(data.previous_development_overlap([], reference))
        self.assertFalse(reference["newBlindTestClaimed"])


class MultitaskSourceReconstructionTests(unittest.TestCase):
    def test_raw_assay_columns_rebuild_labels_and_distinct_missing_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "raw").mkdir()
            (root / "processed").mkdir()
            raw = root / "raw/tox21.csv"
            columns = (*TOX21_ASSAYS, "ID", "X")
            with raw.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerow({TOX21_ASSAYS[0]: "1.0", "ID": "source-a", "X": "c1ccsc1"})
                writer.writerow({TOX21_ASSAYS[1]: "0.0", "ID": "source-b", "X": "C1CCC1"})
            observed, _ = read_source_rows(raw, "tox21")
            retained, _, _, _ = normalize_candidate_rows(observed)
            processed = root / "processed/tox21.csv"
            with processed.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=ROW_FIELDS)
                writer.writeheader()
                writer.writerows(retained)
            with patch("backend.multitask_data.TOX21_PROCESSED_SHA256", sha256_file(processed)), patch.dict(data.TOX21_SPEC, {"documentedRows": 2}):
                rebuilt, audit = data._verified_tox21_rows(root, retained)
                self.assertEqual(len(rebuilt), 2)
                self.assertTrue(audit["rawLabelsRebuiltAndMatched"])
                self.assertEqual(audit["rawMissingAssayCells"], 22)
                self.assertEqual(audit["retainedCandidateMissingAssayCells"], 22)
                altered = copy.deepcopy(retained)
                altered[0]["label"] = 1 - altered[0]["label"]
                with self.assertRaisesRegex(ValueError, "reproduced from pinned raw"):
                    data._verified_tox21_rows(root, altered)


class MultitaskArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "development"
        self.previous = self.root / "previous"
        self.previous.mkdir()
        self.four_rows = [four(smiles, endpoint, i % 2, i + 2) for i, (smiles, endpoint) in enumerate(
            (("c1ccsc1", "herg"), ("c1ccoc1", "ames"), ("C1CCC1", "dili"), ("C1CCCC1", "clintox"), ("c1ccncc1", "ames")))]
        self.tox_rows = [tox("c1ccsc1", 0, 1), tox("c1ccoc1", 1, 0, 3)]
        self.frozen = self.root / "split.json"
        self.frozen.write_text(json.dumps({"splits": {"train": self.four_rows, "validation": [four("C1CC1")], "test": [four("c1ccccc1")]}}), encoding="utf-8")
        self.pool = self.root / "unit-only-exposed-pool.csv"
        self.pool.write_text("smiles\nc1cncnc1\n", encoding="utf-8")
        self.tox_path = self.root / "unit-only-tox21.csv"
        with self.tox_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=tuple(self.tox_rows[0]))
            writer.writeheader()
            writer.writerows(self.tox_rows)
        self.prior_manifest, self.prior_split = self.previous / "manifest.json", self.previous / "split.json"
        self.prior_manifest.write_text(json.dumps({"datasetVersion": data.PREVIOUS_DEVELOPMENT_VERSION}), encoding="utf-8")
        self.prior_split.write_text(json.dumps({"splits": {"train": [row["smiles"] for row in self.four_rows[1:]], "validation": [self.four_rows[0]["smiles"]]}}), encoding="utf-8")
        sources = [("frozen-v2-split", self.frozen, sha256_file(self.frozen)),
                   ("previously-exposed-herg-pool", self.pool, sha256_file(self.pool)),
                   ("audit-only-tox21-clean-structures", self.tox_path, sha256_file(self.tox_path))]
        inputs = (self.four_rows, {"old-validation": ["C1CC1"], "old-test": ["c1ccccc1"], "previously-exposed-herg-karim": ["c1cncnc1"]},
                  {"modelVersion": "unit-only-source-not-real-data"}, {"tox21Rows": self.tox_rows}, sources)
        # Authenticity of tiny nonexperimental sources is substituted; every
        # byte check, chemical grouping, observation and historical index check runs.
        self.patches = [patch("backend.multitask_data._verified_inputs", return_value=inputs),
                        patch("backend.multitask_data._verified_tox21_rows", return_value=(self.tox_rows, {"rawLabelsRebuiltAndMatched": True})),
                        patch("backend.multitask_data._verify_source_role_contract", side_effect=lambda records, directory: {row["role"]: (directory / row["pathFromData"]).resolve() for row in records}),
                        patch("backend.multitask_data.TOX21_PROCESSED_SHA256", sha256_file(self.tox_path)),
                        patch("backend.multitask_data.PREVIOUS_DEVELOPMENT_MANIFEST_SHA256", sha256_file(self.prior_manifest)),
                        patch("backend.multitask_data.PREVIOUS_DEVELOPMENT_SPLIT_SHA256", sha256_file(self.prior_split))]
        for mocked in self.patches:
            mocked.start()

    def tearDown(self):
        for mocked in reversed(self.patches):
            mocked.stop()
        self.temporary.cleanup()

    def prepare(self, previous=None):
        return data.prepare_multitask_data(self.root, self.root, self.root / "candidates", self.output, self.root,
            previous_development_dir=previous or self.previous, encoder_compatible="none")

    def test_real_loader_contract_roundtrip_keeps_mask_and_previous_exposure(self):
        expected = self.prepare()
        manifest, samples = data.load_multitask_data(self.output)
        self.assertEqual(expected, manifest)
        self.assertEqual(len(samples), len(self.four_rows))
        self.assertEqual(manifest["previouslyExposedDevelopment"]["role"], "previously-exposed-development")
        self.assertFalse(manifest["newBlindTestClaimed"])
        self.assertEqual(manifest["calibrationStatus"], "not-fitted")
        source = next(row for row in manifest["sourceFiles"] if row["role"] == "audit-only-tox21-clean-structures")
        self.assertEqual(source["useInThisProtocol"], "multitask-experimental-label-source")

    def test_missing_optional_previous_reference_loads_with_unknown_reuse(self):
        self.prepare(self.root / "not-distributed")
        manifest, _ = data.load_multitask_data(self.output)
        self.assertIsNone(manifest["previouslyExposedDevelopment"]["canonicalOverlapWithCurrentSplits"])
        self.assertTrue(manifest["previouslyExposedDevelopment"]["unknownStructureReuse"])

    def test_hash_changed_or_missing_original_source_refuses_loading(self):
        self.prepare()
        self.tox_path.write_bytes(self.tox_path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            data.load_multitask_data(self.output)
        self.tox_path.unlink()
        with self.assertRaisesRegex(ValueError, "Missing verified"):
            data.load_multitask_data(self.output)

    def test_rehashed_empty_forbidden_index_cannot_bleach_historical_exclusions(self):
        manifest = self.prepare()
        path = self.output / "forbidden-identities.json"
        path.write_bytes(data._json_bytes({}))
        manifest["files"]["forbiddenIdentities"]["sha256"] = sha256_file(path)
        manifest["datasetVersion"] = data._data_version(manifest["files"], manifest["sourceFiles"], manifest["encoderCompatibility"])
        (self.output / "manifest.json").write_bytes(data._json_bytes(manifest))
        with self.assertRaisesRegex(ValueError, "fixed historical references"):
            data.load_multitask_data(self.output)

    def test_rehashed_fake_tox21_label_cannot_become_real_experimental_data(self):
        manifest = self.prepare()
        path = self.output / "samples.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        sample = next(row for row in rows if TOX21_ENDPOINTS[0] in row["labels"])
        sample["labels"][TOX21_ENDPOINTS[0]] = 0
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        manifest["files"]["samples"]["sha256"] = sha256_file(path)
        manifest["datasetVersion"] = data._data_version(manifest["files"], manifest["sourceFiles"], manifest["encoderCompatibility"])
        (self.output / "manifest.json").write_bytes(data._json_bytes(manifest))
        with self.assertRaisesRegex(ValueError, "unchanged verified"):
            data.load_multitask_data(self.output)

    def test_duplicate_source_roles_or_false_training_usage_refused(self):
        manifest = self.prepare()
        repeated = manifest["sourceFiles"] + [manifest["sourceFiles"][0]]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            data._multitask_source_roles(repeated, self.output)
        changed = copy.deepcopy(manifest["sourceFiles"])
        changed[0]["useInThisProtocol"] = "allow-old-test-training"
        with self.assertRaisesRegex(ValueError, "usage contract"):
            data._multitask_source_roles(changed, self.output)

    def test_output_overwrite_and_calibration_certification_are_refused(self):
        manifest = self.prepare()
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.prepare()
        manifest["calibrationCertificationAllowed"] = True
        (self.output / "manifest.json").write_bytes(data._json_bytes(manifest))
        with self.assertRaisesRegex(ValueError, "calibration certification"):
            data.load_multitask_data(self.output)


if __name__ == "__main__":
    unittest.main()
