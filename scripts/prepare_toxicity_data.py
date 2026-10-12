"""Download checksum-pinned real TDC data and produce an auditable training CSV."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.training_data import prepare_data


def main():
    parser = argparse.ArgumentParser(description="下载真实 TDC 毒性数据，规范结构并隔离冲突标签")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--offline", action="store_true", help="仅使用 SHA256 校验通过的已有数据")
    arguments = parser.parse_args()
    try:
        result = prepare_data(arguments.data_dir, arguments.offline)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps({"datasetVersion": result["datasetVersion"], "counts": result["counts"], "output": result["output"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
