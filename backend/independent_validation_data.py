"""Explicit, frozen independent-evaluation data; fail closed without evidence.

The audit is label blind. Labels live in a separate local file and are read
only after two explicit permissions. No API accepts arbitrary filesystem paths.
Human evidence review is recorded, not automatically interpreted as legal or
scientific certification. Historical metrics are never model-selection inputs.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, StrictInt, field_validator, model_validator
from rdkit import rdBase

from backend.chemistry import CHEMISTRY_VERSION, canonicalize_smiles
from backend.dataset_overlap_audit import (build_historical_references, canonical_json, file_sha256,
    historical_forbidden_index, lineage_overlap, object_sha256, validate_references, structural_overlap,
    evaluator_source_sha256, KNOWN_LINEAGE)
from backend.v3_data import IDENTITY_VERSION, IDENTITY_POLICY, identity_tokens

CANDIDATE_SCHEMA = 'aidd.independent.candidate.v1'
PROTOCOL_SCHEMA = 'aidd.independent.protocol.v1'
AUDIT_SCHEMA = 'aidd.independent.data-audit.v1'
SEAL_SCHEMA = 'aidd.independent.sealed-data.v1'
SEALED_FILES = {'structures': 'structures.jsonl', 'labels': 'labels.private.json',
                'metadata': 'metadata.json', 'protocol': 'protocol.json', 'audit': 'audit.json'}
Sha = Annotated[StrictStr, Field(pattern=r'^[0-9a-f]{64}$')]
Identifier = Annotated[StrictStr, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$')]
Role = Literal['independent-evaluation', 'calibration']


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


def _utc(value):
    if value is None:
        return value
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Expected explicit UTC timestamp') from None
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError('Expected explicit UTC timestamp')
    if parsed > datetime.now(timezone.utc):
        raise ValueError('Review/freeze timestamps cannot be in the future')
    return value


class Lineage(Contract):
    families: list[Identifier] = Field(min_length=1, max_length=30)
    publicationIds: list[StrictStr] = Field(default_factory=list, max_length=100)
    cohortIds: list[Identifier] = Field(min_length=1, max_length=100)
    assayIds: list[Identifier] = Field(min_length=1, max_length=100)

    @field_validator('publicationIds')
    @classmethod
    def valid_publications(cls, values):
        if any(not value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value) for value in values):
            raise ValueError('Publication identifiers must be bounded plain text')
        return values


class ReviewedRights(Contract):
    status: Literal['approved-for-local-research', 'unresolved', 'denied']
    allowedRoles: list[Role] = Field(default_factory=list, max_length=2)
    redistributionApproved: StrictBool = False
    commercialUseApproved: StrictBool = False
    cloudTransferAllowed: StrictBool = False
    evidenceSha256: Sha | None = None
    reviewedBy: Identifier | None = None
    reviewedAt: StrictStr | None = None
    _review_time = field_validator('reviewedAt')(_utc)


class IndependenceReview(Contract):
    status: Literal['verified', 'unknown', 'not-independent']
    sourcePopulationIndependent: StrictBool = False
    evidenceSha256: Sha | None = None
    reviewedBy: Identifier | None = None
    reviewedAt: StrictStr | None = None
    _review_time = field_validator('reviewedAt')(_utc)


class Exposure(Contract):
    labelsPreviouslyViewed: StrictBool
    usedForTraining: StrictBool
    usedForModelSelection: StrictBool
    sourcePreviouslyEvaluated: StrictBool


class TaskDefinition(Contract):
    positiveClass: StrictStr = Field(min_length=1, max_length=600)
    assayType: StrictStr = Field(min_length=1, max_length=200)
    species: StrictStr = Field(min_length=1, max_length=100)
    unit: StrictStr = Field(min_length=1, max_length=80)
    threshold: StrictStr | None = Field(default=None, max_length=200)
    definition: StrictStr = Field(min_length=1, max_length=2000)
    negativeClass: StrictStr | None = Field(default=None, min_length=1, max_length=600)
    experimentalConditions: StrictStr | None = Field(default=None, min_length=1, max_length=2000)
    trainedTaskContractSha256: Sha
    comparisonApproved: StrictBool = False
    comparisonEvidenceSha256: Sha | None = None


class CandidateMetadata(Contract):
    schema_version: Literal['aidd.independent.candidate.v1'] = Field(alias='schema')
    datasetId: Identifier
    datasetVersion: Identifier
    sourceId: Identifier
    sourceVersion: StrictStr = Field(min_length=1, max_length=200)
    sourceUrl: StrictStr = Field(min_length=1, max_length=1000)
    retrievedAtUtc: StrictStr | None = None
    originalSampleCount: Annotated[StrictInt, Field(ge=0)] | None = None
    rawSha256: Sha
    rowsSha256: Sha
    labelsSha256: Sha | None = None
    role: Literal['independent-evaluation', 'calibration', 'audit-only']
    taskContractVersion: Identifier
    taskContracts: dict[Identifier, TaskDefinition] = Field(min_length=1, max_length=64)
    lineage: Lineage
    rights: ReviewedRights
    independence: IndependenceReview
    exposure: Exposure
    _retrieval_time = field_validator('retrievedAtUtc')(_utc)

    @field_validator('sourceUrl')
    @classmethod
    def valid_source(cls, value):
        parts = urlsplit(value)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('Source citation must be a credential-free HTTPS URL without query/fragment')
        return value


class ModelBinding(Contract):
    modelId: Identifier
    modelVersion: Identifier
    weightsSha256: Sha
    manifestSha256: Sha
    taskContractVersion: Identifier
    taskContractHashes: dict[Identifier, Sha] = Field(min_length=1, max_length=64)


class LabelPermission(Contract):
    granted: StrictBool = False
    approvedBy: Identifier | None = None
    approvedAt: StrictStr | None = None
    purpose: Literal['evaluate', 'calibrate'] = 'evaluate'
    _approval_time = field_validator('approvedAt')(_utc)


class EvaluationSettings(Contract):
    """Predeclared v1 analysis; missing settings never inherit CLI defaults."""
    version: Literal['aidd.independent.evaluation-settings.v1']
    metrics: list[Literal['auroc', 'averagePrecision', 'brier', 'ece10Bins']]
    bootstrapReplicates: Literal[500]
    bootstrapUnit: Literal['scaffold-cluster']
    minimumBootstrapPerClass: Literal[10]
    minimumBootstrapGroups: Literal[10]
    seed: Literal[42]

    @field_validator('metrics')
    @classmethod
    def exact_metrics(cls, value):
        if value != ['auroc', 'averagePrecision', 'brier', 'ece10Bins']:
            raise ValueError('The predeclared metric set and ordering must match v1')
        return value

    @model_validator(mode='before')
    @classmethod
    def exact_integer_types(cls, value):
        if isinstance(value, dict) and any(type(value.get(key)) is not int for key in
                ('bootstrapReplicates', 'minimumBootstrapPerClass', 'minimumBootstrapGroups', 'seed')):
            raise ValueError('Frozen analysis counts and seed must be strict integers')
        return value


def frozen_evaluation_settings():
    return EvaluationSettings.model_validate({
        'version': 'aidd.independent.evaluation-settings.v1',
        'metrics': ['auroc', 'averagePrecision', 'brier', 'ece10Bins'],
        'bootstrapReplicates': 500, 'bootstrapUnit': 'scaffold-cluster',
        'minimumBootstrapPerClass': 10, 'minimumBootstrapGroups': 10, 'seed': 42,
    }).model_dump()


class IndependentProtocol(Contract):
    schema_version: Literal['aidd.independent.protocol.v1'] = Field(alias='schema')
    protocolId: Identifier
    protocolVersion: Identifier
    role: Role
    status: Literal['draft', 'frozen']
    frozenAt: StrictStr | None = None
    candidateMetadataSha256: Sha
    referencesSha256: Sha
    taskContractVersion: Identifier
    approvedTaskContractHashes: dict[Identifier, Sha] = Field(min_length=1, max_length=64)
    modelBindings: list[ModelBinding] = Field(min_length=1, max_length=12)
    evaluationSettings: EvaluationSettings | None = None
    evaluatorSourceSha256: Sha | None = None
    labelReadPermission: LabelPermission = Field(default_factory=LabelPermission)
    useForTraining: Literal[False] = False
    useForModelSelection: Literal[False] = False
    _freeze_time = field_validator('frozenAt')(_utc)

    @model_validator(mode='after')
    def distinct_models(self):
        if len({row.modelId for row in self.modelBindings}) != len(self.modelBindings):
            raise ValueError('Duplicate frozen model binding')
        return self


class CandidateRow(Contract):
    smiles: StrictStr = Field(min_length=1, max_length=4096)
    sourceRowId: Identifier


def contract_dict(value, cls):
    return cls.model_validate(value).model_dump(by_alias=True)


def _review_complete(review):
    return bool(review.get('evidenceSha256') and review.get('reviewedBy') and review.get('reviewedAt'))


def _analysis_blockers(protocol):
    reasons = []
    if protocol.get('evaluationSettings') is None:
        reasons.append('evaluation-settings-not-predeclared')
    if protocol.get('evaluatorSourceSha256') is None:
        reasons.append('evaluator-source-not-predeclared')
    else:
        try:
            if protocol['evaluatorSourceSha256'] != evaluator_source_sha256():
                reasons.append('evaluator-source-sha256-mismatch')
        except (OSError, ValueError):
            reasons.append('evaluator-source-dependency-unavailable')
    return reasons


def _frozen_review_times(meta, protocol):
    if not protocol.get('frozenAt'):
        return False
    freeze_time = datetime.fromisoformat(protocol['frozenAt'].replace('Z', '+00:00'))
    reviews = (meta['rights'], meta['independence'])
    return all(review.get('reviewedAt') and
               datetime.fromisoformat(review['reviewedAt'].replace('Z', '+00:00')) <= freeze_time
               for review in reviews)


def _group_rows(rows):
    """All related candidate structures share one conservative group ID."""
    roots = list(range(len(rows)))
    def find(i):
        while roots[i] != i:
            roots[i] = roots[roots[i]]
            i = roots[i]
        return i
    owners = {}
    for i, row in enumerate(rows):
        for token in identity_tokens(row['auditIdentity']):
            if token in owners:
                a, b = find(i), find(owners[token])
                roots[max(a, b)] = min(a, b)
            else:
                owners[token] = i
    members = {}
    for i, row in enumerate(rows):
        members.setdefault(find(i), []).append(row['canonicalSmiles'])
    for i, row in enumerate(rows):
        row['groupId'] = object_sha256(sorted(members[find(i)]))


def audit_dataset(rows, metadata, references, protocol):
    """Return private structure projections plus a strictly aggregate publicAudit.

    Invalid contracts raise. Valid but incomplete rights/reference/exposure
    evidence yields not-independently-qualified, never a synthetic dataset.
    Duplicate canonical inputs are quarantined as a group regardless of labels.
    Per-record hashes of binary labels are forbidden: they are enumerable.
    """
    meta = contract_dict(metadata, CandidateMetadata)
    frozen = contract_dict(protocol, IndependentProtocol)
    validate_references(references)
    if not isinstance(rows, list) or len(rows) > 200000:
        raise ValueError('Candidate input must be a bounded row list')
    projected = [contract_dict(row, CandidateRow) for row in rows]
    metadata_sha, protocol_sha = object_sha256(meta), object_sha256(frozen)
    reasons = _analysis_blockers(frozen)
    if object_sha256(projected) != meta['rowsSha256']: reasons.append('candidate-structure-rows-sha256-mismatch')
    if not references['completeReferences']: reasons.append('incomplete-historical-references')
    if frozen['candidateMetadataSha256'] != metadata_sha: reasons.append('candidate-metadata-not-bound-to-protocol')
    if frozen['referencesSha256'] != references['receiptSha256']: reasons.append('historical-references-not-bound-to-protocol')
    if frozen['status'] != 'frozen' or not frozen['frozenAt']: reasons.append('protocol-not-frozen')
    if meta['role'] == 'audit-only' or frozen['role'] != meta['role']: reasons.append('dataset-role-not-approved')
    if (meta['rights']['status'] != 'approved-for-local-research' or frozen['role'] not in meta['rights']['allowedRoles']
            or not _review_complete(meta['rights'])): reasons.append('source-rights-not-reviewed-for-role')
    if (meta['independence']['status'] != 'verified' or not meta['independence']['sourcePopulationIndependent']
            or not _review_complete(meta['independence'])): reasons.append('source-population-independence-not-reviewed')
    if any(meta['exposure'].values()): reasons.append('previously-exposed-or-used-source')
    if not meta['labelsSha256']: reasons.append('label-snapshot-not-bound')
    mappings = {task: row['trainedTaskContractSha256'] for task, row in meta['taskContracts'].items()}
    if (meta['taskContractVersion'] != frozen['taskContractVersion']
            or mappings != frozen['approvedTaskContractHashes']
            or any(not row['comparisonApproved'] or not row['comparisonEvidenceSha256'] for row in meta['taskContracts'].values())):
        reasons.append('task-semantics-not-explicitly-approved')
    for binding in frozen['modelBindings']:
        if (binding['taskContractVersion'] != frozen['taskContractVersion']
                or any(binding['taskContractHashes'].get(task) != checksum for task, checksum in mappings.items())):
            reasons.append('frozen-model-task-binding-mismatch')
    if frozen['frozenAt'] and not _frozen_review_times(meta, frozen):
        reasons.append('source-review-not-complete-before-protocol-freeze')
    sources = lineage_overlap(meta, references)
    if sources: reasons.append('historical-source-lineage-overlap')
    forbidden, reference_counts = historical_forbidden_index(references)
    accepted, rejected, canonical_groups = [], [], {}
    source_id_counts = {}
    for row in projected: source_id_counts[row['sourceRowId']] = source_id_counts.get(row['sourceRowId'], 0) + 1
    valid_rows, row_rejections = [], {}
    for row in projected:
        rejection = None
        if source_id_counts[row['sourceRowId']] > 1: rejection = 'duplicate-source-row-id'
        try:
            canonical = canonicalize_smiles(row['smiles'])
            identity, overlaps = structural_overlap(canonical, forbidden)
        except (ValueError, RuntimeError, TypeError):
            rejected.append({'sourceRowId': row['sourceRowId'], 'reason': 'invalid-or-unsupported-structure'})
            continue
        item = {'canonicalSmiles': canonical, 'structureId': hashlib.sha256(canonical.encode()).hexdigest(),
                'sourceId': meta['sourceId'], 'sourceDataset': meta['datasetId'], 'sourceRowId': row['sourceRowId'],
                'auditIdentity': identity}
        if overlaps: rejection = 'historical-structure-overlap'
        valid_rows.append(item)
        if rejection: row_rejections[id(item)] = {'reason': rejection, 'overlapRoles': overlaps}
    # Group before exclusion, just as v3 does: an overlapping salt/tautomer
    # cannot bridge a nominally disjoint row into a newly eligible component.
    _group_rows(valid_rows)
    contaminated = {row['groupId'] for row in valid_rows if row_rejections.get(id(row), {}).get('reason') == 'historical-structure-overlap'}
    for item in valid_rows:
        rejection = row_rejections.get(id(item))
        if item['groupId'] in contaminated and rejection is None:
            rejection = {'reason': 'historical-overlap-connected-component', 'overlapRoles': []}
        if rejection:
            rejected.append({'structureId': item['structureId'], 'sourceRowId': item['sourceRowId'], **rejection})
        else:
            canonical_groups.setdefault(item['canonicalSmiles'], []).append(item)
    for group in canonical_groups.values():
        if len(group) > 1:
            rejected.extend({'structureId': row['structureId'], 'sourceRowId': row['sourceRowId'],
                             'reason': 'duplicate-or-conflicting-canonical-structure'} for row in group)
        else:
            accepted.extend(group)
    _group_rows(accepted)
    accepted.sort(key=lambda row: row['structureId'])
    if not accepted: reasons.append('no-eligible-real-structures')
    qualified = not reasons
    counts = {}
    for row in rejected: counts[row['reason']] = counts.get(row['reason'], 0) + 1
    audit = {'schema': AUDIT_SCHEMA, 'status': 'independently-qualified-for-sealing' if qualified else 'not-independently-qualified',
             'qualified': qualified, 'datasetId': meta['datasetId'], 'datasetVersion': meta['datasetVersion'],
             'role': meta['role'], 'candidateMetadataSha256': metadata_sha, 'frozenProtocolSha256': protocol_sha,
             'referencesSha256': references['receiptSha256'], 'completeReferences': references['completeReferences'],
             'inputRows': len(rows), 'eligibleStructureCount': len(accepted), 'quarantinedRowCount': len(rejected),
             'originalSampleCount': meta['originalSampleCount'], 'retrievedAtUtc': meta['retrievedAtUtc'],
             'positiveCount': None, 'negativeCount': None, 'missingLabelCount': None,
             'labelCountsStatus': 'not-read-labelblind-audit',
             'quarantineReasons': counts, 'sourceOverlap': sources, 'blockingReasons': sorted(set(reasons)),
             'historicalReferenceCounts': reference_counts, 'labelsRead': False, 'labelsIncluded': False,
             'eligibleRowsSha256': object_sha256(accepted),
             'oldTestLabelsUsedForSelection': False, 'chemistryVersion': CHEMISTRY_VERSION,
             'identityVersion': IDENTITY_VERSION, 'rdkitVersion': rdBase.rdkitVersion,
             'evaluationSettings': frozen['evaluationSettings'], 'evaluatorSourceSha256': frozen['evaluatorSourceSha256'],
             'pretrainingStructureExposure': 'unknown', 'independentAccuracyEvaluated': False,
             'sourceReviewIsOperatorEvidence': True,
             'limitations': ['Structural disjointness does not prove source-population independence.',
                'Rights and task equivalence require human evidence review; this program does not certify legal rights.',
                'Historical test/Karim and prior development cannot become new blind labels.',
                'All acyclic structures share one conservative group; no grouping relaxation for sample size.']}
    return {'status': audit['status'], 'qualified': qualified, 'publicAudit': audit, 'audit': audit,
            'eligibleRows': accepted, 'quarantinedRows': rejected, 'metadata': meta, 'protocol': frozen,
            'candidateSha256': object_sha256(projected), 'metadataSha256': metadata_sha, 'protocolSha256': protocol_sha,
            '_auditInputs': projected, '_references': references}


def _json_bytes(value):
    return (canonical_json(value) + '\n').encode('utf-8')


def _json_loads(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('Duplicate sealed JSON contract key')
            result[key] = item
        return result
    def finite(_):
        raise ValueError('Nonfinite sealed JSON value')
    return json.loads(value, object_pairs_hook=unique, parse_constant=finite)


def _validate_labels(value, rows, tasks):
    if not isinstance(value, dict) or set(value) != {row['structureId'] for row in rows}:
        raise ValueError('Labels must match every sealed structure exactly')
    for row in rows:
        observed = value[row['structureId']]
        if (not isinstance(observed, dict) or not observed
                or any(task not in tasks or type(label) is not int or label not in (0, 1) for task, label in observed.items())):
            raise ValueError('Labels must be observed binary task values; missing labels are omitted')
    return value


def seal_dataset(audit_result, output_dir, *, labels=None, label_read_permission=False):
    """Write a fresh immutable seal; no overwrite, training or evaluation.

    Labels are a separate dictionary, never accepted in audit rows or metadata.
    Their canonical-content checksum must have been fixed before label access.
    """
    audit = audit_result['publicAudit']
    meta = contract_dict(audit_result['metadata'], CandidateMetadata)
    protocol = contract_dict(audit_result['protocol'], IndependentProtocol)
    repeated = audit_dataset(audit_result['_auditInputs'], meta, audit_result['_references'], protocol)
    if (audit_result.get('qualified') is not True or audit.get('qualified') is not True
            or repeated['publicAudit'] != audit or repeated['eligibleRows'] != audit_result['eligibleRows']
            or object_sha256(meta) != audit['candidateMetadataSha256']
            or object_sha256(protocol) != audit['frozenProtocolSha256']):
        raise ValueError('Only a qualified, unchanged audit may be sealed')
    permission = protocol['labelReadPermission']
    needed = 'evaluate' if protocol['role'] == 'independent-evaluation' else 'calibrate'
    if (label_read_permission is not True or permission['granted'] is not True
            or permission['purpose'] != needed or not permission['approvedBy'] or not permission['approvedAt']):
        raise ValueError('Sealing labels requires explicit protocol and caller permission for this role')
    rows = copy.deepcopy(audit_result['eligibleRows'])
    _validate_labels(labels, rows, protocol['approvedTaskContractHashes'])
    if object_sha256(labels) != meta['labelsSha256']:
        raise ValueError('Authorized labels differ from the pre-frozen label snapshot')
    destination = Path(output_dir)
    if destination.exists():
        raise ValueError('Refusing to overwrite an existing seal or history')
    destination.mkdir(parents=True, exist_ok=False)
    from backend.compare_persistence import _secure_path
    _secure_path(destination, directory=True)
    payloads = {'structures': ('structures.jsonl', b''.join(_json_bytes(row) for row in rows)),
                'labels': ('labels.private.json', _json_bytes(labels)),
                'metadata': ('metadata.json', _json_bytes(meta)), 'protocol': ('protocol.json', _json_bytes(protocol)),
                'audit': ('audit.json', _json_bytes(audit))}
    files = {}
    for role, (name, content) in payloads.items():
        path = destination / name
        with path.open('xb') as handle: handle.write(content)
        # Reuse the repository's current-user/SYSTEM ACL, not a public cache.
        _secure_path(path)
        files[role] = {'file': name, 'sha256': file_sha256(path)}
    manifest = {'schema': SEAL_SCHEMA, 'status': 'sealed-' + protocol['role'], 'qualified': True,
                'role': protocol['role'], 'datasetId': meta['datasetId'], 'datasetVersion': meta['datasetVersion'],
                'protocolId': protocol['protocolId'], 'protocolVersion': protocol['protocolVersion'],
                'frozenAt': protocol['frozenAt'], 'sealedAt': datetime.now(timezone.utc).isoformat(),
                'taskContractVersion': protocol['taskContractVersion'], 'taskContractHashes': protocol['approvedTaskContractHashes'],
                'frozenProtocolSha256': object_sha256(protocol), 'candidateMetadataSha256': object_sha256(meta),
                'referencesSha256': protocol['referencesSha256'], 'modelBindings': protocol['modelBindings'],
                'sourceStrata': [meta['sourceId']], 'structureCount': len(rows), 'files': files,
                'chemistryVersion': CHEMISTRY_VERSION, 'identityVersion': IDENTITY_VERSION, 'rdkitVersion': rdBase.rdkitVersion,
                'labelsSeparate': True, 'labelReadPermission': permission,
                'evaluationSettings': protocol['evaluationSettings'], 'evaluatorSourceSha256': protocol['evaluatorSourceSha256'],
                'trainingAllowed': False, 'modelSelectionAllowed': False, 'formalAvailability': False}
    with (destination / 'seal.json').open('xb') as handle: handle.write(_json_bytes(manifest))
    from backend.compare_persistence import _secure_path
    _secure_path(destination / 'seal.json')
    return {**manifest, 'manifestSha256': file_sha256(destination / 'seal.json')}


def load_sealed_dataset(path, expected_sha256, purpose='evaluate', allow_labels=False):
    """Verify a caller-pinned seal. Default does not open the label file."""
    if type(allow_labels) is not bool:
        raise ValueError('Label access must be an explicit strict boolean')
    seal_path = Path(path)
    if seal_path.is_dir(): seal_path = seal_path / 'seal.json'
    if (not isinstance(expected_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256)
            or seal_path.is_symlink() or not seal_path.is_file() or file_sha256(seal_path) != expected_sha256):
        raise ValueError('Sealed manifest missing or SHA256 mismatch')
    manifest = _json_loads(seal_path.read_text(encoding='utf-8'))
    if (manifest.get('schema') != SEAL_SCHEMA or manifest.get('qualified') is not True
            or manifest.get('status') != 'sealed-' + str(manifest.get('role'))
            or manifest.get('role') not in ('independent-evaluation', 'calibration')
            or manifest.get('trainingAllowed') is not False or manifest.get('modelSelectionAllowed') is not False
            or manifest.get('chemistryVersion') != CHEMISTRY_VERSION or manifest.get('identityVersion') != IDENTITY_VERSION
            or manifest.get('rdkitVersion') != rdBase.rdkitVersion or manifest.get('labelsSeparate') is not True):
        raise ValueError('Unsupported, incomplete or non-independent seal')
    required_purpose = 'evaluate' if manifest['role'] == 'independent-evaluation' else 'calibrate'
    if purpose != required_purpose:
        raise ValueError('Dataset role does not permit this use; no training or model selection')
    if allow_labels is True:
        admitted = contract_dict(manifest.get('labelReadPermission'), LabelPermission)
        if (admitted['granted'] is not True or admitted['purpose'] != purpose
                or not admitted['approvedBy'] or not admitted['approvedAt']):
            raise ValueError('No explicit approved label-read permission; label file was not opened')
    files = manifest.get('files', {})
    if set(files) != {'structures', 'labels', 'metadata', 'protocol', 'audit'}:
        raise ValueError('Incomplete sealed file contract')
    resolved = {}
    root = seal_path.parent.resolve()
    for role, entry in files.items():
        if not isinstance(entry, dict) or set(entry) != {'file', 'sha256'}:
            raise ValueError('Invalid sealed file descriptor')
        name = entry.get('file')
        if (not isinstance(name, str) or name != SEALED_FILES[role] or Path(name).name != name or '\\' in name or ':' in name
                or name in ('', '.', '..') or not isinstance(entry.get('sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])):
            raise ValueError('Sealed path escapes its directory')
        target = root / name
        if target.is_symlink() or target.resolve().parent != root:
            raise ValueError('Sealed path is not a local owned file')
        resolved[role] = target
        if role == 'labels':
            continue  # Always validate the *actual protocol* before any label bytes/hash.
        if not target.is_file() or file_sha256(target) != entry.get('sha256'):
            raise ValueError('Sealed artifact SHA256 mismatch: ' + role)
    meta = contract_dict(_json_loads(resolved['metadata'].read_text(encoding='utf-8')), CandidateMetadata)
    protocol = contract_dict(_json_loads(resolved['protocol'].read_text(encoding='utf-8')), IndependentProtocol)
    audit = _json_loads(resolved['audit'].read_text(encoding='utf-8'))
    if (object_sha256(meta) != manifest['candidateMetadataSha256']
            or object_sha256(protocol) != manifest['frozenProtocolSha256']
            or protocol['candidateMetadataSha256'] != object_sha256(meta)
            or protocol['referencesSha256'] != manifest['referencesSha256']
            or audit.get('qualified') is not True or audit.get('frozenProtocolSha256') != object_sha256(protocol)
            or manifest['modelBindings'] != protocol['modelBindings']
            or manifest['taskContractHashes'] != protocol['approvedTaskContractHashes']
            or manifest['taskContractVersion'] != protocol['taskContractVersion']
            or manifest.get('protocolId') != protocol['protocolId']
            or manifest.get('protocolVersion') != protocol['protocolVersion']
            or manifest.get('frozenAt') != protocol['frozenAt']
            or manifest.get('evaluationSettings') != protocol['evaluationSettings']
            or manifest.get('evaluatorSourceSha256') != protocol['evaluatorSourceSha256']
            or audit.get('evaluationSettings') != protocol['evaluationSettings']
            or audit.get('evaluatorSourceSha256') != protocol['evaluatorSourceSha256']
            or _analysis_blockers(protocol) or not _frozen_review_times(meta, protocol)
            or manifest['role'] != protocol['role'] or meta['role'] != protocol['role']
            or protocol['status'] != 'frozen' or not protocol['frozenAt']
            or audit.get('completeReferences') is not True or audit.get('blockingReasons') != []
            or audit.get('candidateMetadataSha256') != object_sha256(meta)
            or audit.get('referencesSha256') != protocol['referencesSha256']
            or audit.get('labelsRead') is not False or any(meta['exposure'].values())
            or meta['rights']['status'] != 'approved-for-local-research'
            or protocol['role'] not in meta['rights']['allowedRoles'] or not _review_complete(meta['rights'])
            or meta['independence']['status'] != 'verified' or not meta['independence']['sourcePopulationIndependent']
            or not _review_complete(meta['independence'])
            or lineage_overlap(meta, {'sourceLineage': KNOWN_LINEAGE})
            or any(not row['comparisonApproved'] or not row['comparisonEvidenceSha256'] for row in meta['taskContracts'].values())
            or {task: row['trainedTaskContractSha256'] for task, row in meta['taskContracts'].items()} != protocol['approvedTaskContractHashes']):
        raise ValueError('Sealed metadata/protocol/source/task binding mismatch')
    for binding in protocol['modelBindings']:
        if (binding['taskContractVersion'] != protocol['taskContractVersion'] or any(
                binding['taskContractHashes'].get(task) != checksum
                for task, checksum in protocol['approvedTaskContractHashes'].items())):
            raise ValueError('Sealed model/task contract mismatch')
    rows = [_json_loads(line) for line in resolved['structures'].read_text(encoding='utf-8').splitlines() if line]
    if not rows or len(rows) != manifest['structureCount'] or len({row['structureId'] for row in rows}) != len(rows):
        raise ValueError('Sealed structure membership/count mismatch')
    for row in rows:
        if set(row) != {'canonicalSmiles', 'structureId', 'sourceId', 'sourceDataset', 'sourceRowId', 'auditIdentity', 'groupId'}:
            raise ValueError('Sealed structure rows may not contain labels, per-record label hashes or extra fields')
        canonical = canonicalize_smiles(row['canonicalSmiles'])
        if (canonical != row['canonicalSmiles'] or hashlib.sha256(canonical.encode()).hexdigest() != row['structureId']
                or row['sourceId'] != meta['sourceId'] or row['sourceDataset'] != meta['datasetId']
                or row['auditIdentity'] != structural_overlap(canonical, {})[0]):
            raise ValueError('Sealed chemical identity or source mismatch')
    expected_rows = copy.deepcopy(rows)
    _group_rows(expected_rows)
    if expected_rows != rows or object_sha256(rows) != audit.get('eligibleRowsSha256'):
        raise ValueError('Sealed group identities or audited structural membership changed')
    labels = None
    if allow_labels is True:
        permission = protocol['labelReadPermission']
        if (permission != manifest['labelReadPermission'] or permission['granted'] is not True
                or not permission['approvedBy'] or not permission['approvedAt'] or permission['purpose'] != purpose):
            raise ValueError('No explicit approved label-read permission for this purpose')
        label_path = resolved['labels']
        if not label_path.is_file() or file_sha256(label_path) != files['labels']['sha256']:
            raise ValueError('Sealed artifact SHA256 mismatch: labels')
        labels = _json_loads(label_path.read_text(encoding='utf-8'))
        _validate_labels(labels, rows, protocol['approvedTaskContractHashes'])
        if object_sha256(labels) != meta['labelsSha256']:
            raise ValueError('Label snapshot no longer matches pre-frozen metadata')
        for row in rows: row['labels'] = copy.deepcopy(labels[row['structureId']])
    return {'manifest': manifest, 'manifestSha256': expected_sha256, 'rows': rows,
            'labels': labels, 'metadata': meta, 'protocol': protocol, 'audit': audit}
