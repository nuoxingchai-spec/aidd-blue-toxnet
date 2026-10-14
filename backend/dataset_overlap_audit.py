"""Label-blind historical identities and source-lineage overlap checks.

No downloads, model calls, row-label parsing, or historical file writes occur.
The weaker v1 candidate-parent identity is deliberately not reused here.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from rdkit import rdBase

from backend.chemistry import CHEMISTRY_VERSION
from backend.v3_data import (IDENTITY_POLICY, IDENTITY_VERSION, V2_MANIFEST_SHA256,
                             V2_SPLIT_SHA256, EXPOSED_POOL_SHA256,
                             leakage_identity, identity_tokens, build_forbidden_index)
from backend.multitask_data import (PREVIOUS_DEVELOPMENT_MANIFEST_SHA256,
    PREVIOUS_DEVELOPMENT_SPLIT_SHA256, TOX21_PROCESSED_SHA256)

REFERENCE_SCHEMA = 'aidd.independent.historical-references.v1'
EVALUATOR_SOURCE_FILES = (
    'backend/independent_validation_metrics.py',
    'backend/validation_predictors.py',
    'scripts/evaluate_independent_models.py',
)
LINEAGE_FIELDS = ('families', 'publicationIds', 'cohortIds', 'assayIds')
REFERENCE_FILES = {
    'v2-manifest': ('models/aidd-toxnet/manifest.json', V2_MANIFEST_SHA256, 'metadata'),
    'v2-splits': ('models/aidd-toxnet/split.json', V2_SPLIT_SHA256, 'v2'),
    'v3-four-manifest': ('data/v3-development/manifest.json', PREVIOUS_DEVELOPMENT_MANIFEST_SHA256, 'metadata'),
    'v3-four-splits': ('data/v3-development/split.json', PREVIOUS_DEVELOPMENT_SPLIT_SHA256, 'v3-four'),
    'v3-multitask-manifest': ('data/v3-multitask/manifest.json', '9a4056bb740b0d6fce763fbd4d7c2b618a55f4670eeea7db423ed9b7465dda15', 'metadata'),
    'v3-multitask-splits': ('data/v3-multitask/split.json', 'df0dfa7195f714df81e93f3795d1623c81d1d5a32433a59388421cf7d2b082f8', 'v3-multitask'),
    'v3-audited-manifest': ('data/v3-multitask-audited/manifest.json', 'ff4837c1bcb0e568c506e503cab9321f23be328f5c432d495be3606f15d7ceae', 'metadata'),
    'v3-audited-splits': ('data/v3-multitask-audited/split.json', 'df0dfa7195f714df81e93f3795d1623c81d1d5a32433a59388421cf7d2b082f8', 'v3-audited'),
    'exposed-herg-karim': ('data/candidates/processed/herg_external_pool.csv', EXPOSED_POOL_SHA256, 'csv'),
    'audited-tox21-source': ('data/candidates/processed/tox21.csv', TOX21_PROCESSED_SHA256, 'csv'),
}
REFERENCE_STRUCTURE_HASHES = {
    'v2-train': 'ddbabcf8e646b6bb144ea5cf1b4b6000642b6e4bd74f553bf831a83500789e27',
    'v2-validation': 'c795273957306b321ece5527575f7e4dda4a508e65a6dbd7ed739d674415b881',
    'v2-test': '77e79f107f902e4f7f0504c26edbc447a9e7d00cdbabbdce4d22e9651c29dcf2',
    'v3-four-train': 'ed70562a3ad14f4c48247f99519504e57dead8cc4681f73b9a8480fc897ae3a9',
    'v3-four-validation': 'f5738699b66c741d2f70c84c64470a31418dc1a940d7b36c37d568c2e28e20dc',
    'v3-multitask-train': '901a987b46398f82b0f44a5239d316031b918aea2d559cb94ab07608d4cd30bb',
    'v3-multitask-validation': '0e702b29fa4b5baacd557e60a4e00a0ff161ef24897711808b6347ca3aae9db8',
    'v3-audited-train': '901a987b46398f82b0f44a5239d316031b918aea2d559cb94ab07608d4cd30bb',
    'v3-audited-validation': '0e702b29fa4b5baacd557e60a4e00a0ff161ef24897711808b6347ca3aae9db8',
    'exposed-herg-karim': 'ed2383020d65fd1c3178da4b524057589cf9b0cf4abaab157a670880a2ca51f0',
    'audited-tox21-source': 'acc38d13693afaa619b2fb6834568a40ac2d7e6dad0eaa36cc4042225dd0b641',
}
KNOWN_LINEAGE = [
    {'sourceId': 'tdc-herg', 'families': ['herg-literature-compilations'], 'publicationIds': ['wang-2016-herg'], 'cohortIds': ['tdc-herg-4259588'], 'assayIds': ['herg-blocker-source-label']},
    {'sourceId': 'tdc-dili', 'families': ['fda-nctr-dili-literature'], 'publicationIds': ['xu-2015-dili'], 'cohortIds': ['tdc-dili-4259585'], 'assayIds': ['drug-induced-liver-injury-source-label']},
    {'sourceId': 'tdc-ames', 'families': ['ames-literature-compilations'], 'publicationIds': ['xu-2012-ames'], 'cohortIds': ['tdc-ames-4259564'], 'assayIds': ['bacterial-reverse-mutation-source-label']},
    {'sourceId': 'tdc-clintox', 'families': ['clinical-outcome-drug-compilations'], 'publicationIds': ['gayvert-2016-clintox'], 'cohortIds': ['tdc-clintox-4259572'], 'assayIds': ['ct-tox']},
    {'sourceId': 'herg_karim', 'families': ['herg-literature-compilations'], 'publicationIds': ['10.1186/s13321-021-00541-z'], 'cohortIds': ['tdc-herg-karim-6822246'], 'assayIds': ['herg-blocker-below-10-micromolar']},
    {'sourceId': 'tox21', 'families': ['tox21-screening-program'], 'publicationIds': ['tox21-program'], 'cohortIds': ['tdc-tox21-4259612'], 'assayIds': ['tox21-12-assay-panel']},
    {'sourceId': 'fda-dilirank', 'families': ['fda-nctr-dili-literature'], 'publicationIds': [], 'cohortIds': ['fda-dilirank-2.0'], 'assayIds': ['drug-induced-liver-injury-source-label']},
    {'sourceId': 'fda-dilist', 'families': ['fda-nctr-dili-literature'], 'publicationIds': [], 'cohortIds': ['fda-dilist'], 'assayIds': ['drug-induced-liver-injury-source-label']},
]


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def object_sha256(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def file_sha256(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def evaluator_source_sha256(root=None):
    """Freeze actual evaluator bytes without importing model/runtime modules.

    This is a local reproducibility binding, not third-party code attestation.
    A reviewed protocol must be frozen again when any pinned source changes;
    old sealed labels are not implicitly approved for a revised experiment.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    hashes = {}
    for relative in EVALUATOR_SOURCE_FILES:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError('Missing regular evaluator source dependency: ' + relative)
        hashes[relative] = file_sha256(path)
    return object_sha256({'schema': 'aidd.independent.evaluator-source.v1', 'files': hashes})


def lineage_key(value):
    result = value.strip().casefold()
    for prefix in ('https://doi.org/', 'http://doi.org/', 'doi:'):
        if result.startswith(prefix):
            result = result[len(prefix):]
    return result.rstrip('/')


def reference_receipt(references):
    """Bind structures without putting their strings in the public receipt."""
    return object_sha256({
        'schema': references.get('schema'), 'identityVersion': references.get('identityVersion'),
        'chemistryVersion': references.get('chemistryVersion'), 'rdkitVersion': references.get('rdkitVersion'),
        'completeReferences': references.get('completeReferences'), 'missing': references.get('missing'),
        'referenceFiles': references.get('referenceFiles'), 'sourceLineage': references.get('sourceLineage'),
        'structureHashesByRole': {role: object_sha256(sorted(set(structures)))
                                  for role, structures in sorted(references.get('structures', {}).items())},
        'labelsRead': False,
    })


def build_historical_references(root):
    """Read only structure members/columns from fixed historical snapshots.

    JSON parsers decode a historical split document, but no label field is
    inspected, converted, counted, returned, or used for any decision. CSV
    readers project only the named smiles column. Hashing is byte-level.
    """
    root = Path(root).resolve()
    structures, files, missing = {}, [], []
    for role, (relative, expected, reader) in REFERENCE_FILES.items():
        path = root / relative
        if not path.is_file():
            missing.append({'role': role, 'reason': 'missing-reference', 'relativeFile': relative})
            continue
        actual = file_sha256(path)
        if actual != expected:
            missing.append({'role': role, 'reason': 'reference-sha256-mismatch', 'relativeFile': relative})
            continue
        files.append({'role': role, 'relativeFile': relative, 'sha256': actual})
        if reader == 'metadata':
            continue
        try:
            if reader == 'csv':
                with path.open(encoding='utf-8-sig', newline='') as handle:
                    source = csv.DictReader(handle)
                    if 'smiles' not in (source.fieldnames or []):
                        raise ValueError('Missing structure column')
                    structures[role] = sorted({row['smiles'] for row in source})
            else:
                document = json.loads(path.read_text(encoding='utf-8'))
                expected_splits = {'train', 'validation', 'test'} if reader == 'v2' else {'train', 'validation'}
                if set(document.get('splits', {})) != expected_splits:
                    raise ValueError('Unexpected historical split roles')
                for split, members in document['splits'].items():
                    structures[reader + '-' + split] = sorted({member['smiles'] if isinstance(member, dict) else member for member in members})
        except (ValueError, KeyError, TypeError, UnicodeError):
            missing.append({'role': role, 'reason': 'invalid-structure-projection', 'relativeFile': relative})
    result = {'schema': REFERENCE_SCHEMA, 'identityVersion': IDENTITY_VERSION, 'identityPolicy': IDENTITY_POLICY,
              'chemistryVersion': CHEMISTRY_VERSION, 'rdkitVersion': rdBase.rdkitVersion,
              'structures': structures, 'referenceFiles': files, 'missing': missing,
              'completeReferences': not missing and len(files) == len(REFERENCE_FILES),
              'sourceLineage': json.loads(canonical_json(KNOWN_LINEAGE)), 'labelsRead': False,
              'pretrainingStructureExposure': 'unknown', 'sourcePopulationIndependenceProvenByStructure': False}
    result['receiptSha256'] = reference_receipt(result)
    result['receipt'] = {'schema': REFERENCE_SCHEMA, 'receiptSha256': result['receiptSha256'],
        'completeReferences': result['completeReferences'], 'labelsRead': False,
        'identityVersion': IDENTITY_VERSION, 'identityPolicy': IDENTITY_POLICY,
        'chemistryVersion': CHEMISTRY_VERSION, 'rdkitVersion': rdBase.rdkitVersion,
        'structureCountsByRole': {role: len(values) for role, values in structures.items()},
        'referenceFiles': [{'role': row['role'], 'sha256': row['sha256']} for row in files],
        'missing': [{key: row[key] for key in ('role', 'reason')} for row in missing],
        'pretrainingStructureExposure': 'unknown', 'sourcePopulationIndependenceProvenByStructure': False}
    return result


def validate_references(references):
    if (not isinstance(references, dict) or references.get('schema') != REFERENCE_SCHEMA
            or references.get('identityVersion') != IDENTITY_VERSION
            or references.get('identityPolicy') != IDENTITY_POLICY
            or references.get('chemistryVersion') != CHEMISTRY_VERSION
            or references.get('rdkitVersion') != rdBase.rdkitVersion
            or references.get('labelsRead') is not False
            or references.get('receiptSha256') != reference_receipt(references)):
        raise ValueError('Historical reference receipt, chemistry, identity or label-blind contract mismatch')
    # Completeness is a verified fixed-file condition, never a user checkbox.
    present = {entry.get('role'): entry for entry in references.get('referenceFiles', [])}
    actual_complete = (not references.get('missing') and len(present) == len(REFERENCE_FILES)
                       and all(present.get(role, {}).get('sha256') == spec[1] for role, spec in REFERENCE_FILES.items())
                       and set(references.get('structures', {})) == set(REFERENCE_STRUCTURE_HASHES)
                       and all(object_sha256(sorted(set(references['structures'][role]))) == checksum
                               for role, checksum in REFERENCE_STRUCTURE_HASHES.items()))
    if references.get('completeReferences') is not actual_complete:
        raise ValueError('Historical reference completeness claim is inconsistent')
    if references.get('sourceLineage') != KNOWN_LINEAGE:
        raise ValueError('Historical source lineage was removed or substituted')
    return references


def lineage_overlap(metadata, references):
    candidate = metadata['lineage']
    matches = []
    for previous in references.get('sourceLineage', []):
        reasons = []
        if lineage_key(metadata['sourceId']) == lineage_key(previous['sourceId']):
            reasons.append('sourceId')
        for field in LINEAGE_FIELDS:
            left = {lineage_key(value) for value in candidate.get(field, [])}
            right = {lineage_key(value) for value in previous.get(field, [])}
            if left & right:
                reasons.append(field)
        if reasons:
            matches.append({'historicalSourceId': previous['sourceId'], 'matchedFields': reasons})
    return matches


def structural_overlap(canonical, forbidden_index):
    identity = leakage_identity(canonical)
    reasons = sorted({role + ':' + token.split(':', 1)[0]
                      for token in identity_tokens(identity) for role in forbidden_index.get(token, [])})
    return identity, reasons


def historical_forbidden_index(references):
    validate_references(references)
    return build_forbidden_index(references.get('structures', {}))
