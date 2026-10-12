"""Offline verified 16-task development preparation; preserve v2 and four-task v3."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.multitask_data import prepare_multitask_data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--model-dir", type=Path, default=ROOT / "models/aidd-toxnet")
    parser.add_argument("--candidate-dir", type=Path, default=ROOT / "data/candidates")
    parser.add_argument("--output", type=Path, default=ROOT / "data/v3-multitask-audited")
    parser.add_argument("--previous-development-dir", type=Path, default=ROOT / "data/v3-development")
    parser.add_argument("--external-evidence", type=Path, default=ROOT / "artifacts/external-herg-evaluation.json")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--validation-fraction", type=float, default=0.2)
    parser.add_argument("--encoder-compatible", choices=("molformer", "none"), default="molformer")
    args = parser.parse_args()
    try:
        manifest = prepare_multitask_data(args.data_dir, args.model_dir, args.candidate_dir, args.output,
            args.external_evidence, previous_development_dir=args.previous_development_dir,
            seed=args.seed, validation_fraction=args.validation_fraction, encoder_compatible=args.encoder_compatible)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(2, f"Multitask data preparation refused: {error}\n")
    print(json.dumps({name: manifest[name] for name in ("datasetVersion", "status", "sampleCounts", "fourTaskMoleculeCounts", "classCounts", "warnings", "splitSha256")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
