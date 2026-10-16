"""Audit private result-free EPA member/structure projections; never download labels.

Without custodian projections, write a truthful blocked aggregate from verified
official network evidence. Existing outputs are never overwritten. This script
cannot run an evaluation, infer labels, authorize research, or call an API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.epa_structure_audit import (Projection, audit_projections, load_projections, strict_json, unavailable_audit)
from backend.dataset_overlap_audit import build_historical_references


def verified_network_evidence(path):
    """Recheck cached metadata bytes; only aggregate safe receipt fields escape."""
    path = Path(path)
    receipt = strict_json(path.read_bytes())
    if (receipt.get('schema') != 'aidd.stage7b.epa-network-evidence.v1'
            or receipt.get('candidateLabelsRead') is not False
            or receipt.get('candidateResultFieldsRequested') is not False):
        raise ValueError('Not the reviewed label-blind official metadata receipt')
    rows = []
    seen = set()
    allowed = {'fileId', 'sourceUrl', 'retrievedAtUtc', 'httpStatus', 'bytes', 'sha256',
               'expectedProjection', 'authenticated'}
    for row in receipt['files']:
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError('Unknown network evidence fields')
        name = row['fileId']
        if not isinstance(name, str) or Path(name).name != name or name in ('.', '..') or name in seen:
            raise ValueError('Evidence path is not a basename')
        seen.add(name)
        Projection.official_https_only(row['sourceUrl'])
        Projection.utc_time(row['retrievedAtUtc'])
        if type(row['bytes']) is not int or not 0 <= row['bytes'] <= 5_000_000:
            raise ValueError('Invalid metadata response size')
        source = path.parent / name
        if source.is_symlink():
            raise ValueError('Evidence must be a regular file')
        raw = source.read_bytes()
        if len(raw) != row['bytes'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('Official metadata response SHA256 mismatch')
        rows.append(dict(row))
    if sum(row['bytes'] for row in rows) > 5_000_000:
        raise ValueError('Metadata response budget exceeded')
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--manifest', type=Path)
    source.add_argument('--network-receipt', type=Path)
    parser.add_argument('--expected-manifest-sha256')
    parser.add_argument('--repository', type=Path, default=ROOT)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('Refusing to overwrite historical evidence')
    try:
        if args.manifest:
            if not args.expected_manifest_sha256:
                raise ValueError('An out-of-band manifest SHA256 is required')
            manifest, members, structures = load_projections(args.manifest, args.expected_manifest_sha256)
            result = audit_projections(manifest, members, structures, build_historical_references(args.repository))
        else:
            if args.expected_manifest_sha256:
                raise ValueError('Manifest hash requires a manifest')
            result = unavailable_audit(verified_network_evidence(args.network_receipt))
        result['processingFilesSha256'] = {
            relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            for relative in ('backend/epa_structure_audit.py', 'scripts/prepare_epa_structure_audit.py',
                'backend/chemistry.py', 'backend/v3_data.py', 'backend/independent_validation_data.py',
                'backend/dataset_overlap_audit.py', 'scripts/survey_chembl_labelblind.py')}
        args.output.parent.mkdir(exist_ok=True, parents=True)
        with args.output.open('xb') as handle:
            handle.write((json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8'))
        print(json.dumps({'aggregateWritten': True, 'gateStatus': 'STAGE7C_BLOCKED', 'candidateLabelsRead': False}))
        return 0
    except (ValueError, OSError, TypeError, KeyError):
        print('EPA audit stopped: missing or invalid verified result-free input; no qualification granted.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
