"""Run real frozen-feature B0/B1/B2 v3 development experiments."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.v3_experiments import DEFAULT_SEEDS, MODELS, run_experiments


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/v3-development")
    parser.add_argument("--output", type=Path, required=True, help="New isolated output directory, e.g. artifacts/v3/baselines-20261013")
    parser.add_argument("--models", nargs="+", choices=MODELS, default=["B0", "B1", "B2"])
    parser.add_argument("--seeds", nargs="+", type=int, default=list(DEFAULT_SEEDS))
    parser.add_argument("--encoder-dir", type=Path, help="Locally audited pinned MoLFormer snapshot")
    parser.add_argument("--embedding-cache", type=Path)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="cpu")
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--patience", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--encoder-batch-size", type=int, default=8)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--fusion-branches", nargs="+", choices=("molformer", "morgan"), default=["molformer", "morgan"], help="F1 ablation; all B0/B1/B2 baselines remain required")
    parser.add_argument("--fine-tune-encoder", action="store_true", help="Reserved; rejected until an audited fine-tuning protocol exists")
    args = parser.parse_args()
    if args.fine_tune_encoder:
        parser.error("Full encoder fine-tuning is not implemented or authorized by this frozen-feature protocol; no training was started.")
    try:
        result = run_experiments(data_dir=args.data_dir, output=args.output, models=tuple(args.models), seeds=tuple(args.seeds),
                                 snapshot_dir=args.encoder_dir, embedding_cache=args.embedding_cache, device=args.device,
                                 epochs=args.epochs, patience=args.patience, batch_size=args.batch_size,
                                 encoder_batch_size=args.encoder_batch_size, threads=args.threads, fusion_branches=tuple(args.fusion_branches))
    except (ValueError, RuntimeError, FileNotFoundError, FileExistsError) as error:
        parser.error(str(error))
    print(json.dumps({key: result[key] for key in ("status", "datasetVersion", "splitSha256", "seeds", "repetitionStatus", "aggregate")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
