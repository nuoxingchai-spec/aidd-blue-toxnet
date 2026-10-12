"""Auditable four toxicity + twelve assay frozen-feature comparisons.

All tasks retain independent experimental meanings. This module never modifies
v2 or the original four-task v3 development implementation or artifacts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from sklearn.linear_model import LogisticRegression
from rdkit import rdBase

from backend.candidate_data import TOX21_ENDPOINTS
from backend.chemistry import CHEMISTRY_VERSION, canonicalize_smiles
from backend.validation import ENDPOINTS as FOUR_ENDPOINTS
from backend.v3_experiments import (DEFAULT_SEEDS, FINGERPRINT_SPEC, dependency_versions,
                                    evaluate_probabilities, morgan_features, resolve_device,
                                    sha256_file, sigmoid, standardize_train_only, write_json)

ALL_ENDPOINTS = (*FOUR_ENDPOINTS, *TOX21_ENDPOINTS)
TASK_CONTRACT_VERSION = "aidd.toxnet.four-toxicity-twelve-assay-tasks.v1"
ARTIFACT_SCHEMA = "aidd.toxnet.multitask.artifact.v1"
ARCHITECTURE_ID = "aidd.toxnet.multitask-frozen-head.v1"
EXPERIMENT_SCHEMA = "aidd.toxnet.multitask-experiment.v1"
MODEL_IDS = ("A4", "B16", "C_Morgan", "B16_noTox21")
ROOT = Path(__file__).resolve().parents[1]


class MultiTaskMLP(nn.Module):
    """Identical shared representation, followed by separate scalar task heads."""
    def __init__(self, input_dim: int, task_count: int, hidden_size: int = 128, dropout: float = 0.1):
        super().__init__()
        if type(input_dim) is not int or input_dim < 1 or task_count not in (4, 16) or hidden_size < 1 or not 0 <= dropout < 1:
            raise ValueError("Unsupported multitask head dimensions or dropout.")
        self.task_count = task_count
        self.shared = nn.Sequential(nn.Linear(input_dim, hidden_size), nn.GELU(), nn.Dropout(dropout))
        self.heads = nn.ModuleList(nn.Linear(hidden_size, 1) for _ in range(task_count))

    def forward(self, features):
        shared = self.shared(features)
        return torch.cat([head(shared) for head in self.heads], dim=1)


def labels_and_masks(samples: list[dict], tasks=ALL_ENDPOINTS):
    if not tasks or len(set(tasks)) != len(tasks) or set(tasks) - set(ALL_ENDPOINTS):
        raise ValueError("Unknown or duplicate multitask endpoint IDs.")
    labels, masks = np.zeros((len(samples), len(tasks)), dtype=np.float32), np.zeros((len(samples), len(tasks)), dtype=np.float32)
    for row, sample in enumerate(samples):
        observed = sample.get("labels")
        if not isinstance(observed, dict) or set(observed) - set(ALL_ENDPOINTS):
            raise ValueError("Experimental labels must belong to the registered sixteen-task contract.")
        for endpoint, label in observed.items():
            if type(label) is not int or label not in (0, 1):
                raise ValueError("Observed labels must be integer 0/1; missing tasks must be omitted.")
            if endpoint in tasks:
                task = tasks.index(endpoint)
                labels[row, task], masks[row, task] = label, 1
    return labels, masks


def validate_samples(samples):
    if not samples or any(sample.get("split") not in ("train", "validation") for sample in samples):
        raise ValueError("Only the new audited multitask train/validation split is allowed.")
    seen, owners = set(), {}
    for sample in samples:
        smiles = sample.get("smiles")
        if canonicalize_smiles(smiles) != smiles or smiles in seen:
            raise ValueError("Samples require unique complete canonical SMILES.")
        seen.add(smiles)
        group = sample.get("groupId")
        if not isinstance(group, str) or not group or owners.setdefault(group, sample["split"]) != sample["split"]:
            raise ValueError("Missing structural audit group or cross-split structural leakage.")
    labels, mask = labels_and_masks(samples)
    train = np.asarray([sample["split"] == "train" for sample in samples])
    four_members = (mask[:, :4].sum(1) > 0)
    if not train.any() or not (~train).any() or not (train & four_members).any():
        raise ValueError("Both train/validation and four-label train membership are required.")
    for task, endpoint in enumerate(FOUR_ENDPOINTS):
        values = labels[train, task][mask[train, task] == 1]
        if len(np.unique(values)) != 2:
            raise ValueError(f"train/{endpoint} lacks both observed classes; the common four-task comparison cannot complete.")
    return labels, mask, train, four_members


def task_weights(tasks, *, tox21_weight=0.25, overrides=None):
    if not math.isfinite(tox21_weight) or tox21_weight < 0:
        raise ValueError("Tox21 weight must be finite and nonnegative.")
    overrides = overrides or {}
    if not isinstance(overrides, dict) or set(overrides) - set(ALL_ENDPOINTS):
        raise ValueError("Task weight overrides must use registered endpoint IDs.")
    if any(type(value) not in (int, float) for value in overrides.values()):
        raise ValueError("Task weight values must be finite numeric values, not strings or booleans.")
    result = np.asarray([overrides.get(task, 1.0 if task in FOUR_ENDPOINTS else tox21_weight) for task in tasks], dtype=np.float32)
    if not np.isfinite(result).all() or (result < 0).any() or any(result[index] <= 0 for index, endpoint in enumerate(tasks) if endpoint in FOUR_ENDPOINTS):
        raise ValueError("Task weights must be finite/nonnegative, with positive weights for the four comparison endpoints.")
    return result


def training_task_status(labels, mask, active_train, tasks, weights):
    status, counts = {}, {}
    for task, endpoint in enumerate(tasks):
        observed = labels[active_train, task][mask[active_train, task] == 1]
        positives, negatives = int(observed.sum()), int(len(observed) - observed.sum())
        counts[endpoint] = {"positive": positives, "negative": negatives, "count": len(observed)}
        status[endpoint] = "disabled" if weights[task] == 0 else "trained" if positives > 0 and negatives > 0 else "not-trainable"
    return status, counts


def positive_weights_train_only(labels, mask, active_train):
    positives = (labels[active_train] * mask[active_train]).sum(0)
    negatives = mask[active_train].sum(0) - positives
    result = np.ones(labels.shape[1], dtype=np.float32)
    valid = (positives > 0) & (negatives > 0)
    result[valid] = np.clip(np.sqrt(negatives[valid] / positives[valid]), .5, 4)
    return result


def weighted_masked_bce(logits, labels, mask, *, weights, positive_weights):
    """Mean each observed task first, then weight tasks; skip zero-observation batches."""
    if logits.shape != labels.shape or labels.shape != mask.shape or weights.shape != (logits.shape[1],) or positive_weights.shape != weights.shape:
        raise ValueError("Multitask loss dimensions do not match.")
    if (not bool(((mask == 0) | (mask == 1)).all()) or not bool(torch.isfinite(weights).all()) or bool((weights < 0).any())
        or not bool(torch.isfinite(positive_weights).all()) or bool((positive_weights <= 0).any())):
        raise ValueError("Invalid label mask or task weights.")
    effective = mask * (weights > 0).to(mask.dtype)
    if not bool(effective.sum()):
        return None
    targets = torch.where(effective.bool(), labels, torch.zeros_like(labels))
    if not bool(((targets[effective.bool()] == 0) | (targets[effective.bool()] == 1)).all()):
        raise ValueError("Observed labels must be binary.")
    safe_logits = torch.where(effective.bool(), logits, torch.zeros_like(logits))
    raw = F.binary_cross_entropy_with_logits(safe_logits, targets, pos_weight=positive_weights, reduction="none")
    counts = effective.sum(0)
    task_mean = (raw * effective).sum(0) / counts.clamp_min(1)
    active_weights = weights * (counts > 0).to(weights.dtype)
    return (task_mean * active_weights).sum() / active_weights.sum()


def predict_head(head, features, *, device="cpu", batch_size=256):
    if isinstance(head, dict):
        return sigmoid(features.astype(np.float64) @ head["coefficients"].T + head["intercepts"])
    head.eval()
    result = []
    with torch.inference_mode():
        for start in range(0, len(features), batch_size):
            result.append(head(torch.from_numpy(features[start:start + batch_size]).to(device)).cpu().numpy())
    return sigmoid(np.concatenate(result)) if result else np.empty((0, head.task_count), dtype=np.float64)


def evaluate_tasks(labels, mask, probabilities, tasks, statuses):
    if labels.shape != mask.shape or probabilities.shape != labels.shape or labels.shape[1] != len(tasks):
        raise ValueError("Evaluation labels, masks, predictions and task contract do not match.")
    if not np.isin(mask, (0, 1)).all():
        raise ValueError("Evaluation observation masks must be binary.")
    output = {}
    for task, endpoint in enumerate(tasks):
        known = mask[:, task] == 1
        if not np.isin(labels[:, task][known], (0, 1)).all():
            raise ValueError("Experimental evaluation labels must be binary.")
        if statuses[endpoint] != "trained":
            positives = int(labels[:, task][known].sum())
            output[endpoint] = {"count": int(known.sum()), "positives": positives, "negatives": int(known.sum()) - positives,
                                "scoreCount": 0, "auroc": None, "averagePrecision": None, "brier": None, "ece10Bins": None,
                                "status": "not-trained", "rankingMetricReason": statuses[endpoint]}
            continue
        # Reuse the established four-task implementation for each independent
        # task; padded columns have no observations and affect no metric.
        y, m, p = np.zeros((len(labels), 4)), np.zeros((len(labels), 4)), np.zeros((len(labels), 4))
        y[:, 0], m[:, 0], p[:, 0] = labels[:, task], mask[:, task], probabilities[:, task]
        value = evaluate_probabilities(y, m, p)["endpoints"][FOUR_ENDPOINTS[0]]
        output[endpoint] = {**value, "scoreCount": value["count"]}
    return {"split": "validation", "evaluationType": "development-validation-used-for-model-selection", "independentTest": False,
            "calibrationStatus": "not-fitted", "probabilityInterpretation": "uncalibrated-label-prediction", "endpoints": output}


def eligible_selection_tasks(labels, mask, validation, *, minimum_per_class=10):
    if type(minimum_per_class) is not int or minimum_per_class < 1:
        raise ValueError("Selection support minimum must be a positive integer.")
    eligible, excluded = [], {}
    for task, endpoint in enumerate(FOUR_ENDPOINTS):
        values = labels[validation, task][mask[validation, task] == 1]
        positives, negatives = int(values.sum()), int(len(values) - values.sum())
        if min(positives, negatives) >= minimum_per_class:
            eligible.append(endpoint)
        else:
            excluded[endpoint] = {"positive": positives, "negative": negatives, "reason": f"minimum-{minimum_per_class}-per-class-not-met"}
    if not eligible:
        raise ValueError("No original four-task validation endpoint meets the preregistered support minimum; cannot select an MLP checkpoint.")
    return {"eligible": eligible, "excluded": excluded, "minimumPerClass": minimum_per_class,
            "policy": "original-four-endpoint macro AUROC then AP; Tox21 excluded from checkpoint selection"}


def selection_key(evaluation, selection):
    metrics = [evaluation["endpoints"][endpoint] for endpoint in selection["eligible"]]
    if not metrics or any(value["auroc"] is None or value["averagePrecision"] is None for value in metrics):
        raise ValueError("Eligible original-task checkpoint metrics are undefined.")
    return float(np.mean([value["auroc"] for value in metrics])), float(np.mean([value["averagePrecision"] for value in metrics]))


def fit_mlp(features, labels, mask, active_train, validation, tasks, statuses, weights, selection, seed,
            *, device="cpu", epochs=40, patience=8, batch_size=64, hidden_size=128):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    head = MultiTaskMLP(features.shape[1], len(tasks), hidden_size).to(device)
    effective_mask = mask.copy()
    for task, endpoint in enumerate(tasks):
        if statuses[endpoint] != "trained":
            effective_mask[:, task] = 0
            head.heads[task].requires_grad_(False)
    # Additional heads consume random draws at construction. Reset dropout RNG
    # so A4 and the zero-Tox21 control share the same four-task stochastic path.
    torch.manual_seed(seed)
    if device == "cuda":
        torch.cuda.manual_seed_all(seed)
    effective_rows = np.flatnonzero(active_train & (effective_mask.sum(1) > 0))
    if not len(effective_rows):
        raise ValueError("No observed train labels remain for optimization.")
    positive_weights = positive_weights_train_only(labels, effective_mask, active_train)
    positive_tensor, weight_tensor = torch.from_numpy(positive_weights).to(device), torch.from_numpy(weights).to(device)
    optimizer = torch.optim.AdamW((parameter for parameter in head.parameters() if parameter.requires_grad), lr=1e-3, weight_decay=1e-3)
    history, best_state, best, best_epoch, stale = [], None, (-math.inf, -math.inf), None, 0
    for epoch in range(1, epochs + 1):
        head.train()
        indices = np.random.default_rng(seed + epoch).permutation(effective_rows)
        losses, steps, skipped = [], 0, 0
        for start in range(0, len(indices), batch_size):
            rows = indices[start:start + batch_size]
            x, y, observed = (torch.from_numpy(value[rows]).to(device) for value in (features, labels, effective_mask))
            loss = weighted_masked_bce(head(x), y, observed, weights=weight_tensor, positive_weights=positive_tensor)
            if loss is None:
                skipped += 1
                continue
            if not bool(torch.isfinite(loss)):
                raise RuntimeError("Multitask training loss is nonfinite; run is incomplete.")
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(head.parameters(), 1)
            optimizer.step()
            losses.append(float(loss.detach().cpu()))
            steps += 1
        if not steps:
            raise ValueError("Epoch has no observed-label optimizer steps.")
        probabilities = predict_head(head, features[validation], device=device, batch_size=batch_size)
        evaluation = evaluate_tasks(labels[validation], mask[validation], probabilities, tasks, statuses)
        key = selection_key(evaluation, selection)
        history.append({"epoch": epoch, "trainLoss": float(np.mean(losses)), "optimizerSteps": steps, "zeroObservationBatchesSkipped": skipped,
                        "validationSelectionMacroAUROC": key[0], "validationSelectionMacroAP": key[1]})
        if key > best:
            best, best_epoch, stale = key, epoch, 0
            best_state = {name: value.detach().cpu().clone() for name, value in head.state_dict().items()}
        else:
            stale += 1
            if stale >= patience:
                break
    if best_state is None:
        raise ValueError("No real multitask checkpoint was selected.")
    head.load_state_dict(best_state, strict=True)
    return head, history, best_epoch, positive_weights


def fit_morgan(features, labels, mask, train, tasks, statuses, seed):
    # Untrainable coefficients are placeholders, never scored or served. Their
    # endpointStatus and null evaluation prohibit use as learned predictions.
    coefficients, intercepts = np.zeros((len(tasks), features.shape[1])), np.zeros(len(tasks))
    trace = []
    for task, endpoint in enumerate(tasks):
        selected = train & (mask[:, task] == 1)
        if statuses[endpoint] != "trained":
            trace.append({"endpoint": endpoint, "status": statuses[endpoint], "fitSplit": "train", "count": int(selected.sum())})
            continue
        classifier = LogisticRegression(C=1.0, solver="lbfgs", max_iter=2000, random_state=seed)
        classifier.fit(features[selected], labels[selected, task])
        coefficients[task], intercepts[task] = classifier.coef_[0], classifier.intercept_[0]
        trace.append({"endpoint": endpoint, "status": "trained", "fitSplit": "train", "count": int(selected.sum()),
                      "positive": int(labels[selected, task].sum()), "iterations": int(classifier.n_iter_[0]),
                      "maximumIterations": classifier.max_iter, "iterationLimitReached": bool(classifier.n_iter_[0] >= classifier.max_iter)})
    return {"coefficients": coefficients, "intercepts": intercepts}, [{"method": "per-endpoint-Morgan-LogisticRegression", "C": 1.0, "seed": seed, "endpoints": trace}]


def fresh_output(output):
    target = Path(output).resolve()
    protected = ((ROOT / "models/aidd-toxnet").resolve(), (ROOT / "artifacts/v3").resolve())
    if any(target == path or target.is_relative_to(path) or path.is_relative_to(target) for path in protected):
        raise ValueError("Previous formal/research artifacts are protected; use a new artifacts/v3-multitask/<experiment> directory.")
    if target.exists():
        raise FileExistsError("Multitask output already exists; use a new directory without overwriting history.")
    return target


def membership_hash(samples, selected, endpoints=FOUR_ENDPOINTS):
    records = [{"smiles": sample["smiles"], "split": sample["split"], "labels": {endpoint: sample["labels"][endpoint] for endpoint in endpoints if endpoint in sample["labels"]}}
               for sample, include in zip(samples, selected) if include]
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def paired_deltas(runs, seeds):
    lookup = {(run["model"], run["seed"]): run for run in runs}
    output = {}
    for endpoint in FOUR_ENDPOINTS:
        output[endpoint] = {}
        for metric in ("auroc", "averagePrecision", "brier", "ece10Bins"):
            pairs = []
            for seed in seeds:
                first, second = lookup.get(("A4", seed)), lookup.get(("B16", seed))
                if first is None or second is None:
                    continue
                if first["fourTaskValidationMembershipSha256"] != second["fourTaskValidationMembershipSha256"]:
                    raise ValueError("Negative-transfer comparison must use identical four-task validation label membership.")
                a, b = first["evaluation"]["endpoints"][endpoint][metric], second["evaluation"]["endpoints"][endpoint][metric]
                pairs.append({"seed": seed, "A4": a, "B16": b, "deltaB16MinusA4": b - a if a is not None and b is not None else None})
            values = [pair["deltaB16MinusA4"] for pair in pairs if pair["deltaB16MinusA4"] is not None]
            output[endpoint][metric] = {"pairs": pairs, "meanDelta": float(np.mean(values)) if values else None,
                                        "standardDeviation": float(np.std(values, ddof=1)) if len(values) > 1 else None,
                                        "definedSeeds": len(values), "adverseDirection": "negative" if metric in ("auroc", "averagePrecision") else "positive"}
    return {"definition": "paired seed B16 minus A4 on identical original-four validation labels; descriptive, no significance claim", "endpoints": output}


def run_multitask(*, data_dir, output, snapshot_dir=None, embedding_cache=None, models=("A4", "B16", "C_Morgan"), seeds=DEFAULT_SEEDS,
                  device="cpu", threads=6, epochs=40, patience=8, batch_size=64, encoder_batch_size=8,
                  minimum_selection_per_class=10, tox21_weight=.25, task_weight_overrides=None):
    if not models or len(set(models)) != len(models) or set(models) - set(MODEL_IDS):
        raise ValueError("Select unique registered A4/B16/C_Morgan/B16_noTox21 models.")
    if "B16_noTox21" in models and not {"A4", "B16", "C_Morgan"}.issubset(models):
        raise ValueError("The zero-Tox21 ablation must retain A4/B16/C_Morgan baselines.")
    if not seeds or len(set(seeds)) != len(seeds) or any(type(seed) is not int or seed < 0 for seed in seeds):
        raise ValueError("Seeds must be unique nonnegative integers.")
    if min(epochs, patience, batch_size, encoder_batch_size) < 1 or not 1 <= threads <= 24:
        raise ValueError("Invalid training limits.")
    output = fresh_output(output)
    device = resolve_device(device)
    torch.set_num_threads(threads)
    if device == "cuda":
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    torch.use_deterministic_algorithms(True)
    from backend.multitask_data import load_multitask_data
    manifest, samples = load_multitask_data(Path(data_dir))
    if manifest.get("taskContractVersion") != TASK_CONTRACT_VERSION or manifest.get("endpoints", list(ALL_ENDPOINTS)) != list(ALL_ENDPOINTS):
        raise ValueError("Multitask data contract does not match the registered sixteen endpoints.")
    if not isinstance(manifest.get("trainingPermission"), dict) or not manifest["trainingPermission"]:
        raise ValueError("The multitask data protocol must record explicit nonempty training permission.")
    contracts = manifest.get("taskContracts")
    if (not isinstance(contracts, dict) or set(contracts) != set(ALL_ENDPOINTS)
        or any(not isinstance(value, dict) or not isinstance(value.get("positiveClass"), str) or not value["positiveClass"].strip() for value in contracts.values())):
        raise ValueError("The sixteen-task data protocol must preserve every source's explicit positive-class contract.")
    labels, masks, train, four_members = validate_samples(samples)
    validation = ~train
    selection = eligible_selection_tasks(labels, masks, validation, minimum_per_class=minimum_selection_per_class)
    source_hash = sha256_file(Path(data_dir) / "manifest.json")
    smiles = [sample["smiles"] for sample in samples]
    print(__import__("json").dumps({"phase": "multitask-data-verified", "molecules": len(samples), "eligibleSelectionTasks": selection["eligible"]}), flush=True)
    feature_start = time.perf_counter()
    embeddings, encoder_info = None, None
    if any(model != "C_Morgan" for model in models):
        if snapshot_dir is None:
            raise FileNotFoundError("A4/B16 require a real verified frozen MoLFormer snapshot; no simulated features are provided.")
        from backend.pretrained_encoder import PretrainedEncoder, cache_embeddings
        encoder = PretrainedEncoder(Path(snapshot_dir), device=device)
        encoder_info = copy.deepcopy(encoder.provenance)
        embedding_cache = Path(embedding_cache) if embedding_cache else Path(data_dir) / "molformer-embeddings.npz"
        embeddings = cache_embeddings(embedding_cache, smiles, encoder, source_hash=source_hash, batch_size=encoder_batch_size)
        if embeddings.shape != (len(samples), encoder_info["dimension"]):
            raise ValueError("Multitask embedding cache dimensions/order do not match the common data.")
        del encoder
        if device == "cuda":
            torch.cuda.empty_cache()
    fingerprints = morgan_features(smiles) if "C_Morgan" in models else None
    feature_seconds = round(time.perf_counter() - feature_start, 3)
    output.mkdir(parents=True)
    four_validation_hash = membership_hash(samples, validation & four_members)
    four_train_hash = membership_hash(samples, train & four_members)
    started = time.perf_counter()
    experiment = {"schema": EXPERIMENT_SCHEMA, "status": "running", "createdAt": datetime.now(timezone.utc).isoformat(),
                  "datasetVersion": manifest["datasetVersion"], "dataManifestSha256": source_hash, "splitSha256": manifest["splitSha256"],
                  "taskContractVersion": TASK_CONTRACT_VERSION, "tasks": list(ALL_ENDPOINTS), "models": list(models), "seeds": list(seeds),
                  "selectionProtocol": selection, "device": device, "dependencies": dependency_versions(), "featureExtractionSeconds": feature_seconds,
                  "fourTaskTrainMembershipSha256": four_train_hash, "fourTaskValidationMembershipSha256": four_validation_hash,
                  "legacyHeldoutAndKarimUsedForSelection": False, "priorV3DevelopmentReuseAllowed": True,
                  "developmentExposureDefinition": "Earlier v3 development train/validation may be reused; protected v2 heldout and exposed Karim labels remain excluded.",
                  "newBlindTestClaimed": False, "runs": []}
    write_json(output / "experiment.json", experiment)
    try:
        for model_id in models:
            tasks = FOUR_ENDPOINTS if model_id == "A4" else ALL_ENDPOINTS
            y, observed = labels[:, :len(tasks)], masks[:, :len(tasks)]
            weights = task_weights(tasks, tox21_weight=tox21_weight, overrides=task_weight_overrides)
            if model_id == "B16_noTox21":
                weights[4:] = 0
            if model_id == "C_Morgan":
                weights[:] = 1  # Independent LR baseline has no task-weighted objective.
            # Explicit zero-Tox21 weights are the same fair ablation regardless
            # of the selected name: no auxiliary-only rows enter either scaler
            # fitting or optimizer batches.
            four_only = model_id == "A4" or (model_id != "C_Morgan" and not bool(weights[4:].sum()))
            active_train = train & four_members if four_only else train
            statuses, train_counts = training_task_status(y, observed, active_train, tasks, weights)
            raw = fingerprints if model_id == "C_Morgan" else embeddings
            features, transform = standardize_train_only(raw, active_train, model_id != "C_Morgan")
            for seed in seeds:
                run_dir = output / f"{model_id}-seed-{seed}"
                run_dir.mkdir()
                write_json(run_dir / "run-status.json", {"status": "training", "model": model_id, "seed": seed})
                kind = "logistic-regression" if model_id == "C_Morgan" else "shared-mlp"
                if kind == "logistic-regression":
                    head, history = fit_morgan(features, y, observed, active_train, tasks, statuses, seed)
                    best_epoch, positive_weights = None, None
                    np.savez_compressed(run_dir / "weights.npz", **head)
                    weight_file = "weights.npz"
                    parameter_count = int(sum(value.size for value in head.values()))
                    trainable_count = sum(features.shape[1] + 1 for endpoint in tasks if statuses[endpoint] == "trained")
                else:
                    head, history, best_epoch, positive_weights = fit_mlp(features, y, observed, active_train, validation, tasks, statuses, weights, selection, seed,
                                                                        device=device, epochs=epochs, patience=patience, batch_size=batch_size)
                    torch.save({name: value.detach().cpu() for name, value in head.state_dict().items()}, run_dir / "weights.pt")
                    weight_file = "weights.pt"
                    parameter_count = sum(parameter.numel() for parameter in head.parameters())
                    trainable_count = sum(parameter.numel() for parameter in head.parameters() if parameter.requires_grad)
                probabilities = predict_head(head, features[validation], device=device, batch_size=batch_size)
                evaluation = evaluate_tasks(y[validation], observed[validation], probabilities, tasks, statuses)
                evaluation.update({"schema": "aidd.toxnet.multitask-evaluation.v1", "model": model_id, "seed": seed, "datasetVersion": manifest["datasetVersion"],
                                   "splitSha256": manifest["splitSha256"], "fourTaskValidationMembershipSha256": four_validation_hash, "selectionProtocol": selection})
                np.savez_compressed(run_dir / "preprocessing.npz", **transform)
                write_json(run_dir / "evaluation.json", evaluation)
                write_json(run_dir / "training-history.json", history)
                predictions = []
                for row, values in zip(np.flatnonzero(validation), probabilities):
                    for task, endpoint in enumerate(tasks):
                        if observed[row, task]:
                            predictions.append({"smiles": smiles[row], "endpoint": endpoint, "label": int(y[row, task]), "split": "validation",
                                                "score": float(values[task]) if statuses[endpoint] == "trained" else None, "predictionStatus": statuses[endpoint]})
                write_json(run_dir / "validation-predictions.json", predictions)
                write_json(run_dir / "training-references.json", [smiles[row] for row in np.flatnonzero(active_train)])
                artifact_files = (("weights", weight_file), ("preprocessing", "preprocessing.npz"), ("evaluation", "evaluation.json"),
                                  ("trainingHistory", "training-history.json"), ("validationPredictions", "validation-predictions.json"), ("trainingReferences", "training-references.json"))
                artifacts = {key: {"file": name, "sha256": sha256_file(run_dir / name)} for key, name in artifact_files}
                completion = "trained-completed" if all(status == "trained" for status in statuses.values()) else "trained-ablation" if all(status in ("trained", "disabled") for status in statuses.values()) else "trained-partial"
                sources = (Path(__file__).resolve(), ROOT / "scripts/train_multitask.py", ROOT / "backend/multitask_data.py", ROOT / "backend/pretrained_encoder.py",
                           ROOT / "backend/v3_experiments.py", ROOT / "backend/v3_data.py", ROOT / "backend/chemistry.py", ROOT / "backend/candidate_data.py")
                model_manifest = {"schema": ARTIFACT_SCHEMA, "status": completion, "modelName": f"蓝研 ToxNet 多任务 {model_id}",
                                  "modelVersion": f"aidd-toxnet-multitask-{model_id.lower()}-{seed}-{artifacts['weights']['sha256'][:12]}",
                                  "tasks": list(tasks), "taskContractVersion": TASK_CONTRACT_VERSION,
                                  "taskContracts": {task: copy.deepcopy(contracts[task]) for task in tasks}, "formalAvailability": False,
                                  "architecture": {"id": ARCHITECTURE_ID, "headType": kind, "inputDim": features.shape[1], "outputDim": len(tasks), "taskOrder": list(tasks),
                                                   "hiddenSize": 128 if kind == "shared-mlp" else None, "dropout": .1 if kind == "shared-mlp" else 0,
                                                   "featureBranches": ["morgan"] if kind == "logistic-regression" else ["molformer"],
                                                   "fingerprint": copy.deepcopy(FINGERPRINT_SPEC) if kind == "logistic-regression" else None,
                                                   "logitsSemantics": "independent binary endpoint logits; Tox21 assay activity is not clinical toxicity",
                                                   "perHeadParameterCount": {task: 129 if kind == "shared-mlp" else features.shape[1] + 1 for task in tasks}},
                                  "parameterCount": parameter_count, "trainableParameterCount": trainable_count,
                                  "frozenEncoderParameterCount": 0 if kind == "logistic-regression" else encoder_info["frozenParameterCount"],
                                  "encoder": None if kind == "logistic-regression" else copy.deepcopy(encoder_info),
                                  "dataset": {"datasetVersion": manifest["datasetVersion"], "splitSha256": manifest["splitSha256"], "manifestSha256": source_hash,
                                              "trainingPermission": copy.deepcopy(manifest["trainingPermission"]), "sourceHashes": copy.deepcopy(manifest.get("sourceFiles", manifest.get("sources", {})))},
                                  "chemistryVersion": CHEMISTRY_VERSION, "rdkitVersion": rdBase.rdkitVersion,
                                  "training": {"status": "completed" if completion == "trained-completed" else "completed-ablation" if completion == "trained-ablation" else "partial",
                                               "optimizationSplit": "train", "selectionSplit": "validation", "testUsedForSelection": False,
                                               "legacyHeldoutAndKarimUsedForSelection": False, "protectedReferenceLabelsUsedForSelection": False,
                                               "priorV3DevelopmentReuseAllowed": True, "featureTransformFitSplit": "train", "encoderFrozen": True,
                                               "featureTransformFitRowPolicy": "original-four-label-train-only" if four_only else "all-multitask-train",
                                               "optimizerRowPolicy": "original-four-label-train-only" if four_only else "observed-enabled-label-train-only",
                                               "splits": {"train": int(active_train.sum()), "validation": int(validation.sum())}, "endpointStatus": statuses,
                                               "classCounts": train_counts, "taskWeights": {endpoint: float(weights[index]) for index, endpoint in enumerate(tasks)},
                                               "positiveWeights": {endpoint: float(positive_weights[index]) for index, endpoint in enumerate(tasks)} if positive_weights is not None else None,
                                               "positiveWeightPolicy": "train-only sqrt(negative/positive) clipped to [0.5,4]; tasks without both classes disabled from loss" if kind == "shared-mlp" else "not-used-independent-logistic-regression",
                                               "loss": "observed-task-mean weighted BCE; zero-observation batches skip optimizer" if kind == "shared-mlp" else "independent-task-logistic-regression",
                                               "seed": seed, "bestEpoch": best_epoch, "epochsExecuted": len(history) if kind == "shared-mlp" else 0,
                                               "selectionProtocol": selection, "fourTaskTrainMembershipSha256": four_train_hash,
                                               "fourTaskValidationMembershipSha256": four_validation_hash, "dropoutSeedResetAfterHeadInitialization": kind == "shared-mlp",
                                               "hyperparameters": {"learningRate": .001, "weightDecay": .001, "epochsLimit": epochs, "patience": patience, "batchSize": batch_size} if kind == "shared-mlp" else {"C": 1, "solver": "lbfgs", "maxIter": 2000},
                                               "device": device, "dependencies": copy.deepcopy(experiment["dependencies"]),
                                               "sourceHashes": {str(path.relative_to(ROOT)).replace("\\", "/"): sha256_file(path) for path in sources if path.is_file()}},
                                  "calibration": {"status": "not-fitted", "certified": False}, "applicability": {"validated": False, "status": "not-validated"},
                                  "evaluationStatus": "development-validation-only", "artifacts": artifacts,
                                  "limitations": ["Validation is used for model selection; no blind or prospective evaluation.",
                                                  "Twelve Tox21 heads predict assay-specific activity, not four toxicity outcomes or mechanisms.",
                                                  "Masked unknown labels are not negatives; disabled/untrainable tasks never return learned scores.",
                                                  "Seed-paired differences are descriptive and do not establish significance or transfer causality."]}
                write_json(run_dir / "manifest.json", model_manifest)
                write_json(run_dir / "run-status.json", {"status": completion, "model": model_id, "seed": seed})
                experiment["runs"].append({"model": model_id, "seed": seed, "directory": run_dir.name, "status": completion,
                                           "manifestSha256": sha256_file(run_dir / "manifest.json"), "fourTaskValidationMembershipSha256": four_validation_hash,
                                           "evaluation": evaluation})
                write_json(output / "experiment.json", experiment)
        aggregate = {}
        for model_id in models:
            tasks = FOUR_ENDPOINTS if model_id == "A4" else ALL_ENDPOINTS
            aggregate[model_id] = {}
            for endpoint in tasks:
                aggregate[model_id][endpoint] = {}
                for metric in ("auroc", "averagePrecision", "brier", "ece10Bins"):
                    values = [run["evaluation"]["endpoints"][endpoint][metric] for run in experiment["runs"] if run["model"] == model_id and run["evaluation"]["endpoints"][endpoint][metric] is not None]
                    aggregate[model_id][endpoint][metric] = {"mean": float(np.mean(values)) if values else None,
                                                           "standardDeviation": float(np.std(values, ddof=1)) if len(values) > 1 else None, "definedSeeds": len(values)}
        experiment.update({"status": "completed-with-partial-models" if any(run["status"] == "trained-partial" for run in experiment["runs"]) else "completed",
                           "aggregate": aggregate, "pairedNegativeTransfer": paired_deltas(experiment["runs"], seeds), "durationSeconds": round(time.perf_counter() - started, 3),
                           "repetitionStatus": "at-least-three-seeds" if len(seeds) >= 3 else "diagnostic-fewer-than-three-seeds"})
        write_json(output / "experiment.json", experiment)
        return experiment
    except BaseException as error:
        experiment.update({"status": "incomplete", "errorType": type(error).__name__, "error": str(error), "durationSeconds": round(time.perf_counter() - started, 3)})
        write_json(output / "experiment.json", experiment)
        if isinstance(error, torch.cuda.OutOfMemoryError):
            raise RuntimeError("Multitask CUDA memory exhausted; reduce batch sizes or explicitly use CPU. Artifacts are incomplete.") from error
        raise
