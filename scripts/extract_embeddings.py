"""Extract actual frozen MoLFormer features from an audited v3 development file."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.pretrained_encoder import EncoderError, PretrainedEncoder, cache_embeddings, file_sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--data-dir", type=Path, help="Audited v3 data directory; cache can be reused by train_toxnet_v3.py")
    group.add_argument("--data", type=Path, help="Standalone JSONL canonicalSmiles/smiles input; this cache is not a v3 training-data artifact")
    parser.add_argument("--snapshot", type=Path, default=Path("models/pretrained/molformer"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/toxnet-v3/embeddings/molformer.npz"))
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    try:
        if args.data_dir:
            from backend.v3_data import load_v3_data
            manifest, records = load_v3_data(args.data_dir)
            source_hash = file_sha256(args.data_dir / "manifest.json")
        else:
            if not args.data.is_file():
                parser.exit(2, f"Error: real input data missing: {args.data}\n")
            records = [json.loads(line) for line in args.data.read_text(encoding="utf-8").splitlines() if line.strip()]
            source_hash = file_sha256(args.data)
        smiles = [record.get("canonicalSmiles", record.get("smiles")) for record in records]
        if not records or any(not isinstance(value, str) for value in smiles):
            parser.exit(2, "Error: real nonempty input rows with canonicalSmiles/smiles are required\n")
        encoder = PretrainedEncoder(args.snapshot, device=args.device)
        values = cache_embeddings(args.output, smiles, encoder, source_hash=source_hash, batch_size=args.batch_size)
        print(json.dumps({"status": "features-extracted", "rows": len(records), "dimension": values.shape[1],
                          "encoder": encoder.provenance, "cache": str(args.output.resolve())}, ensure_ascii=False))
    except (EncoderError, ValueError) as error:
        parser.exit(2, f"Error: {error}\n")


if __name__ == "__main__":
    main()
