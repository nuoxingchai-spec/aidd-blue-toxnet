"""Opt-in offline v3 inference; the deployed v2 provider remains unchanged.

This dispatcher does not promote development-validation results to a formal
model. v3 requires an explicit research opt-in and complete, verified artifacts.
No network access, unrestricted pickle loading, or generated fallback predictions.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import re
import threading
from pathlib import Path

from rdkit import rdBase

from backend.chemistry import CHEMISTRY_VERSION, canonicalize_smiles
from backend.local_model import ARTIFACT_SCHEMA, LocalModelClient
from backend.model_client import ENDPOINT_IDS, ModelAPIError
from backend.v3_data import TASK_CONTRACT_VERSION

V3_SCHEMA = "aidd.toxnet.artifact.v3"
V3_ARCHITECTURE = "aidd.toxnet.frozen-feature-head.v3"
V3_INFERENCE_VERSION = "aidd.toxnet.research-inference.v3"


class V3ArtifactError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _fail(message, code="V3_MODEL_INVALID_ARTIFACT"):
    raise V3ArtifactError(code, message)


def _json(path, limit=4 * 1024 * 1024):
    if not path.is_file():
        _fail("V3 model artifacts are missing; complete a real training run first.", "V3_MODEL_NOT_READY")
    if path.stat().st_size > limit:
        _fail("V3 metadata exceeds its size limit.")
    def reject_constant(value):
        raise ValueError("Non-finite JSON is not allowed")
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    except (ValueError, UnicodeError):
        _fail("V3 metadata is not valid finite JSON.")


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _artifact_path(directory, record):
    if not isinstance(record, dict) or not isinstance(record.get("file"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", ""))):
        _fail("V3 artifact filename/SHA256 contract is incomplete.")
    relative = Path(record["file"])
    target = (directory / relative).resolve()
    if relative.is_absolute() or target == directory or not target.is_relative_to(directory):
        _fail("V3 artifact paths must stay inside their model directory.")
    if not target.is_file():
        _fail("V3 training artifacts are incomplete.", "V3_MODEL_NOT_READY")
    if _digest(target) != record["sha256"]:
        _fail("V3 artifact SHA256 mismatch.", "V3_MODEL_CHECKSUM_MISMATCH")
    return target


def _finite(value):
    return type(value) in (float, int) and math.isfinite(value)


class V3ArtifactClient:
    """Explicit research adapter for frozen-feature v3 heads, on CPU by default."""
    def __init__(self, model_dir, *, snapshot_dir=None, device="cpu", allow_research=False):
        self.model_dir = Path(model_dir).resolve()
        self.snapshot_dir = Path(snapshot_dir).resolve() if snapshot_dir else None
        self.device = device
        self.allow_research = allow_research
        self._lock = threading.RLock()
        self._loaded = None
        self._loaded_signature = None

    def _signature(self):
        if not self.model_dir.is_dir():
            return None
        directories = [self.model_dir]
        if self.snapshot_dir and self.snapshot_dir.is_dir():
            directories.append(self.snapshot_dir)
        return tuple(sorted((str(path), path.stat().st_size, path.stat().st_mtime_ns)
                            for directory in directories for path in directory.rglob("*") if path.is_file()))

    def _ensure_loaded(self):
        with self._lock:
            signature = self._signature()
            if self._loaded is not None and signature == self._loaded_signature:
                return self._loaded
            self._loaded = None
            manifest = _json(self.model_dir / "manifest.json")
            if not isinstance(manifest, dict) or manifest.get("schema") != V3_SCHEMA or manifest.get("endpoints") != list(ENDPOINT_IDS):
                _fail("Unsupported v3 schema or endpoint contract.")
            if manifest.get("chemistryVersion") != CHEMISTRY_VERSION or manifest.get("rdkitVersion") != rdBase.rdkitVersion:
                _fail("V3 chemistry engine or RDKit version differs from the trained feature contract.", "V3_CHEMISTRY_VERSION_MISMATCH")
            if manifest.get("status") != "trained-completed":
                _fail("V3 model has not completed real training and validation.", "V3_MODEL_NOT_READY")
            if manifest.get("formalAvailability") is not False:
                _fail("Development artifacts cannot claim formal availability.")
            if not self.allow_research:
                _fail("V3 is a development model; explicit research opt-in is required.", "V3_MODEL_RESEARCH_OPT_IN_REQUIRED")
            for name in ("modelName", "modelVersion", "taskContractVersion"):
                if not isinstance(manifest.get(name), str) or not 0 < len(manifest[name].strip()) <= 200:
                    _fail("V3 model identity/task contract is incomplete.")
            if manifest["taskContractVersion"] != TASK_CONTRACT_VERSION:
                _fail("Unsupported v3 endpoint task contract.")
            training = manifest.get("training", {})
            if not isinstance(training, dict) or not isinstance(training.get("splits"), dict):
                _fail("Invalid v3 training metadata.")
            if (training.get("status") != "completed" or training.get("optimizationSplit") != "train"
                or training.get("selectionSplit") != "validation" or training.get("testUsedForSelection") is not False
                or training.get("previouslyExposedUsedForSelection") is not False
                or training.get("featureTransformFitSplit") != "train"):
                _fail("V3 training history violates the development-only protocol.", "V3_MODEL_NOT_READY")
            if training.get("endpointStatus") != {endpoint: "trained" for endpoint in ENDPOINT_IDS}:
                _fail("One or more endpoints were not trainable; cannot return four learned predictions.", "V3_MODEL_NOT_READY")
            if any(type(training.get("splits", {}).get(name)) is not int or training["splits"][name] <= 0 for name in ("train", "validation")):
                _fail("V3 requires nonempty real train and validation records.")
            dataset = manifest.get("dataset", {})
            if (not isinstance(dataset, dict) or not isinstance(dataset.get("datasetVersion"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", str(dataset.get("splitSha256", "")))):
                _fail("V3 data version or frozen development-split hash is missing.")
            architecture = manifest.get("architecture", {})
            if not isinstance(architecture, dict):
                _fail("Invalid v3 architecture metadata.")
            if architecture.get("id") != V3_ARCHITECTURE or architecture.get("endpointOrder") != list(ENDPOINT_IDS) or architecture.get("outputDim") != 4:
                _fail("Unsupported v3 head architecture.")
            branches = architecture.get("featureBranches")
            if branches not in (["morgan"], ["molformer"], ["molformer", "morgan"]):
                _fail("Unsupported feature branches or branch order.")
            if "molformer" not in branches and (manifest.get("encoder") is not None or manifest.get("frozenEncoderParameterCount") != 0):
                _fail("Morgan-only models cannot claim pretrained encoder provenance or parameters.")
            fingerprint = architecture.get("fingerprint", {})
            if "morgan" in branches and fingerprint != {"radius": 2, "bits": 2048, "includeChirality": True}:
                _fail("Unsupported Morgan feature contract.")
            if (not isinstance(manifest.get("calibration"), dict) or not isinstance(manifest.get("applicability"), dict)
                or manifest["calibration"].get("status") != "not-fitted" or manifest["applicability"].get("validated") is not False):
                _fail("V3 development artifact has unsupported calibration/domain claims.")
            records = manifest.get("artifacts", {})
            if not isinstance(records, dict) or not {"weights", "preprocessing", "evaluation", "trainingHistory"}.issubset(records):
                _fail("V3 model is missing required auditable artifacts.")
            paths = {name: _artifact_path(self.model_dir, record) for name, record in records.items()}
            evaluation = _json(paths["evaluation"])
            if (not isinstance(evaluation, dict)
                or manifest.get("evaluationStatus") != "evaluated-validation-only"
                or evaluation.get("schema") != "aidd.toxnet.development-evaluation.v3"
                or evaluation.get("status") != "evaluated-validation-only"
                or evaluation.get("split") != "validation"
                or evaluation.get("evaluationType") != "development-validation-used-for-model-selection"
                or evaluation.get("independentTest") is not False
                or evaluation.get("calibrationStatus") != "not-fitted"
                or evaluation.get("datasetVersion") != dataset["datasetVersion"]
                or evaluation.get("splitSha256") != dataset["splitSha256"]
                or type(training.get("seed")) is not int
                or type(evaluation.get("seed")) is not int or evaluation["seed"] != training["seed"]
                or not isinstance(evaluation.get("endpoints"), dict)
                or set(evaluation["endpoints"]) != set(ENDPOINT_IDS)):
                _fail("V3 evaluation must match the completed model's development data, split, seed and task contract.")
            for value in evaluation["endpoints"].values():
                if not isinstance(value, dict) or type(value.get("count")) is not int or value["count"] < 0 or type(value.get("positives")) is not int or not 0 <= value["positives"] <= value["count"]:
                    _fail("V3 validation class counts are invalid.")
                if type(value.get("negatives")) is not int or value["negatives"] < 0 or value["positives"] + value["negatives"] != value["count"]:
                    _fail("V3 validation positive, negative and total counts disagree.")
                for metric in ("auroc", "averagePrecision", "brier", "ece10Bins"):
                    if value.get(metric) is not None and (not _finite(value[metric]) or not 0 <= value[metric] <= 1):
                        _fail("V3 validation metrics are invalid.")
                count, positives = value["count"], value["positives"]
                expected_status = "no-data" if not count else "computed" if 0 < positives < count else "single-class"
                expected_reason = "no-observed-labels" if not count else None if 0 < positives < count else "both-classes-required"
                if value.get("status") != expected_status or value.get("rankingMetricReason") != expected_reason:
                    _fail("V3 evaluation status must reflect its actual observed class support.")
                if expected_status == "computed":
                    if any(value.get(metric) is None for metric in ("auroc", "averagePrecision", "brier", "ece10Bins")):
                        _fail("A two-class V3 validation task is missing actual evaluation metrics.", "V3_MODEL_NOT_READY")
                elif any(value.get(metric) is not None for metric in ("auroc", "averagePrecision")):
                    _fail("Missing or single-class V3 validation cannot claim AUROC/AP.")
                if count and any(value.get(metric) is None for metric in ("brier", "ece10Bins")):
                    _fail("Observed V3 validation predictions require Brier/ECE measurements.", "V3_MODEL_NOT_READY")
                if not count and any(value.get(metric) is not None for metric in ("brier", "ece10Bins")):
                    _fail("Missing V3 validation observations cannot claim Brier/ECE.")
            history = _json(paths["trainingHistory"])
            if not history:
                _fail("V3 actual training history is empty.")
            np = importlib.import_module("numpy")
            if self.device not in ("cpu", "cuda"):
                _fail("V3 research device must be cpu or cuda.")
            if self.device == "cuda" and not importlib.import_module("torch").cuda.is_available():
                _fail("CUDA is unavailable in this PyTorch build.", "V3_CUDA_UNAVAILABLE")
            width = architecture.get("inputDim")
            if type(width) is not int or width <= 0:
                _fail("Invalid v3 input dimension.")
            with np.load(paths["preprocessing"], allow_pickle=False) as arrays:
                if set(arrays.files) != {"mean", "scale"}:
                    _fail("Unsupported preprocessing artifact.")
                mean, scale = arrays["mean"].copy(), arrays["scale"].copy()
            if any(value.shape != (width,) or value.dtype.kind not in "fi" or not np.isfinite(value).all() for value in (mean, scale)) or not (scale > 0).all():
                _fail("V3 preprocessing dimensions or values are invalid.")
            encoder = None
            expected_width = 2048 if "morgan" in branches else 0
            if "molformer" in branches:
                if self.snapshot_dir is None:
                    _fail("A locally verified frozen MoLFormer snapshot is required.", "V3_ENCODER_WEIGHTS_MISSING")
                module = importlib.import_module("backend.pretrained_encoder")
                encoder = module.PretrainedEncoder(self.snapshot_dir, device=self.device)
                provenance = encoder.provenance
                stored = manifest.get("encoder", {})
                if not isinstance(stored, dict):
                    _fail("V3 encoder provenance is incomplete.")
                for key in ("source", "revision", "weightsSha256", "dimension", "chemistryVersion", "rdkitVersion", "encoderVersion", "precision",
                            "files", "pooling", "maxTokens", "truncation", "deterministicEval", "frozenParameterCount", "trainableParameterCount"):
                    if stored.get(key) != provenance.get(key):
                        _fail("V3 frozen encoder revision, chemistry contract or weights do not match.", "V3_ENCODER_PROVENANCE_MISMATCH")
                if manifest.get("frozenEncoderParameterCount") != provenance["frozenParameterCount"]:
                    _fail("V3 frozen encoder parameter count differs from the real loaded model.")
                expected_width += provenance["dimension"]
            if width != expected_width:
                _fail("V3 feature branch dimensions do not match the trained head.")
            kind = architecture.get("headType")
            torch, model = None, None
            if kind == "logistic-regression":
                with np.load(paths["weights"], allow_pickle=False) as arrays:
                    if set(arrays.files) != {"coefficients", "intercepts"}:
                        _fail("Unsupported logistic regression artifact.")
                    coefficients, intercepts = arrays["coefficients"].copy(), arrays["intercepts"].copy()
                if coefficients.shape != (4, width) or intercepts.shape != (4,) or not all(value.dtype.kind in "fi" and np.isfinite(value).all() for value in (coefficients, intercepts)):
                    _fail("V3 LR coefficients are invalid.")
                parameter_count = coefficients.size + intercepts.size
            elif kind == "mlp":
                if type(architecture.get("hiddenSize")) is not int or not 1 <= architecture["hiddenSize"] <= 1024 or not _finite(architecture.get("dropout")) or not 0 <= architecture["dropout"] < 1:
                    _fail("V3 MLP dimensions or dropout are invalid.")
                torch = importlib.import_module("torch")
                module = importlib.import_module("backend.v3_experiments")
                if self.device == "cuda" and not torch.cuda.is_available():
                    _fail("CUDA is unavailable in this PyTorch build.", "V3_CUDA_UNAVAILABLE")
                model = module.FrozenFeatureMLP(width, architecture["hiddenSize"], architecture["dropout"])
                weights = torch.load(paths["weights"], weights_only=True, map_location="cpu")
                if not isinstance(weights, dict) or not weights or not all(isinstance(value, torch.Tensor) and bool(torch.isfinite(value).all()) for value in weights.values()):
                    _fail("V3 MLP tensors are invalid.")
                model.load_state_dict(weights, strict=True)
                model.to(self.device).eval()
                parameter_count = sum(parameter.numel() for parameter in model.parameters())
            else:
                _fail("Unsupported v3 head type.")
            if manifest.get("parameterCount") != parameter_count or manifest.get("trainableParameterCount") != parameter_count:
                _fail("V3 declared parameter count differs from actual weights.")
            self._loaded = {"manifest": manifest, "paths": paths, "np": np, "mean": mean, "scale": scale,
                            "encoder": encoder, "torch": torch, "model": model, "parameterCount": parameter_count,
                            "evaluation": evaluation, "manifestSha256": _digest(self.model_dir / "manifest.json")}
            if kind == "logistic-regression":
                self._loaded.update(coefficients=coefficients, intercepts=intercepts)
            self._loaded_signature = self._signature()
            return self._loaded

    def get_public_config(self):
        try:
            loaded = self._ensure_loaded()
            return {"configured": True, "status": "research-ready", "formalAvailability": False,
                    "providerFormat": "local-frozen-features-v3", "supportsImage": False,
                    "modelName": loaded["manifest"]["modelName"], "version": loaded["manifest"]["modelVersion"]}
        except (V3ArtifactError, OSError, ValueError, ImportError, RuntimeError, TypeError, KeyError, AttributeError) as error:
            return {"configured": False, "status": "unavailable", "formalAvailability": False,
                    "errorCode": getattr(error, "code", "V3_MODEL_LOAD_ERROR"), "message": str(error)}

    def get_model_info(self):
        public = self.get_public_config()
        if not public["configured"]:
            return public
        loaded = self._ensure_loaded()
        manifest = loaded["manifest"]
        return {**public, "schema": V3_SCHEMA, "architecture": copy.deepcopy(manifest["architecture"]),
                "parameterCount": loaded["parameterCount"], "encoder": copy.deepcopy(manifest.get("encoder")),
                "dataset": copy.deepcopy(manifest["dataset"]), "evaluation": copy.deepcopy(loaded["evaluation"]),
                "calibration": {"status": "not-fitted"}, "applicability": {"status": "unvalidated", "validated": False},
                "artifactSha256": loaded["manifestSha256"], "weightsSha256": manifest["artifacts"]["weights"]["sha256"]}

    def predict(self, smiles, structure_id, image_data_url=None):
        try:
            canonical = canonicalize_smiles(smiles)
        except ValueError as error:
            raise ModelAPIError("V3_MODEL_INPUT_ERROR", str(error), 422) from None
        if hashlib.sha256(canonical.encode()).hexdigest() != structure_id:
            raise ModelAPIError("V3_MODEL_INPUT_ERROR", "Structure ID differs from canonical input.", 422)
        try:
            with self._lock:
                loaded = self._ensure_loaded()
                np, architecture = loaded["np"], loaded["manifest"]["architecture"]
                parts = []
                for branch in architecture["featureBranches"]:
                    if branch == "molformer":
                        module = importlib.import_module("backend.pretrained_encoder")
                        try:
                            module.validate_encoder_smiles(canonical)
                        except module.EncoderError as error:
                            raise ModelAPIError("V3_ENCODER_INPUT_UNSUPPORTED", str(error), 422) from None
                        parts.append(loaded["encoder"].encode([canonical], batch_size=1))
                    else:
                        module = importlib.import_module("backend.v3_experiments")
                        parts.append(module.morgan_features([canonical]))
                features = ((np.concatenate(parts, axis=1) - loaded["mean"]) / loaded["scale"]).astype(np.float32)
                module = importlib.import_module("backend.v3_experiments")
                if architecture["headType"] == "logistic-regression":
                    scores = module.predict_feature_head({"coefficients": loaded["coefficients"], "intercepts": loaded["intercepts"]}, features)
                else:
                    scores = module.predict_feature_head(loaded["model"], features, self.device, batch_size=1)
                if scores.shape != (1, 4) or not np.isfinite(scores).all() or not ((scores >= 0) & (scores <= 1)).all():
                    _fail("V3 inference produced invalid predictions.")
                manifest = loaded["manifest"]
                return {"endpoints": [{"id": endpoint, "score": float(value), "drivers": []} for endpoint, value in zip(ENDPOINT_IDS, scores[0])],
                        "scoreKind": "probability", "calibration": {"status": "not-fitted"},
                        "applicability": {"status": "unvalidated", "validated": False},
                        "provenance": {"modelName": manifest["modelName"], "modelVersion": manifest["modelVersion"],
                                       "providerFormat": "local-frozen-features-v3", "inferenceVersion": V3_INFERENCE_VERSION,
                                       "artifactSha256": loaded["manifestSha256"], "weightsSha256": manifest["artifacts"]["weights"]["sha256"],
                                       "datasetVersion": manifest["dataset"]["datasetVersion"], "splitSha256": manifest["dataset"]["splitSha256"],
                                       "encoder": copy.deepcopy(manifest.get("encoder")), "formalAvailability": False,
                                       "evaluationType": "development-validation; not blind or prospective", "explanationMethod": "none"}}
        except V3ArtifactError as error:
            raise ModelAPIError(error.code, error.message, 503) from None
        except (OSError, ImportError, ValueError, RuntimeError):
            raise ModelAPIError("V3_MODEL_INFERENCE_ERROR", "V3 research inference failed; check artifacts, dependencies and input support.", 503) from None


def load_versioned_local_model(model_dir, *, snapshot_dir=None, device="cpu", allow_research=False):
    """Explicit dispatcher: legacy artifacts retain their original implementation."""
    directory = Path(model_dir).resolve()
    manifest = _json(directory / "manifest.json")
    if manifest.get("schema") == ARTIFACT_SCHEMA:
        return LocalModelClient(directory)
    if manifest.get("schema") == V3_SCHEMA:
        return V3ArtifactClient(directory, snapshot_dir=snapshot_dir, device=device, allow_research=allow_research)
    _fail("Unsupported local-model artifact schema.")
