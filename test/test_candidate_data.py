"""Synthetic integrity regressions; these are not model performance tests."""
import tempfile
import unittest
import hashlib
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from backend.candidate_data import (SOURCES, TOX21_ASSAYS, TOX21_ENDPOINTS, audit_overlap,
                                    download_source, ensure_metadata, normalize_candidate_rows, read_source_rows, structural_identity)


def row(smiles, label, endpoint="herg", source_row=2):
    return {"smiles": smiles, "label": label, "endpoint": endpoint, "source_dataset": "fixture",
            "source_id": str(source_row), "source_row": source_row, "source_assay": "Y"}


def split_manifest(train="CCO", validation="c1ccccc1", test="C1CCCCC1"):
    return {"splits": {name: [{"smiles": smiles, "endpoint": "ames", "label": 1}]
                       for name, smiles in (("train", train), ("validation", validation), ("test", test))}}


class CandidateDataTests(unittest.TestCase):
    def test_official_tox21_contract_masks_missing_and_keeps_tasks_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tox21.csv"
            labels = ["", "1.0", "0.0", *("" for _ in range(9))]
            path.write_text(",".join((*TOX21_ASSAYS, "ID", "X")) + "\n" + ",".join((*labels, "source", "CCO")) + "\n", encoding="utf-8")
            rows, summary = read_source_rows(path, "tox21")
        self.assertEqual(len(rows), 2)
        self.assertEqual([item["endpoint"] for item in rows], list(TOX21_ENDPOINTS[1:3]))
        self.assertEqual([item["label"] for item in rows], [1, 0])
        self.assertEqual(summary["endpoints"][TOX21_ENDPOINTS[0]]["missingLabels"], 1)
        self.assertFalse(any(item["endpoint"] in ("herg", "dili", "ames", "clintox") for item in rows))

    def test_conflict_quarantines_all_exact_rows_and_duplicates_keep_provenance(self):
        kept, quarantine, duplicates, summary = normalize_candidate_rows([
            row("CCO", 1), row("OCC", 0, source_row=3), row("CCO", 1, source_row=4),
            row("CCN", 1, source_row=5), row("NCC", 1, source_row=6),
        ])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["source_row"], 5)
        self.assertEqual(len(quarantine), 3)
        self.assertEqual(duplicates[0]["source_row"], 6)
        self.assertEqual(summary["endpoints"]["herg"]["conflictingStructureGroups"], 1)

    def test_salt_charge_and_stereo_parent_identity(self):
        salt = structural_identity("[Cl-].C[NH2+][C@H](C)c1ccccc1")
        free = structural_identity("CN[C@@H](C)c1ccccc1")
        self.assertNotEqual(salt["smiles"], free["smiles"])
        self.assertEqual(salt["parent_smiles"], free["parent_smiles"])
        self.assertEqual(salt["parent_scaffold"], free["parent_scaffold"])

    def test_every_old_split_and_other_endpoint_are_excluded(self):
        kept, _, _, _ = normalize_candidate_rows([
            row("OCC", 1), row("Cc1ccccc1", 1, source_row=3), row("OC1CCCCC1", 0, source_row=4),
            row("c1ccsc1", 0, source_row=5),
        ])
        pool, excluded, summary = audit_overlap(kept, split_manifest())
        self.assertEqual([item["smiles"] for item in pool], ["c1ccsc1"])
        self.assertEqual(len(excluded), 3)
        self.assertTrue(summary["againstAllPriorEndpoints"])
        self.assertEqual(summary["bySplit"]["validation"]["scaffoldLabelRows"], 1)
        self.assertEqual(summary["bySplit"]["test"]["scaffoldLabelRows"], 1)

    def test_parent_match_excludes_salt_and_stereo_even_if_canonical_differs(self):
        kept, _, _, _ = normalize_candidate_rows([row("[Cl-].C[NH2+][C@H](C)c1ccccc1", 1)])
        pool, excluded, summary = audit_overlap(kept, split_manifest(train="CN[C@@H](C)c1ccccc1"))
        self.assertEqual(pool, [])
        self.assertIn("train:parent", excluded[0]["reason"])
        self.assertEqual(summary["bySplit"]["train"]["canonicalLabelRows"], 0)

    def test_within_candidate_cross_source_overlap_is_counted_without_merging_tasks(self):
        herg = row("c1ccsc1", 1)
        herg["source_dataset"] = "herg_karim"
        tox21 = row("c1sccc1", 0, endpoint=TOX21_ENDPOINTS[0], source_row=3)
        tox21["source_dataset"] = "tox21"
        kept, _, _, _ = normalize_candidate_rows([herg, tox21])
        pool, _, summary = audit_overlap(kept, split_manifest())
        self.assertEqual(len(pool), 1)
        self.assertEqual(summary["withinCandidates"]["canonicalStructuresSharedBetweenCandidateSources"], 1)
        self.assertEqual(summary["byDataset"]["tox21"]["candidateUniqueMolecules"], 1)
        self.assertEqual(len(kept), 2)

    def test_external_pool_parent_duplicates_collapsed_conflicts_excluded(self):
        kept, _, _, _ = normalize_candidate_rows([
            row("N[C@H](C)c1ccsc1", 1), row("N[C@@H](C)c1ccsc1", 1, source_row=3),
            row("N[C@H](C)c1ccoc1", 1, source_row=4), row("N[C@@H](C)c1ccoc1", 0, source_row=5),
        ])
        pool, exclusions, summary = audit_overlap(kept, split_manifest())
        self.assertEqual(len(pool), 1)
        self.assertEqual(summary["externalHergPool"]["parentConflictExcludedRows"], 2)
        self.assertEqual(summary["externalHergPool"]["parentDuplicateExcludedRows"], 1)
        self.assertEqual(len(exclusions), 3)

    def test_strict_invalid_wildcard_and_model_size_are_quarantined(self):
        kept, quarantine, _, summary = normalize_candidate_rows([
            row("*CC", 0), row("C1CC", 1, source_row=3), row("CCCC", 1, source_row=4),
            row("[301C]", 1, source_row=5), row("CCO", 0, source_row=6),
        ], max_atoms=3)
        self.assertEqual([item["smiles"] for item in kept], ["CCO"])
        self.assertEqual(len(quarantine), 4)
        self.assertEqual(summary["invalidStructureSourceRows"], 4)

    def test_source_hash_and_contract_changes_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / SOURCES[0]["fileName"]
            path.write_text("Drug_ID\tDrug\tY\n1\tCCO\t1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                download_source(SOURCES[0], Path(folder), offline=True)
            path.write_text("smiles,label\nCCO,1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "column contract"):
                read_source_rows(path, "herg_karim")

    def test_missing_nonbinary_and_boolean_labels_never_become_negatives(self):
        kept, quarantine, _, _ = normalize_candidate_rows([row("CCO", ""), row("CCN", True), row("CCC", "2")])
        self.assertEqual(kept, [])
        self.assertEqual(len(quarantine), 3)

    def test_missing_offline_or_changed_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "metadata.py"
            with self.assertRaisesRegex(ValueError, "Missing verified TDC metadata"):
                ensure_metadata(path, offline=True)
            path.write_text("raise RuntimeError('never execute downloaded metadata')", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "metadata checksum mismatch"):
                ensure_metadata(path)

    def test_downloaded_metadata_hash_mismatch_never_creates_executable_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "metadata.py"
            with patch("backend.candidate_data.urlopen") as response:
                response.return_value.__enter__.return_value.read.return_value = b"raise RuntimeError('untrusted')"
                with self.assertRaisesRegex(ValueError, "checksum changed"):
                    ensure_metadata(path)
            self.assertFalse(path.exists())

    def test_verified_metadata_is_downloaded_and_saved_without_execution(self):
        content = b"raise AssertionError('metadata must stay inert')\n"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog" / "metadata.py"
            with patch("backend.candidate_data.TDC_METADATA_SHA256", hashlib.sha256(content).hexdigest()):
                with patch("backend.candidate_data.urlopen") as response:
                    response.return_value.__enter__.return_value.read.return_value = content
                    ensure_metadata(path)
                self.assertEqual(path.read_bytes(), content)
                ensure_metadata(path, offline=True)

    def test_parent_failure_is_distinguished_from_invalid_input(self):
        kept, quarantine, _, summary = normalize_candidate_rows([row("[SbH6+3]", 1), row("C1CC", 0, source_row=3)])
        self.assertEqual(kept, [])
        self.assertEqual(len(quarantine), 2)
        stage_counts = summary["byDataset"]["fixture"]["structureFailureSourceRowsByStage"]
        self.assertEqual(stage_counts["parentNormalizationFailure"], 1)
        self.assertEqual(stage_counts["invalidInputStructure"], 1)

    def test_official_access_denial_does_not_retry_or_bypass(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "metadata.py"
            with patch("backend.candidate_data.urlopen", side_effect=HTTPError("official", 403, "denied", {}, None)) as call:
                with self.assertRaisesRegex(ValueError, "unavailable \\(403\\)"):
                    ensure_metadata(path)
            self.assertEqual(call.call_count, 1)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
