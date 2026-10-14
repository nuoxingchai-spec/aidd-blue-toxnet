"""Train-only structural coverage heuristics, explicitly not confidence."""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
from rdkit import Chem, DataStructs
from rdkit.Chem import Descriptors, Lipinski, rdFingerprintGenerator
from rdkit.Chem.Scaffolds import MurckoScaffold

from backend.chemistry import canonicalize_smiles
from backend.independent_validation_metrics import digest, valid_sha

DESCRIPTORS = ('MolWt', 'MolLogP', 'TPSA', 'HBD', 'HBA', 'RotatableBonds', 'HeavyAtomCount')
FINGERPRINT = {'radius': 2, 'bits': 2048, 'includeChirality': True}
DEFAULT_THRESHOLDS = {'minimumTanimoto': .4, 'maximumDescriptorZ': 3.0}


def _features(smiles):
    canonical = canonicalize_smiles(smiles)
    molecule = Chem.MolFromSmiles(canonical)
    fp = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048, includeChirality=True).GetFingerprint(molecule)
    scaffold = MurckoScaffold.MurckoScaffoldSmiles(mol=molecule, includeChirality=False) or '[ACYCLIC]'
    descriptors = np.asarray([Descriptors.MolWt(molecule), Descriptors.MolLogP(molecule), Descriptors.TPSA(molecule),
        Lipinski.NumHDonors(molecule), Lipinski.NumHAcceptors(molecule), Lipinski.NumRotatableBonds(molecule), molecule.GetNumHeavyAtoms()], dtype=float)
    if not np.isfinite(descriptors).all():
        raise ValueError('Unsupported descriptor input')
    return canonical, fp, scaffold, descriptors


@dataclass
class ADReference:
    # Private local runtime index; never serialize this object into an API or
    # commit its training structures/fingerprints to a public repository.
    fingerprints: list
    scaffolds: set
    mean: np.ndarray
    scale: np.ndarray
    constant: np.ndarray
    minimum: np.ndarray
    maximum: np.ndarray
    thresholds: dict
    binding: dict
    reference_count: int


def build_reference(training_smiles, *, model_binding, training_reference_sha256, thresholds=None):
    if (model_binding.get('artifactVerified') is not True or not valid_sha(model_binding.get('weightsSha256'))
            or not valid_sha(training_reference_sha256)
            or training_reference_sha256 != model_binding.get('trainingReferenceSha256')):
        raise ValueError('Original SHA-verified model training references are required')
    values = [_features(smiles) for smiles in training_smiles]
    canonical = [value[0] for value in values]
    if not values or len(canonical) != len(set(canonical)) or digest(canonical) != model_binding.get('trainingStructureOrderSha256'):
        raise ValueError('AD reference order/membership differs from the actual training reference')
    thresholds = dict(DEFAULT_THRESHOLDS if thresholds is None else thresholds)
    if (set(thresholds) != set(DEFAULT_THRESHOLDS) or any(type(v) not in (float, int) or not math.isfinite(v) for v in thresholds.values())
            or not 0 <= thresholds['minimumTanimoto'] <= 1 or thresholds['maximumDescriptorZ'] <= 0):
        raise ValueError('AD thresholds must be predeclared finite coverage heuristics')
    matrix = np.stack([value[3] for value in values])
    mean, scale = matrix.mean(0), matrix.std(0)
    constant = scale < 1e-12
    scale[constant] = 1
    binding = {'schema': 'aidd.research-ad-reference.v1', 'modelWeightsSha256': model_binding['weightsSha256'],
               'trainingReferenceSha256': training_reference_sha256, 'trainingStructureOrderSha256': digest(canonical),
               'fingerprint': FINGERPRINT, 'descriptors': list(DESCRIPTORS), 'fitRole': 'original-model-train-only',
               'thresholdPolicy': 'fixed heuristic; no evaluation or calibration labels used', 'thresholds': thresholds,
               'validated': False, 'formalAvailability': False}
    binding['referenceBindingSha256'] = digest(binding)
    return ADReference([value[1] for value in values], {value[2] for value in values}, mean, scale, constant,
                       matrix.min(0), matrix.max(0), thresholds, binding, len(values))


def query_domain(smiles, reference):
    if not isinstance(reference, ADReference):
        return {'status': 'insufficient-evidence', 'validated': False, 'confidence': None, 'errorCode': 'AD_REFERENCE_UNAVAILABLE'}
    try:
        _, fingerprint, scaffold, descriptors = _features(smiles)
    except (ValueError, TypeError, RuntimeError):
        return {'status': 'insufficient-evidence', 'validated': False, 'confidence': None, 'errorCode': 'AD_INPUT_UNSUPPORTED'}
    maximum = max(DataStructs.BulkTanimotoSimilarity(fingerprint, reference.fingerprints))
    z = np.abs((descriptors-reference.mean)/reference.scale)
    constant_mismatch = bool((reference.constant & (np.abs(descriptors-reference.mean) > 1e-8)).any())
    outside = maximum < reference.thresholds['minimumTanimoto'] or float(z.max()) > reference.thresholds['maximumDescriptorZ'] or constant_mismatch
    ranges = {name: {'trainingMinimum': float(reference.minimum[i]), 'trainingMaximum': float(reference.maximum[i]),
                    'queryValue': float(descriptors[i]), 'outsideTrainingRange': bool(descriptors[i] < reference.minimum[i] or descriptors[i] > reference.maximum[i])}
              for i, name in enumerate(DESCRIPTORS)}
    return {'schema': 'aidd.research-ad-query.v1', 'status': 'not-validated',
            'coverageStatus': 'outside-training-coverage-heuristic' if outside else 'within-training-coverage-heuristic',
            'validated': False, 'confidence': None, 'clinicalRiskProbability': False, 'maximumTrainingTanimoto': float(maximum),
            'scaffoldSeenInTraining': scaffold in reference.scaffolds, 'maximumDescriptorZ': float(z.max()),
            'constantDescriptorMismatch': constant_mismatch, 'referenceCount': reference.reference_count,
            'descriptorRanges': ranges, 'atomCountOutsideTrainingRange': ranges['HeavyAtomCount']['outsideTrainingRange'],
            'referenceBindingSha256': reference.binding['referenceBindingSha256'], 'thresholds': reference.thresholds,
            'limitations': 'Similarity and descriptor coverage are not calibrated uncertainty or validated applicability; unseen scaffolds are reported separately.'}
