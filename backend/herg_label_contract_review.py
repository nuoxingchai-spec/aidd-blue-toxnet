"""Evidence-only hERG pre-review policy. No activity/label mapper is enabled.

This module imports historical task declarations, never historical CSV rows or
candidate result fields. It cannot approve a dataset or an evaluation protocol.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from backend.training_data import DATASETS

POLICY_VERSION = 'aidd.stage7b.herg-custodian-prereview.v1'
_POLICY = {
    'schema': POLICY_VERSION,
    'status': 'MANUAL_REVIEW_REQUIRED',
    'purpose': 'label-blind-evidence-review-only',
    'originalTask': 'TDC hERG / Wang 2016 / Dataverse 4259588',
    'originalSourceSha256': 'f2b00c05f12763c9da91d46246800e30925a626d5038e15eec366d633bdffe54',
    'positiveClass': 'hERG blocker (1); non-blocker (0)',
    'threshold': None,
    'comparisonOperator': None,
    'thresholdUnit': None,
    'boundaryValueRule': None,
    'historicalAssayComposition': 'unresolved',
    'directComparisonApproved': False,
    'labelMappingExecutable': False,
    'labelMappingStatus': 'not-run-independent-custodian-required',
    'evaluationAuthorized': False,
    'modelTrainingAuthorized': False,
    'candidateOutcomeFieldsPermitted': [],
    'unitDimensionsOnly': {'M': '1', 'mM': '0.001', 'uM': '0.000001',
                           'µM': '0.000001', 'μM': '0.000001',
                           'nM': '0.000000001', 'pM': '0.000000000001'},
    'unitConversionExecutable': False,
    'unitDimensionWarning': 'Concentration dimensions do not establish endpoint equivalence.',
    'nonInterchangeableMeasurements': ['functional-current-IC50', 'binding-IC50',
                                     'Ki', 'Kd', 'EC50', 'AC50', 'hitcall',
                                     'single-concentration-inhibition'],
    'custodianHandlingBeforeApproval': {
        'exact': 'not-run-pending-original-task-threshold-and-assay-approval',
        'inequality': 'not-run-no-midpoint-or-uncensored-value-imputation',
        'range': 'not-run-no-boundary-classification-before-approved-rule',
        'missing': 'not-run-never-impute-negative',
        'qualityAnomaly': 'not-run-custodian-quarantine-before-result-access',
        'duplicateOrConflictingExperiment': 'not-run-no-outcome-based-selection-or-averaging',
    },
    'requiredEvidenceBeforeAnyNewPolicy': [
        'original-task-threshold-operator-unit-boundary-evidence',
        'historical-assay-system-and-measurement-composition',
        'experiment-publication-cohort-source-family-independence',
        'specific-use-rights-review',
        'independent-custodian-identity-access-control-and-attestation',
        'reviewer-approval-time-evidence-hashes-before-label-access',
        'new-frozen-label-policy-before-any-outcome-classification',
    ],
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def prereview_policy():
    """Return a copy so a caller cannot mutate the process-wide restriction."""
    return copy.deepcopy(_POLICY)


def prereview_policy_sha256():
    """Content pin for evidence collection; this is NOT an approved label policy."""
    return _digest(_POLICY)


def original_contract_review(root=None):
    """Hash regular tracked evidence files; never open CSVs or candidate data."""
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    files = ('backend/training_data.py', 'backend/v3_data.py',
             'backend/multitask_data.py', 'backend/local_model.py',
             'backend/multitask_model.py', 'backend/toxnet.py',
             'scripts/train_toxnet.py', 'models/aidd-toxnet/manifest.json')
    hashes = {}
    for relative in files:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError('Missing regular historical contract evidence: ' + relative)
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    # Source declarations are public metadata. Neither the manifest label
    # counts nor any source-label row is inspected, returned, or selected.
    specification = next(item for item in DATASETS if item['endpoint'] == 'herg')
    declaration = {key: specification[key] for key in
                   ('endpoint', 'name', 'fileId', 'sha256', 'positiveClass',
                    'sourcePopulation', 'reference', 'documentationUrl')}
    result = {'schema': 'aidd.stage7b.herg-contract-evidence.v1',
              'status': 'MANUAL_REVIEW_REQUIRED', 'historicalDeclaration': declaration,
              'sourceFileSha256': hashes, 'prereviewPolicySha256': prereview_policy_sha256(),
              'threshold': None, 'comparisonOperator': None, 'thresholdUnit': None,
              'boundaryValueRule': None, 'labelsRead': False,
              'labelMappingPerformed': False, 'evaluationPerformed': False,
              'canEnterStage7C': False}
    return {**result, 'evidenceSha256': _digest(result)}


def require_custodian_mapping():
    """Always fail closed. No caller flag or supplied result can enable mapping."""
    raise PermissionError('MANUAL_REVIEW_REQUIRED: original hERG threshold, assay '
                          'contract and independent custodian are unresolved; '
                          'candidate label classification is disabled.')
