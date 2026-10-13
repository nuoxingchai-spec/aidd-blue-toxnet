"""Train isolated A4/B16/C_Morgan frozen-feature comparisons on real audited labels."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.multitask_experiments import DEFAULT_SEEDS, MODEL_IDS, run_multitask


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/v3-multitask-audited")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--encoder-dir", type=Path)
    parser.add_argument("--embedding-cache", type=Path)
    parser.add_argument("--models", nargs="+", choices=MODEL_IDS, default=["A4", "B16", "C_Morgan"])
    parser.add_argument("--seeds", nargs="+", type=int, default=list(DEFAULT_SEEDS))
    parser.add_argument("--device", choices=("cpu", "cuda", "auto"), default="cpu")
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--patience", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--encoder-batch-size", type=int, default=8)
    parser.add_argument("--minimum-selection-per-class", type=int, default=10)
    parser.add_argument("--tox21-weight", type=float, default=.25)
    parser.add_argument("--task-weights", type=Path, help="JSON mapping registered endpoint IDs to nonnegative fixed weights")
    args = parser.parse_args()
    try:
        overrides = json.loads(args.task_weights.read_text(encoding="utf-8")) if args.task_weights else None
        result = run_multitask(data_dir=args.data_dir, output=args.output, snapshot_dir=args.encoder_dir,
                               embedding_cache=args.embedding_cache, models=tuple(args.models), seeds=tuple(args.seeds), device=args.device,
                               threads=args.threads, epochs=args.epochs, patience=args.patience, batch_size=args.batch_size,
                               encoder_batch_size=args.encoder_batch_size, minimum_selection_per_class=args.minimum_selection_per_class,
                               tox21_weight=args.tox21_weight, task_weight_overrides=overrides)
    except (ValueError, RuntimeError, FileNotFoundError, FileExistsError) as error:
        parser.error(str(error))
    print(json.dumps({key: result[key] for key in ("status", "datasetVersion", "splitSha256", "seeds", "selectionProtocol", "aggregate", "pairedNegativeTransfer")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
