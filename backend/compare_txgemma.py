"""Opt-in TxGemma worker adapter. No downloads, remote code, or cloud fallback."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import time

from backend.compare_tasks import (BBB_TEMPLATE, TXGEMMA_MODEL_ID, TXGEMMA_REVISION,
                                  get_task, normalized_template, parse_task_output, task_registry)
from backend.compare_adapters import CompareAdapterError, _CleanupLifecycle

SNAPSHOT_SCHEMA = "aidd.txgemma.local-snapshot-audit.v1"
TERMS_URL = "https://developers.google.com/health-ai-developer-foundations/terms"
REVIEWED_TRANSFORMERS_VERSION = "4.57.6"
# Unauthenticated official Hub metadata, pinned 2026-10-13. The Hub hides LFS
# SHA256 until access is granted; an operator-supplied authenticated source index
# is therefore required by the local audit. Never invent missing weight hashes.
OFFICIAL_FILES = {
    ".gitattributes": (1570, "52373fe24473b1aa44333d318f578ae6bf04b49b", False),
    "README.md": (12393, "99895fe4e842d3ad18817dba3e417c6090b928c1", False),
    "config.json": (818, "2693147134bfe819656f31417fa58a196b87598a", False),
    "generation_config.json": (168, "f4d8698503667257881387c38cde9e77efe84c9b", False),
    "model-00001-of-00003.safetensors": (4992576136, "5207c6e3036432b8b2f6f698dc78e03ebaa17937", True),
    "model-00002-of-00003.safetensors": (4983443424, "1a85cd8adff84185d0e3c3da14fd0fc12990eed0", True),
    "model-00003-of-00003.safetensors": (481381384, "f0a3990502dcf887765564da851e05c01d4ec50f", True),
    "model.safetensors.index.json": (24224, "05c7984e8eee8ced110a5b0c9efe23e53ce379b8", False),
    "special_tokens_map.json": (636, "8d6368f7e735fbe4781bf6e956b7c6ad0586df80", False),
    "tdc_prompts.json": (768023, "a87e41f1aedf74a6068a8825a7dd52b9c3492202", False),
    "tokenizer.json": (17525357, "af0eac5c0056f83b8f3fcdb79165f8847111c305", True),
    "tokenizer.model": (4241003, "796efe9ab515c15e146ce7588e6d7b9b8134dbf8", True),
    "tokenizer_config.json": (46379, "2357fc1a512dc3881eb99b9df3ef8e97ef7ffd51", False),
}


class TxGemmaAdapterError(CompareAdapterError):
    pass


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git_blob_oid(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def lfs_pointer_oid(sha256, size):
    pointer = ("version https://git-lfs.github.com/spec/v1\n"
               f"oid sha256:{sha256}\nsize {size}\n").encode()
    return git_blob_oid(pointer)


def _worker_metrics(value, device, dtype):
    allowed = {"loadSeconds", "generationSeconds", "inputTokens", "outputTokens", "tokensPerSecond",
               "device", "dtype", "gpu", "cost", "costStatus"}
    if not isinstance(value, dict) or not set(value).issubset(allowed):
        raise ValueError("Unknown worker metrics.")
    for name in ("loadSeconds", "generationSeconds", "tokensPerSecond"):
        if name in value and (type(value[name]) not in (float, int) or not math.isfinite(value[name]) or value[name] < 0):
            raise ValueError("Non-finite worker performance measurement.")
    for name, upper in (("inputTokens", 2048), ("outputTokens", 8)):
        if type(value.get(name)) is not int or not 0 <= value[name] <= upper:
            raise ValueError("Worker token count is outside contract.")
    if value.get("device", device) != device or value.get("dtype", dtype) != dtype or value.get("cost") is not None:
        raise ValueError("Worker device, dtype, or cost does not match local execution.")
    if "costStatus" in value and value["costStatus"] != "local-no-provider-billing":
        raise ValueError("Unexpected local cost status.")
    gpu = value.get("gpu")
    if gpu is not None:
        if device != "cuda" or not isinstance(gpu, dict) or set(gpu) != {"name", "peakAllocatedMiB", "peakReservedMiB", "measurementScope"}:
            raise ValueError("Unexpected GPU measurement scope.")
        if not isinstance(gpu["name"], str) or not 1 <= len(gpu["name"]) <= 128:
            raise ValueError("Invalid GPU name.")
        for name in ("peakAllocatedMiB", "peakReservedMiB"):
            if type(gpu[name]) not in (float, int) or not math.isfinite(gpu[name]) or gpu[name] < 0:
                raise ValueError("Invalid GPU measurement.")
        if gpu["measurementScope"] != "this-disposable-worker-PyTorch-only":
            raise ValueError("Invalid GPU measurement scope.")
    return copy.deepcopy(value)


def _json(path, max_bytes=2 * 1024 * 1024):
    target = Path(path)
    if not target.is_file() or target.stat().st_size > max_bytes:
        raise ValueError("Required local metadata is absent or too large.")
    return json.loads(target.read_text(encoding="utf-8"),
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))


def _safe_file(root, name):
    if not isinstance(name, str) or "\\" in name:
        raise ValueError("Snapshot paths must be normalized relative filenames.")
    relative = Path(name)
    path = (root / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(root) or path == root:
        raise ValueError("Snapshot path escapes its directory.")
    if not path.is_file():
        raise ValueError("Snapshot file is missing.")
    return path


def check_snapshot_identity(root):
    """Verify built-in Gemma2/tokenizer and the sole reviewed local task prompt."""
    config = _json(root / "config.json")
    tokenizer = _json(root / "tokenizer_config.json")
    if not isinstance(config, dict) or not isinstance(tokenizer, dict):
        raise ValueError("Snapshot config and tokenizer metadata must be JSON objects.")
    if config.get("model_type") != "gemma2" or config.get("architectures") != ["Gemma2ForCausalLM"]:
        raise ValueError("Snapshot does not declare the reviewed Gemma2 architecture.")
    if config.get("auto_map") or tokenizer.get("auto_map"):
        raise ValueError("Remote/custom architecture or tokenizer code is prohibited.")
    if tokenizer.get("tokenizer_class") not in ("GemmaTokenizer", "GemmaTokenizerFast"):
        raise ValueError("Unsupported TxGemma tokenizer identity.")
    prompts = _json(root / "tdc_prompts.json")
    prompt = prompts.get("BBB_Martins") if isinstance(prompts, dict) else None
    if not isinstance(prompt, str) or normalized_template(prompt) != normalized_template(BBB_TEMPLATE):
        raise ValueError("Local BBB_Martins prompt disagrees with the reviewed official notebook.")
    if prompt.count("{Drug SMILES}") != 1:
        raise ValueError("Task requires exactly one SMILES placeholder.")
    index = _json(root / "model.safetensors.index.json")
    weight_map = index.get("weight_map") if isinstance(index, dict) else None
    shards = {name for name in OFFICIAL_FILES if name.endswith(".safetensors")}
    if not isinstance(weight_map, dict) or set(weight_map.values()) != shards:
        raise ValueError("Weight index does not match all three official safetensors shards.")
    return prompt


def verify_snapshot_receipt(snapshot_dir, receipt_path):
    """Local operator audit, never an assertion of independent signed origin."""
    root = Path(snapshot_dir).resolve()
    receipt = _json(receipt_path)
    if (not isinstance(receipt, dict) or receipt.get("schema") != SNAPSHOT_SCHEMA or receipt.get("modelId") != TXGEMMA_MODEL_ID
            or receipt.get("revision") != TXGEMMA_REVISION or receipt.get("status") != "audited"
            or receipt.get("licenseAcceptedByOperator") is not True
            or receipt.get("formalAvailability") is not False):
        raise ValueError("Unreviewed revision, incomplete audit, or absent operator license acceptance.")
    if Path(receipt.get("snapshotDir", "")).resolve() != root:
        raise ValueError("Audit is bound to a different local snapshot directory.")
    records = receipt.get("files")
    if not isinstance(records, dict) or set(records) != set(OFFICIAL_FILES):
        raise ValueError("Audit does not cover every pinned model source file.")
    for name, (size, oid, is_lfs) in OFFICIAL_FILES.items():
        record = records[name]
        if not isinstance(record, dict):
            raise ValueError("Snapshot file record must be a JSON object.")
        path = _safe_file(root, name)
        digest = record.get("sha256") if isinstance(record, dict) else None
        if (record.get("size") != size or record.get("officialBlobOid") != oid or path.stat().st_size != size
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError("Snapshot file metadata does not match the pinned official source.")
        if file_sha256(path) != digest:
            raise ValueError("Snapshot SHA256 mismatch.")
        if is_lfs and lfs_pointer_oid(digest, size) != oid:
            raise ValueError("LFS SHA256 does not reconstruct the pinned official Git pointer.")
        if not is_lfs and git_blob_oid(path.read_bytes()) != oid:
            raise ValueError("Metadata bytes do not match the pinned official Git blob.")
    # Reject executable/pickle additions even if an otherwise complete receipt
    # was supplied. HF download metadata is neither loaded nor sent to clients.
    for path in root.rglob("*"):
        if path.is_file() and path.relative_to(root).as_posix() not in OFFICIAL_FILES:
            relative = path.relative_to(root).as_posix()
            if not relative.startswith(".cache/huggingface/") or path.suffix.lower() in (".py", ".bin", ".pt", ".pth", ".pkl", ".pickle"):
                raise ValueError("Unexpected model source file; remote code/pickle is prohibited.")
            _safe_file(root, relative)
    check_snapshot_identity(root)
    return receipt


class TxGemmaPredictAdapter(_CleanupLifecycle):
    id = "txgemma-2b-predict"

    def __init__(self, env=None):
        values = os.environ if env is None else env
        self.enabled = str(values.get("AIDD_TXGEMMA_ENABLE", "false")).lower() == "true"
        self.license_accepted = str(values.get("AIDD_TXGEMMA_LICENSE_ACCEPTED", "false")).lower() == "true"
        self.snapshot_dir = values.get("AIDD_TXGEMMA_SNAPSHOT_DIR", "")
        self.receipt_path = values.get("AIDD_TXGEMMA_AUDIT_RECEIPT", "")
        self.revision = values.get("AIDD_TXGEMMA_REVISION", TXGEMMA_REVISION)
        self.device = values.get("AIDD_TXGEMMA_DEVICE", "cpu")
        self.dtype = values.get("AIDD_TXGEMMA_DTYPE", "float32" if self.device == "cpu" else "float16")
        try:
            self.timeout = float(values.get("AIDD_TXGEMMA_TIMEOUT_SECONDS", "180"))
        except (ValueError, TypeError):
            self.timeout = float("nan")
        self._receipt = None
        self._signature = None
        self._last_metrics = None
        self._initialize_cleanup()
        self._spawning_task = None

    def _config_error(self):
        if not self.enabled:
            return "TXGEMMA_DISABLED", "TxGemma 研究适配器尚未显式启用。"
        if not self.license_accepted:
            return "TXGEMMA_LICENSE_REQUIRED", "须由用户亲自接受 Google HAI-DEF 条款并显式记录授权。"
        if self.revision != TXGEMMA_REVISION:
            return "TXGEMMA_UNSUPPORTED_REVISION", "仅支持经过审查的固定 TxGemma 修订。"
        if self.device not in ("cpu", "cuda") or self.dtype not in ("float32", "float16", "bfloat16"):
            return "TXGEMMA_INVALID_CONFIG", "TxGemma 设备或 dtype 配置不受支持。"
        if self.device == "cpu" and self.dtype != "float32":
            return "TXGEMMA_INVALID_CONFIG", "CPU 路径目前仅允许 float32；不自动更改设备或精度。"
        if not math.isfinite(self.timeout) or not 1 <= self.timeout <= 600:
            return "TXGEMMA_INVALID_CONFIG", "TxGemma 超时须为 1 到 600 秒。"
        if not self.snapshot_dir or not self.receipt_path:
            return "TXGEMMA_NOT_INSTALLED", "尚未配置用户合法下载并审计的本地 TxGemma 权重。"
        return None

    def _verify(self):
        error = self._config_error()
        if error:
            raise TxGemmaAdapterError(*error)
        try:
            root = Path(self.snapshot_dir).resolve()
            signature = (file_sha256(self.receipt_path), tuple(sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
                         for p in root.rglob("*") if p.is_file())))
            if self._receipt is None or signature != self._signature:
                self._receipt = verify_snapshot_receipt(root, self.receipt_path)
                self._signature = signature
            return self._receipt
        except (OSError, ValueError, TypeError, KeyError):
            raise TxGemmaAdapterError("TXGEMMA_AUDIT_FAILED", "本地 TxGemma 审计、任务模板或文件 SHA256 校验失败。") from None

    async def status(self):
        available, error = False, self._config_error()
        if self._cleanup_state == "quarantined" or self._closed:
            error = ("COMPARE_RESOURCE_QUARANTINED" if not self._closed else "COMPARE_RESOURCE_CLOSED",
                     "本地 TxGemma 清理未确认或适配器已关闭；需要检查并重启服务。")
        versions = {}
        if error is None:
            try:
                await asyncio.to_thread(self._verify)
                if not all(importlib.util.find_spec(name) for name in ("torch", "transformers", "safetensors")):
                    error = ("TXGEMMA_DEPENDENCIES_MISSING", "TxGemma 本地运行依赖缺失。")
                else:
                    versions = {name: importlib.metadata.version(name) for name in ("torch", "transformers", "safetensors")}
                    if versions["transformers"] != REVIEWED_TRANSFORMERS_VERSION:
                        error = ("TXGEMMA_DEPENDENCIES_UNREVIEWED", "Transformers 版本未审核；请使用 requirements-v3.txt 的固定依赖。")
                    elif self.device == "cuda":
                        import torch
                        if not torch.cuda.is_available():
                            error = ("TXGEMMA_CUDA_UNAVAILABLE", "明确配置 CUDA，但当前环境没有可用 CUDA；不会自动降级。")
                        else:
                            available = True
                    else:
                        available = True
            except TxGemmaAdapterError as exc:
                error = (exc.code, exc.public_message)
            except (ImportError, OSError, importlib.metadata.PackageNotFoundError):
                error = ("TXGEMMA_DEPENDENCIES_MISSING", "TxGemma 本地依赖无法加载。")
        return {"id": self.id, "available": available, "configured": bool(self.snapshot_dir and self.receipt_path),
                "actualModelId": TXGEMMA_MODEL_ID, "revision": self.revision,
                "device": self.device, "dtype": self.dtype, "researchOnly": True, "formalAvailability": False,
                "licenseStatus": "operator-accepted" if self.license_accepted else "not-accepted",
                "licenseUrl": TERMS_URL, "source": "https://huggingface.co/" + TXGEMMA_MODEL_ID,
                "reason": error[1] if error else "本地固定权重已审计；首次加载的容量和性能仍需实测。",
                "errorCode": error[0] if error else None, "specialistTasks": task_registry(),
                "capacityValidation": "measured-single-successful-run" if self._last_metrics else "not-measured",
                "lastMeasuredMetrics": copy.deepcopy(self._last_metrics), "dependencyVersions": versions,
                "cleanup": self.cleanup_status(),
                "reviewedTransformersVersion": REVIEWED_TRANSFORMERS_VERSION, "parameterCount": 2614341888,
                "publishedWeightBytes": 10457400944}

    async def execute(self, context):
        status = await self.status()
        if not status["available"]:
            raise TxGemmaAdapterError(status["errorCode"] or "TXGEMMA_UNAVAILABLE", status["reason"])
        request = context["request"]
        task_id = request.get("specialistTask") if isinstance(request, dict) else request.specialistTask
        try:
            task = get_task(task_id)
            if not task.supported:
                raise ValueError("unsupported")
        except ValueError:
            raise TxGemmaAdapterError("TXGEMMA_TASK_UNSUPPORTED", "所选 TxGemma 任务的官方合同尚未通过审核。") from None
        started = time.perf_counter()
        self._begin_execution()
        spawning = None
        payload = {"snapshotDir": self.snapshot_dir, "receiptPath": self.receipt_path,
                   "revision": self.revision, "device": self.device, "dtype": self.dtype,
                   "taskId": task.id, "canonicalSmiles": context["canonicalSmiles"]}
        try:
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-m", "backend.txgemma_worker", stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                cwd=str(Path(__file__).resolve().parents[1]),
                **({"creationflags": 0x08000000} if os.name == "nt" else {})))
            self._spawning_task = spawning
            process = await asyncio.shield(spawning)
            stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(payload).encode()), self.timeout)
            if len(stdout) > 128 * 1024:
                raise TxGemmaAdapterError("TXGEMMA_INVALID_OUTPUT", "TxGemma worker 输出超过限制。")
            response = json.loads(stdout)
            if not isinstance(response, dict):
                raise ValueError("Worker response must be a JSON object.")
            if process.returncode != 0 or response.get("status") != "completed":
                code = response.get("code", "TXGEMMA_WORKER_FAILED")
                allowed = {"TXGEMMA_OOM", "TXGEMMA_CUDA_UNAVAILABLE", "TXGEMMA_AUDIT_FAILED", "TXGEMMA_INPUT_TOO_LONG", "TXGEMMA_INVALID_OUTPUT", "TXGEMMA_LOAD_FAILED"}
                raise TxGemmaAdapterError(code if code in allowed else "TXGEMMA_WORKER_FAILED", "TxGemma 本地预测失败；没有替代输出或云端回退。")
            data = parse_task_output(task.id, response["answer"])
            if set(response) != {"status", "answer", "revision", "metrics"}:
                raise ValueError("Unexpected worker response fields.")
            metrics = _worker_metrics(response["metrics"], self.device, self.dtype)
            # Validate the child's facts again before admitting them into a run.
            if response.get("revision") != TXGEMMA_REVISION:
                raise TxGemmaAdapterError("TXGEMMA_INVALID_OUTPUT", "TxGemma worker 返回了不受支持的模型修订。")
            self._last_metrics = metrics
            return {"status": "completed", "data": {"tasks": [data], "structureId": context["structureId"]},
                    "metrics": {**metrics, "elapsedSeconds": time.perf_counter() - started},
                    "provenance": {"modelId": TXGEMMA_MODEL_ID, "revision": TXGEMMA_REVISION,
                                   "taskContract": data["taskContractVersion"], "templateSha256": task.template_sha256,
                                   "snapshotAuditSha256": file_sha256(self.receipt_path),
                                   "device": self.device, "dtype": self.dtype, "formalAvailability": False}}
        except asyncio.TimeoutError:
            raise TxGemmaAdapterError("TXGEMMA_TIMEOUT", "TxGemma 超时；已请求停止独立进程，请核对清理确认状态。") from None
        except (OSError, NotImplementedError):
            raise TxGemmaAdapterError("TXGEMMA_WORKER_FAILED", "无法创建或连接本地 TxGemma 独立进程。") from None
        except (UnicodeError, ValueError, KeyError, TypeError):
            raise TxGemmaAdapterError("TXGEMMA_INVALID_OUTPUT", "TxGemma worker 未返回合法的受控任务输出。") from None
        finally:
            self._start_cleanup(lambda: self._release_worker(spawning))
            await self.wait_cleanup()

    async def _release_worker(self, spawning):
        if spawning is None:
            return True
        try:
            process = await asyncio.shield(spawning)
        except (OSError, NotImplementedError):
            return True
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        return process.returncode is not None

    async def close(self):
        self._closed = True
        if self._cleanup_state == "running":
            self._start_cleanup(lambda: self._release_worker(self._spawning_task))
        await self.wait_cleanup()
