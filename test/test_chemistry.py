"""Regression tests for chemical validity and representation-independent rules."""

import json
import unittest
from hashlib import sha256

from backend.chemistry import (
    CHEMISTRY_VERSION,
    MAX_HEAVY_ATOMS,
    MAX_SMILES_LENGTH,
    RULES_VERSION,
    analyze_chemistry,
    canonicalize_smiles,
)


class ChemistryTests(unittest.TestCase):
    def assert_equivalent(self, first, second):
        left = analyze_chemistry(first)
        right = analyze_chemistry(second)
        for field in ("canonicalSmiles", "structureId", "known", "descriptors", "alerts", "endpoints", "ruleOverall", "tier", "structure"):
            self.assertEqual(left[field], right[field], f"Equivalent SMILES differ in {field}: {first} / {second}")
        return left, right

    def test_benzene_aromatic_and_kekule_properties(self):
        result, _ = self.assert_equivalent("c1ccccc1", "C1=CC=CC=C1")
        descriptors = result["descriptors"]
        self.assertEqual(descriptors["formula"], "C6H6")
        self.assertEqual(descriptors["elementCounts"], {"C": 6, "H": 6})
        self.assertAlmostEqual(descriptors["molecularWeight"], 78.11, places=2)
        self.assertEqual(descriptors["aromaticAtoms"], 6)
        self.assertEqual(descriptors["rings"], 1)
        self.assertEqual(descriptors["rotatableBonds"], 0)

    def test_ethanol_donor_is_also_an_acceptor(self):
        result, _ = self.assert_equivalent("CCO", "OCC")
        descriptors = result["descriptors"]
        self.assertEqual(descriptors["formula"], "C2H6O")
        self.assertAlmostEqual(descriptors["molecularWeight"], 46.07, places=2)
        self.assertEqual(descriptors["hbd"], 1)
        self.assertEqual(descriptors["hba"], 1)
        self.assertAlmostEqual(descriptors["tpsa"], 20.23, places=2)

    def test_charge_and_explicit_hydrogens_do_not_add_fictitious_hydrogen(self):
        salt, _ = self.assert_equivalent("[Na+].[Cl-]", "[Cl-].[Na+]")
        self.assertEqual(salt["descriptors"]["elementCounts"], {"Cl": 1, "Na": 1})
        self.assertAlmostEqual(salt["descriptors"]["molecularWeight"], 58.44, places=2)
        self.assertEqual(sorted(atom["formalCharge"] for atom in salt["structure"]["atoms"]), [-1, 1])
        water, _ = self.assert_equivalent("O", "[H]O[H]")
        self.assertEqual(water["descriptors"]["formula"], "H2O")
        self.assertAlmostEqual(water["descriptors"]["molecularWeight"], 18.02, places=2)
        self.assertEqual(water["descriptors"]["heavyAtoms"], 1)

    def test_reference_lookup_is_canonical_and_does_not_override_rules(self):
        result, _ = self.assert_equivalent("CC(=O)NC1=CC=C(O)C=C1", "CC(=O)Nc1ccc(O)cc1")
        self.assertEqual(result["known"]["id"], "acetaminophen")
        self.assertIn("未核验真实标签来源", result["known"]["note"])
        self.assertEqual(result["descriptors"]["basicAmines"], 0)
        self.assertNotIn("aniline", {alert["id"] for alert in result["alerts"]})
        dili = next(endpoint for endpoint in result["endpoints"] if endpoint["id"] == "dili")
        self.assertEqual(dili["referenceScore"], 0.78)
        self.assertNotEqual(dili["ruleScore"], dili["referenceScore"])
        self.assertEqual(dili["scoreSource"], "rule")
        self.assertEqual(dili["explanationSource"], "structure-rules")

    def test_all_six_demo_compounds_are_valid_reference_fixtures(self):
        examples = (
            ("CC(=O)NC1=CC=C(O)C=C1", "acetaminophen"),
            ("CC(C)(C)C1=CC=C(C=C1)C(O)CCCN1CCC(CC1)C(C2=CC=CC=C2)(C3=CC=CC=C3)O", "terfenadine"),
            ("C1C(=O)NC(=O)N1N=CC2=CC=C(O2)[N+](=O)[O-]", "nitrofurantoin"),
            ("CC(=O)OC1=CC=CC=C1C(=O)O", "aspirin"),
            ("CCO", "ethanol"),
            ("C1=CC=C2C(=C1)C(=O)C3=CC=CC=C3C2=O", "anthraquinone"),
        )
        for smiles, expected_id in examples:
            with self.subTest(compound=expected_id):
                result = analyze_chemistry(smiles)
                self.assertEqual(result["known"]["id"], expected_id)
                self.assertEqual(len(result["endpoints"]), 4)
                self.assertTrue(all(endpoint["referenceScore"] is not None for endpoint in result["endpoints"]))
                self.assertTrue(all(endpoint["scoreSource"] == "rule" for endpoint in result["endpoints"]))
        nitrofurantoin = analyze_chemistry(examples[2][0])
        self.assertEqual(nitrofurantoin["descriptors"]["formula"], "C8H6N4O5")
        self.assertAlmostEqual(nitrofurantoin["descriptors"]["molecularWeight"], 238.16, places=2)

    def test_ten_alerts_positive_negative_and_equivalent_inputs(self):
        cases = (
            ("nitro", "[O-][N+](=O)c1ccccc1", "O=[N+]([O-])C1=CC=CC=C1", "Nc1ccccc1"),
            ("aniline", "Nc1ccccc1", "NC1=CC=CC=C1", "c1ccncc1"),
            ("hydrazine", "CNN", "NNC", "CN=NC"),
            ("epoxide", "C1OC1", "O2CC2", "C1COC1"),
            ("michael", "C=CC=O", "O=CC=C", "CCCC=O"),
            ("quinone", "O=C1C=CC(=O)C=C1", "C1=CC(=O)C=CC1=O", "O=C1CCC(=O)CC1"),
            ("aromatic_halide", "Clc1ccccc1", "ClC1=CC=CC=C1", "CCCCl"),
            ("acyl_halide", "CC(=O)Cl", "ClC(C)=O", "CC(=O)O"),
            ("thiol", "CCS", "[SH]CC", "CSC"),
            ("sulfonate", "CS(=O)(=O)O", "OS(C)(=O)=O", "CS(=O)(=O)C"),
        )
        for alert_id, positive, equivalent, negative in cases:
            with self.subTest(alert=alert_id):
                result, _ = self.assert_equivalent(positive, equivalent)
                by_id = {alert["id"]: alert for alert in result["alerts"]}
                self.assertIn(alert_id, by_id)
                indices = by_id[alert_id]["matchedAtomIndices"]
                self.assertTrue(indices)
                self.assertEqual(indices, sorted(set(indices)))
                self.assertTrue(all(0 <= index < len(result["structure"]["atoms"]) for index in indices))
                self.assertNotIn(alert_id, {alert["id"] for alert in analyze_chemistry(negative)["alerts"]})

    def test_aniline_excludes_amides_and_ring_nitrogen(self):
        for smiles in ("c1ccncc1", "CC(=O)Nc1ccccc1", "c1cc[nH]c1", "CC(=O)N"):
            with self.subTest(smiles=smiles):
                result = analyze_chemistry(smiles)
                self.assertNotIn("aniline", {alert["id"] for alert in result["alerts"]})
                self.assertEqual(result["descriptors"]["basicAmines"], 0)
        self.assertEqual(analyze_chemistry("CN")["descriptors"]["basicAmines"], 1)
        self.assertEqual(analyze_chemistry("C[NH3+]")["descriptors"]["basicAmines"], 1)

    def test_alert_variants_cover_fused_quinone_thiol_and_sulfonate_salt(self):
        self.assertIn("quinone", {alert["id"] for alert in analyze_chemistry("O=C1c2ccccc2C(=O)c2ccccc21")["alerts"]})
        self.assertIn("quinone", {alert["id"] for alert in analyze_chemistry("O=C1C(=O)C=CC=C1")["alerts"]})
        self.assertIn("thiol", {alert["id"] for alert in analyze_chemistry("CC[S-]")["alerts"]})
        self.assertIn("sulfonate", {alert["id"] for alert in analyze_chemistry("CS(=O)(=O)[O-]")["alerts"]})
        self.assertNotIn("sulfonate", {alert["id"] for alert in analyze_chemistry("CS(=O)(=O)N")["alerts"]})

    def test_ring_opening_or_closing_bond_order_is_equivalent(self):
        result, _ = self.assert_equivalent("C=1CCCCC1", "C1CCCCC=1")
        self.assertEqual(result["descriptors"]["formula"], "C6H10")
        self.assertAlmostEqual(result["descriptors"]["molecularWeight"], 82.15, places=2)
        self.assertEqual(sum(bond["order"] == 2 for bond in result["structure"]["bonds"]), 1)

    def test_malformed_inputs_are_rejected_before_scoring(self):
        for smiles in ("", "  ", "CC=", "CC.", "CC()", "C11", "CC(C)(C)(C)C", "C1CC", "CC(=)(O)", "cco", "CCO ethanol", "C C O", "[*]", None, 123):
            for operation in (analyze_chemistry, canonicalize_smiles):
                with self.subTest(smiles=smiles, operation=operation.__name__):
                    with self.assertRaises(ValueError):
                        operation(smiles)

    def test_public_canonicalizer_shares_analysis_identity_and_limits(self):
        for smiles in ("  CCO  ", "[CH3:1][CH2:2][OH:3]", "C1=CC=CC=C1", "[Na+].[Cl-]", "F[C@](Cl)(Br)I", "[13CH4]"):
            with self.subTest(smiles=smiles):
                self.assertEqual(canonicalize_smiles(smiles), analyze_chemistry(smiles)["canonicalSmiles"])
        for smiles in ("C" * (MAX_SMILES_LENGTH + 1), "C" * (MAX_HEAVY_ATOMS + 1)):
            with self.subTest(length=len(smiles)):
                with self.assertRaises(ValueError):
                    canonicalize_smiles(smiles)

    def test_limits_and_valid_single_atom(self):
        with self.assertRaisesRegex(ValueError, "过长"):
            analyze_chemistry("C" * (MAX_SMILES_LENGTH + 1))
        with self.assertRaisesRegex(ValueError, "过大"):
            analyze_chemistry("C" * (MAX_HEAVY_ATOMS + 1))
        self.assertEqual(analyze_chemistry("C")["descriptors"]["formula"], "CH4")

    def test_isotope_and_stereochemistry_are_preserved_in_identity(self):
        methane = analyze_chemistry("C")
        carbon13 = analyze_chemistry("[13CH4]")
        self.assertNotEqual(methane["structureId"], carbon13["structureId"])
        self.assertGreater(carbon13["descriptors"]["molecularWeight"], methane["descriptors"]["molecularWeight"] + 0.9)
        self.assertNotEqual(analyze_chemistry("F[C@](Cl)(Br)I")["structureId"], analyze_chemistry("F[C@@](Cl)(Br)I")["structureId"])
        mapped, _ = self.assert_equivalent("[CH3:1][CH2:2][OH:3]", "CCO")
        self.assertEqual(mapped["known"]["id"], "ethanol")

    def test_report_contract_svg_and_score_provenance(self):
        result = analyze_chemistry("  C#N  ")
        self.assertEqual(result["input"], "C#N")
        self.assertEqual(result["structureId"], sha256(result["canonicalSmiles"].encode("utf-8")).hexdigest())
        self.assertIsNone(result["known"])
        self.assertIn("<svg", result["moleculeSvg"])
        self.assertIn("</svg>", result["moleculeSvg"])
        self.assertEqual(result["structure"]["bonds"][0]["order"], 3)
        self.assertEqual(result["provenance"]["chemistryVersion"], CHEMISTRY_VERSION)
        self.assertEqual(result["provenance"]["rulesVersion"], RULES_VERSION)
        self.assertFalse(result["rules"]["calibrated"])
        self.assertEqual(result["rules"]["scoreMeaning"], "heuristic-score-not-probability")
        self.assertEqual(result["provenance"]["referenceScores"], {"kind": "demonstration-fixture", "source": None, "verified": False, "usedForScoring": False})
        for endpoint in result["endpoints"]:
            self.assertEqual(endpoint["scoreSource"], "rule")
            self.assertEqual(endpoint["explanationSource"], "structure-rules")
            self.assertIsNone(endpoint["referenceScore"])
            self.assertAlmostEqual(sum(driver["contribution"] for driver in endpoint["ruleDrivers"]), endpoint["ruleScore"], delta=0.0006)
        # The complete report is directly serializable by the HTTP layer.
        json.dumps(result, ensure_ascii=False, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
