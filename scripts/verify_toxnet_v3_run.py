"""Recheck real v3 artifacts, validation membership and recorded metrics.

This verifier reads an existing experiment; it never fits a model, calibrates,
changes a split, accesses old held-out labels, or rewrites experiment records.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import torch
from backend.v3_data import load_v3_data
from backend.v3_experiments import ENDPOINTS, evaluate_probabilities, label_arrays, sha256_file
from backend.versioned_local_model import V3ArtifactClient
from backend.training_provenance import verify_training_sources


def verify_run(experiment: Path, data_dir: Path, snapshot: Path, *, training_source_ref=None) -> dict:
    torch.set_num_threads(6)
    summary = json.loads((experiment / "experiment.json").read_text(encoding="utf-8"))
    data, samples = load_v3_data(data_dir)
    if (summary.get("status") != "completed" or summary.get("datasetVersion") != data["datasetVersion"]
        or summary.get("splitSha256") != data["splitSha256"]
        or summary.get("dataManifestSha256") != sha256_file(data_dir / "manifest.json")):
        raise ValueError("Experiment is incomplete or belongs to a different data/split version")
    seeds = summary.get("seeds", [])
    if len(seeds) < 3 or len(set(seeds)) != len(seeds):
        raise ValueError("At least three recorded unique seeds are required")
    runs = summary.get("runs", [])
    models = {run["model"] for run in runs}
    if not {"B0", "B1", "B2"}.issubset(models) or {(run["model"], run["seed"]) for run in runs} != {(model, seed) for model in models for seed in seeds} or len(runs) != len(models) * len(seeds):
        raise ValueError("Incomplete or duplicated baseline/seed runs")
    validation = [sample for sample in samples if sample["split"] == "validation"]
    labels, mask = label_arrays(validation)
    expected = {(sample["smiles"], endpoint): label for sample in validation for endpoint, label in sample["labels"].items()}
    positions = {sample["smiles"]: i for i, sample in enumerate(validation)}
    verified = []
    for run in runs:
        directory = (experiment / run["directory"]).resolve()
        if not directory.is_relative_to(experiment.resolve()) or sha256_file(directory / "manifest.json") != run["manifestSha256"]:
            raise ValueError("Run path or manifest checksum is invalid")
        client = V3ArtifactClient(directory, snapshot_dir=snapshot, allow_research=True)
        info = client.get_model_info()
        if not info.get("configured"):
            raise ValueError(f"Real run cannot load: {run['directory']}: {info.get('errorCode')}")
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        training_sources = verify_training_sources(manifest['training']['sourceHashes'], ROOT, commit=training_source_ref)
        if info["dataset"]["splitSha256"] != data["splitSha256"] or info["dataset"]["datasetVersion"] != data["datasetVersion"]:
            raise ValueError("Run has a different development data identity")
        records = json.loads((directory / "validation-predictions.json").read_text(encoding="utf-8"))
        observed = {}
        # Mask-zero cells are storage placeholders and are never evaluated or
        # published as predictions; every observed score must come from records.
        probabilities = np.zeros_like(labels, dtype=np.float64)
        for row in records:
            key = row["smiles"], row["endpoint"]
            if key in observed or key not in expected or row["label"] != expected[key] or row.get("split") != "validation":
                raise ValueError("Predictions violate exact validation membership/labels")
            score = row["score"]
            if type(score) not in (float, int) or not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("Invalid recorded probability")
            observed[key] = score
            probabilities[positions[key[0]], ENDPOINTS.index(key[1])] = score
        if set(observed) != set(expected):
            raise ValueError("Missing observed validation predictions")
        measured = evaluate_probabilities(labels, mask, probabilities)
        for endpoint in ENDPOINTS:
            for field in ("count", "positives", "negatives", "auroc", "averagePrecision", "brier", "ece10Bins"):
                actual, recorded = measured["endpoints"][endpoint][field], run["evaluation"]["endpoints"][endpoint][field]
                if (actual is None) != (recorded is None) or actual is not None and abs(actual - recorded) > 1e-12:
                    raise ValueError("Recorded metric does not reproduce from actual validation predictions")
        probe = validation[0]["smiles"]
        prediction = client.predict(probe, hashlib.sha256(probe.encode()).hexdigest())
        by_endpoint = {row["id"]: row["score"] for row in prediction["endpoints"]}
        errors = [abs(by_endpoint[endpoint] - observed[(probe, endpoint)]) for endpoint in validation[0]["labels"]]
        if max(errors) > 3e-5:
            raise ValueError("Offline loaded head differs from cached batch validation predictions")
        verified.append({"model": run["model"], "seed": run["seed"], "modelVersion": info["version"],
                         "manifestSha256": run["manifestSha256"], "weightsSha256": info["weightsSha256"],
                         "parameterCount": info["parameterCount"], "recordedMetricRecomputation": "passed",
                         "trainingSourceVerification": training_sources,
                         "validationPredictions": len(records), "loadedInferenceMaximumAbsoluteDifference": max(errors)})
    return {"schema": "aidd.toxnet.run-verification.v3", "status": "verified", "experimentSha256": sha256_file(experiment / "experiment.json"),
            "datasetVersion": data["datasetVersion"], "splitSha256": data["splitSha256"], "seeds": seeds,
            "runs": verified, "independentTest": False, "oldHeldoutOrKarimLabelsUsed": False,
            "purpose": "Artifact/inference/membership verification; not a new performance experiment"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, default=Path("artifacts/v3/baselines-20261013"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/v3-development"))
    parser.add_argument("--snapshot", type=Path, default=Path("models/pretrained/molformer"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/v3/integration-verification.json"))
    parser.add_argument('--training-source-ref', help='Full immutable Git commit SHA for historical recorded training bytes; never executed')
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Verification output exists; preserve it and choose a new path")
    try:
        result = verify_run(args.experiment, args.data_dir, args.snapshot, training_source_ref=args.training_source_ref)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"Verification failed: {error}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "runs": len(result["runs"]), "experimentSha256": result["experimentSha256"]}))


if __name__ == "__main__":
    main()
