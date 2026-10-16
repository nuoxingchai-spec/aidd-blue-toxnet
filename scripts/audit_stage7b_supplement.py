"""Offline expanded metadata audit; never fetch, train, predict or read results."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.dataset_overlap_audit import build_historical_references, file_sha256, object_sha256
from backend.stage7b_supplement_audit import audit_expanded
from scripts.survey_chembl_expanded_labelblind import load_snapshot
from scripts.survey_chembl_labelblind import encoded


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-dir', type=Path, required=True)
    parser.add_argument('--receipt-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('Refusing to overwrite an existing scientific receipt')
    try:
        sources = {relative: file_sha256(ROOT / relative) for relative in (
            'backend/stage7b_supplement_audit.py', 'backend/stage7_audit.py',
            'backend/dataset_overlap_audit.py', 'backend/v3_data.py',
            'scripts/audit_stage7b_supplement.py')}
        bundle, receipt = load_snapshot(args.private_dir, args.receipt_sha256)
        result = audit_expanded(bundle, build_historical_references(ROOT), root=ROOT)
        result['sourceFileSha256'] = sources
        result['retrievalBinding'] = {
            'receiptSha256': args.receipt_sha256, 'bundleFileSha256': receipt['bundleSha256'],
            'frozenAcquisitionPlanSha256': receipt['planSha256'],
            'availableTargetActivityMetadataCount': receipt['availableTargetActivityMetadataCount'],
            'retrievedRecordCount': receipt['retrievedRecordCount'], 'wholeTargetSurvey': receipt['wholeTargetSurvey'],
            'retrievedAtUtc': receipt['retrievedAtUtc'], 'responseBytes': receipt['responseBytes'],
            'extractionCodeSha256': receipt['extractorSha256'],
            'sharedProjectionReaderSha256': receipt['projectionReaderSha256']}
        plan_time = datetime.fromtimestamp((args.private_dir / 'plan.json').stat().st_mtime, timezone.utc)
        first_completed = min(datetime.fromisoformat(item['retrievedAtUtc']) for item in receipt['responses'])
        result['planChronology'] = {
            'writtenBeforeFirstRequestByExtractorControlFlow': True,
            'observedPlanMtimeUtc': plan_time.isoformat(),
            'firstCompletedResponseUtc': first_completed.isoformat(),
            'observedPlanTimeBeforeFirstCompletedResponse': plan_time <= first_completed,
            'evidenceLimitation': 'Local filesystem/time/control-flow evidence; no independent custodian or tamper-proof timestamp.'}
        result['auditSha256'] = object_sha256({key: value for key, value in result.items() if key != 'auditSha256'})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('xb') as handle:
            handle.write(encoded(result))
        print(json.dumps({'status': result['status'], 'labelsRead': False,
                          'records': result['baseFixedReferenceAudit']['inputRecordCount'],
                          'screenPassedStructures': result['combinedStructureScreen']['verifiedStructureAndDuplicatePasses']}))
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print('Expanded audit stopped: verify pinned metadata, historical sources and contracts; no qualification granted.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
