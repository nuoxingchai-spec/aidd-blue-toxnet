"""Auditable frozen-feature v3 experiments on development train/validation only.

No legacy test set, calibration fitting, remote labels, or simulated predictions
are accepted here. The encoder stays frozen; v2's inference path is untouched.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
from rdkit import Chem, DataStructs, rdBase
from rdkit.Chem import rdFingerprintGenerator
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.preprocessing import StandardScaler
from torch import nn
from torch.nn import functional as F

from backend.chemistry import CHEMISTRY_VERSION, canonicalize_smiles
from backend.validation import ENDPOINTS

EXPERIMENT_VERSION = "aidd.toxnet.frozen-feature-experiment.v3"
ARTIFACT_SCHEMA = "aidd.toxnet.artifact.v3"
HEAD_ARCHITECTURE = "aidd.toxnet.frozen-feature-head.v3"
DEFAULT_SEEDS = (42, 43, 44)
MODELS = ("B0", "B1", "B2", "F1")
FINGERPRINT_SPEC = {"radius": 2, "bits": 2048, "includeChirality": True}
ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value) -> None:
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def dependency_versions() -> dict:
    result = {"python": __import__("platform").python_version(), "rdkit": rdBase.rdkitVersion}
    for name in ("torch", "numpy", "scikit-learn", "transformers", "tokenizers", "safetensors", "huggingface-hub"):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = "not-installed"
    return result


def resolve_device(device: str) -> str:
    if device not in ("cpu", "cuda", "auto"):
        raise ValueError("device must be cpu, cuda or auto")
    selected = "cuda" if device == "auto" and torch.cuda.is_available() else "cpu" if device == "auto" else device
    if selected == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable in this PyTorch build; install a CUDA build or explicitly use --device cpu.")
    return selected


def morgan_features(smiles: list[str]) -> np.ndarray:
    """Return ordered chirality-aware radius-2 / 2048-bit fingerprints."""
    generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048, includeChirality=True)
    result = np.zeros((len(smiles), 2048), dtype=np.float32)
    for index, value in enumerate(smiles):
        canonical = canonicalize_smiles(value)
        DataStructs.ConvertToNumpyArray(generator.GetFingerprint(Chem.MolFromSmiles(canonical)), result[index])
    return result


def label_arrays(samples: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """Missing labels are masked placeholders, never experimental negatives."""
    labels = np.zeros((len(samples), len(ENDPOINTS)), dtype=np.float32)
    mask = np.zeros_like(labels)
    for index, sample in enumerate(samples):
        observed = sample.get("labels")
        if not isinstance(observed, dict) or set(observed) - set(ENDPOINTS):
            raise ValueError("Sample labels must map only the four retained endpoints.")
        for endpoint, label in observed.items():
            if type(label) is not int or label not in (0, 1):
                raise ValueError("Observed experimental labels must be integer 0 or 1.")
            task = ENDPOINTS.index(endpoint)
            labels[index, task], mask[index, task] = label, 1
    return labels, mask


def validate_development_samples(samples: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    if not samples or any(sample.get("split") not in ("train", "validation") for sample in samples):
        raise ValueError("v3 experiments accept only audited development train/validation samples, never legacy test or external hERG.")
    owners, group_owners = {}, {}
    for sample in samples:
        canonical = canonicalize_smiles(sample["smiles"])
        if canonical != sample["smiles"]:
            raise ValueError("v3 sample SMILES must already match the audited full canonical structure.")
        if canonical in owners:
            raise ValueError("Duplicate canonical structures cannot enter the frozen feature experiment.")
        owners[canonical] = sample["split"]
        group = sample.get("groupId")
        if not isinstance(group, str) or not group:
            raise ValueError("v3 samples must carry audited structural group IDs.")
        if group_owners.setdefault(group, sample["split"]) != sample["split"]:
            raise ValueError("Structural group leakage across train and validation.")
    labels, masks = label_arrays(samples)
    train = np.array([sample["split"] == "train" for sample in samples])
    validation = ~train
    if not train.any() or not validation.any():
        raise ValueError("Both development train and validation must be nonempty.")
    for task, endpoint in enumerate(ENDPOINTS):
        observed = labels[train, task][masks[train, task] == 1]
        if len(np.unique(observed)) != 2:
            raise ValueError(f"train/{endpoint} lacks two experimentally observed classes; no complete four-endpoint model can be trained.")
    if not any(len(np.unique(labels[validation, task][masks[validation, task] == 1])) == 2 for task in range(len(ENDPOINTS))):
        raise ValueError("Validation has no endpoint with both classes; AUROC/AP cannot select a model.")
    return labels, masks


def evaluate_probabilities(labels: np.ndarray, mask: np.ndarray, probabilities: np.ndarray) -> dict:
    if labels.shape != mask.shape or probabilities.shape != labels.shape or labels.ndim != 2 or labels.shape[1] != 4:
        raise ValueError("Labels, masks and predictions must have matching [molecules, four endpoints] shapes.")
    if not np.isin(mask, (0, 1)).all() or not np.isfinite(probabilities).all() or (probabilities < 0).any() or (probabilities > 1).any():
        raise ValueError("Invalid observation mask or nonfinite/out-of-range probabilities.")
    if not np.isin(labels[mask == 1], (0, 1)).all():
        raise ValueError("Observed labels must be binary.")
    endpoints = {}
    for task, endpoint in enumerate(ENDPOINTS):
        y, p = labels[:, task][mask[:, task] == 1], probabilities[:, task][mask[:, task] == 1]
        count, positives = len(y), int(y.sum())
        result = {"count": count, "positives": positives, "negatives": count - positives,
                  "auroc": None, "averagePrecision": None, "brier": None, "ece10Bins": None,
                  "status": "no-data" if not count else "single-class" if len(np.unique(y)) != 2 else "computed",
                  "rankingMetricReason": "no-observed-labels" if not count else "both-classes-required" if len(np.unique(y)) != 2 else None}
        if count:
            result["brier"] = float(np.mean(np.square(p - y)))
            bins = np.minimum((p * 10).astype(int), 9)
            result["ece10Bins"] = float(sum(np.sum(bins == bucket) / count * abs(float(p[bins == bucket].mean() - y[bins == bucket].mean())) for bucket in range(10) if np.any(bins == bucket)))
            if len(np.unique(y)) == 2:
                result["auroc"] = float(roc_auc_score(y, p))
                result["averagePrecision"] = float(average_precision_score(y, p))
        endpoints[endpoint] = result
    def macro(field):
        values = [result[field] for result in endpoints.values() if result[field] is not None]
        return float(np.mean(values)) if values else None
    return {"split": "validation", "evaluationType": "development-validation-used-for-model-selection",
            "independentTest": False, "calibrationStatus": "not-fitted", "probabilityInterpretation": "uncalibrated-model-output",
            "macroAUROC": macro("auroc"), "macroAP": macro("averagePrecision"), "endpoints": endpoints}


def masked_binary_loss(logits: torch.Tensor, labels: torch.Tensor, mask: torch.Tensor, positive_weight=None) -> torch.Tensor:
    if logits.shape != labels.shape or labels.shape != mask.shape or not bool(((mask == 0) | (mask == 1)).all()):
        raise ValueError("Masked loss shapes or mask are invalid.")
    # Replace unknown placeholders before BCE, so even NaN unknown labels cannot
    # pollute the loss. No gradient ever reaches an unobserved endpoint.
    targets = torch.where(mask.bool(), labels, torch.zeros_like(labels))
    observed_targets = targets[mask.bool()]
    if not bool(((observed_targets == 0) | (observed_targets == 1)).all()):
        raise ValueError("Observed labels must be binary.")
    raw = F.binary_cross_entropy_with_logits(logits, targets, pos_weight=positive_weight, reduction="none")
    return (raw * mask).sum() / mask.sum().clamp_min(1)


class FrozenFeatureMLP(nn.Module):
    def __init__(self, input_dim: int, hidden_size: int = 128, dropout: float = 0.1):
        super().__init__()
        if input_dim <= 0 or hidden_size <= 0 or not 0 <= dropout < 1:
            raise ValueError("Invalid frozen-head architecture.")
        self.layers = nn.Sequential(nn.Linear(input_dim, hidden_size), nn.GELU(), nn.Dropout(dropout), nn.Linear(hidden_size, 4))

    def forward(self, features):
        return self.layers(features)


def standardize_train_only(features: np.ndarray, train: np.ndarray, enabled: bool) -> tuple[np.ndarray, dict]:
    if features.ndim != 2 or not np.isfinite(features).all() or len(train) != len(features) or not train.any():
        raise ValueError("Invalid ordered features or training membership.")
    if enabled:
        scaler = StandardScaler().fit(features[train])
        mean, scale = scaler.mean_, scaler.scale_
    else:
        mean, scale = np.zeros(features.shape[1], dtype=np.float64), np.ones(features.shape[1], dtype=np.float64)
    transformed = ((features.astype(np.float64) - mean) / scale).astype(np.float32)
    return transformed, {"mean": mean, "scale": scale}


def sigmoid(logits: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-np.clip(np.asarray(logits, dtype=np.float64), -50, 50)))


def predict_feature_head(head, features: np.ndarray, device: str = "cpu", batch_size: int = 256) -> np.ndarray:
    if isinstance(head, dict):
        return sigmoid(features.astype(np.float64) @ head["coefficients"].T + head["intercepts"])
    head.eval()
    values = []
    with torch.inference_mode():
        for start in range(0, len(features), batch_size):
            values.append(head(torch.from_numpy(features[start:start + batch_size]).to(device)).cpu().numpy())
    return sigmoid(np.concatenate(values)) if values else np.empty((0, 4), dtype=np.float64)


def fit_logistic_head(features, labels, mask, train, seed, *, return_trace=False):
    coefficients, intercepts = [], []
    records = []
    for task in range(4):
        selected = train & (mask[:, task] == 1)
        classifier = LogisticRegression(C=1.0, max_iter=2000, solver="lbfgs", random_state=seed)
        classifier.fit(features[selected], labels[selected, task])
        if not np.array_equal(classifier.classes_, [0, 1]):
            raise ValueError("Both observed training classes are required for each logistic head.")
        coefficients.append(classifier.coef_[0])
        intercepts.append(classifier.intercept_[0])
        records.append({"endpoint": ENDPOINTS[task], "fitSplit": "train", "observedTrainingLabels": int(selected.sum()),
                        "positiveTrainingLabels": int(labels[selected, task].sum()), "iterations": int(classifier.n_iter_[0]),
                        "maximumIterations": classifier.max_iter, "iterationLimitReached": bool(classifier.n_iter_[0] >= classifier.max_iter)})
    head = {"coefficients": np.asarray(coefficients, dtype=np.float64), "intercepts": np.asarray(intercepts, dtype=np.float64)}
    trace = [{"method": "independent-endpoint-LogisticRegression", "C": 1.0, "solver": "lbfgs", "seed": seed, "fitSplit": "train", "endpoints": records}]
    return (head, trace) if return_trace else head


def fit_mlp_head(features, labels, mask, train, seed, *, device="cpu", epochs=40, patience=8, batch_size=64, hidden_size=128):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if device == "cuda":
        torch.cuda.manual_seed_all(seed)
    model = FrozenFeatureMLP(features.shape[1], hidden_size).to(device)
    optimization = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-3)
    positives = (labels[train] * mask[train]).sum(0)
    negatives = mask[train].sum(0) - positives
    positive_weight = torch.tensor(np.clip(np.sqrt(negatives / positives), 0.5, 4), device=device)
    train_indices, validation = np.flatnonzero(train), ~train
    history, best_state, best_key, stale, best_epoch = [], None, (-math.inf, -math.inf), 0, None
    for epoch in range(1, epochs + 1):
        model.train()
        indices = np.random.default_rng(seed + epoch).permutation(train_indices)
        losses = []
        for start in range(0, len(indices), batch_size):
            selected = indices[start:start + batch_size]
            x = torch.from_numpy(features[selected]).to(device)
            y, observed = torch.from_numpy(labels[selected]).to(device), torch.from_numpy(mask[selected]).to(device)
            optimization.zero_grad(set_to_none=True)
            loss = masked_binary_loss(model(x), y, observed, positive_weight)
            if not bool(torch.isfinite(loss)):
                raise RuntimeError("Nonfinite v3 training loss; run is incomplete.")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
            optimization.step()
            losses.append(float(loss.detach().cpu()))
        probabilities = predict_feature_head(model, features[validation], device, batch_size)
        metrics = evaluate_probabilities(labels[validation], mask[validation], probabilities)
        key = (metrics["macroAUROC"], metrics["macroAP"])
        if key[0] is None or key[1] is None:
            raise ValueError("No defined validation AUROC/AP for checkpoint selection.")
        history.append({"epoch": epoch, "trainLoss": float(np.mean(losses)), "validationMacroAUROC": key[0], "validationMacroAP": key[1]})
        if key > best_key:
            best_key, stale, best_epoch = key, 0, epoch
            best_state = {name: tensor.detach().cpu().clone() for name, tensor in model.state_dict().items()}
        else:
            stale += 1
            if stale >= patience:
                break
    if best_state is None:
        raise ValueError("No actual MLP checkpoint was selected.")
    model.load_state_dict(best_state)
    return model, history, best_epoch


def select_feature_branches(model_id: str, fusion_branches: tuple[str, ...] = ("molformer", "morgan")) -> tuple[str, ...]:
    fixed = {"B0": ("morgan",), "B1": ("molformer",), "B2": ("molformer",)}
    if model_id in fixed:
        return fixed[model_id]
    if model_id != "F1" or not fusion_branches or len(set(fusion_branches)) != len(fusion_branches) or set(fusion_branches) - {"molformer", "morgan"}:
        raise ValueError("Unknown model or invalid frozen fusion branches.")
    return tuple(branch for branch in ("molformer", "morgan") if branch in fusion_branches)


def assemble_features(branches: tuple[str, ...], fingerprints, embeddings) -> np.ndarray:
    selected = []
    for branch in branches:
        value = fingerprints if branch == "morgan" else embeddings if branch == "molformer" else None
        if value is None or value.ndim != 2 or not np.isfinite(value).all():
            raise ValueError(f"Real validated features for {branch} are unavailable.")
        selected.append(value)
    if not selected or len({len(value) for value in selected}) != 1:
        raise ValueError("Feature branches do not have matching molecular input order/length.")
    return np.concatenate(selected, axis=1).astype(np.float32, copy=False)


def assert_fresh_output(path: Path) -> Path:
    target = Path(path).resolve()
    protected = (ROOT / "models" / "aidd-toxnet").resolve()
    if target == protected or target.is_relative_to(protected) or protected.is_relative_to(target):
        raise ValueError("The formal v2 model directory is protected; use artifacts/v3/<new-experiment>.")
    if target.exists():
        raise FileExistsError("Experiment output already exists; select a new directory to preserve previous records.")
    return target


def run_experiments(*, data_dir: Path, output: Path, models=("B0", "B1", "B2"), seeds=DEFAULT_SEEDS,
                    snapshot_dir: Path | None = None, embedding_cache: Path | None = None, device="cpu",
                    epochs=40, patience=8, batch_size=64, encoder_batch_size=8, threads=6,
                    fusion_branches=("molformer", "morgan")) -> dict:
    """Train real models, with completion written only after all artifacts exist."""
    if not models or len(set(models)) != len(models) or set(models) - set(MODELS):
        raise ValueError("Select unique model identifiers from B0, B1, B2, F1.")
    if "F1" in models and not {"B0", "B1", "B2"}.issubset(models):
        raise ValueError("Fusion must not skip B0/B1/B2; include all three baselines in this experiment.")
    if len(set(seeds)) != len(seeds) or not seeds or any(type(seed) is not int or seed < 0 for seed in seeds):
        raise ValueError("Seeds must be unique nonnegative integers.")
    if min(epochs, patience, batch_size, encoder_batch_size) < 1 or not 1 <= threads <= 24:
        raise ValueError("Invalid experiment epochs/patience/batch/thread limits.")
    output = assert_fresh_output(output)
    device = resolve_device(device)
    torch.set_num_threads(threads)
    from backend.v3_data import load_v3_data
    data_manifest, samples = load_v3_data(Path(data_dir))
    print(json.dumps({"phase": "data-verified", "molecules": len(samples), "datasetVersion": data_manifest["datasetVersion"]}), flush=True)
    labels, mask = validate_development_samples(samples)
    train = np.asarray([sample["split"] == "train" for sample in samples])
    validation = ~train
    source_hash = sha256_file(Path(data_dir) / "manifest.json")
    smiles = [sample["smiles"] for sample in samples]
    branches = {model: select_feature_branches(model, fusion_branches) for model in models}
    fingerprints = morgan_features(smiles) if any("morgan" in selected for selected in branches.values()) else None
    embeddings, encoder_provenance = None, None
    embedding_cache_reused = False
    feature_started = time.perf_counter()
    if any("molformer" in selected for selected in branches.values()):
        if snapshot_dir is None:
            raise FileNotFoundError("B1/B2/fusion require a locally audited MoLFormer snapshot; supply --encoder-dir. No proxy embeddings are generated.")
        from backend.pretrained_encoder import PretrainedEncoder, cache_embeddings
        encoder = PretrainedEncoder(snapshot_dir=Path(snapshot_dir), device=device)
        encoder_provenance = copy.deepcopy(encoder.provenance)
        cache = Path(embedding_cache) if embedding_cache is not None else Path(data_dir) / "molformer-embeddings.npz"
        embedding_cache_reused = cache.exists()
        embeddings = cache_embeddings(cache, smiles, encoder, source_hash=source_hash, batch_size=encoder_batch_size)
        if embeddings.shape != (len(samples), encoder_provenance["dimension"]):
            raise ValueError("Embedding cache dimension or molecular order contract is invalid.")
        del encoder
        if device == "cuda":
            torch.cuda.empty_cache()
    feature_seconds = round(time.perf_counter() - feature_started, 3)
    print(json.dumps({"phase": "features-ready", "molecules": len(samples), "featureExtractionSeconds": feature_seconds}), flush=True)
    if device == "cuda":
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    torch.use_deterministic_algorithms(True)
    output.mkdir(parents=True)
    started = time.perf_counter()
    context = {"schema": "aidd.toxnet.experiment-summary.v3", "version": EXPERIMENT_VERSION,
               "status": "running", "createdAt": datetime.now(timezone.utc).isoformat(), "seeds": list(seeds),
               "datasetVersion": data_manifest["datasetVersion"], "dataManifestSha256": source_hash,
               "splitSha256": data_manifest["splitSha256"], "taskContractVersion": data_manifest["taskContractVersion"],
               "featureExtractionSeconds": feature_seconds,
               "dependencies": dependency_versions(), "device": device, "models": list(models), "runs": [],
               "legacyTestUsed": False, "externalHergUsed": False, "newBlindTestClaimed": False}
    write_json(output / "experiment.json", context)
    try:
        for model_id in models:
            raw = assemble_features(branches[model_id], fingerprints, embeddings)
            # Morgan remains raw binary. Every embedding/fusion standardizer is
            # fitted on train structures only, independent of validation labels.
            features, preprocessing = standardize_train_only(raw, train, model_id != "B0")
            for seed in seeds:
                run_dir = output / f"{model_id}-seed-{seed}"
                run_dir.mkdir()
                write_json(run_dir / "run-status.json", {"status": "training", "model": model_id, "seed": seed})
                head_type = "logistic-regression" if model_id in ("B0", "B1") else "mlp"
                if head_type == "logistic-regression":
                    head, history = fit_logistic_head(features, labels, mask, train, seed, return_trace=True)
                    best_epoch = None
                    np.savez_compressed(run_dir / "weights.npz", **head)
                    weight_file = "weights.npz"
                    parameter_count = int(sum(value.size for value in head.values()))
                else:
                    head, history, best_epoch = fit_mlp_head(features, labels, mask, train, seed, device=device,
                                                           epochs=epochs, patience=patience, batch_size=batch_size)
                    torch.save({name: value.detach().cpu() for name, value in head.state_dict().items()}, run_dir / "weights.pt")
                    weight_file = "weights.pt"
                    parameter_count = sum(parameter.numel() for parameter in head.parameters())
                probabilities = predict_feature_head(head, features[validation], device, batch_size)
                evaluation = evaluate_probabilities(labels[validation], mask[validation], probabilities)
                evaluation.update({"schema": "aidd.toxnet.development-evaluation.v3", "model": model_id,
                                   "seed": seed, "datasetVersion": data_manifest["datasetVersion"],
                                   "splitSha256": data_manifest["splitSha256"], "status": "evaluated-validation-only"})
                np.savez_compressed(run_dir / "preprocessing.npz", **preprocessing)
                write_json(run_dir / "evaluation.json", evaluation)
                write_json(run_dir / "training-history.json", history)
                predictions = []
                for row, values in zip(np.flatnonzero(validation), probabilities):
                    for task, endpoint in enumerate(ENDPOINTS):
                        if mask[row, task]:
                            predictions.append({"smiles": smiles[row], "endpoint": endpoint, "label": int(labels[row, task]),
                                                "score": float(values[task]), "split": "validation"})
                write_json(run_dir / "validation-predictions.json", predictions)
                write_json(run_dir / "training-references.json", [smiles[row] for row in np.flatnonzero(train)])
                artifacts = {name: {"file": file, "sha256": sha256_file(run_dir / file)} for name, file in
                             (("weights", weight_file), ("preprocessing", "preprocessing.npz"), ("evaluation", "evaluation.json"),
                              ("trainingHistory", "training-history.json"), ("validationPredictions", "validation-predictions.json"),
                              ("trainingReferences", "training-references.json"))}
                architecture = {"id": HEAD_ARCHITECTURE, "headType": head_type, "inputDim": features.shape[1],
                                "outputDim": 4, "endpointOrder": list(ENDPOINTS), "hiddenSize": 128 if head_type == "mlp" else None,
                                "dropout": 0.1 if head_type == "mlp" else 0.0, "featureBranches": list(branches[model_id]),
                                "fingerprint": copy.deepcopy(FINGERPRINT_SPEC) if "morgan" in branches[model_id] else None}
                manifest = {"schema": ARTIFACT_SCHEMA, "status": "trained-completed", "modelName": f"蓝研 ToxNet v3 {model_id}",
                            "modelVersion": f"aidd-toxnet-v3-{model_id.lower()}-{seed}-{artifacts['weights']['sha256'][:12]}",
                            "endpoints": list(ENDPOINTS), "architecture": architecture, "parameterCount": parameter_count,
                            "trainableParameterCount": parameter_count,
                            "frozenEncoderParameterCount": encoder_provenance["frozenParameterCount"] if "molformer" in branches[model_id] else 0,
                            "encoder": copy.deepcopy(encoder_provenance) if "molformer" in branches[model_id] else None,
                            "datasetVersion": data_manifest["datasetVersion"], "taskContractVersion": data_manifest["taskContractVersion"],
                            "dataset": {"manifestSha256": source_hash, "splitSha256": data_manifest["splitSha256"],
                                        "version": data_manifest["datasetVersion"], "datasetVersion": data_manifest["datasetVersion"],
                                        "trainingPermission": copy.deepcopy(data_manifest.get("trainingPermission", data_manifest.get("trainingPermissions")))},
                            "chemistryVersion": CHEMISTRY_VERSION, "rdkitVersion": rdBase.rdkitVersion,
                            "training": {"status": "completed", "optimizationSplit": "train", "selectionSplit": "validation",
                                         "testUsedForSelection": False, "previouslyExposedUsedForSelection": False, "externalHergUsed": False, "seed": seed,
                                         "splits": {"train": int(train.sum()), "validation": int(validation.sum())},
                                         "classCounts": {split: {endpoint: {"positive": int((labels[selected, task] * mask[selected, task]).sum()),
                                                                            "negative": int((mask[selected, task] * (1 - labels[selected, task])).sum())}
                                                                for task, endpoint in enumerate(ENDPOINTS)} for split, selected in (("train", train), ("validation", validation))},
                                         "selectionRule": "validation macro AUROC, then macro AP; defined endpoints only",
                                         "bestEpoch": best_epoch, "epochsExecuted": len(history) if head_type == "mlp" else 0, "batchSize": batch_size,
                                         "encoderFrozen": True, "preprocessingFitSplit": "train", "featureTransformFitSplit": "train", "labelSource": "experimental-only",
                                         "endpointStatus": {endpoint: "trained" for endpoint in ENDPOINTS},
                                         "hyperparameters": {"logisticC": 1.0, "logisticMaxIter": 2000} if head_type == "logistic-regression" else {"learningRate": 1e-3, "weightDecay": 1e-3, "epochsLimit": epochs, "patience": patience},
                                         "dependencies": copy.deepcopy(context["dependencies"]),
                                         "device": "cpu" if head_type == "logistic-regression" else device,
                                         "featureExtractionDevice": device if "molformer" in branches[model_id] and not embedding_cache_reused else None,
                                         "featureExtractionMode": ("verified-cache-load" if embedding_cache_reused else "computed-this-run") if "molformer" in branches[model_id] else "not-used",
                                         "encoderLoadDevice": device if "molformer" in branches[model_id] else None,
                                         "sourceHashes": {str(path.relative_to(ROOT)).replace("\\", "/"): sha256_file(path)
                                                          for path in (Path(__file__).resolve(), ROOT / "scripts/train_toxnet_v3.py",
                                                                       ROOT / "backend/chemistry.py", ROOT / "backend/v3_data.py", ROOT / "backend/pretrained_encoder.py") if path.is_file()}},
                            "evaluationStatus": "evaluated-validation-only", "formalAvailability": False,
                            "calibration": {"status": "not-fitted", "certified": False, "reason": "validation is used for development selection; no independent calibration set"},
                            "applicability": {"status": "not-validated", "validated": False}, "artifacts": artifacts,
                            "limitations": ["Development validation is used for selection; metrics are not an independent performance estimate.",
                                            "No prospective evaluation, calibrated risk claim, or applicability-domain certification.",
                                            "MoLFormer representations do not establish reliable toxicity mechanisms.",
                                            "Standard Morgan fingerprints do not encode isotope mass; distinct isotopic identities may share B0 features.",
                                            "Legacy v2 test/validation and exposed hERG_Karim are excluded from development."]}
                # Completion is the last record, after actual weights, metrics
                # and hashes were persisted. Any exception leaves no manifest.
                write_json(run_dir / "manifest.json", manifest)
                write_json(run_dir / "run-status.json", {"status": "trained-completed", "model": model_id, "seed": seed})
                context["runs"].append({"model": model_id, "seed": seed, "directory": run_dir.name,
                                        "manifestSha256": sha256_file(run_dir / "manifest.json"), "evaluation": evaluation})
                write_json(output / "experiment.json", context)
        aggregate = {}
        for model_id in models:
            aggregate[model_id] = {}
            for endpoint in ENDPOINTS:
                aggregate[model_id][endpoint] = {}
                for field in ("auroc", "averagePrecision", "brier", "ece10Bins"):
                    values = [run["evaluation"]["endpoints"][endpoint][field] for run in context["runs"] if run["model"] == model_id and run["evaluation"]["endpoints"][endpoint][field] is not None]
                    aggregate[model_id][endpoint][field] = {"mean": float(np.mean(values)) if values else None,
                                                           "standardDeviation": float(np.std(values, ddof=1)) if len(values) > 1 else None,
                                                           "definedSeeds": len(values)}
        context.update({"status": "completed", "aggregate": aggregate, "durationSeconds": round(time.perf_counter() - started, 3),
                        "repetitionStatus": "at-least-three-seeds" if len(seeds) >= 3 else "fewer-than-three-seeds-diagnostic-only"})
        write_json(output / "experiment.json", context)
        return context
    except BaseException as error:
        context.update({"status": "incomplete", "errorType": type(error).__name__, "error": str(error), "durationSeconds": round(time.perf_counter() - started, 3)})
        write_json(output / "experiment.json", context)
        if isinstance(error, (torch.cuda.OutOfMemoryError,)):
            raise RuntimeError("CUDA memory exhausted; experiment is incomplete. Reduce --encoder-batch-size/--batch-size or use --device cpu.") from error
        raise
