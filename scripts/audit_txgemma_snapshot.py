"""Audit an operator-authorized fixed TxGemma snapshot entirely offline.

This command never reads HF tokens, contacts the network, or downloads weights.
SHA256 of LFS objects is authenticated by reconstructing pinned Git pointers.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.compare_tasks import TXGEMMA_MODEL_ID, TXGEMMA_REVISION, task_registry
from backend.compare_txgemma import (OFFICIAL_FILES, SNAPSHOT_SCHEMA, TERMS_URL,
                                    check_snapshot_identity, file_sha256, git_blob_oid,
                                    lfs_pointer_oid, verify_snapshot_receipt, _safe_file)


def audit_snapshot(snapshot_dir, output, *, license_accepted, revision=TXGEMMA_REVISION):
    if license_accepted is not True:
        raise ValueError("User must personally accept the HAI-DEF terms before downloading/using this model.")
    if revision != TXGEMMA_REVISION:
        raise ValueError("Only the reviewed full commit revision is supported; main is prohibited.")
    root, destination = Path(snapshot_dir).resolve(), Path(output).resolve()
    if not root.is_dir():
        raise ValueError("Local snapshot is missing; no substitute data or weights are generated.")
    if destination.is_relative_to(root):
        raise ValueError("Keep the audit receipt outside the immutable snapshot.")
    if destination.exists():
        raise ValueError("Output already exists; audit receipts must never be overwritten.")
    records = {}
    for name, (expected_size, oid, is_lfs) in OFFICIAL_FILES.items():
        path = _safe_file(root, name)
        size = path.stat().st_size
        if size != expected_size:
            raise ValueError(f"Size mismatch for {name}; incomplete or different upstream snapshot.")
        digest = file_sha256(path)
        observed_oid = lfs_pointer_oid(digest, size) if is_lfs else git_blob_oid(path.read_bytes())
        if observed_oid != oid:
            raise ValueError(f"Official fixed-revision Git {'LFS pointer' if is_lfs else 'blob'} mismatch for {name}.")
        records[name] = {"size": size, "sha256": digest, "officialBlobOid": oid,
                         "sourceVerification": "reconstructed-official-git-lfs-pointer" if is_lfs else "official-git-blob"}
    check_snapshot_identity(root)
    receipt = {"schema": SNAPSHOT_SCHEMA, "modelId": TXGEMMA_MODEL_ID, "revision": revision,
               "status": "audited", "formalAvailability": False, "researchOnly": True,
               "snapshotDir": str(root), "licenseAcceptedByOperator": True, "licenseUrl": TERMS_URL,
               "createdAt": datetime.now(timezone.utc).isoformat(), "files": records,
               "tasks": task_registry(), "runtimeCode": "built-in-transformers-gemma2; trust_remote_code=false",
               "capacityValidation": "not-measured", "trainingStatus": "upstream-predict-model; no-local-training",
               "sourceVerification": "pinned-official-git-blobs-and-reconstructed-lfs-pointers"}
    # Validate every file, identity and unexpected addition before publication.
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Use an exclusive temporary receipt, remove it only if validation fails.
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(receipt, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
    try:
        verify_snapshot_receipt(root, destination)
    except Exception:
        destination.unlink()
        raise
    return {"status": "audited", "revision": revision, "filesVerified": len(records),
            "receiptSha256": file_sha256(destination), "formalAvailability": False,
            "capacityValidation": "not-measured"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--revision", default=TXGEMMA_REVISION)
    parser.add_argument("--license-accepted", action="store_true",
                        help="Attest that YOU personally accepted the model terms; this script does not accept them.")
    args = parser.parse_args()
    try:
        print(json.dumps(audit_snapshot(args.snapshot, args.output, license_accepted=args.license_accepted,
                                        revision=args.revision), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "unavailable", "reason": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
