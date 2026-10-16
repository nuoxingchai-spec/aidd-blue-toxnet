"""Bounded ChEMBL 37 metadata survey; no result values, evaluation or sealing.

Only the official API's server-side `only` projection is used. Source rows and
response bytes stay in an operator-owned private directory, outside the repo.
An existing snapshot can be verified/audited offline without another download.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

API = 'https://www.ebi.ac.uk/chembl/api/data/'
RELEASE = 'https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_37/'
VERSION = 'ChEMBL_37'
TARGET = 'CHEMBL240'
FIELDS = {
    'target': ('target_chembl_id', 'pref_name', 'organism', 'target_type'),
    'activity': ('activity_id', 'molecule_chembl_id', 'canonical_smiles', 'assay_chembl_id',
                 'document_chembl_id', 'src_id', 'record_id', 'standard_type', 'standard_units', 'type', 'units'),
    'assay': ('assay_chembl_id', 'assay_type', 'assay_organism', 'assay_tax_id',
              'assay_cell_type', 'assay_tissue', 'confidence_score', 'relationship_type',
              'document_chembl_id', 'src_id'),
    'document': ('document_chembl_id', 'doi', 'pubmed_id', 'title', 'journal', 'year', 'src_id'),
}
PLURALS = {'target': 'targets', 'activity': 'activities', 'assay': 'assays', 'document': 'documents'}
MAX_RECORDS = 200
MAX_RESPONSE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 5_000_000
MAX_REQUESTS = 30
FILTERS = {
    'target': {'target_components__accession', 'target_type'},
    'activity': {'target_chembl_id', 'order_by'},
    'assay': {'assay_chembl_id__in', 'order_by'},
    'document': {'document_chembl_id__in', 'order_by'},
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def validate_query(resource, params):
    """Metadata-only fields AND metadata-only selection/order are mandatory."""
    expected_keys = {'only', 'limit', 'offset'} | FILTERS[resource]
    if not isinstance(params, dict) or set(params) != expected_keys:
        raise ValueError('Unexpected metadata query')
    if (params['only'] != ','.join(FIELDS[resource]) or type(params['limit']) is not int
            or params['limit'] != MAX_RECORDS or type(params['offset']) is not int or params['offset'] != 0):
        raise ValueError('Invalid projection, page size or offset')
    if resource == 'target':
        if params['target_components__accession'] != 'Q12809' or params['target_type'] != 'SINGLE PROTEIN':
            raise ValueError('Unexpected target query')
    elif resource == 'activity':
        if params['target_chembl_id'] != TARGET or params['order_by'] != 'activity_id':
            raise ValueError('Outcome ordering and alternate target selection are forbidden')
    else:
        field = resource + '_chembl_id'
        text = params[field + '__in']
        if (params['order_by'] != field or not isinstance(text, str)
                or not re.fullmatch(r'CHEMBL[0-9]+(?:,CHEMBL[0-9]+){0,39}', text)
                or text.split(',') != sorted(set(text.split(',')))):
            raise ValueError('Invalid metadata join query')


def projected_page(payload, resource):
    """Fail closed if the remote server ignores projection; do not save it."""
    plural = PLURALS[resource]
    if not isinstance(payload, dict) or set(payload) != {plural, 'page_meta'}:
        raise ValueError('Unexpected projected response envelope')
    page = payload['page_meta']
    if not isinstance(page, dict) or set(page) != {'limit', 'offset', 'total_count', 'next', 'previous'}:
        raise ValueError('Unexpected pagination contract')
    if any(type(page[key]) is not int or page[key] < 0 for key in ('limit', 'offset', 'total_count')):
        raise ValueError('Invalid pagination numbers')
    rows = payload[plural]
    if not isinstance(rows, list) or len(rows) > MAX_RECORDS or page['total_count'] < len(rows):
        raise ValueError('Invalid or excessive projected row count')
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(FIELDS[resource]):
            # Never emit the unexpected values/keys or assert labels stayed blind.
            raise ValueError('Server projection violation; stop and review label exposure before reuse')
    return rows, page


class BoundedReader:
    def __init__(self, client, directory):
        self.client, self.directory = client, directory
        self.receipts = []
        self.total_bytes = 0
        self.requests = 0

    def read(self, name, url, params=None, resource=None):
        if url not in {API + item + '.json' for item in ('status', *FIELDS)} | {RELEASE + 'LICENSE', RELEASE + 'REQUIRED.ATTRIBUTION'}:
            raise ValueError('Source URL is outside the fixed official allowlist')
        actual_resource = next((item for item in FIELDS if url == API + item + '.json'), None)
        if actual_resource != resource or (resource is not None and (params or {}).get('only') != ','.join(FIELDS[resource])):
            raise ValueError('Server-side projection is mandatory')
        if resource is not None:
            validate_query(resource, params)
        elif params:
            raise ValueError('Unexpected status/license query')
        if self.requests >= MAX_REQUESTS:
            raise ValueError('Metadata request budget exhausted')
        self.requests += 1
        with self.client.stream('GET', url, params=params, follow_redirects=False) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError('Redirects and non-200 responses are not accepted')
            data = bytearray()
            for chunk in response.iter_bytes():
                self.total_bytes += len(chunk)
                if len(data) + len(chunk) > MAX_RESPONSE_BYTES or self.total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError('Small-metadata download budget exceeded')
                data.extend(chunk)
        raw = bytes(data)
        payload = strict_json(raw) if url.startswith(API) else None
        if resource:
            projected_page(payload, resource)
        if url == API + 'status.json':
            if payload.get('chembl_db_version') != VERSION or payload.get('status') != 'UP':
                raise ValueError('Live API is not the required ChEMBL 37 release; use a verified offline snapshot')
        (self.directory / name).write_bytes(raw)
        self.receipts.append({'resource': name, 'url': url, 'params': params or {},
                              'sha256': sha(raw), 'bytes': len(raw),
                              'retrievedAtUtc': datetime.now(timezone.utc).isoformat()})
        return payload

    def page(self, resource, name, filters):
        if resource not in FILTERS or set(filters) - FILTERS[resource]:
            raise ValueError('Unsupported metadata filter')
        payload = self.read(name, API + resource + '.json',
                            {'only': ','.join(FIELDS[resource]), 'limit': MAX_RECORDS,
                             'offset': 0, **filters}, resource)
        return projected_page(payload, resource)


def collect(directory, client):
    """Fetch the first 200 activity IDs, never select on activity outcomes."""
    directory.mkdir(parents=True, exist_ok=False)
    reader = BoundedReader(client, directory)
    before = reader.read('status-before.json', API + 'status.json')
    reader.read('LICENSE.txt', RELEASE + 'LICENSE')
    reader.read('REQUIRED.ATTRIBUTION.txt', RELEASE + 'REQUIRED.ATTRIBUTION')
    targets, page = reader.page('target', 'target.json',
                                {'target_components__accession': 'Q12809', 'target_type': 'SINGLE PROTEIN'})
    if page['total_count'] != 1 or len(targets) != 1 or targets[0]['target_chembl_id'] != TARGET or targets[0]['organism'] != 'Homo sapiens':
        raise ValueError('Human KCNH2/Q12809 mapping was not verified')
    activities, activity_page = reader.page('activity', 'activity.json',
                                            {'target_chembl_id': TARGET, 'order_by': 'activity_id'})
    ids = [row['activity_id'] for row in activities]
    if any(type(item) is not int for item in ids) or ids != sorted(set(ids)):
        raise ValueError('Activity metadata IDs are not unique and ordered')
    joined = {}
    for resource in ('assay', 'document'):
        field = resource + '_chembl_id'
        identifiers = sorted({row[field] for row in activities if row[field] is not None})
        if any(not isinstance(item, str) or not item.startswith('CHEMBL') or not item[6:].isdigit() for item in identifiers):
            raise ValueError('Invalid metadata join ID')
        joined[resource] = []
        for start in range(0, len(identifiers), 40):
            batch = identifiers[start:start + 40]
            rows, page = reader.page(resource, f'{resource}-{start // 40}.json',
                                     {field + '__in': ','.join(batch), 'order_by': field})
            if page['total_count'] != len(rows) or page['next'] is not None:
                raise ValueError('Metadata join unexpectedly paginated')
            if len(rows) != len(batch) or {row[field] for row in rows} != set(batch):
                raise ValueError('Missing or substituted assay/document mapping')
            joined[resource].extend(rows)
    after = reader.read('status-after.json', API + 'status.json')
    if before != after:
        raise ValueError('ChEMBL metadata changed during retrieval; snapshot is not accepted')
    bundle = {'schema': 'aidd.stage7.chembl-labelblind.v1', 'version': VERSION, 'target': targets[0],
              'activities': activities, 'assays': joined['assay'], 'documents': joined['document']}
    bundle_bytes = encoded(bundle)
    (directory / 'bundle.json').write_bytes(bundle_bytes)
    receipt = {'schema': 'aidd.stage7.retrieval.v1', 'version': VERSION,
               'retrievedAtUtc': datetime.now(timezone.utc).isoformat(),
               'selection': 'target CHEMBL240; ascending activity_id; first 200; no outcome filter',
               'recordLimit': MAX_RECORDS, 'availableTargetActivityMetadataCount': activity_page['total_count'],
               'retrievedRecordCount': len(activities), 'wholeTargetSurvey': activity_page['total_count'] == len(activities),
               'serverSideProjection': True, 'resultFieldsRequested': False,
               'labelsRead': False, 'bundleSha256': sha(bundle_bytes),
               'responseBytes': reader.total_bytes, 'responses': reader.receipts}
    (directory / 'receipt.json').write_bytes(encoded(receipt))
    return bundle, receipt


def load_snapshot(directory, expected_receipt_sha256):
    """Offline reproduction binds both exact bundle bytes and raw projections."""
    receipt_bytes = (directory / 'receipt.json').read_bytes()
    if (not isinstance(expected_receipt_sha256, str)
            or not re.fullmatch(r'[0-9a-f]{64}', expected_receipt_sha256)
            or sha(receipt_bytes) != expected_receipt_sha256):
        raise ValueError('Expected private receipt SHA256 is required and must match')
    receipt = strict_json(receipt_bytes)
    if receipt.get('schema') != 'aidd.stage7.retrieval.v1' or receipt.get('version') != VERSION:
        raise ValueError('Unsupported metadata receipt')
    if (receipt.get('serverSideProjection') is not True or receipt.get('resultFieldsRequested') is not False
            or receipt.get('labelsRead') is not False or receipt.get('recordLimit') != MAX_RECORDS):
        raise ValueError('Snapshot does not assert the label-blind acquisition policy')
    raw = (directory / 'bundle.json').read_bytes()
    if sha(raw) != receipt.get('bundleSha256'):
        raise ValueError('Private bundle SHA256 mismatch')
    responses = receipt['responses']
    if not isinstance(responses, list) or len(responses) > MAX_REQUESTS:
        raise ValueError('Invalid response count')
    pages, statuses, names, total_bytes = {}, {}, set(), 0
    for item in responses:
        name = item['resource']
        if not isinstance(name, str) or name in names or not re.fullmatch(r'(status-before|status-after|target|activity|assay-\d+|document-\d+)\.json|LICENSE\.txt|REQUIRED\.ATTRIBUTION\.txt', name):
            raise ValueError('Invalid response file role')
        names.add(name)
        raw_response = (directory / name).read_bytes()
        if sha(raw_response) != item['sha256'] or len(raw_response) != item['bytes']:
            raise ValueError('Projected response SHA256 mismatch')
        total_bytes += len(raw_response)
        if len(raw_response) > MAX_RESPONSE_BYTES or total_bytes > MAX_TOTAL_BYTES:
            raise ValueError('Snapshot exceeds the small-metadata budget')
        resource = next((key for key in FIELDS if name == key + '.json' or name.startswith(key + '-')), None)
        if resource:
            if item['url'] != API + resource + '.json':
                raise ValueError('Snapshot projection provenance mismatch')
            validate_query(resource, item['params'])
            rows, page = projected_page(strict_json(raw_response), resource)
            pages.setdefault(resource, []).extend(rows)
            if resource == 'activity' and page['total_count'] != receipt.get('availableTargetActivityMetadataCount'):
                raise ValueError('Snapshot target count mismatch')
        elif name.startswith('status-'):
            status = strict_json(raw_response)
            if item['url'] != API + 'status.json' or status.get('chembl_db_version') != VERSION or status.get('status') != 'UP':
                raise ValueError('Snapshot release mismatch')
            statuses[name] = status
        else:
            if item['url'] != RELEASE + ('LICENSE' if name == 'LICENSE.txt' else 'REQUIRED.ATTRIBUTION'):
                raise ValueError('Snapshot license provenance mismatch')
    if (not {'status-before.json', 'status-after.json', 'target.json', 'activity.json', 'LICENSE.txt', 'REQUIRED.ATTRIBUTION.txt'} <= names
            or statuses['status-before.json'] != statuses['status-after.json']
            or len(pages.get('target', [])) != 1 or total_bytes != receipt.get('responseBytes')):
        raise ValueError('Incomplete or inconsistent snapshot provenance')
    reconstructed = {'schema': 'aidd.stage7.chembl-labelblind.v1', 'version': VERSION,
                     'target': pages['target'][0], 'activities': pages.get('activity', []),
                     'assays': pages.get('assay', []), 'documents': pages.get('document', [])}
    bundle = strict_json(raw)
    if reconstructed != bundle or len(bundle['activities']) != receipt.get('retrievedRecordCount'):
        raise ValueError('Private bundle does not match the original projected responses')
    return bundle, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-dir', type=Path, required=True,
                        help='New private directory outside the repository/OneDrive; use same snapshot for offline runs')
    parser.add_argument('--fetch', action='store_true', help='Explicit bounded official metadata HTTP; no large downloads')
    parser.add_argument('--receipt-sha256', help='Offline: pre-recorded SHA256 of the private receipt, required')
    parser.add_argument('--output', type=Path, required=True, help='New public aggregate only')
    args = parser.parse_args(argv)
    private = args.private_dir.resolve()
    if private.is_relative_to(ROOT) or any('onedrive' in part.casefold() for part in private.parts):
        parser.error('Private metadata must stay outside the repository and OneDrive')
    if args.output.exists():
        parser.error('Refusing to overwrite existing audit evidence')
    try:
        if args.fetch:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=40.0) as client:
                bundle, receipt = collect(private, client)
        else:
            bundle, receipt = load_snapshot(private, args.receipt_sha256)
        from backend.dataset_overlap_audit import build_historical_references
        from backend.stage7_audit import audit_survey
        result = audit_survey(bundle, build_historical_references(ROOT))
        result['retrieval'] = {key: value for key, value in receipt.items() if key != 'responses'}
        result['retrieval']['receiptSha256'] = sha((private / 'receipt.json').read_bytes())
        # Public provenance keeps hashes, sizes, resource roles; no raw records or ID lists.
        result['retrieval']['responses'] = [{key: item[key] for key in ('resource', 'sha256', 'bytes', 'retrievedAtUtc')}
                                            for item in receipt['responses']]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('xb') as handle:
            handle.write(encoded(result))
        print(json.dumps({'status': result['status'], 'labelsRead': False, 'aggregateWritten': True}))
        return 0
    except (ValueError, OSError, KeyError, TypeError, httpx.HTTPError) as error:
        # Avoid raw body, credentials, or private paths from network/parser exceptions.
        print('Stage 7 survey stopped: ' + type(error).__name__ + '; no qualification granted. Check source/projection/dependency evidence locally.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
