"""Screen pinned public TDC candidates without modifying the deployed model."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.candidate_data import prepare_candidates


def main():
    parser = argparse.ArgumentParser(description="准备真实 TDC hERG_Karim/Tox21 候选数据并审计全部旧划分泄漏")
    parser.add_argument("--candidate-dir", type=Path, default=Path("data/candidates"))
    parser.add_argument("--split", type=Path, default=Path("models/aidd-toxnet/split.json"))
    parser.add_argument("--model-manifest", type=Path, default=Path("models/aidd-toxnet/manifest.json"))
    parser.add_argument("--metadata", type=Path, default=Path("data/catalog/tdc-metadata.py"))
    parser.add_argument("--offline", action="store_true", help="仅使用 SHA256 校验通过的已有官方数据")
    args = parser.parse_args()
    try:
        manifest = prepare_candidates(args.candidate_dir, args.split, args.model_manifest, args.metadata, args.offline)
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))
    print(json.dumps({"candidateVersion": manifest["candidateVersion"], "normalization": manifest["normalization"],
                      "overlap": manifest["overlap"], "artifacts": manifest["artifacts"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
