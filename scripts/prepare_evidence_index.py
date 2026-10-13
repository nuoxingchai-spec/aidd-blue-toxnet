"""Explicit offline construction of a local-only audited experimental index."""
import argparse
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.evidence_search import prepare_index

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=ROOT / 'data/evidence-index')
    p.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    p.add_argument('--candidate-dir', type=Path, default=ROOT / 'data/candidates')
    p.add_argument('--model-dir', type=Path, default=ROOT / 'models/aidd-toxnet')
    a = p.parse_args()
    try:
        result = prepare_index(a.output, a.data_dir, a.candidate_dir, a.model_dir)
    except (OSError, ValueError, KeyError) as e:
        p.exit(2, f'Evidence preparation refused: {e}\n')
    print(json.dumps({k: result[k] for k in ('schema', 'datasetVersion', 'uniqueStructures', 'rawLabelsRebuiltAndVerified')}, ensure_ascii=False))
