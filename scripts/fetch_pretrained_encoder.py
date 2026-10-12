"""Explicit, pinned download/import. This script never imports remote Python."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.pretrained_encoder import (
    EncoderError, FILE_HASHES, MODEL_REVISION, MODEL_SOURCE, SNAPSHOT_SCHEMA,
    SOURCE_DIR, WEIGHTS_BYTES, WEIGHTS_SHA256, validate_snapshot, verify_file,
    verify_reviewed_sources,
)


def prepare_snapshot(output: Path, *, local_weight: Path | None = None, offline: bool = False) -> dict:
    verify_reviewed_sources()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    weight = output / "model.safetensors"
    if (output / "snapshot.json").exists():
        return validate_snapshot(output)
    # Audited small files are copied byte for byte, not re-fetched from main.
    for name in FILE_HASHES:
        destination = output / name
        if destination.exists():
            verify_file(destination, FILE_HASHES[name])
        else:
            shutil.copyfile(SOURCE_DIR / name, destination)
    if weight.exists():
        verify_file(weight, WEIGHTS_SHA256)
    elif local_weight is not None:
        verify_file(local_weight, WEIGHTS_SHA256)
        shutil.copyfile(local_weight, weight)
    elif offline:
        raise EncoderError("Offline mode requires model.safetensors or --local-weight. No substitute/random encoder is generated.")
    else:
        url = f"https://huggingface.co/{MODEL_SOURCE}/resolve/{MODEL_REVISION}/model.safetensors"
        temporary = output / "model.safetensors.partial"
        print(f"Downloading fixed revision {MODEL_REVISION}: {WEIGHTS_BYTES:,} bytes", flush=True)
        request = Request(url, headers={"User-Agent": "aidd-toxnet-v3-pinned-download/1"})
        try:
            with urlopen(request, timeout=60) as response, temporary.open("wb") as handle:
                total = 0
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > WEIGHTS_BYTES:
                        raise EncoderError("Download exceeds the audited weight file size")
                    handle.write(block)
            if temporary.stat().st_size != WEIGHTS_BYTES:
                raise EncoderError("Incomplete pretrained weight download")
            verify_file(temporary, WEIGHTS_SHA256)
            temporary.replace(weight)
        except (URLError, TimeoutError, OSError) as error:
            raise EncoderError(
                "Pretrained download failed. Retry explicitly; alternatively download the pinned model.safetensors URL "
                "in a browser on an allowed network, then use --offline --local-weight PATH. SHA256 verification remains mandatory. "
                f"Network error: {error}"
            ) from error
    verify_file(weight, WEIGHTS_SHA256)
    if weight.stat().st_size != WEIGHTS_BYTES:
        raise EncoderError("Weight file size does not match the reviewed upstream file")
    receipt = {
        "schema": SNAPSHOT_SCHEMA, "status": "verified", "source": MODEL_SOURCE,
        "revision": MODEL_REVISION, "license": "Apache-2.0",
        "files": {**FILE_HASHES, "model.safetensors": WEIGHTS_SHA256},
        "weightBytes": WEIGHTS_BYTES, "verifiedAt": datetime.now(timezone.utc).isoformat(),
        "remoteCodeExecution": False, "runtimeCode": "verified vendored project source only",
    }
    (output / "snapshot.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return validate_snapshot(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("models/pretrained/molformer"))
    parser.add_argument("--local-weight", type=Path, help="Import a manually downloaded safetensors file after official SHA256 validation")
    parser.add_argument("--offline", action="store_true", help="Never access the network")
    parser.add_argument("--revision", default=MODEL_REVISION, choices=[MODEL_REVISION])
    args = parser.parse_args()
    try:
        receipt = prepare_snapshot(args.output, local_weight=args.local_weight, offline=args.offline)
        print(json.dumps({"status": receipt["status"], "source": MODEL_SOURCE, "revision": MODEL_REVISION,
                          "weightsSha256": WEIGHTS_SHA256, "snapshotDir": str(args.output.resolve())}, ensure_ascii=False))
    except (EncoderError, ValueError) as error:
        parser.exit(2, f"Error: {error}\n")


if __name__ == "__main__":
    main()
