"""Use only labeled, provenance-tracked data; writes no simulated research result."""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.validation import evaluate_scores, scaffold_split


def main():
    parser = argparse.ArgumentParser(description="按骨架划分数据或评估真实的留出测试分数")
    parser.add_argument("operation", choices=("split", "evaluate"))
    parser.add_argument("csv", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--model-version")
    parser.add_argument("--score-kind", choices=("probability", "uncalibrated_score", "binary_label"), default="uncalibrated_score")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--split-manifest", type=Path)
    args = parser.parse_args()
    if args.csv.resolve() == args.output.resolve():
        parser.error("输出路径不能覆盖输入 CSV。")
    with args.csv.open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    if args.operation == "evaluate" and not args.model_version:
        parser.error("evaluate 必须提供 --model-version。")
    try:
        for row in rows:
            if row.get("label") not in ("0", "1"):
                raise ValueError("label 必须为 0 或 1，缺失标签不得猜测。")
            row["label"] = int(row["label"])
            if args.operation == "evaluate":
                row["score"] = float(row["score"])
        manifest = json.loads(args.split_manifest.read_text(encoding="utf-8")) if args.split_manifest else None
        if manifest and manifest.get("datasetVersion") != args.dataset_version:
            raise ValueError("划分清单数据版本与 --dataset-version 不一致。")
        result = scaffold_split(rows, args.seed) if args.operation == "split" else evaluate_scores(rows, args.score_kind, manifest)
    except (ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    result.update(datasetVersion=args.dataset_version, modelVersion=args.model_version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
