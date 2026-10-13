"""Small explainable test models, no GPU or downloaded pretrained weights."""
import unittest
import threading
from unittest.mock import patch
import numpy as np
import torch
from rdkit import Chem, DataStructs
from rdkit.Chem import rdFingerprintGenerator

from backend.attribution import (AttributionEngine, integrated_gradients, embedded_forward,
                                 morgan_to_atoms, group_contributions, legal_fragment_deletions)
from backend.toxnet import build_model, featurize_smiles
from backend.chemistry import canonicalize_smiles


class AttributionTests(unittest.TestCase):
    def test_linear_signed_contributions_and_completeness(self):
        model = torch.nn.Linear(3, 1)
        with torch.no_grad():
            model.weight.copy_(torch.tensor([[2., -3., .5]])); model.bias.fill_(7.)
        x = torch.tensor([[1., 2., 4.]])
        result, diag = integrated_gradients(lambda value: model(value).sum(), (x,), (torch.zeros_like(x),), steps=8)
        torch.testing.assert_close(result[0], torch.tensor([[2., -6., 2.]], dtype=torch.float64))
        self.assertAlmostEqual(diag['completenessResidual'], 0, places=10)
        self.assertTrue(all(p.grad is None for p in model.parameters()))

    def test_joint_branches_and_nonzero_baseline(self):
        a, b = torch.tensor([2., 3.]), torch.tensor([5.])
        result, diag = integrated_gradients(lambda x, y: x.sum() * 2 - y.sum() * 3, (a, b), (torch.ones_like(a), torch.ones_like(b)), steps=4)
        self.assertAlmostEqual(float(result[0].sum()), 6)
        self.assertAlmostEqual(float(result[1].sum()), -12)
        self.assertAlmostEqual(diag['completenessResidual'], 0, places=10)

    def test_actual_graph_forward_matches_embedding_path(self):
        torch.manual_seed(42)
        m = build_model({'hiddenSize': 12, 'heads': 3, 'layers': 1, 'feedForward': 24, 'dropout': 0}).eval()
        f = featurize_smiles('CCO', m.architecture)
        x = sum(e(f['atoms'][..., i]) for i, e in enumerate(m.atom_embeddings)) / len(m.atom_embeddings)**.5
        torch.testing.assert_close(m(**f), embedded_forward(m, f, x[:, 1:], f['fingerprint']), rtol=0, atol=0)

    def test_morgan_mapping_conserves_and_uses_actual_input(self):
        mol = Chem.MolFromSmiles('CCO')
        fp = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048, includeChirality=True).GetFingerprint(mol)
        values = np.zeros(2048); values[list(fp.GetOnBits())] = np.arange(1, len(fp.GetOnBits())+1)
        atoms, records = morgan_to_atoms(mol, values)
        self.assertAlmostEqual(atoms.sum(), values.sum())
        self.assertTrue(records)
        with self.assertRaises(ValueError): morgan_to_atoms(mol, values, expected_fingerprint=np.zeros(2048))

    def test_inactive_bit_cannot_masquerade_as_atom_contribution(self):
        with self.assertRaises(ValueError): morgan_to_atoms(Chem.MolFromSmiles('CCO'), np.ones(2048))

    def test_group_names_only_sum_supplied_neural_contributions(self):
        mol = Chem.MolFromSmiles('CC(=O)N')
        values = np.arange(mol.GetNumAtoms(), dtype=float)
        groups = group_contributions(mol, values)
        self.assertTrue(groups)
        for group in groups:
            self.assertAlmostEqual(group['contribution'], sum(values[i] for i in group['atomIndices']))
            self.assertIn('neural', group['source'])

    def test_legal_fragments_original_identity_unchanged(self):
        for original in ('CCO', 'CC(=O)Nc1ccccc1', '[13CH3]CO', '[Na+].CC(=O)[O-]'):
            canonical = canonicalize_smiles(original)
            proposals = legal_fragment_deletions(canonical)
            self.assertEqual(canonicalize_smiles(original), canonical)
            for p in proposals:
                self.assertEqual(canonicalize_smiles(p['perturbedSmiles']), p['perturbedSmiles'])
                self.assertNotEqual(p['perturbedSmiles'], canonical)
                self.assertTrue(p['removedAtomIndices'])
            self.assertLessEqual(len(proposals), 8)

    def test_missing_models_and_external_predictions_no_rule_fallback(self):
        engine = AttributionEngine(None)
        for model in ('v2', 'v3', 'external'):
            value = engine.explain('CCO', model=model)
            self.assertEqual(value['status'], 'unavailable'); self.assertEqual(value['atoms'], [])

    def test_invalid_settings_or_smiles_unavailable(self):
        for smiles, steps in [('CC=', 32), ('CCO', True), ('CCO', 129)]:
            self.assertEqual(AttributionEngine(None).explain(smiles, steps=steps)['status'], 'unavailable')

    def test_integrator_rejects_nonfinite_target(self):
        with self.assertRaises(ValueError): integrated_gradients(lambda x: x.sum()*float('nan'), (torch.ones(1),), (torch.zeros(1),), steps=4)

    def test_research_probe_model_change_invalidates_entire_explanation(self):
        class ResearchFixture:
            _lock = threading.RLock()
            calls = 0
            def predict_all(self, smiles):
                self.calls += 1
                # A transient probe snapshot must not be silently skipped.
                return {'endpoints': [{'id': 'herg', 'score': .5}],
                        'provenance': {'weightsSha256': 'b' if self.calls == 2 else 'a',
                                       'manifestSha256': 'unit-only', 'modelVersion': 'fixture'}}
        client = ResearchFixture()
        proposal = {'perturbedSmiles': 'CC', 'removedAtomIndices': [2], 'deletionMethod': 'unit-fixture'}
        with patch('backend.attribution.legal_fragment_deletions', return_value=[proposal, proposal]):
            result = AttributionEngine(None, client).explain('CCO', model='v3')
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['errorCode'], 'ATTRIBUTION_MODEL_CHANGED')
        self.assertEqual(client.calls, 2)
