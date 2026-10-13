"""Small chemistry/provenance fixtures, never experimental model results."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import v3_data
from backend.training_data import ENDPOINTS, sha256_file


def observation(smiles, label=1, endpoint="ames", number=2):
    return {"smiles": smiles, "endpoint": endpoint, "label": label, "source_id": "unit-fixture-not-real-data", "source_row": number}


class V3DevelopmentChemistryTests(unittest.TestCase):
    def test_invalid_or_wildcard_smiles_do_not_enter_development(self):
        for smiles in ("C1CC", "*CC", "CCO ethanol", "", "[301C]"):
            with self.assertRaises(ValueError):
                v3_data.leakage_identity(smiles)

    def test_salt_stereo_and_isotope_comparison_preserves_real_input(self):
        salt = v3_data.leakage_identity("[Cl-].C[NH2+][C@H](C)c1ccccc1")
        free = v3_data.leakage_identity("CN[C@@H](C)c1ccccc1")
        isotope = v3_data.leakage_identity("[13CH3]N[C@H](C)c1ccccc1")
        self.assertNotEqual(salt["canonical"], free["canonical"])
        self.assertNotEqual(isotope["canonical"], free["canonical"])
        self.assertEqual(salt["parent"], free["parent"])
        self.assertEqual(isotope["parent"], free["parent"])
        self.assertIn("13", isotope["canonical"])
        self.assertNotIn("13", isotope["parent"])

    def test_tautomer_equivalents_are_audit_keys_not_label_replacements(self):
        lactam = v3_data.leakage_identity("O=c1cccc[nH]1")
        lactim = v3_data.leakage_identity("Oc1ccccn1")
        self.assertNotEqual(lactam["canonical"], lactim["canonical"])
        self.assertEqual(lactam["tautomerParent"], lactim["tautomerParent"])

    def test_old_test_parent_and_scaffold_forbid_whole_connected_group(self):
        rows = [observation("N[C@H](C)c1ccccc1"), observation("Cc1ccccc1", number=3),
                observation("c1ccsc1", number=4), observation("C1CCC1", number=5)]
        samples, exclusions, audit, _ = v3_data.split_development(rows, {"old-test": ["N[C@@H](C)c1ccccc1"]})
        self.assertEqual(len(exclusions), 2)
        self.assertFalse(any("c1ccccc1" in sample["smiles"] for sample in samples))
        self.assertEqual(audit["forbiddenRoles"]["old-test"]["structures"], 1)
        self.assertFalse(audit["exposedLabelsUsedForDevelopment"])

    def test_tautomer_and_isotope_holdout_identity_blocks_training(self):
        for candidate, holdout in (("O=c1cccc[nH]1", "Oc1ccccn1"), ("[13CH3]C1CCC1", "CC1CCC1")):
            samples, exclusions, _, _ = v3_data.split_development([observation(candidate)], {"old-validation": [holdout]})
            self.assertEqual(samples, [])
            self.assertEqual(len(exclusions), 1)

    def test_no_missing_label_imputation_and_boolean_labels_rejected(self):
        samples, _, _, _ = v3_data.split_development([observation("c1ccsc1")], {})
        self.assertEqual(samples[0]["labels"], {"ames": 1})
        for label in (None, "", True, "1", 2):
            with self.assertRaises(ValueError):
                v3_data.split_development([observation("CCO", label)], {})
        with self.assertRaises(ValueError):
            v3_data.split_development([observation("CCO", endpoint="tox21_sr_p53")], {})

    def test_conflicting_and_duplicate_canonical_labels_fail_closed(self):
        for second_label in (0, 1):
            with self.assertRaises(ValueError):
                v3_data.split_development([observation("CCO", 1), observation("OCC", second_label)], {})

    def test_acyclic_group_is_indivisible_even_if_split_is_impossible(self):
        rows = [observation("C" * length, length % 2, number=length + 2) for length in range(1, 12)]
        samples, _, audit, _ = v3_data.split_development(rows, {})
        self.assertEqual({sample["split"] for sample in samples}, {"train"})
        self.assertEqual(len({sample["groupId"] for sample in samples}), 1)
        self.assertEqual(audit["acyclicGroupSizes"], [11])
        self.assertEqual(audit["status"], "blocked-insufficient-disjoint-groups")
        self.assertTrue(any("empty" in warning for warning in audit["warnings"]))

    def test_scaffold_and_parent_components_cannot_cross_train_validation(self):
        rows = [observation(smiles, index % 2, number=index + 2) for index, smiles in enumerate(
            ("c1ccccc1", "Cc1ccccc1", "c1ccncc1", "C1CCCC1", "C1CCC1", "c1ccsc1", "c1ccoc1"))]
        samples, _, _, forbidden = v3_data.split_development(rows, {}, seed=42)
        repeated, _, _, _ = v3_data.split_development(list(reversed(rows)), {}, seed=42)
        self.assertEqual(samples, repeated)
        self.assertEqual({sample["split"] for sample in samples}, {"train", "validation"})
        changed = copy.deepcopy(samples)
        benzene = [sample for sample in changed if sample["auditIdentity"]["scaffolds"] == ["c1ccccc1"]]
        self.assertEqual(len(benzene), 2)
        benzene[1]["split"] = "validation" if benzene[0]["split"] == "train" else "train"
        with self.assertRaisesRegex(ValueError, "leakage"):
            v3_data.verify_development_samples(changed, forbidden)

    def test_common_encoder_filter_is_label_blind_and_never_truncates(self):
        def reject_long(smiles):
            if len(smiles) > 8:
                raise ValueError("token-budget-fixture")
            return smiles

        rows = [observation("CCCCCCCCCC", 1), observation("c1ccsc1", 0, number=3)]
        samples, exclusions, _, _ = v3_data.split_development(rows, {}, label_blind_filter=reject_long)
        self.assertEqual([sample["smiles"] for sample in samples], ["c1ccsc1"])
        self.assertEqual(exclusions[0]["reason"], "label-blind-encoder-input-incompatible")
        reversed_labels = [{**row, "label": 1 - row["label"]} for row in rows]
        other, _, _, _ = v3_data.split_development(reversed_labels, {}, label_blind_filter=reject_long)
        self.assertEqual([sample["smiles"] for sample in samples], [sample["smiles"] for sample in other])

    def test_unchanged_old_train_label_provenance_is_required(self):
        source = observation("c1ccsc1", 1)
        samples, _, _, _ = v3_data.split_development([source], {})
        frozen = {"splits": {"train": [source], "validation": [], "test": []}}
        v3_data._verify_old_train_membership(samples, frozen)
        samples[0]["labels"]["ames"] = 0
        with self.assertRaisesRegex(ValueError, "old TRAIN"):
            v3_data._verify_old_train_membership(samples, frozen)
        with self.assertRaisesRegex(ValueError, "old TRAIN"):
            v3_data._verify_old_train_membership(samples, {"splits": {"train": [], "test": [source]}})


class V3DevelopmentArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.rows = [observation(smiles, index % 2, number=index + 2) for index, smiles in enumerate(
            ("c1ccccc1", "c1ccncc1", "C1CCCC1", "C1CCC1", "c1ccsc1", "c1ccoc1"))]
        self.source = self.root / "unit-only-frozen-split.json"
        self.source.write_text(json.dumps({"splits": {"train": self.rows, "validation": [], "test": []}}), encoding="utf-8")
        self.sources = [("frozen-v2-split", self.source, sha256_file(self.source))]
        self.inputs = (self.rows, {}, {"modelVersion": "test-fixture-not-real-data"},
                       {"tox21Rows": [], "candidateManifest": {}}, self.sources)
        self.output = self.root / "development"
        # Only authenticity of tiny nonexperimental fixtures is replaced. Byte,
        # source-race, chemistry and label-membership verification still execute.
        self.input_patch = patch("backend.v3_data._verified_inputs", return_value=self.inputs)
        self.role_patch = patch("backend.v3_data._verify_source_role_contract", return_value={"frozen-v2-split": self.source})
        self.input_patch.start()
        self.role_patch.start()

    def tearDown(self):
        self.input_patch.stop()
        self.role_patch.stop()
        self.temporary.cleanup()

    def prepare(self):
        return v3_data.prepare_v3_data(self.root, self.root, self.root / "candidates", self.output,
                                       self.root, encoder_compatible="none")

    def test_prepare_load_preserves_order_mask_and_independent_version(self):
        prepared = self.prepare()
        manifest, samples = v3_data.load_v3_data(self.output)
        self.assertEqual(prepared, manifest)
        self.assertEqual(samples, sorted(samples, key=lambda sample: sample["smiles"]))
        self.assertEqual(len(samples), len(self.rows))
        self.assertTrue(all(set(sample["labels"]) == {"ames"} for sample in samples))
        self.assertEqual(manifest["evaluationStatus"], "not-evaluated")
        self.assertFalse(manifest["trainingPermission"]["externalOrHeldoutLabelsAllowed"])
        self.assertFalse(manifest["trainingPermission"]["commercialRedistributionApproved"])

    def test_output_overwrite_is_refused(self):
        self.prepare()
        before = (self.output / "manifest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.prepare()
        self.assertEqual((self.output / "manifest.json").read_bytes(), before)

    def test_source_missing_or_changed_hash_refuses_loading(self):
        self.prepare()
        original = self.source.read_bytes()
        self.source.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            v3_data.load_v3_data(self.output)
        self.source.unlink()
        with self.assertRaisesRegex(ValueError, "Missing verified"):
            v3_data.load_v3_data(self.output)

    def test_data_hash_failure_prevents_loading(self):
        self.prepare()
        (self.output / "samples.jsonl").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            v3_data.load_v3_data(self.output)

    def test_blocked_data_and_new_chemistry_revision_refuse_loading(self):
        self.prepare()
        manifest_path = self.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for field, value, message in (("status", "completed", "blocked/incomplete"),
                                      ("chemistryVersion", "future-unknown", "Unsupported v3"),
                                      ("taskContractVersion", "future-unknown", "Unsupported v3")):
            altered = {**manifest, field: value}
            manifest_path.write_text(json.dumps(altered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, message):
                v3_data.load_v3_data(self.output)

    def test_fda_without_structure_mapping_is_explicitly_not_a_zero_overlap(self):
        result = v3_data._fda_audit(self.root / "missing-fda", [])
        self.assertFalse(result["trainingImported"])
        self.assertEqual(result["structureOverlap"], "not-assessable-no-verified-structure-mapping")
        self.assertEqual(result["status"], "missing-audit-only-dependency")

    def test_authenticity_contract_cannot_omit_real_source_inputs(self):
        self.role_patch.stop()
        with self.assertRaisesRegex(ValueError, "immutable experimental/heldout source contract"):
            v3_data._verify_source_role_contract([], self.root)
        self.role_patch.start()


if __name__ == "__main__":
    unittest.main()
