"""Offline, result-free EPA membership/DSSTox mapping audit (never qualification).

An assay inventory is not membership. Only explicit endpoint-member projections
and separate structure projections are accepted. No HTTP or model execution is
implemented here, and passing this mapper cannot authorize a scientific test.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator
from rdkit import Chem, rdBase

from backend.chemistry import CHEMISTRY_VERSION, canonicalize_smiles
from backend.dataset_overlap_audit import (historical_forbidden_index, lineage_overlap,
    object_sha256, structural_overlap)
from backend.independent_validation_data import _group_rows
from backend.v3_data import IDENTITY_POLICY, IDENTITY_VERSION
from scripts.survey_chembl_labelblind import strict_json

SCHEMA = 'aidd.stage7b.epa-structure-projections.v1'
AUDIT_SCHEMA = 'aidd.stage7b.epa-structure-audit.v1'
PROCESSING_VERSION = 'epa-labelblind-mapping-1'
ENDPOINTS = (686, 3184, 3210)
OFFICIAL_HOSTS = {'www.epa.gov', 'comptox.epa.gov', 'clowder.edap-cluster.com', 'api.figshare.com', 'epa.figshare.com'}
SHA = r'^[0-9a-f]{64}$'


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class Projection(Strict):
    fileId: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,100}\.json$')
    sha256: str = Field(pattern=SHA)
    sourceUrl: str = Field(max_length=2000)
    retrievedAtUtc: str
    # A release name is a supplied binding, not proof that a live API is v4.3.
    sourceRelease: Literal['invitrodb4.3']
    releaseEvidenceSha256: str | None = Field(default=None, pattern=SHA)

    @field_validator('sourceUrl')
    @classmethod
    def official_https_only(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname not in OFFICIAL_HOSTS
                or parsed.username or parsed.password or parsed.fragment
                or parsed.port not in (None, 443)
                or any(word in parsed.query.casefold() for word in ('token', 'key', 'secret', 'password', 'credential'))):
            raise ValueError('Use an official credential-free HTTPS provenance URL')
        return value

    @field_validator('retrievedAtUtc')
    @classmethod
    def utc_time(cls, value):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError('Retrieval time must explicitly use UTC')
        return value


class ProjectionManifest(Strict):
    schemaVersion: Literal['aidd.stage7b.epa-structure-projections.v1']
    selectionPolicy: Literal['endpoint-membership-without-results']
    chemistryVersion: str
    identityVersion: str
    rdkitVersion: str
    membership: Projection
    structures: Projection


class Member(Strict):
    aeid: Literal[686, 3184, 3210]
    sourceSubstanceId: str = Field(min_length=1, max_length=128)
    dtxsid: str | None = Field(pattern=r'^DTXSID[0-9]+$')
    # A missing batch is retained as missing, not inferred from assay name.
    batchId: str | None = Field(default=None, min_length=1, max_length=128)


class Structure(Strict):
    dtxsid: str = Field(pattern=r'^DTXSID[0-9]+$')
    dtxcid: str | None = Field(pattern=r'^DTXCID[0-9]+$')
    smiles: str | None = Field(default=None, max_length=5000)
    substanceType: Literal['single-substance', 'salt', 'mixture', 'unknown']
    mappingRecordId: str = Field(min_length=1, max_length=128)


def _load_projection(directory: Path, entry: Projection):
    path = directory / entry.fileId
    if not path.is_file() or path.is_symlink():
        raise ValueError('Missing regular result-free projection; no synthetic fallback')
    raw = path.read_bytes()
    if len(raw) > 5_000_000 or hashlib.sha256(raw).hexdigest() != entry.sha256:
        raise ValueError('Projection SHA256/size mismatch')
    return raw


def load_projections(manifest_path, expected_sha256):
    """Require an out-of-band manifest hash, then pin bytes before row parsing.

    The independent data custodian must create these structure-only files from
    safe official projections. This module never projects a full result matrix.
    A hash attests bytes/integrity; it does not attest the custodian or release.
    """
    path = Path(manifest_path)
    raw = path.read_bytes()
    if path.is_symlink() or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('Projection manifest SHA256 mismatch')
    manifest = ProjectionManifest.model_validate(strict_json(raw))
    if (manifest.chemistryVersion != CHEMISTRY_VERSION or manifest.identityVersion != IDENTITY_VERSION
            or manifest.rdkitVersion != rdBase.rdkitVersion):
        raise ValueError('Frozen chemistry/identity/RDKit version mismatch')
    # Verify both file hashes before inspecting either list.
    raw_projections = [_load_projection(path.parent, entry) for entry in (manifest.membership, manifest.structures)]
    projected = [strict_json(raw) for raw in raw_projections]
    if any(not isinstance(rows, list) for rows in projected):
        raise ValueError('Projections must contain only explicit row lists')
    members = [Member.model_validate(row) for row in projected[0]]
    structures = [Structure.model_validate(row) for row in projected[1]]
    return manifest, members, structures


def audit_projections(manifest, members, structures, references):
    """Map actual membership only, audit conservative identities, expose aggregates.

    DTXSID denotes a substance and DTXCID a structure; neither is inferred from
    names. A one-to-many/conflicting mapping is quarantined, not chosen by score.
    """
    if (manifest.chemistryVersion != CHEMISTRY_VERSION or manifest.identityVersion != IDENTITY_VERSION
            or manifest.rdkitVersion != rdBase.rdkitVersion):
        raise ValueError('Frozen chemistry/identity/RDKit version mismatch')
    forbidden, _ = historical_forbidden_index(references)
    if references.get('completeReferences') is not True:
        raise ValueError('Complete frozen historical references are required')
    by_sid = defaultdict(list)
    raw_id_sids = defaultdict(set)
    for member in members:
        raw_id_sids[member.sourceSubstanceId].add(member.dtxsid)
    cid_canonicals = defaultdict(set)
    canonical_by_record = {}
    for row in structures:
        by_sid[row.dtxsid].append(row)
        try:
            canonical = canonicalize_smiles(row.smiles) if row.smiles else None
        except (ValueError, TypeError, RuntimeError):
            canonical = None
        canonical_by_record[(row.dtxsid, row.mappingRecordId, row.smiles)] = canonical
        if row.dtxcid and canonical:
            cid_canonicals[row.dtxcid].add(canonical)
    exclusions = Counter()
    candidates = set()
    duplicate_members = 0
    seen_members = set()
    mapped_members = 0
    multifragment_members = 0
    missing_batch = 0
    endpoint_member_counts = Counter()
    for member in sorted(members, key=lambda m: (m.aeid, m.sourceSubstanceId, m.dtxsid or '', m.batchId or '')):
        key = (member.aeid, member.sourceSubstanceId, member.dtxsid, member.batchId)
        if key in seen_members:
            duplicate_members += 1
            continue
        seen_members.add(key)
        endpoint_member_counts[str(member.aeid)] += 1
        missing_batch += member.batchId is None
        if len(raw_id_sids[member.sourceSubstanceId]) > 1:
            exclusions['ambiguous-source-substance-id'] += 1
            continue
        if member.dtxsid is None:
            exclusions['missing-dtxsid'] += 1
            continue
        rows = by_sid.get(member.dtxsid, [])
        if not rows:
            exclusions['missing-structure-mapping'] += 1
            continue
        signatures = {(row.dtxcid, canonical_by_record[(row.dtxsid, row.mappingRecordId, row.smiles)], row.substanceType) for row in rows}
        if len(signatures) != 1 or any(len(cid_canonicals[row.dtxcid]) > 1 for row in rows if row.dtxcid):
            exclusions['ambiguous-dtxsid-or-dtxcid-mapping'] += 1
            continue
        row = rows[0]
        if row.substanceType in ('mixture', 'unknown'):
            exclusions['mixture' if row.substanceType == 'mixture' else 'unknown-substance-type'] += 1
            continue
        canonical = canonical_by_record[(row.dtxsid, row.mappingRecordId, row.smiles)]
        if not canonical or not row.dtxcid:
            exclusions['invalid-or-missing-structure'] += 1
            continue
        multifragment_members += len(Chem.GetMolFrags(Chem.MolFromSmiles(canonical))) > 1
        mapped_members += 1
        candidates.add(canonical)
    overlap = Counter()
    surviving = []
    auditable = []
    invalid_identity = 0
    for canonical in sorted(candidates):
        try:
            identity, reasons = structural_overlap(canonical, forbidden)
        except (ValueError, RuntimeError, TypeError):
            invalid_identity += 1
            continue
        auditable.append({'canonicalSmiles': canonical, 'auditIdentity': identity, 'overlaps': reasons})
        overlap.update(set(reason.split(':', 1)[1] for reason in reasons))
    # Group before any historical or duplicate exclusion. A salt/scaffold bridge
    # propagates historical contamination to its entire connected component.
    # Related candidate structures are all quarantined; never choose the first.
    _group_rows(auditable)
    component_sizes = Counter(row['groupId'] for row in auditable)
    contaminated = {row['groupId'] for row in auditable if row['overlaps']}
    direct_count = sum(bool(row['overlaps']) for row in auditable)
    connected_count = sum(not row['overlaps'] and row['groupId'] in contaminated for row in auditable)
    duplicate_identity = sum(component_sizes[row['groupId']] > 1 for row in auditable)
    for row in auditable:
        if row['groupId'] not in contaminated and component_sizes[row['groupId']] == 1:
            surviving.append(row['canonicalSmiles'])
    source_matches = lineage_overlap({'sourceId': 'epa-invitrodb4.3', 'lineage': {
        'families': ['tox21-screening-program'] if any(m.aeid == 3210 for m in members) else [],
        'publicationIds': [], 'cohortIds': [], 'assayIds': ['epa-aeid-' + str(i) for i in ENDPOINTS]}}, references)
    return {
        'schema': AUDIT_SCHEMA, 'processingVersion': PROCESSING_VERSION, 'sourceRelease': 'invitrodb4.3',
        'identityVersion': IDENTITY_VERSION, 'identityPolicy': IDENTITY_POLICY,
        'chemistryVersion': CHEMISTRY_VERSION, 'rdkitVersion': rdBase.rdkitVersion,
        'referenceReceiptSha256': references['receiptSha256'],
        'inputProjectionHashes': {'membership': manifest.membership.sha256, 'structures': manifest.structures.sha256},
        'membershipRows': len(members), 'duplicateMembershipRows': duplicate_members,
        'endpointMembershipRows': dict(sorted(endpoint_member_counts.items())),
        'uniqueRawSubstanceIds': len({m.sourceSubstanceId for m in members}),
        'uniqueDtxsids': len({m.dtxsid for m in members if m.dtxsid}),
        'structureProjectionRows': len(structures), 'mappedMembershipRows': mapped_members,
        'multifragmentMappedMembershipRows': multifragment_members, 'missingBatchMembershipRows': missing_batch,
        'mappingExclusions': dict(sorted(exclusions.items())), 'uniqueCanonicalStructures': len(candidates),
        'historicalOverlapStructures': direct_count + connected_count,
        'directHistoricalOverlapStructures': direct_count,
        'connectedHistoricalOverlapStructures': connected_count,
        'historicalOverlapReasonsNonexclusive': dict(sorted(overlap.items())),
        'invalidAuditIdentityStructures': invalid_identity, 'internalNearEquivalentOrScaffoldDuplicates': duplicate_identity,
        'structurallyPassedStructures': len(surviving), 'survivingStructureSetSha256': object_sha256(surviving),
        'knownSourceFamilyMatches': source_matches, 'sourceIndependenceStatus': 'MANUAL_REVIEW_REQUIRED',
        'releaseBindingStatus': 'MANUAL_REVIEW_REQUIRED', 'rightsStatus': 'MANUAL_REVIEW_REQUIRED',
        'taskEquivalenceStatus': 'MANUAL_REVIEW_REQUIRED', 'independentlyQualifiedStructures': 0,
        'candidateLabelsRead': False, 'modelsExecuted': False, 'evaluationPerformed': False,
        'qualificationGranted': False, 'canEnterStage7C': False, 'gateStatus': 'STAGE7C_BLOCKED',
    }


def unavailable_audit(evidence):
    """No input means unknown counts, never a fabricated empty-assay result."""
    return {
        'schema': AUDIT_SCHEMA, 'processingVersion': PROCESSING_VERSION, 'sourceRelease': 'invitrodb4.3',
        'investigatedEndpointIds': list(ENDPOINTS), 'endpointMetadataCount': len(ENDPOINTS),
        'membershipRows': None, 'uniqueDtxsids': None, 'uniqueCanonicalStructures': None,
        'mappedMembershipRows': None, 'structurallyPassedStructures': None,
        'independentlyQualifiedStructures': None, 'mappingExclusions': None,
        'mappingAuditStatus': 'not-run-no-verified-endpoint-membership-and-structure-projection',
        'blockers': ['CTX-membership-projection-requires-individual-API-key',
                     'live-CTX-release-not-bound-to-invitrodb4.3',
                     'no-verified-result-free-member-to-structure-files',
                     'source-and-batch-lineage-not-reviewed', 'original-hERG-task-equivalence-unresolved',
                     'fixed-release-rights-review-unresolved'],
        'officialEvidence': evidence, 'candidateLabelsRead': False, 'modelsExecuted': False,
        'evaluationPerformed': False, 'qualificationGranted': False,
        'canEnterStage7C': False, 'gateStatus': 'STAGE7C_BLOCKED',
    }
