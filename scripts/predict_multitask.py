"""Explicit offline research inference; no formal deployment or aggregate risk."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.multitask_model import MultitaskModelClient
from backend.model_client import ModelAPIError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--encoder', type=Path)
    parser.add_argument('--smiles', required=True)
    parser.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    parser.add_argument('--tasks', choices=('tox21', 'all'), default='tox21')
    parser.add_argument('--allow-research', action='store_true')
    args = parser.parse_args()
    client = MultitaskModelClient(args.model, encoder_dir=args.encoder, device=args.device, allow_research=args.allow_research)
    try:
        result = client.predict_tox21(args.smiles) if args.tasks == 'tox21' else client.predict_all(args.smiles)
    except ModelAPIError as error:
        parser.exit(2, f'{error.code}: {error.public_message}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
