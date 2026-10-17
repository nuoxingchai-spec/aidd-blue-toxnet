"""Offline EPA no-key source audit: metadata only, no CTX, no activity readers.

Recheck previously acquired official evidence and fixed assay metadata. This
command cannot download large archives, claim tested membership, evaluate or
grant scientific eligibility. Existing evidence is never overwritten.
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

from backend.epa_no_api_fallback import audit_sources


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-receipt', type=Path, required=True)
    parser.add_argument('--expected-receipt-sha256', required=True)
    parser.add_argument('--metadata-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('Refusing to overwrite scientific evidence')
    try:
        result = audit_sources(args.evidence_receipt, args.expected_receipt_sha256, args.metadata_dir)
        result['processingFilesSha256'] = {
            relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            for relative in ('backend/epa_no_api_fallback.py', 'scripts/audit_epa_no_api_fallback.py',
                'scripts/audit_toxcast_metadata.py', 'scripts/survey_chembl_labelblind.py',
                'backend/herg_label_contract_review.py', 'backend/chemistry.py', 'backend/v3_data.py')}
        raw = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('xb') as handle:
            handle.write(raw)
        print(json.dumps({'metadataAuditWritten': True, 'endpointMetadataCount': len(result['endpoints']),
                          'actualTestedMembershipRows': None, 'gateStatus': 'STAGE7C_BLOCKED'}))
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print('No-key audit stopped: verify official metadata caches and pinned hashes; no qualification granted.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
