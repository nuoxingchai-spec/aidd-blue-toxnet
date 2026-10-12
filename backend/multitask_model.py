"""Opt-in offline Tox21 assay inference from verified sixteen-task artifacts.

The deployed four-endpoint v2 loader is unchanged. This module has no startup
imports of torch, numpy, transformers, or training modules and never downloads
weights. Assay activity scores are never combined into clinical toxicity risk.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
from pickle import UnpicklingError
import re
import threading
from zipfile import BadZipFile

from rdkit import rdBase

from backend.candidate_data import TOX21_ASSAYS, TOX21_ENDPOINTS
from backend.chemistry import CHEMISTRY_VERSION, canonicalize_smiles
from backend.model_client import ENDPOINT_IDS, ModelAPIError

ARTIFACT_SCHEMA = "aidd.toxnet.multitask.artifact.v1"
ARCHITECTURE_ID = "aidd.toxnet.multitask-frozen-head.v1"
TASK_CONTRACT_VERSION = "aidd.toxnet.four-toxicity-twelve-assay-tasks.v1"
SERVICE_CONTRACT_VERSION = "aidd.research.tox21.api.v1"
ALL_TASKS = (*ENDPOINT_IDS, *TOX21_ENDPOINTS)
ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT = {"radius": 2, "bits": 2048, "includeChirality": True}


class MultitaskArtifactError(ModelAPIError):
    def __init__(self, code, message, status_code=503):
        super().__init__(code, message, status_code)


def _fail(message, code="MULTITASK_INVALID_ARTIFACT"):
    raise MultitaskArtifactError(code, message)


def _digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json(path, limit=8 * 1024 * 1024):
    if not path.is_file():
        _fail("研究模型工件缺失；请先完成真实多任务训练。", "MULTITASK_MODEL_NOT_READY")
    if path.stat().st_size > limit:
        _fail("研究模型元数据超过大小限制。")
    def reject_nonfinite(value):
        raise ValueError("Non-finite JSON")
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_nonfinite)
    except (UnicodeError, ValueError):
        _fail("研究模型元数据格式无效。")


def _artifact_path(directory, record):
    if not isinstance(record, dict) or not isinstance(record.get("file"), str) or not _is_digest(record.get("sha256")):
        _fail("研究模型工件文件名或 SHA256 记录缺失。")
    relative = Path(record["file"])
    target = (directory / relative).resolve()
    if relative.is_absolute() or not target.is_relative_to(directory) or target == directory:
        _fail("工件路径必须位于独立模型目录内。")
    if not target.is_file():
        _fail("研究模型工件不完整。", "MULTITASK_MODEL_NOT_READY")
    if _digest(target) != record["sha256"]:
        _fail("研究模型工件 SHA256 校验失败。", "MULTITASK_CHECKSUM_MISMATCH")
    return target


def _is_digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _mapping(value, name):
    if not isinstance(value, dict):
        _fail(f"研究模型 {name} 元数据必须是对象。")
    return value


class MultitaskModelClient:
    def __init__(self, model_dir=None, *, encoder_dir=None, device="cpu", allow_research=False):
        self.model_dir = Path(model_dir).resolve() if model_dir else None
        self.encoder_dir = Path(encoder_dir).resolve() if encoder_dir else None
        self.device, self.allow_research = device, allow_research
        self._lock = threading.RLock()
        self._loaded = None
        self._signature_loaded = None

    @classmethod
    def from_env(cls, env=None):
        values = os.environ if env is None else env
        return cls(values.get("TOXNET_MULTITASK_MODEL_DIR") or None,
                   encoder_dir=values.get("TOXNET_MULTITASK_ENCODER_DIR") or None,
                   device=values.get("TOXNET_MULTITASK_DEVICE", "cpu"),
                   allow_research=str(values.get("TOXNET_MULTITASK_ENABLE", "false")).strip().lower() == "true")

    def _signature(self):
        directories = [path for path in (self.model_dir, self.encoder_dir) if path and path.is_dir()]
        return tuple(sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                            for directory in directories for p in directory.rglob("*") if p.is_file()))

    def _ensure_loaded(self):
        with self._lock:
            if not self.allow_research:
                _fail("Tox21 研究服务默认关闭；需显式启用 TOXNET_MULTITASK_ENABLE=true。", "MULTITASK_RESEARCH_DISABLED")
            if self.model_dir is None:
                _fail("尚未配置真实 Tox21 研究模型目录。", "MULTITASK_MODEL_UNCONFIGURED")
            signature = self._signature()
            if self._loaded is not None and signature == self._signature_loaded:
                return self._loaded
            self._loaded = None
            manifest = _mapping(_json(self.model_dir / "manifest.json"), "manifest")
            if manifest.get("schema") != ARTIFACT_SCHEMA or manifest.get("taskContractVersion") != TASK_CONTRACT_VERSION:
                _fail("不支持的多任务工件 schema 或任务合同。")
            if manifest.get("status") not in ("trained-completed", "trained-ablation"):
                _fail("多任务模型未完成真实训练与评价。", "MULTITASK_MODEL_NOT_READY")
            if manifest.get("formalAvailability") is not False:
                _fail("开发评价模型不能声明正式可用。")
            if manifest.get("chemistryVersion") != CHEMISTRY_VERSION or manifest.get("rdkitVersion") != rdBase.rdkitVersion:
                _fail("多任务化学引擎版本与训练特征不一致。")
            tasks = manifest.get("tasks")
            if tasks not in (list(ENDPOINT_IDS), list(ALL_TASKS)):
                _fail("不支持的多任务列表或顺序。", "MULTITASK_TASKS_UNSUPPORTED")
            task_count = len(tasks)
            data_module = importlib.import_module("backend.multitask_data")
            contracts = data_module.task_contracts()
            if manifest.get("taskContracts") != {task: contracts[task] for task in tasks}:
                _fail("任务正类语义或真实标签来源合同不匹配。")
            for name in ("modelName", "modelVersion"):
                if not isinstance(manifest.get(name), str) or not 0 < len(manifest[name].strip()) <= 200:
                    _fail("研究模型名称或版本缺失。")
            training = _mapping(manifest.get("training"), "training")
            expected_training_status = "completed" if manifest["status"] == "trained-completed" else "completed-ablation"
            if (training.get("status") != expected_training_status or training.get("optimizationSplit") != "train"
                or training.get("selectionSplit") != "validation" or training.get("testUsedForSelection") is not False
                or training.get("legacyHeldoutAndKarimUsedForSelection") is not False
                or training.get("featureTransformFitSplit") != "train"):
                _fail("多任务训练记录不符合独立开发协议。")
            endpoint_status = _mapping(training.get("endpointStatus"), "training.endpointStatus")
            if set(endpoint_status) != set(tasks) or any(state not in ("trained", "disabled") for state in endpoint_status.values()) or any(endpoint_status.get(task) != "trained" for task in ENDPOINT_IDS):
                _fail("多任务监督训练状态无效，原四端点必须实际训练。", "MULTITASK_MODEL_NOT_READY")
            weights = _mapping(training.get("taskWeights"), "training.taskWeights")
            if set(weights) != set(tasks) or any(not _finite(value) or value < 0 for value in weights.values()):
                _fail("多任务损失权重必须完整、有限且非负。")
            if any((endpoint_status[task] == "disabled") != (weights[task] == 0) for task in tasks):
                _fail("禁用任务状态必须与零损失权重一致。")
            if (manifest["status"] == "trained-completed") != all(state == "trained" for state in endpoint_status.values()):
                _fail("训练完成/消融状态与实际任务训练记录不一致。")
            class_counts = _mapping(training.get("classCounts"), "training.classCounts")
            if set(class_counts) != set(tasks):
                _fail("多任务 train 类别计数必须覆盖全部任务。")
            for task, counts in class_counts.items():
                counts = _mapping(counts, "training class count")
                if any(type(counts.get(key)) is not int or counts[key] < 0 for key in ("positive", "negative", "count")) or counts["positive"] + counts["negative"] != counts["count"]:
                    _fail("多任务 train 阳性、阴性和总计数不一致。")
                if endpoint_status[task] == "trained" and not (counts["positive"] > 0 and counts["negative"] > 0):
                    _fail("训练任务缺少真实双类观测，不能提供学习输出。", "MULTITASK_MODEL_NOT_READY")
            splits = _mapping(training.get("splits"), "training.splits")
            if any(type(splits.get(key)) is not int or splits[key] <= 0 for key in ("train", "validation")):
                _fail("多任务训练记录需要非空 train / validation。")
            source_hashes = _mapping(training.get("sourceHashes"), "training.sourceHashes")
            if not source_hashes or any(not isinstance(path, str) or not _is_digest(checksum) for path, checksum in source_hashes.items()):
                _fail("多任务训练源码 SHA256 记录缺失或无效。")
            dataset = _mapping(manifest.get("dataset"), "dataset")
            if not isinstance(dataset.get("datasetVersion"), str) or not dataset["datasetVersion"] or not all(_is_digest(dataset.get(key)) for key in ("manifestSha256", "splitSha256")):
                _fail("独立多任务数据版本及 manifest / split SHA256 缺失。")
            if dataset.get("trainingPermission") != data_module.training_permission():
                _fail("多任务来源训练许可与标签使用合同不匹配。")
            source_records = dataset.get("sourceHashes")
            if not isinstance(source_records, list) or not source_records or any(not isinstance(record, dict) or not _is_digest(record.get("sha256")) or not isinstance(record.get("role"), str) for record in source_records):
                _fail("真实输入来源文件的角色和 SHA256 记录缺失。")
            try:
                # This existing helper checks roles, pinned hashes and explicit
                # usage only; it resolves provenance paths without reading raw
                # source data. Deployment need not redistribute the datasets.
                roles = data_module._multitask_source_roles(source_records, self.model_dir)
                expected = {"audit-only-tox21-clean-structures": data_module.TOX21_PROCESSED_SHA256}
                previous = {"previously-exposed-v3-development-manifest", "previously-exposed-v3-development-split"}
                present = previous & set(roles)
                if present and present != previous:
                    raise ValueError("Incomplete exposed-development ledger")
                if present:
                    expected.update({"previously-exposed-v3-development-manifest": data_module.PREVIOUS_DEVELOPMENT_MANIFEST_SHA256,
                                     "previously-exposed-v3-development-split": data_module.PREVIOUS_DEVELOPMENT_SPLIT_SHA256})
                for role, checksum in expected.items():
                    matches = [row for row in source_records if row["role"] == role]
                    if len(matches) != 1 or matches[0]["sha256"] != checksum:
                        raise ValueError("Pinned source ledger changed")
            except (ValueError, KeyError, TypeError):
                _fail("多任务固定来源角色、SHA256 或实际使用合同不匹配。")
            architecture = _mapping(manifest.get("architecture"), "architecture")
            if architecture.get("id") != ARCHITECTURE_ID or architecture.get("taskOrder") != tasks or architecture.get("outputDim") != task_count:
                _fail("不支持的多任务架构或任务输出顺序。")
            branches = architecture.get("featureBranches")
            if branches not in (["molformer"], ["morgan"]):
                _fail("不支持的多任务特征分支。")
            if "morgan" in branches and architecture.get("fingerprint") != FINGERPRINT:
                _fail("不支持的 Morgan 特征合同。")
            calibration, applicability = _mapping(manifest.get("calibration"), "calibration"), _mapping(manifest.get("applicability"), "applicability")
            if calibration.get("status") != "not-fitted" or applicability.get("validated") is not False:
                _fail("本研究服务不接受未经验证的校准或适用域声明。")
            artifacts = _mapping(manifest.get("artifacts"), "artifacts")
            required = {"weights", "preprocessing", "evaluation", "trainingHistory", "validationPredictions", "trainingReferences"}
            if not required.issubset(artifacts):
                _fail("完整多任务工件、预测和训练记录缺失。", "MULTITASK_MODEL_NOT_READY")
            paths = {name: _artifact_path(self.model_dir, record) for name, record in artifacts.items()}
            evaluation = _mapping(_json(paths["evaluation"]), "evaluation")
            metrics = _mapping(evaluation.get("endpoints"), "evaluation.endpoints")
            if (evaluation.get("schema") != "aidd.toxnet.multitask-evaluation.v1" or evaluation.get("split") != "validation"
                or evaluation.get("datasetVersion") != dataset["datasetVersion"] or evaluation.get("splitSha256") != dataset["splitSha256"]
                or set(metrics) != set(tasks)):
                _fail("多任务评价必须记录对应任务列表的开发 validation。")
            for task, value in metrics.items():
                value = _mapping(value, "evaluation metric")
                if type(value.get("count")) is not int or value["count"] < 0 or type(value.get("positives")) is not int or not 0 <= value["positives"] <= value["count"]:
                    _fail("多任务评价样本和阳性计数无效。")
                if type(value.get("negatives")) is not int or value["negatives"] < 0 or value["negatives"] + value["positives"] != value["count"]:
                    _fail("多任务 validation 阳性、阴性和总计数不一致。")
                for key in ("auroc", "averagePrecision", "brier", "ece10Bins"):
                    if value.get(key) is not None and (not _finite(value[key]) or not 0 <= value[key] <= 1):
                        _fail("多任务评价指标无效。")
                if value["count"] in (0, value["positives"]) or value["positives"] == 0:
                    if value.get("auroc") is not None or value.get("averagePrecision") is not None:
                        _fail("缺失或单类 validation 不能声称 AUROC / AP。")
                if endpoint_status[task] == "disabled" and any(value.get(key) is not None for key in ("auroc", "averagePrecision", "brier", "ece10Bins")):
                    _fail("禁用任务不能声明模型评价指标。")
                if endpoint_status[task] == "trained" and 0 < value["positives"] < value["count"] and any(value.get(key) is None for key in ("auroc", "averagePrecision", "brier", "ece10Bins")):
                    _fail("可评价双类任务缺少真实计算指标。")
            if not _json(paths["trainingHistory"]):
                _fail("多任务真实训练历史为空。")
            np = importlib.import_module("numpy")
            if self.device not in ("cpu", "cuda"):
                _fail("研究设备仅支持 cpu 或 cuda。")
            if self.device == "cuda" and not importlib.import_module("torch").cuda.is_available():
                _fail("CUDA 不可用；请使用 CPU 或核验 PyTorch / 驱动。", "MULTITASK_CUDA_UNAVAILABLE")
            width = architecture.get("inputDim")
            expected_width = 768 if branches == ["molformer"] else 2048
            if width != expected_width:
                _fail("多任务输入特征维度与分支不一致。")
            with np.load(paths["preprocessing"], allow_pickle=False) as values:
                if set(values.files) != {"mean", "scale"}:
                    _fail("不支持的多任务预处理工件。")
                mean, scale = values["mean"].copy(), values["scale"].copy()
            if any(value.shape != (width,) or value.dtype.kind not in "fi" or not np.isfinite(value).all() for value in (mean, scale)) or not (scale > 0).all():
                _fail("多任务预处理参数维度或数值无效。")
            encoder = None
            if branches == ["molformer"]:
                if self.encoder_dir is None:
                    _fail("缺少本地已核验 MoLFormer 编码器目录。", "MULTITASK_ENCODER_MISSING")
                module = importlib.import_module("backend.pretrained_encoder")
                encoder = module.PretrainedEncoder(self.encoder_dir, device=self.device)
                stored = _mapping(manifest.get("encoder"), "encoder")
                for key in ("source", "revision", "weightsSha256", "dimension", "chemistryVersion", "rdkitVersion", "encoderVersion", "precision",
                            "files", "pooling", "maxTokens", "truncation", "deterministicEval", "frozenParameterCount", "trainableParameterCount"):
                    if stored.get(key) != encoder.provenance.get(key):
                        _fail("多任务预训练编码器版本或完整特征合同不匹配。", "MULTITASK_ENCODER_MISMATCH")
                if manifest.get("frozenEncoderParameterCount") != encoder.provenance["frozenParameterCount"]:
                    _fail("多任务冻结编码器参数数与实际权重不一致。")
            elif manifest.get("encoder") is not None or manifest.get("frozenEncoderParameterCount") != 0:
                _fail("Morgan 模型不得声称使用预训练编码器。")
            module = importlib.import_module("backend.multitask_experiments")
            head_type, head, torch = architecture.get("headType"), None, None
            if head_type == "logistic-regression" and branches == ["morgan"]:
                with np.load(paths["weights"], allow_pickle=False) as values:
                    if set(values.files) != {"coefficients", "intercepts"}:
                        _fail("不支持的多任务 LR 权重工件。")
                    coefficients, intercepts = values["coefficients"].copy(), values["intercepts"].copy()
                if coefficients.shape != (task_count, width) or intercepts.shape != (task_count,) or not all(value.dtype.kind in "fi" and np.isfinite(value).all() for value in (coefficients, intercepts)):
                    _fail("多任务 LR 权重维度或数值无效。")
                head = {"coefficients": coefficients, "intercepts": intercepts}
                count = coefficients.size + intercepts.size
                trainable_count = sum(width + 1 for task in tasks if endpoint_status[task] == "trained")
            elif head_type == "shared-mlp" and branches == ["molformer"]:
                hidden, dropout = architecture.get("hiddenSize"), architecture.get("dropout")
                if type(hidden) is not int or not 1 <= hidden <= 1024 or not _finite(dropout) or not 0 <= dropout < 1:
                    _fail("多任务 MLP 隐藏维度或 dropout 无效。")
                torch = importlib.import_module("torch")
                head = module.MultiTaskMLP(width, task_count, hidden_size=hidden, dropout=dropout)
                tensors = torch.load(paths["weights"], map_location="cpu", weights_only=True)
                if not isinstance(tensors, dict) or not tensors or not all(isinstance(value, torch.Tensor) and bool(torch.isfinite(value).all()) for value in tensors.values()):
                    _fail("多任务 MLP 权重张量无效。")
                head.load_state_dict(tensors, strict=True)
                for index, task in enumerate(tasks):
                    head.heads[index].requires_grad_(endpoint_status[task] == "trained")
                trainable_count = sum(value.numel() for value in head.parameters() if value.requires_grad)
                head.to(self.device).eval().requires_grad_(False)
                count = sum(value.numel() for value in head.parameters())
            else:
                _fail("不支持的多任务 head / 特征分支组合。")
            if manifest.get("parameterCount") != count or manifest.get("trainableParameterCount") != trainable_count:
                _fail("多任务 head 声明参数数与实际权重不一致。")
            self._loaded = {"manifest": manifest, "manifestSha256": _digest(self.model_dir / "manifest.json"),
                            "paths": paths, "np": np, "mean": mean, "scale": scale, "encoder": encoder,
                            "head": head, "module": module, "parameterCount": count, "trainableParameterCount": trainable_count}
            self._signature_loaded = self._signature()
            return self._loaded

    @staticmethod
    def _require_tox21(loaded):
        manifest = loaded["manifest"]
        states = manifest["training"]["endpointStatus"]
        if manifest["tasks"] != list(ALL_TASKS) or any(states.get(task) != "trained" for task in TOX21_ENDPOINTS):
            _fail("Tox21 接口要求全部十二通路实际完成监督训练；A4 或禁用通路的消融模型不可用。", "MULTITASK_TASKS_UNSUPPORTED")

    def get_model_info(self):
        """General research-head availability, including A4 and loss-zero ablation.

        This is intentionally separate from get_status(), which reports Tox21
        service availability. Disabled task heads never produce public scores.
        """
        try:
            loaded = self._ensure_loaded()
            manifest = loaded["manifest"]
            return {"configured": True, "status": "research-ready", "formalAvailability": False,
                    "modelName": manifest["modelName"], "modelVersion": manifest["modelVersion"],
                    "version": manifest["modelVersion"], "tasks": list(manifest["tasks"]),
                    "endpointStatus": copy.deepcopy(manifest["training"]["endpointStatus"]),
                    "architecture": copy.deepcopy(manifest["architecture"]), "parameterCount": loaded["parameterCount"],
                    "trainableParameterCount": loaded["trainableParameterCount"],
                    "frozenEncoderParameterCount": manifest["frozenEncoderParameterCount"],
                    "dataset": copy.deepcopy(manifest["dataset"]), "encoder": copy.deepcopy(manifest.get("encoder")),
                    "evaluation": _json(loaded["paths"]["evaluation"]), "taskContractVersion": TASK_CONTRACT_VERSION,
                    "manifestSha256": loaded["manifestSha256"], "artifactSha256": loaded["manifestSha256"],
                    "weightsSha256": manifest["artifacts"]["weights"]["sha256"]}
        except ModelAPIError as error:
            return {"configured": False, "status": "unavailable", "formalAvailability": False,
                    "errorCode": error.code, "message": error.public_message}
        except (OSError, ValueError, ImportError, RuntimeError, TypeError, KeyError, AttributeError, UnpicklingError, BadZipFile, EOFError):
            return {"configured": False, "status": "unavailable", "formalAvailability": False,
                    "errorCode": "MULTITASK_MODEL_LOAD_ERROR", "message": "研究模型无法加载；请核验工件、依赖、权重和设备。"}

    def get_status(self):
        base = {"contractVersion": SERVICE_CONTRACT_VERSION, "configured": False, "status": "unavailable",
                "formalAvailability": False, "researchOnly": True, "tasks": list(TOX21_ENDPOINTS),
                "semantics": "Tox21 assay activity; not clinical toxicity or overall risk"}
        try:
            loaded = self._ensure_loaded()
            self._require_tox21(loaded)
            manifest = loaded["manifest"]
            return {**base, "configured": True, "status": "research-ready", "modelName": manifest["modelName"],
                    "modelVersion": manifest["modelVersion"], "parameterCount": loaded["parameterCount"],
                    "frozenEncoderParameterCount": manifest["frozenEncoderParameterCount"], "taskContractVersion": TASK_CONTRACT_VERSION}
        except ModelAPIError as error:
            return {**base, "errorCode": error.code, "message": error.public_message}
        except (OSError, ValueError, ImportError, RuntimeError, TypeError, KeyError, AttributeError, UnpicklingError, BadZipFile, EOFError):
            return {**base, "errorCode": "MULTITASK_MODEL_LOAD_ERROR", "message": "研究模型无法加载；请核验工件、依赖、权重和设备。"}

    def _predict_vectors(self, smiles, *, tox21_only=False):
        try:
            canonical = canonicalize_smiles(smiles)
        except ValueError as error:
            raise MultitaskArtifactError("INVALID_SMILES", str(error), 422) from None
        try:
            with self._lock:
                loaded = self._ensure_loaded()
                if tox21_only:
                    self._require_tox21(loaded)
                manifest, np = loaded["manifest"], loaded["np"]
                if loaded["encoder"] is not None:
                    module = importlib.import_module("backend.pretrained_encoder")
                    try:
                        module.validate_encoder_smiles(canonical)
                    except module.EncoderError as error:
                        raise MultitaskArtifactError("MULTITASK_INPUT_UNSUPPORTED", str(error), 422) from None
                    features = loaded["encoder"].encode([canonical], batch_size=1)
                else:
                    features = loaded["module"].morgan_features([canonical])
                features = ((features - loaded["mean"]) / loaded["scale"]).astype(np.float32)
                scores = loaded["module"].predict_head(loaded["head"], features, device=self.device, batch_size=1)
                if scores.shape != (1, len(manifest["tasks"])) or not np.isfinite(scores).all() or not ((scores >= 0) & (scores <= 1)).all():
                    _fail("多任务模型输出无效。")
                return canonical, scores, loaded
        except ModelAPIError:
            raise
        except (OSError, ValueError, ImportError, RuntimeError, TypeError, KeyError, AttributeError, UnpicklingError, BadZipFile, EOFError):
            raise MultitaskArtifactError("MULTITASK_INFERENCE_UNAVAILABLE", "真实 Tox21 研究推理不可用；请核验工件、依赖、权重和设备。") from None

    @staticmethod
    def _provenance(loaded):
        manifest = loaded["manifest"]
        return {"modelName": manifest["modelName"], "modelVersion": manifest["modelVersion"],
                "artifactSchema": ARTIFACT_SCHEMA, "taskContractVersion": TASK_CONTRACT_VERSION,
                "manifestSha256": loaded["manifestSha256"], "weightsSha256": manifest["artifacts"]["weights"]["sha256"],
                "datasetVersion": manifest["dataset"]["datasetVersion"], "dataManifestSha256": manifest["dataset"]["manifestSha256"],
                "splitSha256": manifest["dataset"]["splitSha256"], "sourceHashes": copy.deepcopy(manifest["training"]["sourceHashes"]),
                "parameterCount": loaded["parameterCount"], "frozenEncoderParameterCount": manifest["frozenEncoderParameterCount"],
                "trainableParameterCount": loaded["trainableParameterCount"],
                "encoder": copy.deepcopy(manifest.get("encoder")), "formalAvailability": False,
                "endpointStatus": copy.deepcopy(manifest["training"]["endpointStatus"]),
                "researchOnly": True, "evaluationType": "development-validation; not blind or prospective"}

    def predict_all(self, smiles):
        canonical, scores, loaded = self._predict_vectors(smiles)
        manifest = loaded["manifest"]
        return {"canonicalSmiles": canonical, "structureId": hashlib.sha256(canonical.encode()).hexdigest(),
                "endpoints": [{"id": task, "score": float(scores[0, index])} for index, task in enumerate(manifest["tasks"])
                              if manifest["training"]["endpointStatus"][task] == "trained"],
                "scoreKind": "uncalibrated_score", "calibration": {"status": "not-fitted"},
                "applicability": {"status": "unvalidated", "validated": False}, "provenance": self._provenance(loaded)}

    def predict_tox21(self, smiles):
        canonical, scores, loaded = self._predict_vectors(smiles, tox21_only=True)
        return {"contractVersion": SERVICE_CONTRACT_VERSION, "canonicalSmiles": canonical,
                "structureId": hashlib.sha256(canonical.encode()).hexdigest(),
                "pathways": [{"id": task, "assay": assay, "score": float(scores[0, 4 + index]),
                              "positiveClass": "assay active"} for index, (task, assay) in enumerate(zip(TOX21_ENDPOINTS, TOX21_ASSAYS))],
                "semantics": {"taskType": "assay-activity", "scoreKind": "uncalibrated_score",
                              "clinicalToxicityProbability": False, "aggregateRiskProvided": False,
                              "meaning": "Separate Tox21 pathway assay activity outputs; not clinical toxicity probabilities."},
                "calibration": {"status": "not-fitted"}, "applicability": {"status": "unvalidated", "validated": False},
                "provenance": self._provenance(loaded)}
