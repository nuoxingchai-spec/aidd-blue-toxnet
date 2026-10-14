"""Verify historic artifacts against their recorded SHA and immutable Git base.

This checks tracked historical files even without large local research weights.
Only hashes and model metadata are written; no experimental rows are exported.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COMPARE_BASE = '2d51de2173622f2afafa85c8bd95d875e1af3823'


def sha(value):
    return hashlib.sha256(value).hexdigest()


def verify():
    original = json.loads((ROOT / 'artifacts/compare/artifact-preservation.json').read_text(encoding='utf-8'))
    rows = []
    for item in original['files']:
        path = item['file']
        current = sha((ROOT / path).read_bytes())
        baseline = sha(subprocess.run(['git', 'show', original['baseCommit'] + ':' + path], cwd=ROOT,
            check=True, capture_output=True).stdout)
        if current != item['sha256'] or current != baseline:
            raise RuntimeError('Historical artifact changed: ' + path)
        rows.append({'file': path, 'sha256': current, 'unchangedFromHistoricalMain': True})
    old_receipts = []
    for name in ('artifact-preservation.json', 'runtime-verification.json', 'validation.json', 'browser-verification.json'):
        relative = 'artifacts/compare/' + name
        content = (ROOT / relative).read_bytes()
        current = sha(content)
        baseline_content = subprocess.run(['git', 'show', COMPARE_BASE + ':' + relative], cwd=ROOT,
            check=True, capture_output=True).stdout
        # Git may check out these JSON source receipts with CRLF on Windows.
        # Scientific artifacts above still require their exact recorded bytes.
        canonical = content.replace(b'\r\n', b'\n')
        if canonical != baseline_content.replace(b'\r\n', b'\n'):
            raise RuntimeError('Original Compare verification was overwritten: ' + relative)
        old_receipts.append({'file': relative, 'sha256': current, 'gitCanonicalSha256': sha(canonical),
            'unchangedGitContent': True, 'comparison': 'Git text content; checkout CRLF normalized'})
    return {'schema': 'aidd.compare.stability-preservation.v1', 'status': 'unchanged',
        'generatedAt': datetime.now(timezone.utc).isoformat(), 'historicalMain': original['baseCommit'],
        'compareBase': COMPARE_BASE, 'trackedHistoricalFiles': len(rows), 'files': rows,
        'originalCompareReceipts': old_receipts, 'newMetricsInvented': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    args = parser.parse_args()
    result = verify()
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'historicalFiles': result['trackedHistoricalFiles'],
        'originalCompareReceipts': len(result['originalCompareReceipts'])}))


if __name__ == '__main__':
    main()
