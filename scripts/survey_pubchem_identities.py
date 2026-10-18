"""Bounded official all-SID identities only; no assay data, labels or evaluation.

Writes a plan before requests and validated identifier projections outside the
checkout. Never caches raw network bodies/errors. Descriptions, result metadata,
activity selectors, compound data and arbitrary endpoints are not supported.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.survey_chembl_labelblind import strict_json

AIDS = (588834, 1671200, 1511)
MAX_BYTES = 5 * 1024 * 1024
MAX_SIDS = 500_000
SCHEMA = 'aidd.stage7b.pubchem-all-sid.v1'


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def identity_url(aid):
    if type(aid) is not int or aid not in AIDS:
        raise ValueError('Only the three reviewed assay IDs are permitted')
    return f'https://pubchem.ncbi.nlm.nih.gov/rest/pug/assay/aid/{aid}/sids/JSON?sid_type=all'


def parse_identities(raw, aid):
    """Reject every unexpected key before any projection or output is persisted."""
    identity_url(aid)
    if len(raw) > MAX_BYTES:
        raise ValueError('Identity response exceeds the frozen byte limit')
    payload = strict_json(raw)
    if not isinstance(payload, dict) or set(payload) != {'InformationList'}:
        raise ValueError('Unexpected all-SID envelope')
    top = payload['InformationList']
    if not isinstance(top, dict) or set(top) != {'Information'}:
        raise ValueError('Unexpected all-SID information fields')
    entries = top['Information']
    if not isinstance(entries, list) or len(entries) != 1:
        raise ValueError('Expected exactly one reviewed AID')
    row = entries[0]
    if (not isinstance(row, dict) or set(row) != {'AID', 'SID'}
            or type(row['AID']) is not int or row['AID'] != aid
            or not isinstance(row['SID'], list) or len(row['SID']) > MAX_SIDS):
        raise ValueError('Unexpected all-SID row contract')
    ids = row['SID']
    if any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Invalid, duplicate or ambiguous SID identities')
    return {'schema': SCHEMA, 'aid': aid, 'selection': 'official-all-associated-SIDs',
            'sids': sorted(ids), 'rawTestedPopulationCompletenessProven': False,
            'qualificationGranted': False, 'canEnterStage7C': False}


def collect(directory, aids, client):
    directory = Path(directory).resolve()
    if directory.is_relative_to(ROOT) or 'onedrive' in str(directory).casefold():
        raise ValueError('Use a private directory outside the checkout and OneDrive')
    if not aids or len(set(aids)) != len(aids):
        raise ValueError('Distinct reviewed assay IDs are required')
    urls = [identity_url(aid) for aid in aids]
    directory.mkdir(parents=True, exist_ok=False)
    plan = {'schema': 'aidd.stage7b.pubchem-identity-plan.v1', 'aids': aids,
        'urls': urls, 'selection': 'sid_type=all; no activity selector',
        'maximumResponseBytes': MAX_BYTES, 'maximumSids': MAX_SIDS,
        'scriptSha256': sha(Path(__file__).read_bytes()), 'rawNetworkBodiesRetained': False,
        'resultDataEndpointsAllowed': False, 'canEnterStage7C': False}
    (directory / 'plan.json').write_bytes(encoded(plan))
    receipts = []
    for aid, url in zip(aids, urls):
        receipt = {'aid': aid, 'sourceUrl': url, 'httpStatus': None,
            'retrievedAtUtc': None, 'responseSha256': None, 'responseBytesRead': 0,
            'projectionSha256': None, 'associatedSidCount': None, 'status': 'unavailable',
            'rawBodyRetained': False, 'failureCategory': None}
        raw = bytearray()
        try:
            with client.stream('GET', url, follow_redirects=False) as response:
                receipt['httpStatus'] = response.status_code
                if response.status_code != 200:
                    receipt['failureCategory'] = 'http-status-not-200'
                    continue
                if 'application/json' not in response.headers.get('content-type', '').casefold():
                    receipt['failureCategory'] = 'not-json-no-body-read'
                    continue
                length = response.headers.get('content-length')
                if length is not None and (not length.isdigit() or int(length) > MAX_BYTES):
                    receipt['failureCategory'] = 'declared-response-size-exceeds-limit'
                    continue
                for chunk in response.iter_bytes(chunk_size=16384):
                    receipt['responseBytesRead'] += len(chunk)
                    if receipt['responseBytesRead'] > MAX_BYTES:
                        raise ValueError('Body budget exceeded')
                    raw.extend(chunk)
                if (length is not None and not response.headers.get('content-encoding')
                        and len(raw) != int(length)):
                    raise ValueError('Response length mismatch')
                receipt['responseSha256'] = sha(raw)
                projection = parse_identities(bytes(raw), aid)
                projected_bytes = encoded(projection)
                # No write precedes successful strict validation of the entire response.
                (directory / f'aid-{aid}-identities.json').write_bytes(projected_bytes)
                receipt.update(status='identity-projection-verified', failureCategory=None,
                    associatedSidCount=len(projection['sids']), projectionSha256=sha(projected_bytes))
        except (ValueError, httpx.HTTPError):
            receipt['failureCategory'] = 'invalid-or-unavailable-response-no-body-retained'
        finally:
            raw.clear()
            receipt['retrievedAtUtc'] = datetime.now(timezone.utc).isoformat()
            receipts.append(receipt)
    result = {'schema': 'aidd.stage7b.pubchem-identity-receipt.v1',
        'planSha256': sha(encoded(plan)), 'responses': receipts,
        'rawTestedPopulationCompletenessProven': False, 'formalAvailability': False,
        'qualificationGranted': False, 'canEnterStage7C': False, 'gateStatus': 'STAGE7C_BLOCKED'}
    (directory / 'receipt.json').write_bytes(encoded(result))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-dir', required=True, type=Path)
    parser.add_argument('--aid', type=int, choices=AIDS, action='append', required=True)
    args = parser.parse_args(argv)
    try:
        with httpx.Client(trust_env=False, timeout=30) as client:
            receipt = collect(args.private_dir, args.aid, client)
        print(json.dumps({'assays': [{'aid': r['aid'], 'status': r['status'],
            'associatedSidCount': r['associatedSidCount']} for r in receipt['responses']],
            'gateStatus': 'STAGE7C_BLOCKED'}))
        return 0 if all(r['status'] == 'identity-projection-verified' for r in receipt['responses']) else 2
    except (ValueError, OSError):
        print('Identity survey stopped: verify reviewed IDs, private directory and bounded official access.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
