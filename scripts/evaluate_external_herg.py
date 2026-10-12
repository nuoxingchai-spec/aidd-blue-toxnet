"""Score a frozen model on an audited, structurally novel hERG source.

This retrospective source-shift evaluation never fits neural weights,
calibration, thresholds or hyperparameters on candidate labels.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import torch
from rdkit import Chem, DataStructs
from rdkit.Chem import rdFingerprintGenerator
from sklearn.linear_model import LogisticRegression
from backend.candidate_data import audit_overlap, structural_identity
from backend.local_model import LocalModelClient
from backend.toxnet import build_model, featurize_smiles
from backend.validation import evaluate_scores
from scripts.train_toxnet import predict_records, sigmoid


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-manifest", type=Path, default=ROOT / "data/candidates/processed/manifest.json")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/external-herg-evaluation.json")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Evaluation already recorded; preserve it or select a new output path")
    started = time.perf_counter()
    model_dir = ROOT / "models/aidd-toxnet"
    model_path = model_dir / "manifest.json"
    split_path = model_dir / "split.json"
    manifest = json.loads(model_path.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate_manifest.read_text(encoding="utf-8"))
    if candidate["reference"]["modelManifestSha256"] != digest(model_path) or candidate["reference"]["splitSha256"] != digest(split_path):
        raise ValueError("Candidate exclusion audit belongs to a different frozen model")
    pool_record = candidate["artifacts"]["externalHergPool"]
    pool_path = args.candidate_manifest.parent / pool_record["file"]
    if pool_path.resolve().parent != args.candidate_manifest.resolve().parent or digest(pool_path) != pool_record["sha256"]:
        raise ValueError("External pool path/checksum does not match its audit")
    with pool_path.open(encoding="utf-8", newline="") as stream:
        rows = [{**row, "label": int(row["label"])} for row in csv.DictReader(stream)]
    if len(rows) != pool_record["rows"] or not rows or any(row["endpoint"] != "herg" or row["label"] not in (0, 1) for row in rows):
        raise ValueError("External pool row/endpoint/label contract is invalid")
    for row in rows:
        identity = structural_identity(row["smiles"], manifest["architecture"]["maxAtoms"])
        if any(row[field] != identity[field] for field in identity):
            raise ValueError("External pool identity was modified after its audit")
    split = json.loads(split_path.read_text(encoding="utf-8"))
    verified_pool, excluded, overlap = audit_overlap(rows, split, manifest["architecture"]["maxAtoms"])
    if excluded or len(verified_pool) != len(rows):
        raise ValueError("External pool contains prior structures, parent equivalents, scaffolds or duplicates")
    client = LocalModelClient(model_dir=model_dir)
    info = client.get_model_info()
    if not info["configured"]:
        raise ValueError(info.get("message", "Frozen model cannot be loaded"))
    torch.set_num_threads(6)
    model = build_model(manifest["architecture"])
    model.load_state_dict(torch.load(model_dir / manifest["artifacts"]["weights"]["file"], weights_only=True, map_location="cpu"))
    records = [{"smiles": row["smiles"], "features": featurize_smiles(row["smiles"], manifest["architecture"])} for row in rows]
    logits = predict_records(model, records, "cpu", 48)[:, 0]
    calibration = manifest["calibration"]
    scores = sigmoid(logits / calibration["temperatures"]["herg"] + calibration["biases"]["herg"])
    predictions = [{"smiles": row["smiles"], "endpoint": "herg", "label": row["label"], "score": float(score), "split": "test"} for row, score in zip(rows, scores)]
    for row, score in zip(rows[:3], scores[:3]):
        structure_id = hashlib.sha256(row["smiles"].encode()).hexdigest()
        direct_score = client.predict(row["smiles"], structure_id)["endpoints"][0]["score"]
        if abs(direct_score - score) > 1e-6:
            raise ValueError("Batch scoring does not agree with deployed local inference")
    neural = evaluate_scores(predictions, "probability")["endpoints"]["herg"]
    # Recreate the pre-specified C=1 baseline on the original TRAIN only.
    original_train = [row for row in split["splits"]["train"] if row["endpoint"] == "herg"]
    generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048, includeChirality=True)
    def fingerprints(smiles):
        out = np.zeros((len(smiles), 2048), dtype=np.float32)
        for i, value in enumerate(smiles):
            DataStructs.ConvertToNumpyArray(generator.GetFingerprint(Chem.MolFromSmiles(value)), out[i])
        return out
    classifier = LogisticRegression(C=1.0, max_iter=1000, random_state=manifest["training"]["seed"])
    classifier.fit(fingerprints([row["smiles"] for row in original_train]), [row["label"] for row in original_train])
    baseline_scores = classifier.predict_proba(fingerprints([row["smiles"] for row in rows]))[:, 1]
    baseline = evaluate_scores([{**row, "score": float(value)} for row, value in zip(predictions, baseline_scores)], "probability")["endpoints"]["herg"]
    result = {"schema": "aidd.external-herg-evaluation.v1", "status": "completed", "modelVersion": info["version"],
              "weightsSha256": info["weightsSha256"], "candidateVersion": candidate["candidateVersion"],
              "poolSha256": pool_record["sha256"], "modelManifestSha256": digest(model_path), "splitSha256": digest(split_path),
              "heldOutStatus": "canonical-parent-full-and-parent-scaffold-separation-reverified-against-all-prior-endpoints",
              "evaluationType": "retrospective-external-source-under-label-and-source-shift", "prospective": False,
              "sourcePopulationIndependenceVerified": False, "neuralModel": neural, "fingerprintBaseline": baseline,
              "calibrationRefit": False, "thresholdSelected": False, "modelUpdated": False,
              "baselineFitSplit": "original-train-only", "durationSeconds": round(time.perf_counter() - started, 1),
              "limitations": candidate["limitations"] + ["External hERG_Karim labels use a <10 micromolar cutoff; original hERG training collection does not establish a uniform matching threshold.", "Metrics are descriptive, without prospective validation or a scaffold-cluster uncertainty interval."]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    prediction_path = args.output.with_name(args.output.stem + "-predictions.json")
    prediction_path.write_text(json.dumps(predictions, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
