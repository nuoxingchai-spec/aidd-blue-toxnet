"""Reproducible from-scratch molecular Transformer training on real labels."""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import torch
from scipy.optimize import minimize
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from torch.nn import functional as F
from rdkit import rdBase
from backend.toxnet import DEFAULT_ARCHITECTURE, build_model, collate_features, featurize_smiles, predict_logits
from backend.validation import ENDPOINTS, _ranking_metrics, evaluate_scores, scaffold_split


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def molecular_records(rows):
    molecules = {}
    for row in rows:
        smiles = row["canonicalSmiles"]
        if smiles not in molecules:
            molecules[smiles] = {"smiles": smiles, "labels": np.zeros(4, dtype=np.float32), "mask": np.zeros(4, dtype=np.float32)}
        record = molecules[smiles]
        index = ENDPOINTS.index(row["endpoint"])
        record["labels"][index] = row["label"]
        record["mask"][index] = 1
    return [molecules[key] for key in sorted(molecules)]


def batches(records, batch_size, shuffle, seed):
    indices = list(range(len(records)))
    rng = random.Random(seed)
    if shuffle:
        rng.shuffle(indices)
    packed = []
    for start in range(0, len(indices), batch_size * 20):
        bucket = sorted(indices[start:start + batch_size * 20], key=lambda i: records[i]["features"]["atoms"].shape[1])
        packed.extend(bucket[j:j + batch_size] for j in range(0, len(bucket), batch_size))
    if shuffle:
        rng.shuffle(packed)
    for group in packed:
        yield group


def predict_records(model, records, device, batch_size):
    logits = np.zeros((len(records), 4), dtype=np.float64)
    model.eval()
    with torch.inference_mode():
        for indices in batches(records, batch_size, False, 0):
            inputs = {key: value.to(device) for key, value in collate_features([records[i]["features"] for i in indices]).items()}
            logits[indices] = predict_logits(model, inputs).cpu().numpy()
    return logits


def validation_auc(records, logits):
    aucs = []
    for index, endpoint in enumerate(ENDPOINTS):
        rows = [{"label": int(record["labels"][index]), "score": float(logits[i, index])} for i, record in enumerate(records) if record["mask"][index]]
        auc, _ = _ranking_metrics(rows)
        if auc is not None:
            aucs.append(auc)
    return float(np.mean(aucs)) if aucs else None


def sigmoid(value):
    return 1 / (1 + np.exp(-np.clip(value, -50, 50)))


def calibrate(records, logits):
    temperatures, biases, statuses = {}, {}, {}
    for index, endpoint in enumerate(ENDPOINTS):
        selected = [i for i, record in enumerate(records) if record["mask"][index]]
        values = logits[selected, index]
        labels = np.array([records[i]["labels"][index] for i in selected], dtype=np.float64)
        temperatures[endpoint], biases[endpoint] = 1.0, 0.0
        if min(int(labels.sum()), int(len(labels) - labels.sum())) < 5:
            statuses[endpoint] = "not-fitted-insufficient-classes"
            continue
        def objective(parameters):
            adjusted = values / math.exp(parameters[0]) + parameters[1]
            # Fixed weak regularization; only validation labels are used.
            return float(np.mean(np.logaddexp(0, adjusted) - labels * adjusted) + 0.001 * np.sum(np.square(parameters)))
        fit = minimize(objective, np.zeros(2), method="L-BFGS-B", bounds=[(math.log(0.05), math.log(20)), (-10, 10)])
        if fit.success and np.isfinite(fit.x).all():
            temperatures[endpoint] = float(math.exp(fit.x[0]))
            biases[endpoint] = float(fit.x[1])
            statuses[endpoint] = "fitted"
        else:
            statuses[endpoint] = "not-fitted-optimizer-failure"
    return {"status": "measured-not-certified", "method": "validation-temperature-and-bias", "fitSplit": "validation", "temperatures": temperatures, "biases": biases, "fitStatus": statuses, "certified": False}


def long_predictions(records, logits, calibration):
    rows = []
    for i, record in enumerate(records):
        for j, endpoint in enumerate(ENDPOINTS):
            if record["mask"][j]:
                score = sigmoid(logits[i, j] / calibration["temperatures"][endpoint] + calibration["biases"][endpoint])
                rows.append({"smiles": record["smiles"], "endpoint": endpoint, "label": int(record["labels"][j]), "score": float(score), "split": "test"})
    return rows


def add_uncertainty(evaluation, rows, seed):
    rng = np.random.default_rng(seed)
    for endpoint in ENDPOINTS:
        subset = [row for row in rows if row["endpoint"] == endpoint]
        values = []
        y = np.array([row["label"] for row in subset])
        p = np.array([row["score"] for row in subset])
        for _ in range(500):
            indices = rng.integers(0, len(subset), len(subset))
            if len(np.unique(y[indices])) == 2:
                values.append(float(roc_auc_score(y[indices], p[indices])))
        if values:
            evaluation["endpoints"][endpoint]["aurocBootstrap95CI"] = np.quantile(values, [0.025, 0.975]).tolist()
    evaluation["uncertaintyMethod"] = "500 molecule-level bootstrap samples per endpoint; percentile CI; no correction for model selection or dataset bias"


def baseline_predictions(train, test, seed):
    predictions = []
    for index, endpoint in enumerate(ENDPOINTS):
        training = [record for record in train if record["mask"][index]]
        testing = [record for record in test if record["mask"][index]]
        classifier = LogisticRegression(C=1.0, max_iter=1000, random_state=seed)
        classifier.fit(np.concatenate([record["features"]["fingerprint"].numpy() for record in training]), [int(record["labels"][index]) for record in training])
        scores = classifier.predict_proba(np.concatenate([record["features"]["fingerprint"].numpy() for record in testing]))[:, 1]
        predictions.extend({"smiles": record["smiles"], "endpoint": endpoint, "label": int(record["labels"][index]), "score": float(score), "split": "test"} for record, score in zip(testing, scores))
    return predictions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "data/processed/toxicity.csv")
    parser.add_argument("--data-manifest", type=Path, default=ROOT / "data/processed/manifest.json")
    parser.add_argument("--output", type=Path, default=ROOT / "models/aidd-toxnet")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--patience", type=int, default=7)
    parser.add_argument("--batch-size", type=int, default=48)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    args = parser.parse_args()
    if args.epochs < 1 or args.batch_size < 1 or args.patience < 1 or not 1 <= args.threads <= 24:
        parser.error("Invalid training arguments")
    if (args.output / "manifest.json").exists():
        parser.error("A completed model already exists; use a new output directory to preserve it.")
    args.output.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(args.threads)
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    random.seed(args.seed)
    torch.use_deterministic_algorithms(True)
    device = "cuda" if args.device == "auto" and torch.cuda.is_available() else "cpu" if args.device == "auto" else args.device
    if device == "cuda" and not torch.cuda.is_available():
        parser.error("CUDA is unavailable in the installed PyTorch build")
    if device == "cuda":
        import os
        os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
    provenance = json.loads(args.data_manifest.read_text(encoding="utf-8"))
    with args.data.open(encoding="utf-8", newline="") as stream:
        rows = [{**row, "label": int(row["label"])} for row in csv.DictReader(stream)]
    if provenance["output"]["sha256"] != digest(args.data):
        raise ValueError("Processed label CSV does not match its source manifest")
    split = scaffold_split(rows, seed=args.seed)
    if split["warnings"]:
        raise ValueError("Scaffold split is not suitable: " + "; ".join(split["warnings"]))
    for name, endpoints in split["classCounts"].items():
        if any(min(counts["positive"], counts["negative"]) < 1 for counts in endpoints.values()):
            raise ValueError(f"{name} must contain both observed classes for all four endpoints")
    write_json(args.output / "split.json", split)
    records = {name: molecular_records(members) for name, members in split["splits"].items()}
    architecture = copy.deepcopy(DEFAULT_ARCHITECTURE)
    excluded = []
    for name, molecules in records.items():
        for record in molecules:
            record["features"] = featurize_smiles(record["smiles"], architecture)
    model = build_model(architecture).to(device)
    parameter_count = sum(parameter.numel() for parameter in model.parameters())
    train, validation, test = records["train"], records["validation"], records["test"]
    masks = np.stack([record["mask"] for record in train])
    labels = np.stack([record["labels"] for record in train])
    positives = (masks * labels).sum(0)
    counts = masks.sum(0)
    positive_weight = torch.tensor(np.clip(np.sqrt((counts - positives) / positives), 0.5, 4), device=device)
    task_weight = torch.tensor(len(train) / counts, device=device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=3e-4, weight_decay=0.01)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs, eta_min=3e-5)
    best_auc, best_epoch, stale = -1.0, 0, 0
    history, started = [], time.time()
    print(json.dumps({"device": device, "parameters": parameter_count, "labelRows": split["actualCounts"], "molecules": {name: len(items) for name, items in records.items()}, "classCounts": split["classCounts"]}), flush=True)
    for epoch in range(1, args.epochs + 1):
        model.train()
        losses = []
        for indices in batches(train, args.batch_size, True, args.seed + epoch):
            batch = [train[i] for i in indices]
            inputs = {key: value.to(device) for key, value in collate_features([record["features"] for record in batch]).items()}
            targets = torch.tensor(np.stack([record["labels"] for record in batch]), device=device)
            observed = torch.tensor(np.stack([record["mask"] for record in batch]), device=device)
            logits = predict_logits(model, inputs)
            raw_loss = F.binary_cross_entropy_with_logits(logits, targets, pos_weight=positive_weight, reduction="none")
            loss = (raw_loss * observed * task_weight).sum() / (len(batch) * 4)
            if not bool(torch.isfinite(loss)):
                raise RuntimeError("Training produced a nonfinite loss")
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            losses.append(float(loss.detach()))
        validation_logits = predict_records(model, validation, device, args.batch_size)
        auc = validation_auc(validation, validation_logits)
        entry = {"epoch": epoch, "trainLoss": float(np.mean(losses)), "validationMacroAUROC": auc, "elapsedSeconds": round(time.time() - started, 1)}
        history.append(entry)
        print(json.dumps(entry), flush=True)
        write_json(args.output / "training-progress.json", {"status": "training", "history": history})
        if auc > best_auc + 1e-5:
            best_auc, best_epoch, stale = auc, epoch, 0
            torch.save({key: value.detach().cpu() for key, value in model.state_dict().items()}, args.output / "weights.pt")
        else:
            stale += 1
        scheduler.step()
        if stale >= args.patience:
            break
    model.load_state_dict(torch.load(args.output / "weights.pt", weights_only=True, map_location=device))
    validation_logits = predict_records(model, validation, device, args.batch_size)
    calibration = calibrate(validation, validation_logits)
    # Test labels first enter scoring here, after architecture/checkpoint/calibrator freeze.
    test_logits = predict_records(model, test, device, args.batch_size)
    predictions = long_predictions(test, test_logits, calibration)
    evaluation = evaluate_scores(predictions, "probability", split)
    evaluation["trainingHistoryVerified"] = True
    evaluation["trainingHistoryEvidence"] = "This model was initialized randomly and optimized only on the hash-recorded training split; validation selected checkpoint and calibration; held-out test did not select any parameter."
    add_uncertainty(evaluation, predictions, args.seed)
    baseline = evaluate_scores(baseline_predictions(train, test, args.seed), "probability", split)
    evaluation["fingerprintLogisticRegressionBaseline"] = {"method": "Morgan radius2 2048-bit + LogisticRegression C=1; fit on train only; no hyperparameter search", "endpoints": baseline["endpoints"]}
    identity = {"temperatures": {endpoint: 1 for endpoint in ENDPOINTS}, "biases": {endpoint: 0 for endpoint in ENDPOINTS}}
    evaluation["beforeCalibration"] = evaluate_scores(long_predictions(test, test_logits, identity), "probability", split)["endpoints"]
    write_json(args.output / "evaluation.json", evaluation)
    write_json(args.output / "test-predictions.json", predictions)
    write_json(args.output / "training-references.json", [record["smiles"] for record in train])
    write_json(args.output / "training-history.json", history)
    weights_hash = digest(args.output / "weights.pt")
    model_version = "aidd-toxnet-v2-" + weights_hash[:12]
    manifest = {
        "schema": "aidd.toxnet.artifact.v1", "modelName": "蓝研 ToxNet", "modelVersion": model_version,
        "architecture": architecture, "parameterCount": parameter_count, "endpoints": list(ENDPOINTS),
        "datasetVersion": provenance["datasetVersion"], "datasetProvenance": provenance["sources"],
        "training": {"status": "completed", "initialization": "random-from-scratch", "optimizationSplit": "train", "selectionSplit": "validation", "testUsedForSelection": False, "seed": args.seed, "epochs": len(history), "bestEpoch": best_epoch, "bestValidationMacroAUROC": best_auc, "completedAt": datetime.now(timezone.utc).isoformat(), "durationSeconds": round(time.time() - started, 1), "device": device, "torchVersion": str(torch.__version__), "rdkitVersion": rdBase.rdkitVersion, "threads": args.threads, "batchSize": args.batch_size, "maximumEpochs": args.epochs, "patience": args.patience, "splits": split["actualCounts"], "uniqueMoleculeCounts": {name: len(items) for name, items in records.items()}, "classCounts": split["classCounts"], "splitMethod": split["method"], "targetFractions": split["requestedFractions"], "encodingExclusions": excluded, "optimizer": "AdamW lr3e-4 weightDecay0.01 cosine schedule; clipped gradient1; task-balanced masked BCE with sqrt class weight", "sourceHashes": {"modelCode": digest(ROOT / "backend/toxnet.py"), "trainingScript": digest(Path(__file__)), "labels": digest(args.data), "split": digest(args.output / "split.json")}},
        "calibration": calibration, "applicability": {"threshold": 0.5, "validated": False, "thresholdSource": "fixed exploratory similarity cutoff; not fitted as a validated applicability domain"},
        "artifacts": {key: {"file": filename, "sha256": digest(args.output / filename)} for key, filename in (("weights", "weights.pt"), ("trainingReferences", "training-references.json"), ("evaluation", "evaluation.json"))},
        "limitations": ["Single scaffold split; internal test only, no external prospective validation", "ClinTox has few positive labels; uncertain performance", "Calibration fitted on validation and measured on test, not certified", "Similarity is a heuristic, not validated applicability", "Source licenses require original-rights review before commercial redistribution", "No mechanistic model attribution is provided; rule explanations are separate", "Isotope mass is encoded, but six isotope-labelled ClinTox records do not establish radiotoxicity performance; no radiation-specific endpoint"],
    }
    write_json(args.output / "manifest.json", manifest)
    write_json(args.output / "training-progress.json", {"status": "completed", "modelVersion": model_version, "history": history})
    print(json.dumps({"status": "completed", "modelVersion": model_version, "parameters": parameter_count, "evaluation": evaluation["endpoints"]}), flush=True)


if __name__ == "__main__":
    main()
