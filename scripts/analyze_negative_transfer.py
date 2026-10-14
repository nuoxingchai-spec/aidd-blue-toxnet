"""Export descriptive exposed-development A4/B16 differences, never retrain."""
from pathlib import Path
import sys
import argparse
import json

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.negative_transfer import analyze_development_experiment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment-dir', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--bootstrap-replicates', type=int, default=500)
    args = parser.parse_args()
    target = Path(args.output)
    if target.exists():
        raise FileExistsError('Use a new retrospective output; no historical overwrite')
    report = analyze_development_experiment(args.experiment_dir, bootstrap_replicates=args.bootstrap_replicates)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    print(json.dumps({'role': report['role'], 'seedRuns': len(report['runs']), 'independentTest': False}))


if __name__ == '__main__':
    main()
