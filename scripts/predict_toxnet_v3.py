"""Explicit offline research inference; never installs or downloads a model."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.chemistry import canonicalize_smiles
from backend.versioned_local_model import load_versioned_local_model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--smiles", required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--allow-research", action="store_true", help="Explicitly allow development-validation v3 models; not certified screening")
    args = parser.parse_args()
    try:
        canonical = canonicalize_smiles(args.smiles)
        client = load_versioned_local_model(args.model, snapshot_dir=args.snapshot, device=args.device, allow_research=args.allow_research)
        result = client.predict(canonical, hashlib.sha256(canonical.encode()).hexdigest())
    except Exception as error:
        parser.exit(1, f"Research inference unavailable: {getattr(error, 'public_message', str(error))}\n")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
