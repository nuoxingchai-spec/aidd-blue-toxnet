"""CPU inference for locally trained AIDD ToxNet artifacts.

The module can be imported without PyTorch. The local provider becomes available
only when its dependency and a complete, checksum-verified artifact are present.
It never substitutes structural rules or generated text for learned predictions.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import os
import threading
from pathlib import Path

from rdkit import Chem, DataStructs, rdBase
from rdkit.Chem import rdFingerprintGenerator

from backend.chemistry import canonicalize_smiles
from backend.model_client import ENDPOINT_IDS, ModelAPIError


ARTIFACT_SCHEMA = "aidd.toxnet.artifact.v1"
INFERENCE_VERSION = "aidd.toxnet.inference.v1"
DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[1] / "models" / "aidd-toxnet"
_MODEL_NAME = "AIDD ToxNet"


class _ArtifactError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _reject_json_constant(value):
    raise ValueError("non-finite JSON constant")


def _read_json(path: Path, maximum=16 * 1024 * 1024):
    if path.stat().st_size > maximum:
        raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型元数据文件超过允许大小。")
    try:
        result = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
    except (ValueError, UnicodeError):
        raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型元数据格式无效。") from None
    return result


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_path(directory: Path, record: dict):
    if not isinstance(record, dict) or not isinstance(record.get("file"), str) or not isinstance(record.get("sha256"), str):
        raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型工件清单不完整。")
    checksum = record["sha256"]
    if len(checksum) != 64 or any(character not in "0123456789abcdef" for character in checksum):
        raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型校验码无效。")
    relative = Path(record["file"])
    candidate = (directory / relative).resolve()
    if relative.is_absolute() or not candidate.is_relative_to(directory) or candidate == directory:
        raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型工件路径必须位于模型目录内。")
    if not candidate.is_file():
        raise _ArtifactError("LOCAL_MODEL_NOT_READY", "本地模型训练工件不完整，请完成训练或恢复完整模型目录。")
    if _sha256(candidate) != checksum:
        raise _ArtifactError("LOCAL_MODEL_CHECKSUM_MISMATCH", "本地模型工件校验失败，请重新训练或恢复经过验证的工件。")
    return candidate


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


class LocalModelClient:
    """ModelClient-compatible adapter with lazy, synchronized artifact loading."""

    def __init__(self, model_dir=None, env=None):
        values = os.environ if env is None else env
        self.model_dir = Path(model_dir or values.get("AIDD_LOCAL_MODEL_DIR") or DEFAULT_MODEL_DIR).resolve()
        self._lock = threading.RLock()
        self._loaded_signature = None
        self._loaded = None
        self._error = None

    def _signature(self):
        # Replacing or modifying any artifact invalidates the loaded snapshot.
        # Checksums are reverified before any replacement snapshot is trusted.
        if not self.model_dir.is_dir():
            return None
        return tuple(sorted((str(path.relative_to(self.model_dir)), path.stat().st_mtime_ns, path.stat().st_size) for path in self.model_dir.rglob("*") if path.is_file()))

    def _ensure_loaded(self):
        with self._lock:
            signature = self._signature()
            if signature == self._loaded_signature and self._loaded is not None:
                return self._loaded
            self._loaded = None
            manifest_path = self.model_dir / "manifest.json"
            if not manifest_path.is_file():
                raise _ArtifactError("LOCAL_MODEL_NOT_READY", "自主模型尚未完成训练，请先生成本地模型工件。")
            manifest = _read_json(manifest_path, 2 * 1024 * 1024)
            if not isinstance(manifest, dict) or manifest.get("schema") != ARTIFACT_SCHEMA or manifest.get("endpoints") != list(ENDPOINT_IDS):
                raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型版本或毒性端点契约无效。")
            for field in ("modelName", "modelVersion"):
                if not isinstance(manifest.get(field), str) or not manifest[field].strip() or len(manifest[field]) > 200:
                    raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型名称或版本信息缺失。")
            if not isinstance(manifest.get("datasetVersion"), str) or not manifest["datasetVersion"].strip():
                raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型缺少可追溯的数据集版本。")
            training = manifest.get("training")
            if not isinstance(training, dict) or training.get("status") != "completed" or training.get("optimizationSplit") != "train" or training.get("selectionSplit") != "validation" or training.get("testUsedForSelection") is not False:
                raise _ArtifactError("LOCAL_MODEL_NOT_READY", "本地模型尚未完成独立训练与验证，不能提供预测。")
            train_count = training.get("splits", {}).get("train") if isinstance(training.get("splits"), dict) else None
            if type(train_count) is not int or train_count <= 0:
                raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型缺少实际训练集记录。")
            architecture = manifest.get("architecture")
            artifacts = manifest.get("artifacts")
            if not isinstance(architecture, dict) or not isinstance(artifacts, dict):
                raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型架构或工件信息缺失。")
            paths = {name: _artifact_path(self.model_dir, record) for name, record in artifacts.items()}
            if "weights" not in paths or "trainingReferences" not in paths or "evaluation" not in paths:
                raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型必须包含权重、训练参考结构和独立评估记录。")
            try:
                torch = importlib.import_module("torch")
                network = importlib.import_module("backend.toxnet")
            except (ImportError, OSError):
                raise _ArtifactError("LOCAL_MODEL_DEPENDENCY_MISSING", "自主模型推理依赖未安装，请安装模型依赖后重启服务。") from None
            try:
                model = network.build_model(architecture)
                state = torch.load(paths["weights"], weights_only=True, map_location="cpu")
                if not isinstance(state, dict) or not state or not all(isinstance(value, torch.Tensor) and bool(torch.isfinite(value).all()) for value in state.values()):
                    raise ValueError("invalid weights")
                model.load_state_dict(state, strict=True)
                model.to("cpu")
                model.eval()
            except Exception:
                raise _ArtifactError("LOCAL_MODEL_INVALID_WEIGHTS", "本地模型权重与架构不匹配或含无效数值。") from None
            references = _read_json(paths["trainingReferences"])
            if not isinstance(references, list) or not references or not all(isinstance(value, str) for value in references):
                raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型训练参考结构无效。")
            try:
                canonical_references = sorted({canonicalize_smiles(value) for value in references})
            except ValueError:
                raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "训练参考集中存在无效分子结构。") from None
            generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
            fingerprints = [generator.GetFingerprint(Chem.MolFromSmiles(value)) for value in canonical_references]
            evaluation = _read_json(paths["evaluation"])
            if not isinstance(evaluation, dict) or evaluation.get("split") != "test" or evaluation.get("heldOutStatus") != "manifest-membership-and-scaffold-separation-verified" or not isinstance(evaluation.get("endpoints"), dict) or set(evaluation["endpoints"]) != set(ENDPOINT_IDS):
                raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型独立评估记录无效。")
            for metrics in evaluation["endpoints"].values():
                if not isinstance(metrics, dict) or type(metrics.get("count")) is not int or metrics["count"] < 0:
                    raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型测试集计数无效。")
                if metrics["count"]:
                    if type(metrics.get("positives")) is not int or not 0 <= metrics["positives"] <= metrics["count"]:
                        raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型测试集类别计数无效。")
                    for field in ("auroc", "averagePrecision", "brier", "ece10Bins"):
                        value = metrics.get(field)
                        if value is not None and (not _finite_number(value) or not 0 <= value <= 1):
                            raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型测试性能或校准指标无效。")
            calibration = manifest.get("calibration", {})
            temperatures = calibration.get("temperatures") if isinstance(calibration, dict) else None
            if not isinstance(temperatures, dict) or set(temperatures) != set(ENDPOINT_IDS) or not all(_finite_number(value) and value > 0 for value in temperatures.values()) or calibration.get("fitSplit") != "validation":
                raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型校准参数或校准数据划分无效。")
            biases = calibration.get("biases", {endpoint: 0.0 for endpoint in ENDPOINT_IDS})
            if not isinstance(biases, dict) or set(biases) != set(ENDPOINT_IDS) or not all(_finite_number(value) for value in biases.values()):
                raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型校准偏置无效。")
            applicability = manifest.get("applicability", {})
            threshold = applicability.get("threshold", 0.5) if isinstance(applicability, dict) else None
            if not _finite_number(threshold) or not 0 <= threshold <= 1:
                raise _ArtifactError("LOCAL_MODEL_INVALID_ARTIFACT", "本地模型结构相似度阈值无效。")
            self._loaded = {"manifest": manifest, "torch": torch, "network": network, "model": model, "generator": generator, "fingerprints": fingerprints, "references": canonical_references, "evaluation": evaluation, "temperatures": [temperatures[endpoint] for endpoint in ENDPOINT_IDS], "biases": [biases[endpoint] for endpoint in ENDPOINT_IDS], "threshold": threshold, "manifestSha256": _sha256(manifest_path), "parameterCount": sum(parameter.numel() for parameter in model.parameters())}
            self._loaded_signature = signature
            self._error = None
            return self._loaded

    def get_public_config(self):
        try:
            loaded = self._ensure_loaded()
            return self._public_ready(loaded)
        except (_ArtifactError, OSError) as error:
            return self._public_unavailable(error)

    @staticmethod
    def _public_ready(loaded):
        manifest = loaded["manifest"]
        return {"configured": True, "providerFormat": "local-pytorch", "modelName": manifest["modelName"], "version": manifest["modelVersion"], "supportsImage": False, "local": True, "status": "ready"}

    def _public_unavailable(self, error):
        self._error = error
        return {"configured": False, "providerFormat": "local-pytorch", "modelName": _MODEL_NAME, "version": None, "supportsImage": False, "local": True, "status": "unavailable", "errorCode": getattr(error, "code", "LOCAL_MODEL_ARTIFACT_ERROR"), "message": getattr(error, "message", "无法读取自主模型工件，请检查模型目录。")}

    def get_model_info(self):
        try:
            with self._lock:
                loaded = self._ensure_loaded()
                manifest = loaded["manifest"]
                # No absolute local paths, binary weights, credentials or training
                # structures are returned by the public model information endpoint.
                return {
                    **self._public_ready(loaded),
                    "schema": ARTIFACT_SCHEMA,
                    "architecture": copy.deepcopy(manifest["architecture"]),
                    "parameterCount": loaded["parameterCount"],
                    "device": "cpu",
                    "endpoints": list(ENDPOINT_IDS),
                    "datasetVersion": manifest["datasetVersion"],
                    "datasetProvenance": copy.deepcopy(manifest.get("datasetProvenance", [])),
                    "training": copy.deepcopy(manifest.get("training", {})),
                    "evaluation": copy.deepcopy(loaded["evaluation"]),
                    "calibration": {**copy.deepcopy(manifest["calibration"]), "status": "measured-not-certified", "certified": False},
                    "applicability": {"method": "Morgan-radius2-2048/Tanimoto", "referenceCount": len(loaded["references"]), "threshold": loaded["threshold"], "validated": False},
                    "artifactSha256": loaded["manifestSha256"],
                    "weightsSha256": manifest["artifacts"]["weights"]["sha256"],
                    "inferenceVersion": INFERENCE_VERSION,
                    "limitations": copy.deepcopy(manifest.get("limitations", [])),
                }
        except (_ArtifactError, OSError) as error:
            return self._public_unavailable(error)

    def predict(self, canonical_smiles, structure_id, image_data_url=None):
        try:
            canonical = canonicalize_smiles(canonical_smiles)
        except ValueError as error:
            raise ModelAPIError("LOCAL_MODEL_INPUT_ERROR", str(error), 422) from None
        expected_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if structure_id != expected_id:
            raise ModelAPIError("LOCAL_MODEL_INPUT_ERROR", "结构标识与规范分子输入不一致。", 422)
        try:
            with self._lock:
                loaded = self._ensure_loaded()
                torch = loaded["torch"]
                with torch.inference_mode():
                    features = loaded["network"].featurize_smiles(canonical, loaded["manifest"]["architecture"])
                    logits = loaded["network"].predict_logits(loaded["model"], features)
                    if not isinstance(logits, torch.Tensor) or tuple(logits.shape) != (1, len(ENDPOINT_IDS)) or not bool(torch.isfinite(logits).all()):
                        raise RuntimeError("invalid network output")
                    temperature = torch.tensor(loaded["temperatures"], dtype=logits.dtype, device="cpu")
                    bias = torch.tensor(loaded["biases"], dtype=logits.dtype, device="cpu")
                    scores = torch.sigmoid(logits.cpu() / temperature + bias).reshape(-1).tolist()
                if not all(_finite_number(value) and 0 <= value <= 1 for value in scores):
                    raise RuntimeError("invalid prediction")
                fingerprint = loaded["generator"].GetFingerprint(Chem.MolFromSmiles(canonical))
                maximum_similarity = max(DataStructs.BulkTanimotoSimilarity(fingerprint, loaded["fingerprints"]))
                manifest = loaded["manifest"]
                calibration = {**copy.deepcopy(manifest["calibration"]), "status": "measured-not-certified", "source": "local-validation-calibration-and-held-out-measurement", "evaluationSha256": manifest["artifacts"]["evaluation"]["sha256"], "certified": False}
                applicability = {"status": "similarity-heuristic", "insideThreshold": maximum_similarity >= loaded["threshold"], "maximumTanimoto": maximum_similarity, "threshold": loaded["threshold"], "referenceCount": len(loaded["references"]), "referenceDatasetVersion": manifest.get("datasetVersion"), "method": "Morgan-radius2-2048/Tanimoto", "source": "local-training-reference-structures", "validated": False}
                return {"endpoints": [{"id": endpoint, "score": float(score), "drivers": []} for endpoint, score in zip(ENDPOINT_IDS, scores)], "scoreKind": "probability", "calibration": calibration, "applicability": applicability, "provenance": {"providerFormat": "local-pytorch", "modelName": manifest["modelName"], "modelVersion": manifest["modelVersion"], "modelVersionSource": "checksum-verified-local-training-artifact", "scoreKind": "probability", "endpointContractVersion": "toxilens.endpoints.v1", "inferenceVersion": INFERENCE_VERSION, "architecture": copy.deepcopy(manifest["architecture"]), "parameterCount": loaded["parameterCount"], "device": "cpu", "torchVersion": str(torch.__version__), "rdkitVersion": rdBase.rdkitVersion, "artifactSha256": loaded["manifestSha256"], "weightsSha256": manifest["artifacts"]["weights"]["sha256"], "datasetVersion": manifest["datasetVersion"], "datasetProvenance": copy.deepcopy(manifest.get("datasetProvenance", [])), "training": copy.deepcopy(manifest.get("training", {})), "explanationMethod": "none", "scoreMeaning": "validation-adjusted neural-network probability; not clinical risk or experimental assay measurement"}}
        except _ArtifactError as error:
            raise ModelAPIError(error.code, error.message, 503) from None
        except OSError:
            raise ModelAPIError("LOCAL_MODEL_ARTIFACT_ERROR", "无法读取自主模型工件，请检查模型目录。", 503) from None
        except ValueError:
            raise ModelAPIError("LOCAL_MODEL_INPUT_ERROR", "当前自主模型无法编码该分子，请检查模型支持的输入范围。", 422) from None
        except ModelAPIError:
            raise
        except Exception:
            raise ModelAPIError("LOCAL_MODEL_INFERENCE_ERROR", "自主模型推理失败，请核查模型版本与推理依赖。", 500) from None
