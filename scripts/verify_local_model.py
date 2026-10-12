"""Verify the completed production artifact and same-origin local HTTP service.

No third-party API is called. This is an integration check with real weights,
separate from fixture-based unit tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.local_model import LocalModelClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:4173")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/local-model-verification.json")
    args = parser.parse_args()
    if args.base_url not in ("http://127.0.0.1:4173", "http://localhost:4173"):
        parser.error("This check only calls the local development service")

    def call(path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(args.base_url + path, data=data, headers={"Content-Type": "application/json"})
        with build_opener(ProxyHandler({})).open(request, timeout=60) as response:
            return json.load(response)

    info = call("/api/local-model/info")
    assert info["configured"] is True, info.get("message")
    manifest = json.loads((ROOT / "models/aidd-toxnet/manifest.json").read_text(encoding="utf-8"))
    for field, filename in (("modelCode", "backend/toxnet.py"), ("trainingScript", "scripts/train_toxnet.py")):
        assert hashlib.sha256((ROOT / filename).read_bytes()).hexdigest() == manifest["training"]["sourceHashes"][field]
    local = LocalModelClient()
    pairs = [("CCO", "OCC"), ("c1ccccc1", "C1=CC=CC=C1"),
             ("CC(=O)Nc1ccc(O)cc1", "CC(=O)NC1=CC=C(O)C=C1"),
             ("[Na+].[Cl-]", "[Cl-].[Na+]"), ("CC#N", "N#CC")]
    evidence = []
    for left, right in pairs:
        started = time.perf_counter()
        first = call("/api/analyze", {"smiles": left, "mode": "model", "provider": "local"})
        duration = time.perf_counter() - started
        second = call("/api/analyze", {"smiles": right, "mode": "model", "provider": "local"})
        assert first["structureId"] == second["structureId"]
        assert first["scoreKind"] == "probability"
        assert first["provenance"]["model"]["weightsSha256"] == info["weightsSha256"]
        direct = local.predict(first["canonicalSmiles"], first["structureId"])
        for a, b, c in zip(first["endpoints"], second["endpoints"], direct["endpoints"]):
            assert a["score"] == b["score"]
            assert abs(a["score"] - c["score"]) < 1e-6
            assert a["score"] == a["modelScore"]
            assert a["explanationSource"] == "none" and a["drivers"] == []
            assert a["ruleScore"] is not None
        evidence.append({"input": left, "equivalentInput": right, "canonicalSmiles": first["canonicalSmiles"],
                         "scores": {item["id"]: item["score"] for item in first["endpoints"]},
                         "httpDurationSeconds": round(duration, 4)})
    result = {"status": "passed", "modelVersion": info["version"], "parameterCount": info["parameterCount"],
              "weightsSha256": info["weightsSha256"], "equivalentRepresentationPairs": evidence,
              "note": "Measured local HTTP timings are smoke checks, not a throughput benchmark."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
