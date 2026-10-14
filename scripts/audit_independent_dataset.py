"""Local label-blind inventory/audit only. Never downloads, predicts or seals.

The output contains aggregate receipts only. Candidate metadata/row/protocol
paths are operator-side CLI arguments, never browser/API request fields.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.dataset_overlap_audit import (build_historical_references, file_sha256,
    historical_forbidden_index, structural_overlap, lineage_overlap)
from backend.independent_validation_data import audit_dataset
from backend.candidate_data import SOURCES
from backend.training_data import LICENSE_NOTE
from backend.v3_data import FDA_HASHES


def _load_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('Duplicate JSON contract key')
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def inventory(root):
    references = build_historical_references(root)
    forbidden, _ = historical_forbidden_index(references)
    result = {'schema': 'aidd.independent.local-inventory.v1', 'status': 'not-independently-qualified',
              'historicalReferences': references['receipt'], 'candidates': [], 'labelsRead': False,
              'rawRowsIncluded': False, 'structureStringsIncluded': False, 'newBlindTestCreated': False,
              'modelsExecuted': False, 'sourceRightsNote': LICENSE_NOTE,
              'positiveCount': None, 'negativeCount': None, 'missingLabelCount': None,
              'labelCountsStatus': 'not-read-labelblind-audit'}
    candidate_root = Path(root) / 'data/candidates'
    manifest_path = candidate_root / 'processed/manifest.json'
    candidate_manifest = _load_json(manifest_path) if manifest_path.is_file() else None
    for spec in SOURCES:
        raw = candidate_root / 'raw' / spec['fileName']
        base = {'sourceId': spec['id'], 'sourceSha256': spec['sha256'], 'status': 'not-independently-qualified',
                'sourceRights': 'unresolved', 'sourcePopulationIndependent': False,
                'exposure': 'previously-audited-source; tox21-used-in-development' if spec['id'] == 'tox21' else 'previously-exposed-evaluation-source',
                'labelsRead': False, 'sourceDefinition': spec['labelMeaning'], 'sourceUrl': spec['documentationUrl'],
                'retrievedAtUtc': None, 'positiveCount': None, 'negativeCount': None, 'missingLabelCount': None,
                'labelCountsStatus': 'not-read-labelblind-audit', 'commercialUseApproved': False,
                'redistributionApproved': False}
        if not raw.is_file() or file_sha256(raw) != spec['sha256'] or candidate_manifest is None:
            base.update(blockingReasons=['missing-or-changed-pinned-candidate-source'])
            result['candidates'].append(base)
            continue
        with raw.open(encoding='utf-8-sig', newline='') as handle:
            # Count records only, never inspect any label field or its value.
            source_rows = sum(1 for _ in csv.DictReader(handle, delimiter='\t' if spec['format'] == 'tsv' else ','))
        base['originalSampleCount'] = source_rows
        entry = candidate_manifest.get('artifacts', {}).get(spec['id'], {})
        name = entry.get('file')
        if not isinstance(name, str) or Path(name).name != name or '\\' in name:
            base.update(blockingReasons=['invalid-processed-candidate-file-contract'])
            result['candidates'].append(base)
            continue
        path = candidate_root / 'processed' / name
        if not path.is_file() or file_sha256(path) != entry.get('sha256'):
            base.update(blockingReasons=['missing-or-changed-processed-candidate'])
            result['candidates'].append(base)
            continue
        with path.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            if 'smiles' not in (reader.fieldnames or []): raise ValueError('Candidate has no structure column')
            # All label columns are deliberately ignored, including Karim.
            structures = sorted({row['smiles'] for row in reader})
        overlapping, invalid, overlap_roles = 0, 0, Counter()
        for canonical in structures:
            try:
                _, reasons = structural_overlap(canonical, forbidden)
            except (ValueError, RuntimeError, TypeError):
                invalid += 1
                continue
            if reasons:
                overlapping += 1
                overlap_roles.update({reason.split(':', 1)[0] for reason in reasons})
        base.update(uniqueStructures=len(structures), structurallyOverlapping=overlapping,
                    invalidOrUnsupportedStructures=invalid,
                    structurallyDisjoint=len(structures) - overlapping - invalid,
                    overlapCountsByHistoricalRole=dict(overlap_roles),
                    blockingReasons=['historical-source-lineage-and-exposure', 'source-rights-unresolved'],
                    processedSha256=entry['sha256'])
        result['candidates'].append(base)
    fda_path = candidate_root / 'fda-provenance.json'
    if fda_path.is_file():
        fda_files = {'DILIrank 2.0': 'dilirank_fda.xlsx', 'DILIst': 'dilist_fda.xlsx'}
        for record in _load_json(fda_path).get('datasets', []):
            name = fda_files.get(record.get('datasetName'))
            raw = candidate_root/'raw'/name if name else None
            source_verified = (raw is not None and raw.is_file() and record.get('sha256') == FDA_HASHES[name]
                               and file_sha256(raw) == FDA_HASHES[name])
            result['candidates'].append({'sourceId': record['datasetName'], 'status': 'not-independently-qualified',
                'sourceSha256': record['sha256'], 'rawDrugRows': record['rawDrugRows'],
                'sourceFileVerified': source_verified, 'sourceUrl': record.get('sourceUrl'),
                'retrievedAtUtc': record.get('retrievedAtUtc'),
                'positiveCount': None, 'negativeCount': None, 'missingLabelCount': None,
                'labelCountsStatus': 'not-read-labelblind-audit', 'commercialUseApproved': False,
                'redistributionApproved': False,
                'structureMappingStatus': record.get('structureMappingStatus'),
                'blockingReasons': ['no-verified-structure-mapping', 'possible-fda-dili-source-lineage-overlap', 'source-rights-unresolved']
                    + ([] if source_verified else ['missing-or-changed-pinned-fda-source']),
                'labelsRead': False, 'sourceRights': record.get('license', {}).get('status', 'unresolved')})
    else:
        result['candidates'].append({'sourceId': 'fda-dili-candidates', 'status': 'unavailable',
                                    'blockingReasons': ['missing-source-provenance'], 'labelsRead': False})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--metadata', type=Path)
    parser.add_argument('--rows', type=Path)
    parser.add_argument('--protocol', type=Path)
    args = parser.parse_args(argv)
    if args.output.exists(): parser.error('Refusing to overwrite prior audit evidence')
    supplied = [args.metadata is not None, args.rows is not None, args.protocol is not None]
    if any(supplied) and not all(supplied): parser.error('metadata, rows and protocol must be supplied together')
    try:
        if all(supplied):
            rows = [_load_json_line(line) for line in args.rows.read_text(encoding='utf-8').splitlines() if line.strip()]
            result = audit_dataset(rows, _load_json(args.metadata), build_historical_references(args.root),
                                   _load_json(args.protocol))['publicAudit']
        else:
            result = inventory(args.root)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write('\n')
        print(json.dumps({'status': result['status'], 'labelsRead': False, 'aggregateWritten': True}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print('Independent audit unavailable: ' + type(error).__name__ + '; inspect local input contracts/dependencies.', file=sys.stderr)
        return 2


def _load_json_line(line):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('Duplicate JSON row key')
            result[key] = value
        return result
    return json.loads(line, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


if __name__ == '__main__':
    raise SystemExit(main())
