"""Opt-in, checksum-bound third-party TxGemma GGUF runtime for Ollama.

This runtime is distinct from the fixed official Transformers snapshot. The
quantizer did not publish an upstream HF commit, so it remains unknown. Status
uses metadata and local file verification only; it never loads/pulls a model.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import math
import os
from pathlib import Path
import re
import stat
from time import perf_counter

from backend.chemistry import canonicalize_smiles
from backend.compare_adapters import (CompareAdapterError, OllamaQwenAdapter,
                                     _duration, _error_evidence, _http_json,
                                     _json, _report_usage, validate_ollama_url)
from backend.compare_tasks import (TASK_CONTRACT, TXGEMMA_MODEL_ID, get_task,
                                  parse_task_output, task_registry)

GGUF_REPOSITORY = "matrixportalx/txgemma-2b-predict-GGUF"
GGUF_REVISION = "e93f94fc209e1b5971fa96582f346fc7a4d7bec9"
GGUF_FILENAME = "txgemma-2b-predict-q4_k_m.gguf"
GGUF_SHA256 = "d2631215ae8be65343b8a22820e2551947aee1d0cf00e9dad872f610a3cf2ace"
GGUF_SIZE = 1708582112
GGUF_SOURCE_URL = f"https://huggingface.co/{GGUF_REPOSITORY}/blob/{GGUF_REVISION}/{GGUF_FILENAME}"
GGUF_AUDIT_SCHEMA = "aidd.txgemma.ollama-gguf-audit.v1"
PARAMETER_COUNT = 2614341888
TERMS_URL = "https://developers.google.com/health-ai-developer-foundations/terms"
HEX64 = re.compile(r"[0-9a-f]{64}")
MODEL_NAME = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]{0,127}):([A-Za-z0-9][A-Za-z0-9_.-]{0,63})")
METADATA_LIMIT = 2 * 1024 * 1024
CONTEXT_LENGTH = 2048
MODEL_CONTEXT_LENGTH = 8192
TOKENIZER_IDENTITY = {
    "tokenizer.ggml.bos_token_id": 2, "tokenizer.ggml.eos_token_id": 1,
    "tokenizer.ggml.add_bos_token": True, "tokenizer.ggml.add_eos_token": False,
}


def _digest(value):
    return value.removeprefix("sha256:") if isinstance(value, str) else None


def _file_signature(path):
    current = path.stat()
    if not stat.S_ISREG(current.st_mode):
        raise ValueError("Model store entry must be a regular file.")
    return (current.st_dev, current.st_ino, current.st_size,
            current.st_mtime_ns, current.st_ctime_ns)


def _bounded_file(root, relative):
    """Only server-configured model-store paths; never API-provided paths."""
    target = root.joinpath(*relative).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError("Model store path escapes its configured root.")
    _file_signature(target)
    return target


def _read_metadata(path):
    if path.stat().st_size > METADATA_LIMIT:
        raise ValueError("Model metadata exceeds its bound.")
    return path.read_bytes()


def _hash_file(path):
    before = _file_signature(path)
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if _file_signature(path) != before:
        raise ValueError("Model store changed while hashing.")
    return checksum


def _config_identity(config):
    if (config.get("model_format") != "gguf"
            or config.get("model_family") != "gemma2"
            or config.get("file_type") != "Q4_K_M"):
        raise ValueError("Manifest config disagrees with the approved GGUF.")


def validate_gguf_provenance(provenance):
    """Admission check, not a substitute for adapter-side byte verification."""
    if not isinstance(provenance, dict):
        return False
    model = provenance.get("modelId")
    manifest = provenance.get("revision")
    return (
        isinstance(model, str) and MODEL_NAME.fullmatch(model) is not None
        and isinstance(manifest, str) and HEX64.fullmatch(manifest) is not None
        and provenance.get("runtime") == "ollama-gguf"
        and provenance.get("sourceKind") == "third-party-quantized"
        and provenance.get("sourceRepository") == GGUF_REPOSITORY
        and provenance.get("sourceRevision") == GGUF_REVISION
        and provenance.get("sourceFile") == GGUF_FILENAME
        and provenance.get("ggufSha256") == GGUF_SHA256
        and provenance.get("ggufSizeBytes") == GGUF_SIZE
        and provenance.get("baseModelId") == TXGEMMA_MODEL_ID
        and provenance.get("upstreamRevision") is None
        and provenance.get("upstreamRevisionStatus") == "unknown-not-published-by-quantizer"
        and provenance.get("licenseStatus") == "operator-accepted"
        and provenance.get("quantization") == "Q4_K_M"
        and provenance.get("parameterCount") == PARAMETER_COUNT
        and provenance.get("localPackageVerification") == "manifest-and-source-gguf-sha256"
        and provenance.get("localPackageVerified") is True
        and provenance.get("auditSchema") == GGUF_AUDIT_SCHEMA
        and provenance.get("formalAvailability") is False
    )


class OllamaTxGemmaAdapter(OllamaQwenAdapter):
    """Raw official BBB task prompting with no JSON grammar or chat template."""
    id = model_id = "txgemma-2b-predict"

    def __init__(self, env=None, *, transport=None):
        values = os.environ if env is None else env
        self.enabled = str(values.get("AIDD_TXGEMMA_ENABLE", "false")).lower() == "true"
        self.license_accepted = str(values.get("AIDD_TXGEMMA_LICENSE_ACCEPTED", "false")).lower() == "true"
        self.expected_digest = _digest(values.get("AIDD_TXGEMMA_OLLAMA_DIGEST"))
        self.models_root = Path(values.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models").expanduser().resolve()
        try:
            timeout = float(values.get("AIDD_TXGEMMA_TIMEOUT_SECONDS", "180"))
        except (ValueError, TypeError):
            timeout = float("nan")
        super().__init__(
            values.get("TOXNET_OLLAMA_URL", "http://127.0.0.1:11434"),
            values.get("AIDD_TXGEMMA_OLLAMA_MODEL", "txgemma-2b:latest"),
            timeout_seconds=timeout, transport=transport)
        self._audit_cache = None
        self._audit_lock = asyncio.Lock()
        self._last_success = None

    def _config_error(self):
        if not self.enabled:
            return "TXGEMMA_DISABLED", "Ollama TxGemma 研究运行时尚未显式启用。"
        if not self.license_accepted:
            return "TXGEMMA_LICENSE_REQUIRED", "须由操作者亲自接受模型条款并记录许可授权。"
        if (MODEL_NAME.fullmatch(self.requested_model) is None
                or not isinstance(self.expected_digest, str)
                or HEX64.fullmatch(self.expected_digest) is None):
            return "TXGEMMA_INVALID_CONFIG", "须配置确切本机模型 ID 与固定 Ollama manifest digest。"
        if not math.isfinite(self.timeout_seconds) or not 1 <= self.timeout_seconds <= 600:
            return "TXGEMMA_INVALID_CONFIG", "TxGemma 超时须为 1 到 600 秒。"
        return None

    def _audit_package(self, *, force=False):
        """Status caches by inode/size/mtime/ctime; executions rehash every blob.

        Pre/post-execution hashing catches ordinary replacement, including an
        unchanged tag. It does not secure a hostile same-user process altering
        bytes transiently; use an exclusively maintained trusted local store.
        """
        try:
            parts = MODEL_NAME.fullmatch(self.requested_model).groups()
            # Ollama 0.40+ uses manifests-v2/ollama.com. These are the only
            # supported layouts, even if a service supplies another path.
            locations = [
                self.models_root.joinpath("manifests", "registry.ollama.ai", "library", *parts),
                self.models_root.joinpath("manifests-v2", "ollama.com", "library", *parts),
            ]
            existing = [path for path in locations if path.exists()]
            if len(existing) != 1:
                raise ValueError("Missing or ambiguous model manifest.")
            manifest = _bounded_file(self.models_root, existing[0].relative_to(self.models_root).parts)
            manifest_raw = _read_metadata(manifest)
            if hashlib.sha256(manifest_raw).hexdigest() != self.expected_digest:
                raise ValueError("Manifest does not match the pinned local digest.")
            document = _json(manifest_raw)
            layers = document.get("layers")
            config_record = document.get("config")
            # This reviewed package has exactly one model layer. Additional
            # adapters/projectors/LoRA weights would change actual inference,
            # even if the original GGUF checksum still matches.
            if (document.get("schemaVersion") != 2 or not isinstance(layers, list)
                    or len(layers) != 1 or not isinstance(config_record, dict)
                    or config_record.get("mediaType") != "application/vnd.docker.container.image.v1+json"):
                raise ValueError("Invalid Ollama manifest.")
            records = [config_record, *layers]
            model_records = [record for record in layers if isinstance(record, dict)
                             and record.get("mediaType") == "application/vnd.ollama.image.model"]
            if (len(model_records) != 1 or _digest(model_records[0].get("digest")) != GGUF_SHA256
                    or model_records[0].get("size") != GGUF_SIZE):
                raise ValueError("Model layer is not the reviewed third-party GGUF.")
            paths = []
            for record in records:
                checksum = _digest(record.get("digest")) if isinstance(record, dict) else None
                size = record.get("size") if isinstance(record, dict) else None
                if (not isinstance(checksum, str) or HEX64.fullmatch(checksum) is None
                        or type(size) is not int or size < 1):
                    raise ValueError("Invalid local layer metadata.")
                target = _bounded_file(self.models_root, ("blobs", "sha256-" + checksum))
                if target.stat().st_size != size:
                    raise ValueError("Layer size mismatch.")
                if record is not model_records[0] and size > METADATA_LIMIT:
                    raise ValueError("Non-model layer exceeds metadata size bound.")
                paths.append((target, checksum))
            signature = (_file_signature(manifest),
                         tuple((str(path.relative_to(self.models_root)), _file_signature(path)) for path, _ in paths))
            if not force and self._audit_cache is not None and signature == self._audit_cache:
                return signature
            for target, checksum in paths:
                if _hash_file(target) != checksum:
                    raise ValueError("Local layer SHA256 mismatch.")
            config = _json(_read_metadata(paths[0][0]))
            _config_identity(config)
            after = (_file_signature(manifest),
                     tuple((str(path.relative_to(self.models_root)), _file_signature(path)) for path, _ in paths))
            if after != signature or hashlib.sha256(_read_metadata(manifest)).hexdigest() != self.expected_digest:
                raise ValueError("Package changed while auditing.")
            self._audit_cache = signature
            return signature
        except (OSError, ValueError, AttributeError, TypeError, CompareAdapterError):
            self._audit_cache = None
            raise CompareAdapterError(
                "TXGEMMA_GGUF_AUDIT_FAILED",
                "本机 Ollama manifest、模型来源文件 SHA256 或架构核验失败；未启动预测。") from None

    async def _package(self, *, force=False):
        async with self._audit_lock:
            return await asyncio.to_thread(self._audit_package, force=force)

    async def _tags(self, client, base):
        payload = await _http_json(client, "GET", base + "/api/tags", timeout=3)
        models = payload.get("models")
        if not isinstance(models, list):
            raise CompareAdapterError("OLLAMA_INVALID_TAGS", "Ollama 模型清单无效。")
        selected = next((record for record in models if isinstance(record, dict)
                         and self.requested_model in (record.get("name"), record.get("model"))), None)
        if selected is None:
            raise CompareAdapterError("TXGEMMA_NOT_INSTALLED", "本机 Ollama 没有配置的确切 TxGemma 模型；不会下载。")
        if _digest(selected.get("digest")) != self.expected_digest:
            raise CompareAdapterError("TXGEMMA_MODEL_CHANGED", "Ollama TxGemma 标签 digest 与固定配置不一致。")
        return selected

    async def _probe(self, client, base, *, force=False):
        await self._tags(client, base)
        signature = await self._package(force=force)
        shown = await _http_json(client, "POST", base + "/api/show",
                                 payload={"model": self.requested_model}, timeout=3)
        details, info = shown.get("details"), shown.get("model_info")
        if (not isinstance(details, dict) or not isinstance(info, dict)
                or details.get("format") != "gguf" or details.get("family") != "gemma2"
                or details.get("quantization_level") != "Q4_K_M"
                or info.get("general.architecture") != "gemma2"
                or str(info.get("general.finetune", "")).lower() != "predict"
                or info.get("general.parameter_count") != PARAMETER_COUNT
                or info.get("gemma2.context_length") != MODEL_CONTEXT_LENGTH
                or info.get("tokenizer.ggml.bos_token_id") != 2
                or info.get("tokenizer.ggml.eos_token_id") != 1
                or info.get("tokenizer.ggml.add_bos_token") is not True
                or info.get("tokenizer.ggml.add_eos_token") is not False):
            raise CompareAdapterError("TXGEMMA_GGUF_IDENTITY_FAILED",
                                      "Ollama 模型架构、Predict 身份、参数量或 Tokenizer 边界与审核来源不一致。")
        await self._tags(client, base)
        return signature

    def _provenance(self):
        return {
            "modelId": self.requested_model, "revision": self.expected_digest,
            "revisionSource": "verified-local-ollama-manifest-sha256",
            "runtime": "ollama-gguf", "sourceKind": "third-party-quantized",
            "sourceRepository": GGUF_REPOSITORY, "sourceRevision": GGUF_REVISION,
            "sourceFile": GGUF_FILENAME, "sourceUrl": GGUF_SOURCE_URL,
            "ggufSha256": GGUF_SHA256, "ggufSizeBytes": GGUF_SIZE,
            "baseModelId": TXGEMMA_MODEL_ID, "upstreamRevision": None,
            "upstreamRevisionStatus": "unknown-not-published-by-quantizer",
            "licenseStatus": "operator-accepted" if self.license_accepted else "not-accepted",
            "licenseUrl": TERMS_URL, "quantization": "Q4_K_M",
            "parameterCount": PARAMETER_COUNT, "formalAvailability": False,
            "device": "ollama-managed; unmeasured", "auditSchema": GGUF_AUDIT_SCHEMA,
            "localPackageVerification": "manifest-and-source-gguf-sha256",
            "sourceAuthenticity": "pinned-third-party-hub-file; upstream-revision-unverified",
            "taskContract": TASK_CONTRACT, "modelContextLength": MODEL_CONTEXT_LENGTH,
            "tokenizerIdentity": deepcopy(TOKENIZER_IDENTITY),
            "rawPrompt": True, "generationOptions": {
                "temperature": 0, "num_ctx": CONTEXT_LENGTH, "num_predict": 8, "stop": []},
        }

    async def status(self):
        base = {
            "available": False, "configured": False, "actualModelId": self.requested_model,
            "revision": self.expected_digest, "runtime": "ollama-gguf",
            "device": "ollama-managed; unmeasured", "tasks": task_registry(),
            "source": GGUF_SOURCE_URL, "formalAvailability": False,
            "licenseStatus": "operator-accepted" if self.license_accepted else "not-accepted",
            "licenseUrl": TERMS_URL, "cleanup": self.cleanup_status(),
            "capacityVerified": False, "capacityStatus": "not-measured",
            "sourceKind": "third-party-quantized", "sourceRevision": GGUF_REVISION,
            "ggufSha256": GGUF_SHA256, "upstreamRevision": None,
            "upstreamRevisionStatus": "unknown-not-published-by-quantizer",
            "errorCode": None, "reason": None,
            "realExecutionVerified": self._last_success is not None,
            "lastSuccessfulExecution": deepcopy(self._last_success),
        }
        error = self._config_error()
        if self._closed or not self.cleanup_safe:
            error = ("COMPARE_RESOURCE_CLOSED" if self._closed else "COMPARE_RESOURCE_QUARANTINED",
                     "本地适配器已关闭或清理未确认；不启动预测。")
        if error:
            return {**base, "errorCode": error[0], "reason": error[1]}
        try:
            url = validate_ollama_url(self.base_url)
            base["configured"] = True
            async with self._client(timeout=3) as client:
                await self._probe(client, url)
            return {**base, "available": True, "accessVerified": True,
                    "parameterCount": PARAMETER_COUNT, "quantization": "Q4_K_M",
                    "reason": ("本机来源与 Tokenizer 核验通过，已有实际 BBB 类别执行记录；不代表科学效能验证。"
                               if self._last_success is not None else
                               "本机服务、固定包 digest、GGUF 来源 SHA256 与 Predict 元数据已核验；尚需实际任务验收。")}
        except CompareAdapterError as exc:
            return {**base, "errorCode": exc.code, "reason": exc.public_message}

    def _task_context(self, context):
        try:
            request = context["request"]
            task_id = request.get("specialistTask") if isinstance(request, dict) else request.specialistTask
            task = get_task(task_id)
            if not task.supported:
                raise ValueError("Unsupported task.")
        except (ValueError, KeyError, AttributeError, TypeError):
            raise CompareAdapterError("TXGEMMA_TASK_UNSUPPORTED", "所选 TxGemma 任务未通过审核。") from None
        try:
            canonical = context["canonicalSmiles"]
            if (canonicalize_smiles(canonical) != canonical
                    or hashlib.sha256(canonical.encode()).hexdigest() != context["structureId"]):
                raise ValueError("Mismatched canonical identity.")
        except (ValueError, KeyError, TypeError):
            raise CompareAdapterError("TXGEMMA_INVALID_INPUT", "TxGemma 输入结构身份或合法性验证失败。") from None
        prompt = task.template.replace("{Drug SMILES}", canonical)
        # A conservative ASCII-byte bound avoids Ollama context truncation.
        # It intentionally rejects some long inputs that might tokenize shorter.
        if len(prompt.encode("utf-8")) + 32 + 8 > CONTEXT_LENGTH:
            raise CompareAdapterError("TXGEMMA_INPUT_TOO_LONG", "输入超过保守 token 上限；不截断分子或提示。")
        return task, prompt

    async def execute(self, context):
        error = self._config_error()
        if error:
            raise CompareAdapterError(*error)
        task, prompt = self._task_context(context)
        url = validate_ollama_url(self.base_url)
        provenance = {**self._provenance(), "templateSha256": task.template_sha256,
                      "promptMode": "official-task-template-raw-no-chat-no-grammar",
                      "localPackageVerified": False}
        metrics = {"promptTokens": None, "completionTokens": None, "totalTokens": None,
                   "cost": None, "costStatus": "local-compute-cost-not-estimated",
                   "httpRequestState": "not-attempted", "httpResponseReceived": False,
                   "gpu": {"measured": False, "peakAllocatedBytes": None, "peakReservedBytes": None}}
        pending_error = None
        started = perf_counter()
        self._begin_execution()
        self._cleanup_target, self._cleanup_confirmed = None, False
        try:
            async with self._client() as client:
                signature = await self._probe(client, url, force=True)
                provenance["localPackageVerified"] = True
                self._cleanup_target = (url, self.requested_model)
                metrics["httpRequestState"] = "attempted-unconfirmed"
                payload = await _http_json(
                    client, "POST", url + "/api/generate",
                    payload={"model": self.requested_model, "prompt": prompt, "raw": True,
                             "stream": False, "keep_alive": 0,
                             "options": {"temperature": 0, "num_predict": 8,
                                         "num_ctx": CONTEXT_LENGTH, "stop": []}},
                    metrics=metrics, usage_provider="ollama")
                _report_usage(payload, "ollama", metrics)
                metrics.update(
                    loadSeconds=_duration(payload.get("load_duration")),
                    generationSeconds=_duration(payload.get("eval_duration")),
                    totalDurationSeconds=_duration(payload.get("total_duration")))
                seconds, count = metrics["generationSeconds"], metrics["completionTokens"]
                metrics["tokensPerSecond"] = count / seconds if count is not None and seconds and seconds > 0 else None
                if payload.get("model") != self.requested_model:
                    raise CompareAdapterError("TXGEMMA_MODEL_CHANGED", "Ollama 返回了不同模型 ID。")
                if payload.get("done") is not True or payload.get("done_reason") != "stop":
                    raise CompareAdapterError("TXGEMMA_INVALID_OUTPUT", "TxGemma 未自然完成或输出已截断；保留真实计量，不发布类别。")
                if (metrics["promptTokens"] is not None and metrics["promptTokens"] > CONTEXT_LENGTH - 8
                        or metrics["completionTokens"] is not None and metrics["completionTokens"] > 8):
                    raise CompareAdapterError("TXGEMMA_INVALID_OUTPUT", "服务 Token 数超过受控任务长度。")
                try:
                    result = parse_task_output(task.id, payload.get("response"))
                except ValueError:
                    raise CompareAdapterError("TXGEMMA_INVALID_OUTPUT", "TxGemma 未返回官方任务允许的 A/B 类别；不推断概率或构造替代结果。") from None
                await self._tags(client, url)
                after = await self._package(force=True)
                if after != signature:
                    raise CompareAdapterError("TXGEMMA_MODEL_CHANGED", "模型包在预测期间变化；结果未发布。")
            return {"status": "completed", "data": {"tasks": [result], "structureId": context["structureId"]},
                    "metrics": metrics, "provenance": provenance}
        except BaseException as exc:
            pending_error = exc
            raise
        finally:
            self._start_cleanup(self._release_ollama)
            cleanup_error = None
            try:
                await self.wait_cleanup()
            except BaseException as exc:
                cleanup_error = exc
                raise
            finally:
                metrics.update(wallSeconds=perf_counter() - started, resourceCleanupVerified=self.cleanup_safe)
                if pending_error is None and cleanup_error is None and self.cleanup_safe:
                    self._last_success = {
                        "taskId": task.id, "class": result["class"],
                        **{key: metrics.get(key) for key in (
                            "wallSeconds", "promptTokens", "completionTokens",
                            "tokensPerSecond", "resourceCleanupVerified")},
                    }
                if pending_error is not None:
                    _error_evidence(pending_error, metrics, provenance)
                if cleanup_error is not None:
                    _error_evidence(cleanup_error, metrics, provenance)

    async def _unload(self, url, model_id):
        try:
            async with self._client(timeout=3) as client:
                result = await _http_json(client, "POST", url + "/api/generate",
                                         payload={"model": model_id, "keep_alive": 0})
                active = await _http_json(client, "GET", url + "/api/ps")
            models = active.get("models")
            return (result.get("done") is True and isinstance(models, list)
                    and not any(isinstance(record, dict) and (
                        model_id in (record.get("name"), record.get("model"))
                        or _digest(record.get("digest")) == self.expected_digest) for record in models))
        except Exception:
            return False
