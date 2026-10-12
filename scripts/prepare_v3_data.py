"""Prepare verified isolated v3 development data, offline, without modifying v2."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.v3_data import prepare_v3_data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--model-dir", type=Path, default=ROOT / "models/aidd-toxnet")
    parser.add_argument("--candidate-dir", type=Path, default=ROOT / "data/candidates")
    parser.add_argument("--external-evidence", type=Path, default=ROOT / "artifacts/external-herg-evaluation.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/v3-development")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--validation-fraction", type=float, default=0.2)
    parser.add_argument("--encoder-compatible", choices=("none", "molformer"), default="molformer",
                        help="Label-blind tokenizer eligibility applied before the common B0/B1/B2 split")
    args = parser.parse_args()
    try:
        manifest = prepare_v3_data(args.data_dir, args.model_dir, args.candidate_dir, args.output,
                                   args.external_evidence, args.seed, args.validation_fraction, args.encoder_compatible)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(2, f"v3 data preparation refused: {error}\n")
    print(json.dumps({key: manifest[key] for key in ("datasetVersion", "status", "sampleCounts", "classCounts", "warnings", "splitSha256")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
