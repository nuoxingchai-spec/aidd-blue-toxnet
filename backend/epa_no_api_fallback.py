"""Verify public offline-source evidence without reading activities or labels.

This additive Stage 7B path cannot recover members from a chemical inventory,
approve a dataset, execute a model, or download anything. Result-free member
projections, when available, belong to the existing epa_structure_audit path.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path
import re
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rdkit import rdBase

from backend.chemistry import CHEMISTRY_VERSION
from backend.herg_label_contract_review import prereview_policy_sha256
from backend.v3_data import IDENTITY_VERSION
from scripts.audit_toxcast_metadata import PINS, metadata_rows
from scripts.survey_chembl_labelblind import strict_json

SCHEMA = 'aidd.stage7b.epa-no-api-fallback.v1'
EVIDENCE_SCHEMA = 'aidd.stage7b.epa-no-api-evidence.v1'
PROCESSING_VERSION = 'epa-no-api-metadata-1'
SUMMARY_DATASET = '687e392ae4b02565bc3e2914'
ENDPOINTS = {
    686: (423, 237, 'NVS_IC_hKhERGCh'),
    3184: (2914, 897, 'ERF_TW_IC_hKCNH2'),
    3210: (2939, 910, 'TOX21_hERG_U2OS_Antagonist'),
}
SAFE_DOWNLOAD_IDS = {
    '68af6bd3e4b02565fc7c3aa8',  # fixed assay annotations, never activities
    '68af6bd3e4b02565fc7c3aa0',  # fixed target annotations
    '697b7530e4b0731a6170449e',  # summary README
    '68b751b7e4b02565fc7c77e5',  # MySQL README; not the database dump
}
OFFICIAL_HOSTS = {
    'www.epa.gov', 'comptox.epa.gov', 'clowder.edap-cluster.com',
    'api.figshare.com', 'epa.figshare.com', 'doi.org', 's3.amazonaws.com',
}
MAX_FILE_BYTES = 5_000_000
MAX_TOTAL_BYTES = 20_000_000
SHA = r'^[0-9a-f]{64}$'


def official_url(value):
    """No credentials, CTX, alternate ports, or arbitrary cloud storage paths."""
    parsed = urlsplit(value)
    decoded = unquote(value).casefold()
    if (parsed.scheme != 'https' or parsed.hostname not in OFFICIAL_HOSTS
            or parsed.username or parsed.password or parsed.fragment
            or parsed.port not in (None, 443) or '/ctx-api' in decoded
            or parsed.query or '%' in value
            or '\\' in value or any(ord(c) < 32 for c in value)):
        raise ValueError('Only credential-free official non-CTX evidence URLs are permitted')
    if parsed.hostname == 'doi.org' and not parsed.path.startswith('/10.23645/epacomptox.'):
        raise ValueError('Not an EPA DOI')
    if parsed.hostname == 's3.amazonaws.com' and not parsed.path.startswith('/dmap-ccte-ccd/current/manual/'):
        raise ValueError('Only the EPA-linked public Dashboard manual is permitted on S3')
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class Evidence(Strict):
    fileId: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$')
    sourceUrl: str = Field(max_length=2000)
    retrievedAtUtc: str
    httpStatus: int = Field(ge=100, le=599)
    bytes: int = Field(ge=0, le=MAX_FILE_BYTES)
    sha256: str = Field(pattern=SHA)
    kind: Literal['official-page', 'file-inventory', 'file-metadata', 'readme',
                  'assay-annotations', 'assay-targets']
    location: str | None = Field(default=None, max_length=2000)

    @model_validator(mode='after')
    def metadata_only(self):
        official_url(self.sourceUrl)
        if self.location:
            official_url(self.location)
        dt = datetime.fromisoformat(self.retrievedAtUtc.replace('Z', '+00:00'))
        if dt.tzinfo is None or dt.utcoffset().total_seconds() != 0:
            raise ValueError('UTC evidence retrieval time required')
        parsed = urlsplit(self.sourceUrl)
        path = parsed.path
        if self.kind == 'file-inventory' and (parsed.hostname != 'clowder.edap-cluster.com'
                or not re.fullmatch(r'/api/datasets/[a-f0-9]{24}/files', path)):
            raise ValueError('File inventories must be official dataset metadata, not records')
        if self.kind == 'file-metadata' and (parsed.hostname != 'clowder.edap-cluster.com'
                or not re.fullmatch(r'/api/files/[a-f0-9]{24}/metadata', path)):
            raise ValueError('File metadata path required')
        if self.kind in ('readme', 'assay-annotations', 'assay-targets'):
            if (urlsplit(self.sourceUrl).hostname != 'clowder.edap-cluster.com'
                    or path not in {'/api/files/' + item for item in SAFE_DOWNLOAD_IDS}):
                raise ValueError('Only fixed reviewed result-free download IDs are permitted')
        if self.kind == 'official-page':
            page_ok = {
                'www.epa.gov': lambda: path.startswith(('/comptox-tools/', '/chemical-research/')),
                'doi.org': lambda: True,  # already constrained to EPA DOI above
                's3.amazonaws.com': lambda: True,  # already constrained to EPA manual above
                'api.figshare.com': lambda: bool(re.fullmatch(
                    r'/v2/articles/(?:6062623|6062524|6062527|5588566)(?:/versions/11)?', path)),
                'epa.figshare.com': lambda: bool(re.fullmatch(
                    r'/articles/dataset/[A-Za-z0-9_()\-]+/(?:6062623|6062524|6062527|5588566)(?:/11)?', path)),
                'clowder.edap-cluster.com': lambda: bool(re.fullmatch(r'/(?:files|datasets|spaces)/[a-f0-9]{24}', path)),
            }
            if parsed.hostname not in page_ok or not page_ok[parsed.hostname]():
                raise ValueError('Not a reviewed public metadata/page route')
        if self.fileId in ('.', '..') or '..' in self.fileId:
            raise ValueError('Evidence cache filename must be a basename')
        return self


class Receipt(Strict):
    schemaVersion: Literal['aidd.stage7b.epa-no-api-evidence.v1']
    selectionPolicy: Literal['official-source-metadata-only-no-CTX-no-outcomes']
    files: list[Evidence] = Field(min_length=1, max_length=100)
    probes: list['ArchiveProbe'] = Field(default_factory=list, max_length=20)


class ArchiveProbe(Strict):
    """Header/availability evidence is distinct from downloaded/cached bodies.

    Error responses and timeouts do not acquire a file. A successful range only
    establishes range support; it does not attest a complete archive checksum.
    """
    sourceUrl: str
    retrievedAtUtc: str | None
    requestMethod: Literal['HEAD', 'GET']
    requestRange: str | None = None
    httpStatus: int | None = Field(default=None, ge=100, le=599)
    errorKind: Literal['ReadTimeout', 'ConnectTimeout', 'ConnectError'] | None = None
    timeoutSeconds: int | None = Field(default=None, ge=1, le=60)
    observedContentRange: str | None = None
    observedContentLength: int | None = Field(default=None, ge=0)
    responseBodyReadBytes: int = Field(default=0, ge=0, le=65536)
    observedBodySha256: str | None = Field(default=None, pattern=SHA)

    @model_validator(mode='after')
    def bounded_archive_probe(self):
        official_url(self.sourceUrl)
        if self.sourceUrl not in {
                'https://clowder.edap-cluster.com/api/files/68af6b70e4b02565fc7c3a98',
                'https://clowder.edap-cluster.com/files/68af6b70e4b02565fc7c3a98/blob'}:
            raise ValueError('Only the reviewed summary archive can be probed')
        if self.retrievedAtUtc is not None:
            dt = datetime.fromisoformat(self.retrievedAtUtc.replace('Z', '+00:00'))
            if dt.tzinfo is None or dt.utcoffset().total_seconds() != 0:
                raise ValueError('UTC probe retrieval time required')
        elif self.errorKind is None:
            raise ValueError('Only a documented connection failure can have missing retrieval time')
        if (self.httpStatus is None) == (self.errorKind is None):
            raise ValueError('Record exactly one HTTP status or connection error')
        if self.requestMethod == 'HEAD':
            if self.requestRange is not None or self.responseBodyReadBytes:
                raise ValueError('HEAD cannot read an archive body')
        else:
            match = re.fullmatch(r'bytes=(?:(\d+)-(\d+)|-(\d+))', self.requestRange or '')
            if not match:
                raise ValueError('GET archive probes require an explicit bounded Range')
            size = int(match[3]) if match[3] else int(match[2]) - int(match[1]) + 1
            if not 0 < size <= 65536 or (match[2] and int(match[2]) >= 7502075199):
                raise ValueError('Probe Range exceeds reviewed archive or byte budget')
            if self.responseBodyReadBytes > size:
                raise ValueError('Read beyond requested range')
        if self.responseBodyReadBytes and (self.httpStatus != 206 or not self.observedBodySha256):
            raise ValueError('Read bodies require a partial response and observed checksum')
        if not self.responseBodyReadBytes and self.observedBodySha256 is not None:
            raise ValueError('An unread body has no observed checksum')
        if self.httpStatus == 206:
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', self.observedContentRange or '')
            if self.requestMethod != 'GET' or not match:
                raise ValueError('Partial response requires exact Content-Range evidence')
            start, end, total = map(int, match.groups())
            request = re.fullmatch(r'bytes=(?:(\d+)-(\d+)|-(\d+))', self.requestRange)
            expected = (total - int(request[3]), total - 1) if request[3] else (int(request[1]), int(request[2]))
            if total != 7502075199 or (start, end) != expected:
                raise ValueError('Content-Range does not match reviewed archive/request')
            if (self.observedContentLength not in (None, end - start + 1)
                    or self.responseBodyReadBytes not in (0, end - start + 1)):
                raise ValueError('Partial response length/checksum observation mismatch')
        return self


Receipt.model_rebuild()


def _regular_bytes(path, limit):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
        raise ValueError('Missing regular bounded metadata file')
    with path.open('rb') as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise ValueError('Metadata byte budget exceeded')
    return value


def load_evidence(receipt_path, expected_sha256):
    """Verify receipt and all cached bytes before inspecting any source metadata."""
    path = Path(receipt_path)
    raw = _regular_bytes(path, MAX_FILE_BYTES)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('Evidence receipt SHA256 mismatch')
    receipt = Receipt.model_validate(strict_json(raw))
    if len({r.fileId for r in receipt.files}) != len(receipt.files):
        raise ValueError('Duplicate evidence cache filename')
    if sum(r.bytes for r in receipt.files) > MAX_TOTAL_BYTES:
        raise ValueError('Cumulative source metadata budget exceeded')
    verified = {}
    for entry in receipt.files:
        body = _regular_bytes(path.parent / entry.fileId, MAX_FILE_BYTES)
        if len(body) != entry.bytes or hashlib.sha256(body).hexdigest() != entry.sha256:
            raise ValueError('Official evidence byte count/SHA256 mismatch')
        verified[entry.fileId] = body
    return receipt, verified


def endpoint_identities(directory):
    """Pin both workbooks, then bind endpoint/component/assay IDs and target."""
    raw = {name: _regular_bytes(Path(directory) / name, MAX_FILE_BYTES) for name in PINS}
    if any(hashlib.sha256(raw[name]).hexdigest() != pin for name, pin in PINS.items()):
        raise ValueError('Fixed invitrodb4.3 assay metadata SHA256 mismatch')
    annotations = metadata_rows(raw['epa-assay-annotations-v43.xlsx'],
        {'aeid', 'acid', 'aid', 'assay_name', 'assay_component_endpoint_name',
         'assay_source_name', 'organism', 'normalized_data_type', 'assay_design_type', 'assay_function_type'})
    target_rows = metadata_rows(raw['epa-assay-target-mappings-v43.xlsx'], {'aeid', 'official_symbol'})
    target_ids = {r['aeid'] for r in target_rows if r['official_symbol'].casefold() == 'kcnh2'}
    endpoints = []
    for aeid, (acid, aid, name) in sorted(ENDPOINTS.items()):
        rows = [r for r in annotations if r['aeid'] == str(aeid)]
        if len(rows) != 1 or str(aeid) not in target_ids:
            raise ValueError('Missing/duplicate endpoint annotation or target mapping')
        row = rows[0]
        if (row['acid'], row['aid'], row['assay_component_endpoint_name']) != (str(acid), str(aid), name):
            raise ValueError('Fixed endpoint/component/assay identity mismatch')
        endpoints.append({'aeid': aeid, 'acid': acid, 'aid': aid,
            'assayComponentEndpointName': name, 'assaySource': row['assay_source_name'],
            'organism': row['organism'], 'designType': row['assay_design_type'],
            'functionType': row['assay_function_type'], 'normalizedDataType': row['normalized_data_type'],
            'actualTestedMemberCount': None, 'uniqueSpidCount': None, 'uniqueChidCount': None,
            'uniqueDtxsidCount': None, 'mappedStructureCount': None,
            'taskEquivalenceStatus': 'MANUAL_REVIEW_REQUIRED'})
    return endpoints


def summary_inventory(receipt, bodies, dataset=SUMMARY_DATASET):
    url = 'https://clowder.edap-cluster.com/api/datasets/' + dataset + '/files'
    matches = [e for e in receipt.files if e.sourceUrl == url and e.httpStatus == 200 and e.kind == 'file-inventory']
    if len(matches) != 1:
        raise ValueError('Exactly one verified Summary_Files inventory is required')
    rows = strict_json(bodies[matches[0].fileId])
    if not isinstance(rows, list) or not rows:
        raise ValueError('Missing summary file inventory')
    result, seen = [], set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'id', 'size', 'filename', 'date-created', 'contentType'}
                or not re.fullmatch(r'[a-f0-9]{24}', str(row['id']))
                or row['id'] in seen or not re.fullmatch(r'[0-9]+', str(row['size']))
                or not isinstance(row['filename'], str) or len(row['filename']) > 150
                or not re.fullmatch(r'[A-Za-z0-9_(). -]+', row['filename'])
                or any(not isinstance(row[key], str) or len(row[key]) > 100
                       for key in ('date-created', 'contentType'))):
            raise ValueError('Invalid or changed official inventory schema')
        seen.add(row['id'])
        result.append({'fileId': row['id'], 'filename': row['filename'], 'reportedBytes': int(row['size']),
            'reportedCreatedAt': row['date-created'], 'sourceUrl': 'https://clowder.edap-cluster.com/api/files/' + row['id'],
            'inventoryIsTestedMembership': False})
    return sorted(result, key=lambda r: r['fileId'])


def archive_inventory(receipt, bodies):
    """Read publisher HTML inventory only, never ZIP entries or an activity CSV."""
    url = 'https://clowder.edap-cluster.com/files/68af6b70e4b02565fc7c3a98'
    rows = [e for e in receipt.files if e.sourceUrl == url and e.httpStatus == 200 and e.kind == 'official-page']
    if len(rows) != 1:
        raise ValueError('Exactly one verified summary archive metadata page is required')
    text = bodies[rows[0].fileId].decode('utf-8')
    names = sorted(set(re.findall(r'<li class="md-block">\s*([A-Za-z0-9_()./ -]+\.(?:R[Dd]ata|xlsx|csv))\s*</li>', text)))
    declared = set(re.findall(r'<strong>inventory size:</strong>\s*([0-9]+)', text))
    digests = set(re.findall(r'<strong>sha256:</strong>\s*([0-9a-f]{64})', text))
    if not names or len(declared) != 1 or len(digests) != 1:
        raise ValueError('Missing or changed publisher archive metadata schema')
    if any('..' in name or name.startswith('/') for name in names) or len(names) > int(next(iter(declared))):
        raise ValueError('Unsafe or contradictory archive metadata inventory')
    return {'sourceUrl': url, 'sourceHtmlSha256': rows[0].sha256,
        'publisherProvidedSha256': next(iter(digests)), 'publisherSha256LocallyVerified': False,
        'completeLocalArchiveSha256': None, 'declaredInventoryItems': int(next(iter(declared))),
        'parsedDataFileCount': len(names), 'parsedFileNames': names,
        'inventoryIsTestedMembership': False,
        'limitation': 'Publisher metadata inventory, not a locally parsed ZIP central directory or proof of tested members.'}


def audit_sources(receipt_path, expected_sha256, metadata_directory):
    receipt, bodies = load_evidence(receipt_path, expected_sha256)
    endpoints = endpoint_identities(metadata_directory)
    inventory = summary_inventory(receipt, bodies)
    contents = archive_inventory(receipt, bodies)
    mysql_dataset = '687e3940e4b02565bc3e291e'
    mysql_url = 'https://clowder.edap-cluster.com/api/datasets/' + mysql_dataset + '/files'
    mysql_inventory = summary_inventory(receipt, bodies, mysql_dataset) if any(
        e.sourceUrl == mysql_url and e.httpStatus == 200 for e in receipt.files) else None
    return {
        'schema': SCHEMA, 'processingVersion': PROCESSING_VERSION,
        'targetRelease': 'invitrodb4.3', 'targetReleaseDoi': '10.23645/epacomptox.6062623.v11',
        'evidenceReceiptSha256': expected_sha256,
        'metadataWorkbooksSha256': dict(PINS), 'officialEvidence': [e.model_dump() for e in receipt.files],
        'archiveAvailabilityProbes': [p.model_dump() for p in receipt.probes],
        'completeArchiveSha256': None,
        'verifiedCachedMetadataBytes': sum(e.bytes for e in receipt.files),
        'verifiedCachedMetadataFiles': len(receipt.files), 'summaryFileInventory': inventory,
        'archiveContentInventory': contents,
        'mysqlFileInventory': mysql_inventory,
        'endpoints': endpoints, 'chemistryVersion': CHEMISTRY_VERSION,
        'identityVersion': IDENTITY_VERSION, 'rdkitVersion': rdBase.rdkitVersion,
        'hERGPrereviewPolicySha256': prereview_policy_sha256(),
        'memberRecoveryStatus': 'not-recovered-no-verified-result-free-endpoint-membership',
        'actualTestedMembershipRows': None, 'uniqueSubstances': None, 'uniqueStructures': None,
        'mappedMembershipRows': None, 'missingStructureRows': None, 'ambiguousMappingRows': None,
        'invalidStructureRows': None, 'historicalOverlapStructures': None,
        'structurallyPassedStructures': None, 'independentlyQualifiedStructures': None,
        'structureAuditStatus': 'not-run-no-actual-member-and-structure-projections',
        'sourceIndependenceStatus': 'MANUAL_REVIEW_REQUIRED',
        'rightsStatus': 'MANUAL_REVIEW_REQUIRED', 'labelCustodyStatus': 'not-established',
        'candidateLabelsRead': False, 'ctxApiCalled': False, 'candidateModelsExecuted': False,
        'evaluationPerformed': False, 'trainingPerformed': False, 'sealingPerformed': False,
        'formalAvailability': False, 'qualificationGranted': False, 'canEnterStage7C': False,
        'gateStatus': 'STAGE7C_BLOCKED',
        'blockers': ['no-verified-v4.3-result-free-actual-tested-members',
            'no-verified-member-to-DSSTox-structure-snapshot',
            'experiment-batch-publication-source-lineage-not-reviewed',
            'original-hERG-threshold-operator-unit-boundary-unresolved',
            'EPA-binding-or-functional-readouts-not-approved-equivalent-to-original-task',
            'file-level-rights-and-redistribution-conditions-unresolved',
            'independent-custodian-and-frozen-qualified-evaluation-set-not-established'],
    }
