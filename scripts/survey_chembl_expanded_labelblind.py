"""Bounded ChEMBL 37 expansion with a plan frozen before activity retrieval.

Only structural and source metadata are requested. Activity values, qualifiers,
quality flags, comments and labels are deliberately unavailable to this reader.
Private raw projections never belong in Git. No evaluation or qualification is
performed here; a verified snapshot is input to the separate conservative audit.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.survey_chembl_labelblind import (
    API, RELEASE, VERSION, TARGET, FIELDS, FILTERS, PLURALS,
    encoded, projected_page, sha, strict_json,
)
from backend.v3_data import IDENTITY_POLICY, IDENTITY_VERSION
from backend.herg_label_contract_review import prereview_policy, prereview_policy_sha256

SCHEMA = 'aidd.stage7.chembl-expanded-labelblind.v1'
RECEIPT_SCHEMA = 'aidd.stage7.expanded-retrieval.v1'
PLAN_SCHEMA = 'aidd.stage7.expanded-plan.v1'
PAGE_SIZE = 200
MAX_RECORDS = 2000
MAX_RESPONSE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 5_000_000
MAX_REQUESTS = 120


def frozen_plan():
    return {
        'schema': PLAN_SCHEMA, 'version': VERSION, 'target': TARGET,
        'targetAccession': 'Q12809', 'targetOrganism': 'Homo sapiens',
        'targetType': 'SINGLE PROTEIN', 'recordLimit': MAX_RECORDS,
        'pageSize': PAGE_SIZE, 'orderBy': 'activity_id',
        'selection': 'first 2000 ascending activity_id; no outcome or publication-date selection',
        'fields': {key: list(value) for key, value in FIELDS.items()},
        'maxResponseBytes': MAX_RESPONSE_BYTES, 'maxTotalResponseBytes': MAX_TOTAL_BYTES,
        'maxRequests': MAX_REQUESTS, 'joinBatchSize': 40,
        'resultFieldsRequested': False, 'labelsRead': False, 'predictionsAllowed': False,
        'endpointMixingAllowed': False, 'qualifierAndQualityReview': 'independent-custodian-required',
        'eligibilityGranted': False,
        'identityVersion': IDENTITY_VERSION, 'identityPolicy': IDENTITY_POLICY,
        'historyPolicy': 'unchanged-stage6-all-fixed-historical-roles-and-source-lineage',
        'labelMappingStatus': 'MANUAL_REVIEW_REQUIRED-no-threshold-inference',
        'minimumEvaluationSupport': {'perClass': 10, 'scaffoldClusters': 10,
                                     'candidateClassCountsVisible': False},
        'hergPrereviewPolicy': prereview_policy(),
        'hergPrereviewPolicySha256': prereview_policy_sha256(),
    }


def validate_query(resource, params):
    if resource not in FILTERS or not isinstance(params, dict):
        raise ValueError('Unknown metadata resource')
    if set(params) != {'only', 'limit', 'offset'} | FILTERS[resource]:
        raise ValueError('Unexpected metadata query fields')
    if params['only'] != ','.join(FIELDS[resource]) or type(params['limit']) is not int or params['limit'] != PAGE_SIZE:
        raise ValueError('Metadata projection and page size are fixed')
    offset = params['offset']
    if type(offset) is not int or offset < 0:
        raise ValueError('Invalid metadata offset')
    if resource == 'activity':
        if offset >= MAX_RECORDS or offset % PAGE_SIZE or params['target_chembl_id'] != TARGET or params['order_by'] != 'activity_id':
            raise ValueError('Outcome selection, alternate targets and unfrozen offsets are forbidden')
    elif offset != 0:
        raise ValueError('Only activity metadata may have nonzero offsets')
    if resource == 'target':
        if params['target_components__accession'] != 'Q12809' or params['target_type'] != 'SINGLE PROTEIN':
            raise ValueError('Human KCNH2 target selection is fixed')
    elif resource in ('assay', 'document'):
        field = resource + '_chembl_id'
        identifiers = params[field + '__in']
        if (params['order_by'] != field or not isinstance(identifiers, str)
                or not re.fullmatch(r'CHEMBL[0-9]+(?:,CHEMBL[0-9]+){0,39}', identifiers)
                or identifiers.split(',') != sorted(set(identifiers.split(',')))):
            raise ValueError('Metadata joins require distinct sorted batches of at most 40 IDs')


def validate_page(payload, resource, params):
    rows, page = projected_page(payload, resource)
    if page['limit'] != PAGE_SIZE or page['offset'] != params['offset']:
        raise ValueError('Server returned another metadata page')
    # Never follow remote next/previous URLs; only frozen offsets are requested.
    if any(page[key] is not None and not isinstance(page[key], str) for key in ('next', 'previous')):
        raise ValueError('Invalid metadata pagination links')
    expected = min(PAGE_SIZE, max(0, page['total_count'] - params['offset']))
    if len(rows) != expected:
        raise ValueError('Metadata page is incomplete or substituted')
    return rows, page


class BoundedExpandedReader:
    def __init__(self, client, directory):
        self.client, self.directory = client, directory
        self.receipts = []
        self.total_bytes = self.requests = 0

    def read(self, name, url, params=None, resource=None):
        allowed = {API + item + '.json' for item in ('status', *FIELDS)} | {RELEASE + 'LICENSE', RELEASE + 'REQUIRED.ATTRIBUTION'}
        if url not in allowed:
            raise ValueError('Only fixed official HTTPS metadata sources are allowed')
        actual = next((key for key in FIELDS if url == API + key + '.json'), None)
        if actual != resource:
            raise ValueError('Metadata resource and URL mismatch')
        if resource:
            validate_query(resource, params)
        elif params:
            raise ValueError('Unexpected status/license query')
        if (self.directory / name).exists():
            raise ValueError('Refusing to overwrite an existing raw receipt')
        if self.requests >= MAX_REQUESTS:
            raise ValueError('Small-metadata request budget exhausted')
        self.requests += 1
        with self.client.stream('GET', url, params=params, follow_redirects=False) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError('Redirects and non-200 responses are forbidden')
            raw = bytearray()
            for chunk in response.iter_bytes():
                self.total_bytes += len(chunk)
                if len(raw) + len(chunk) > MAX_RESPONSE_BYTES or self.total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError('Small-metadata byte budget exceeded')
                raw.extend(chunk)
        raw = bytes(raw)
        payload = strict_json(raw) if url.startswith(API) else None
        if resource:
            validate_page(payload, resource, params)
        elif url == API + 'status.json':
            if not isinstance(payload, dict) or payload.get('chembl_db_version') != VERSION or payload.get('status') != 'UP':
                raise ValueError('The official API is not the pinned ChEMBL 37 release')
        with (self.directory / name).open('xb') as handle:
            handle.write(raw)
        self.receipts.append({'resource': name, 'url': url, 'params': params or {},
                              'sha256': sha(raw), 'bytes': len(raw),
                              'retrievedAtUtc': datetime.now(timezone.utc).isoformat()})
        return payload

    def page(self, resource, name, filters, offset=0):
        params = {'only': ','.join(FIELDS[resource]), 'limit': PAGE_SIZE, 'offset': offset, **filters}
        return validate_page(self.read(name, API + resource + '.json', params, resource), resource, params)


def _validate_members(bundle, page_sets, available):
    target = bundle['target']
    if target['target_chembl_id'] != TARGET or target['organism'] != 'Homo sapiens' or target['target_type'] != 'SINGLE PROTEIN':
        raise ValueError('Human KCNH2/Q12809 target identity was not established')
    activities = bundle['activities']
    ids = [row['activity_id'] for row in activities]
    if any(type(value) is not int for value in ids) or ids != sorted(set(ids)):
        raise ValueError('Activity IDs must be distinct and ordered across all pages')
    if len(activities) != min(MAX_RECORDS, available):
        raise ValueError('Frozen activity range was not completely retrieved')
    for resource in ('assay', 'document'):
        field = resource + '_chembl_id'
        expected = sorted({row[field] for row in activities if row[field] is not None})
        rows = bundle[PLURALS[resource]]
        actual = [row[field] for row in rows]
        if len(actual) != len(set(actual)) or sorted(actual) != expected:
            raise ValueError('Missing, duplicate or substituted source metadata joins')
        queries = [item['params'][field + '__in'] for item in page_sets if item['kind'] == resource]
        wanted = [','.join(expected[start:start + 40]) for start in range(0, len(expected), 40)]
        if queries != wanted:
            raise ValueError('Source metadata query batches differ from frozen input members')


def collect(directory, client):
    directory.mkdir(parents=True, exist_ok=False)
    plan_bytes = encoded(frozen_plan())
    (directory / 'plan.json').write_bytes(plan_bytes)
    reader = BoundedExpandedReader(client, directory)
    before = reader.read('status-before.json', API + 'status.json')
    reader.read('LICENSE.txt', RELEASE + 'LICENSE')
    reader.read('REQUIRED.ATTRIBUTION.txt', RELEASE + 'REQUIRED.ATTRIBUTION')
    targets, page = reader.page('target', 'target.json',
                                {'target_components__accession': 'Q12809', 'target_type': 'SINGLE PROTEIN'})
    if page['total_count'] != 1 or len(targets) != 1 or targets[0]['target_chembl_id'] != TARGET or targets[0]['organism'] != 'Homo sapiens':
        raise ValueError('Human KCNH2 target mapping was not verified')
    activities, available, pages = [], None, []
    for offset in range(0, MAX_RECORDS, PAGE_SIZE):
        rows, page = reader.page('activity', f'activity-{offset // PAGE_SIZE}.json',
                                 {'target_chembl_id': TARGET, 'order_by': 'activity_id'}, offset)
        if available is None:
            available = page['total_count']
        elif available != page['total_count']:
            raise ValueError('Target metadata count changed during retrieval')
        activities.extend(rows)
        pages.append({'kind': 'activity', 'params': reader.receipts[-1]['params']})
        if offset + PAGE_SIZE >= available:
            break
    joined = {}
    for resource in ('assay', 'document'):
        field = resource + '_chembl_id'
        identifiers = sorted({row[field] for row in activities if row[field] is not None})
        joined[resource] = []
        for start in range(0, len(identifiers), 40):
            rows, page = reader.page(resource, f'{resource}-{start // 40}.json',
                                     {field + '__in': ','.join(identifiers[start:start + 40]), 'order_by': field})
            if page['next'] is not None or page['total_count'] != len(rows):
                raise ValueError('Unexpected paginated source metadata join')
            joined[resource].extend(rows)
            pages.append({'kind': resource, 'params': reader.receipts[-1]['params']})
    after = reader.read('status-after.json', API + 'status.json')
    if before != after:
        raise ValueError('Pinned release status changed during retrieval')
    bundle = {'schema': SCHEMA, 'version': VERSION, 'target': targets[0],
              'activities': activities, 'assays': joined['assay'], 'documents': joined['document']}
    _validate_members(bundle, pages, available)
    raw_bundle = encoded(bundle)
    (directory / 'bundle.json').write_bytes(raw_bundle)
    receipt = {'schema': RECEIPT_SCHEMA, 'version': VERSION,
               'retrievedAtUtc': datetime.now(timezone.utc).isoformat(),
               'selection': frozen_plan()['selection'], 'recordLimit': MAX_RECORDS,
               'availableTargetActivityMetadataCount': available,
               'retrievedRecordCount': len(activities), 'wholeTargetSurvey': available == len(activities),
               'planSha256': sha(plan_bytes), 'bundleSha256': sha(raw_bundle),
               'extractorSha256': sha(Path(__file__).read_bytes()),
               'projectionReaderSha256': sha((ROOT / 'scripts/survey_chembl_labelblind.py').read_bytes()),
               'serverSideProjection': True, 'resultFieldsRequested': False, 'labelsRead': False,
               'outcomeQualifiersRead': False, 'qualityFlagsRead': False,
               'responseBytes': reader.total_bytes, 'responses': reader.receipts}
    (directory / 'receipt.json').write_bytes(encoded(receipt))
    return bundle, receipt


def load_snapshot(directory, expected_receipt_sha256):
    receipt_bytes = (directory / 'receipt.json').read_bytes()
    if (not isinstance(expected_receipt_sha256, str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_receipt_sha256)
            or sha(receipt_bytes) != expected_receipt_sha256):
        raise ValueError('A matching independently recorded private receipt SHA256 is required')
    receipt = strict_json(receipt_bytes)
    if receipt.get('schema') != RECEIPT_SCHEMA or receipt.get('version') != VERSION:
        raise ValueError('Unsupported expanded retrieval receipt')
    if (receipt.get('recordLimit') != MAX_RECORDS or receipt.get('serverSideProjection') is not True
            or any(receipt.get(key) is not False for key in ('resultFieldsRequested', 'labelsRead', 'outcomeQualifiersRead', 'qualityFlagsRead'))):
        raise ValueError('Snapshot does not satisfy the label-blind frozen acquisition contract')
    plan_bytes = (directory / 'plan.json').read_bytes()
    if sha(plan_bytes) != receipt.get('planSha256') or strict_json(plan_bytes) != frozen_plan():
        raise ValueError('Frozen acquisition plan mismatch')
    raw = (directory / 'bundle.json').read_bytes()
    if sha(raw) != receipt.get('bundleSha256'):
        raise ValueError('Private bundle SHA256 mismatch')
    responses = receipt.get('responses')
    if not isinstance(responses, list) or not 1 <= len(responses) <= MAX_REQUESTS:
        raise ValueError('Invalid raw response inventory')
    names, pages, statuses, members, total, available, offsets = set(), [], {}, {}, 0, None, []
    for item in responses:
        name = item['resource']
        if not isinstance(name, str) or name in names or not re.fullmatch(r'(status-before|status-after|target|activity-\d+|assay-\d+|document-\d+)\.json|LICENSE\.txt|REQUIRED\.ATTRIBUTION\.txt', name):
            raise ValueError('Invalid raw response name')
        names.add(name)
        response = (directory / name).read_bytes()
        if sha(response) != item['sha256'] or len(response) != item['bytes']:
            raise ValueError('Original metadata response SHA256 mismatch')
        total += len(response)
        if len(response) > MAX_RESPONSE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError('Offline snapshot exceeds its original byte budget')
        resource = next((key for key in FIELDS if name == key + '.json' or name.startswith(key + '-')), None)
        if resource:
            if item['url'] != API + resource + '.json':
                raise ValueError('Metadata source URL mismatch')
            validate_query(resource, item['params'])
            rows, page = validate_page(strict_json(response), resource, item['params'])
            if resource == 'activity':
                if name != f"activity-{item['params']['offset'] // PAGE_SIZE}.json":
                    raise ValueError('Activity response role does not match its offset')
                if available is None:
                    available = page['total_count']
                elif available != page['total_count']:
                    raise ValueError('Inconsistent target activity totals')
                offsets.append(item['params']['offset'])
            elif resource in ('assay', 'document') and (page['next'] is not None or page['total_count'] != len(rows)):
                raise ValueError('Paginated source join is forbidden')
            members.setdefault(resource, []).extend(rows)
            pages.append({'kind': resource, 'params': item['params']})
        elif name.startswith('status-'):
            status = strict_json(response)
            if item['url'] != API + 'status.json' or item['params'] or status.get('chembl_db_version') != VERSION or status.get('status') != 'UP':
                raise ValueError('Pinned release status mismatch')
            statuses[name] = status
        elif item['url'] != RELEASE + ('LICENSE' if name == 'LICENSE.txt' else 'REQUIRED.ATTRIBUTION') or item['params']:
            raise ValueError('License provenance mismatch')
    if (not {'status-before.json', 'status-after.json', 'target.json', 'activity-0.json', 'LICENSE.txt', 'REQUIRED.ATTRIBUTION.txt'} <= names
            or statuses['status-before.json'] != statuses['status-after.json']
            or len(members.get('target', [])) != 1 or total != receipt.get('responseBytes')
            or available != receipt.get('availableTargetActivityMetadataCount')):
        raise ValueError('Incomplete metadata snapshot provenance')
    expected_offsets = list(range(0, min(MAX_RECORDS, max(1, available)), PAGE_SIZE))
    if offsets != expected_offsets:
        raise ValueError('Activity pages do not cover the frozen ordered offset range')
    reconstructed = {'schema': SCHEMA, 'version': VERSION, 'target': members['target'][0],
                     'activities': members.get('activity', []), 'assays': members.get('assay', []),
                     'documents': members.get('document', [])}
    bundle = strict_json(raw)
    if (bundle != reconstructed or len(bundle['activities']) != receipt.get('retrievedRecordCount')
            or receipt.get('wholeTargetSurvey') != (available == len(bundle['activities']))
            or receipt.get('selection') != frozen_plan()['selection']):
        raise ValueError('Bundle does not match the original projected source responses')
    _validate_members(bundle, pages, available)
    return bundle, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-dir', type=Path, required=True)
    parser.add_argument('--fetch', action='store_true', help='Explicit metadata only; at most 2000 records and 5 MB')
    parser.add_argument('--receipt-sha256', help='Offline: independently recorded exact private receipt hash')
    parser.add_argument('--output', type=Path, required=True, help='New aggregate retrieval receipt only; no structure rows')
    args = parser.parse_args(argv)
    private = args.private_dir.resolve()
    if private.is_relative_to(ROOT) or any('onedrive' in part.casefold() for part in private.parts):
        parser.error('Private source metadata must stay outside the repository and OneDrive')
    if args.output.exists():
        parser.error('Refusing to overwrite existing evidence')
    try:
        if args.fetch:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=40.0) as client:
                bundle, receipt = collect(private, client)
        else:
            bundle, receipt = load_snapshot(private, args.receipt_sha256)
        public = {key: value for key, value in receipt.items() if key != 'responses'}
        public['receiptSha256'] = sha((private / 'receipt.json').read_bytes())
        public['responses'] = [{key: row[key] for key in ('resource', 'sha256', 'bytes', 'retrievedAtUtc')} for row in receipt['responses']]
        public['status'] = 'retrieved-metadata-only-not-qualified'
        public['assayMetadataCount'] = len(bundle['assays'])
        public['documentMetadataCount'] = len(bundle['documents'])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('xb') as handle:
            handle.write(encoded(public))
        print(json.dumps({'status': public['status'], 'records': len(bundle['activities']), 'labelsRead': False}))
        return 0
    except (ValueError, OSError, KeyError, TypeError, httpx.HTTPError):
        print('Expanded ChEMBL survey stopped; no qualification granted. Check fixed release, projections, budgets and private receipt evidence.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
