"""Isolated molecular inference and independent report-only HTTP adapters.

No model is pulled or downloaded. Report providers never create trusted model
scores. Cloud calls require per-run permission and server-side credentials.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from collections import OrderedDict
import json
import math
import os
from pathlib import Path
import re
import sys
from time import monotonic, perf_counter
from urllib.parse import urlsplit

import httpx

from backend.model_client import ModelConfig
from backend.compare_resources import wait_tracked_cleanup

ROOT = Path(__file__).resolve().parents[1]
MAX_HTTP_BYTES = 2 * 1024 * 1024
MAX_WORKER_BYTES = 24 * 1024 * 1024
WORKER_SCHEMA = "aidd.compare.b16.worker.v1"


class CompareAdapterError(Exception):
    def __init__(self, code, public_message, *, metrics=None, provenance=None):
        super().__init__(public_message)
        self.code, self.public_message = code, public_message
        self.metrics = deepcopy(metrics or {})
        self.provenance = deepcopy(provenance or {})


class _CleanupLifecycle:
    """One bounded cleanup per execution, retained until safely drained."""
    def _initialize_cleanup(self):
        self.cleanup_safe, self._cleanup_state = True, "idle"
        self._cleanup_task, self._cleanup_error = None, None
        self._closed = False

    def _begin_execution(self):
        if self._closed:
            raise CompareAdapterError("COMPARE_RESOURCE_CLOSED", "本地适配器已经关闭；不会重新加载模型。")
        if self._cleanup_state in ("running", "cleaning"):
            raise CompareAdapterError("COMPARE_RESOURCE_BUSY", "同一本地适配器仍在执行或清理；没有并行加载。")
        if not self.cleanup_safe:
            raise CompareAdapterError("COMPARE_RESOURCE_QUARANTINED", "上次本地资源清理尚未确认；请检查本机服务后重启。")
        self.cleanup_safe, self._cleanup_state = False, "running"
        self._cleanup_task, self._cleanup_error = None, None

    def _start_cleanup(self, operation):
        if self._cleanup_task is None:
            self.cleanup_safe, self._cleanup_state = False, "cleaning"
            async def owned_cleanup():
                confirmed = False
                try:
                    # Real subprocess termination is normally immediate. Local
                    # HTTP cleanup gets its own bounded deadline as well.
                    confirmed = bool(await asyncio.wait_for(operation(), timeout=10))
                except (Exception, asyncio.CancelledError):
                    self._cleanup_error = "LOCAL_MODEL_CLEANUP_UNCONFIRMED"
                self.cleanup_safe = confirmed
                self._cleanup_state = "released" if confirmed else "quarantined"
                if not confirmed:
                    self._cleanup_error = "LOCAL_MODEL_CLEANUP_UNCONFIRMED"
                return confirmed
            self._cleanup_task = asyncio.create_task(owned_cleanup())
        return self._cleanup_task

    def cleanup_status(self):
        return {"state": self._cleanup_state, "pending": self._cleanup_task is not None and not self._cleanup_task.done(),
                "confirmed": self.cleanup_safe, "errorCode": self._cleanup_error}

    async def wait_cleanup(self):
        if self._cleanup_task is not None:
            return await wait_tracked_cleanup(self._cleanup_task)
        return self.cleanup_safe

    async def close(self):
        self._closed = True
        await self.wait_cleanup()


def _json(raw, code="COMPARE_INVALID_RESPONSE"):
    try:
        def reject_constant(value):
            raise ValueError("nonfinite")
        def unique_keys(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = item
            return result
        def finite_tree(item):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("nonfinite")
            if isinstance(item, dict):
                for child in item.values():
                    finite_tree(child)
            elif isinstance(item, list):
                for child in item:
                    finite_tree(child)
        value = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_keys)
        finite_tree(value)
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise CompareAdapterError(code, "模型未返回有效的有限 JSON 对象。") from None


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _duration(value):
    return value / 1e9 if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _report_usage(payload, provider, metrics):
    """Keep valid counters before report parsing; invalid counters stay null."""
    if provider == "ollama":
        fields = {"prompt_eval_count": "promptTokens", "eval_count": "completionTokens"}
        usage = payload
    else:
        usage = payload.get("usage")
        fields = {"prompt_tokens": "promptTokens", "completion_tokens": "completionTokens", "total_tokens": "totalTokens"}
        if usage is None:
            metrics["usageStatus"] = "not-returned"
            return
        if not isinstance(usage, dict):
            metrics.update(usageStatus="invalid", invalidUsageFields=["usage"])
            raise CompareAdapterError("COMPARE_INVALID_USAGE", "模型 Token 计量格式无效；未将无效计量当作零。", metrics=metrics)
    invalid, present = [], False
    for source, destination in fields.items():
        value = usage.get(source)
        if value is None:
            continue
        present = True
        if type(value) is int and 0 <= value <= 2**53 - 1:
            metrics[destination] = value
        else:
            metrics[destination] = None
            invalid.append(source)
    metrics["usageStatus"] = "invalid" if invalid else "provider-reported" if present else "not-returned"
    if invalid:
        metrics["invalidUsageFields"] = invalid
        raise CompareAdapterError("COMPARE_INVALID_USAGE", "模型 Token 计量含负数或无效类型；有效字段保留，无效字段为 null。", metrics=metrics)


def _error_evidence(error, metrics, provenance):
    if isinstance(error, CompareAdapterError):
        error.metrics = {**deepcopy(metrics), **error.metrics}
        error.provenance = deepcopy(provenance)
    elif isinstance(error, asyncio.CancelledError):
        error.metrics, error.provenance = deepcopy(metrics), deepcopy(provenance)


def _messages(context):
    messages = context.get("messages")
    if (not isinstance(messages, list) or len(messages) != 2
        or [item.get("role") if isinstance(item, dict) else None for item in messages] != ["system", "user"]
        or any(set(item) != {"role", "content"} or not isinstance(item["content"], str) or not item["content"]
               or len(item["content"]) > 256 * 1024 for item in messages)):
        raise CompareAdapterError("COMPARE_REPORT_CONTEXT_INVALID", "报告必须来自服务器生成的受控事实快照与固定提示。")
    snapshot = context.get("snapshot")
    checksum = _snapshot_sha(context)
    from backend.compare_reporting import CLOUD_CATEGORIES, encoded, report_messages
    if (snapshot.get("experimentalRowsIncluded") is not False or snapshot.get("privatePathsIncluded") is not False
        or snapshot.get("freeQuestionIncluded") is not False or snapshot.get("sentInformationCategories") != CLOUD_CATEGORIES
        or encoded(messages) != encoded(report_messages(snapshot,
            report_contract=context.get('request', {}).get('reportContract', 'v1'),
            report_mode=context.get('request', {}).get('reportMode', 'deep')))):
        raise CompareAdapterError("COMPARE_REPORT_CONTEXT_INVALID", "报告输入不符合受控事实、固定提示及最小外传合同。")
    return messages


def _snapshot_sha(context):
    from backend.compare_reporting import digest
    snapshot = context.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("schema") != "aidd.compare.facts.v1":
        raise CompareAdapterError("COMPARE_REPORT_CONTEXT_INVALID", "报告缺少统一的已核验事实快照。")
    checksum = snapshot.get("snapshotSha256")
    if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        raise CompareAdapterError("COMPARE_SNAPSHOT_MISMATCH", "报告事实快照 SHA256 缺失或无效。")
    try:
        calculated = digest({key: value for key, value in snapshot.items() if key != "snapshotSha256"})
    except (TypeError, ValueError, RecursionError):
        raise CompareAdapterError("COMPARE_SNAPSHOT_MISMATCH", "报告事实快照无法通过有限 JSON 与 SHA256 核验。") from None
    if calculated != checksum or context.get("snapshotSha256", checksum) != checksum:
        raise CompareAdapterError("COMPARE_SNAPSHOT_MISMATCH", "报告事实快照 SHA256 不一致；未调用语言模型。")
    return checksum


async def _http_json(client, method, url, *, payload=None, headers=None, timeout=None, metrics=None, usage_provider=None):
    observed = metrics if metrics is not None else {}
    try:
        async with client.stream(method, url, json=payload, headers=headers,
                                 **({"timeout": timeout} if timeout is not None else {})) as response:
            observed.update(httpStatusCode=response.status_code, httpResponseReceived=True, httpRequestState='response-received')
            if not 200 <= response.status_code < 300:
                code = ("COMPARE_HTTP_RATE_LIMITED" if response.status_code == 429 else
                        "COMPARE_HTTP_SERVER_ERROR" if 500 <= response.status_code < 600 else
                        "COMPARE_HTTP_ACCESS_DENIED" if response.status_code in (401, 403) else
                        "COMPARE_HTTP_REDIRECT_BLOCKED" if 300 <= response.status_code < 400 else "COMPARE_HTTP_ERROR")
                # Some error responses also contain genuine provider usage.
                # Inspect bounded JSON counters only, never retain its text.
                if usage_provider:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_HTTP_BYTES:
                            break
                    if len(raw) <= MAX_HTTP_BYTES:
                        try:
                            _report_usage(_json(bytes(raw)), usage_provider, observed)
                        except CompareAdapterError:
                            pass  # Keep HTTP error identity and any valid counters.
                raise CompareAdapterError(code, f"模型服务请求失败（HTTP {response.status_code}）；未重试或切换模型。", metrics=observed)
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_HTTP_BYTES:
                    raise CompareAdapterError("COMPARE_RESPONSE_TOO_LARGE", "模型响应超过允许大小。", metrics=observed)
            return _json(bytes(raw))
    except httpx.TimeoutException as error:
        phase = {"ConnectTimeout": "connect", "ReadTimeout": "read", "WriteTimeout": "write", "PoolTimeout": "pool"}.get(type(error).__name__, "unknown")
        observed["timeoutPhase"] = phase
        raise CompareAdapterError("COMPARE_TIMEOUT", "模型服务响应超时。", metrics=observed) from None
    except httpx.HTTPError:
        raise CompareAdapterError("COMPARE_CONNECTION_ERROR", "无法连接模型服务；未自动切换到其他模型。", metrics=observed) from None
    except CompareAdapterError as error:
        error.metrics = {**deepcopy(observed), **error.metrics}
        raise


def validate_ollama_url(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port if parsed.port is not None else 80
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or not 1 <= port <= 65535):
            raise ValueError()
        # Resolve no user-controlled DNS names. localhost is made literal.
        host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
        return f"http://{host}:{port}"
    except (ValueError, TypeError):
        raise CompareAdapterError("OLLAMA_LOOPBACK_REQUIRED", "Ollama 只允许本机 loopback HTTP 地址，不接受远程主机、代理、凭证或重定向。") from None


def validate_deepseek_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname != "api.deepseek.com" or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("/chat/completions", "/v1/chat/completions")):
            raise ValueError()
        return "https://api.deepseek.com" + parsed.path
    except (ValueError, TypeError):
        raise CompareAdapterError("DEEPSEEK_TRUSTED_URL_REQUIRED", "Compare 报告仅允许官方 api.deepseek.com 的 HTTPS Chat Completions 地址。") from None


class B16Adapter(_CleanupLifecycle):
    model_id = "toxnet-v3-b16"

    def __init__(self, *, model_dir=None, encoder_dir=None, device="cpu", enabled=False,
                 evidence_index=None, python_executable=None, timeout_seconds=180):
        self.model_dir = str(Path(model_dir).resolve()) if model_dir else None
        self.encoder_dir = str(Path(encoder_dir).resolve()) if encoder_dir else None
        self.evidence_index = str(Path(evidence_index or ROOT / "data/evidence-index").resolve())
        self.device, self.enabled = device, enabled
        self.python_executable = python_executable or sys.executable
        self.timeout_seconds = timeout_seconds
        self._status_cache = None
        self.last_partial = None
        self._initialize_cleanup()
        self._spawning_task = None

    @classmethod
    def from_env(cls, env=None):
        values = os.environ if env is None else env
        return cls(model_dir=values.get("TOXNET_MULTITASK_MODEL_DIR"), encoder_dir=values.get("TOXNET_MULTITASK_ENCODER_DIR"),
                   device=values.get("TOXNET_MULTITASK_DEVICE", "cpu"),
                   enabled=str(values.get("TOXNET_MULTITASK_ENABLE", "false")).lower() == "true",
                   evidence_index=values.get("TOXNET_EVIDENCE_INDEX_DIR"), python_executable=values.get("TOXNET_COMPARE_PYTHON"))

    def _base(self):
        return {"available": False, "configured": bool(self.enabled and self.model_dir and self.encoder_dir),
                "actualModelId": None, "revision": None, "device": self.device, "formalAvailability": False,
                "licenseStatus": "not-verified", "reason": "尚未完成真实 B16 工件检查。", "errorCode": None}

    async def _worker(self, action, context=None):
        if not self.enabled:
            raise CompareAdapterError("MULTITASK_RESEARCH_DISABLED", "必须显式启用 TOXNET_MULTITASK_ENABLE=true。")
        if not self.model_dir or not self.encoder_dir:
            raise CompareAdapterError("COMPARE_B16_UNCONFIGURED", "尚未配置完整 B16 与固定 MoLFormer 离线目录。")
        if self.device not in ("cpu", "cuda", "cuda:0"):
            raise CompareAdapterError("COMPARE_DEVICE_UNSUPPORTED", "Compare B16 仅支持 cpu、cuda 或 cuda:0。")
        payload = {"schema": WORKER_SCHEMA, "action": action, "modelDir": self.model_dir, "encoderDir": self.encoder_dir,
                   "device": self.device, "enabled": self.enabled, "evidenceIndex": self.evidence_index}
        if action == "execute":
            request = (context or {}).get("request", {})
            include_attribution = request.get("includeAttribution", request.get("explainEndpoint") is not None)
            payload.update(canonicalSmiles=context["canonicalSmiles"], structureId=context["structureId"],
                           includeTox21=request.get("includeTox21", True), includeEvidence=request.get("includeEvidence", False),
                           includeAttribution=include_attribution,
                           evidenceEndpoint=request.get("evidenceEndpoint") if request.get("includeEvidence") else None,
                           explainEndpoint=request.get("explainEndpoint") if include_attribution else None)
        self._begin_execution()
        process, fallback, spawning = None, None, None
        self.last_partial = None
        def retain_prediction(code):
            if fallback is None:
                return None
            result = fallback["result"]
            result["status"] = "partial"
            for field in ("evidence", "attribution"):
                if result["data"].get(field, {}).get("status") == "pending":
                    result["data"][field] = {"status": "unavailable", "results": [], "atoms": [], "fragments": [], "errorCode": code}
            result["provenance"]["optionalAuditIncomplete"] = True
            return fallback
        try:
            # Unbuffered stdin/stdout; no shell, inherited HF/network proxy env
            # is disabled for this already-local loading path.
            worker_env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONIOENCODING": "utf-8"}
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(str(self.python_executable), "-m", "backend.compare_worker", cwd=str(ROOT),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                        env=worker_env, limit=MAX_WORKER_BYTES,
                        **({"creationflags": 0x08000000} if os.name == "nt" else {})))
            self._spawning_task = spawning
            process = await asyncio.shield(spawning)
            process.stdin.write(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            async def read_responses():
                nonlocal fallback
                total, final = 0, None
                while True:
                    raw = await process.stdout.readline()
                    if not raw:
                        break
                    total += len(raw)
                    if total > MAX_WORKER_BYTES:
                        raise CompareAdapterError("COMPARE_WORKER_INVALID", "B16 worker 响应超过允许大小。")
                    response = _json(raw, "COMPARE_WORKER_INVALID")
                    if response.get("kind") == "prediction" and response.get("ok") is True and isinstance(response.get("result"), dict):
                        fallback = response
                        self.last_partial = response["result"]
                    elif response.get("kind") == "final":
                        final = response
                    else:
                        raise CompareAdapterError("COMPARE_WORKER_INVALID", "B16 worker 返回未知协议事件。")
                await process.wait()
                if final is None:
                    raise CompareAdapterError("COMPARE_WORKER_FAILED", "B16 worker 未返回完成事件。")
                return final
            response = await asyncio.wait_for(read_responses(), self.timeout_seconds)
            if response.get("ok") is not True:
                error = response.get("error") or {}
                code = error.get("code") if isinstance(error.get("code"), str) else "COMPARE_B16_UNAVAILABLE"
                message = error.get("message") if isinstance(error.get("message"), str) else "B16 worker 真实推理不可用。"
                retained = retain_prediction(code)
                if retained is not None:
                    return retained
                raise CompareAdapterError(code, message)
            if process.returncode != 0:
                raise CompareAdapterError("COMPARE_WORKER_FAILED", "B16 worker 未正常退出；未发布不完整输出。")
            return response
        except TimeoutError:
            retained = retain_prediction("COMPARE_OPTIONAL_AUDIT_TIMEOUT")
            if retained is not None:
                return retained
            raise CompareAdapterError("COMPARE_TIMEOUT", "B16 离线进程超时并已停止。") from None
        except (OSError, ValueError):
            retained = retain_prediction("COMPARE_WORKER_UNAVAILABLE")
            if retained is not None:
                return retained
            raise CompareAdapterError("COMPARE_WORKER_UNAVAILABLE", "无法启动隔离 B16 推理进程；请检查本机 Python 环境。") from None
        except CompareAdapterError as error:
            retained = retain_prediction(error.code)
            if retained is not None:
                return retained
            raise
        finally:
            self._start_cleanup(lambda: self._release_worker(spawning))
            await self.wait_cleanup()

    async def _release_worker(self, spawning):
        if spawning is None:
            return True
        try:
            process = await asyncio.shield(spawning)
        except (OSError, NotImplementedError):
            return True  # No child exists.
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

    async def status(self):
        if self._cleanup_state == "quarantined" or self._closed:
            return {**self._base(), "errorCode": "COMPARE_RESOURCE_QUARANTINED" if not self._closed else "COMPARE_RESOURCE_CLOSED",
                    "reason": "本地资源清理未确认或适配器已关闭；需要检查并重启服务。", "cleanup": self.cleanup_status()}
        # Status is advisory; every execute re-verifies actual file bytes.
        if self._status_cache and monotonic() - self._status_cache[0] < 30:
            return {**self._status_cache[1], "cleanup": self.cleanup_status()}
        base = self._base()
        try:
            response = await self._worker("status")
            result = {**base, **response["info"]}
        except CompareAdapterError as error:
            result = {**base, "errorCode": error.code, "reason": error.public_message}
        self._status_cache = (monotonic(), result)
        return {**result, "cleanup": self.cleanup_status()}

    async def execute(self, context):
        started = perf_counter()
        response = await self._worker("execute", context)
        result = response.get("result")
        if not isinstance(result, dict) or result.get("status") not in ("completed", "partial") or not isinstance(result.get("data"), dict):
            raise CompareAdapterError("COMPARE_WORKER_INVALID", "B16 worker 结果不符合 Compare 合同。")
        result.setdefault("metrics", {})["wallSeconds"] = perf_counter() - started
        self._status_cache = (monotonic(), {**self._base(), **response.get("info", {})})
        return result


class OllamaQwenAdapter(_CleanupLifecycle):
    model_id = "ollama-qwen-local"

    def __init__(self, base_url="http://127.0.0.1:11434", model_id="qwen3.5:4b", *, timeout_seconds=180, transport=None,
                 context_tokens=8192, output_tokens=4096):
        self.base_url, self.requested_model = base_url, model_id
        self.timeout_seconds, self.transport = timeout_seconds, transport
        self.context_tokens, self.output_tokens = context_tokens, output_tokens
        self._initialize_cleanup()
        self._cleanup_target = None
        self._cleanup_confirmed = False

    @classmethod
    def from_env(cls, env=None):
        values = os.environ if env is None else env
        return cls(values.get("TOXNET_OLLAMA_URL", "http://127.0.0.1:11434"),
                   values.get("TOXNET_OLLAMA_MODEL", "qwen3.5:4b"),
                   context_tokens=values.get('TOXNET_OLLAMA_CONTEXT_TOKENS', 8192),
                   output_tokens=values.get('TOXNET_OLLAMA_OUTPUT_TOKENS', 4096))

    def _generation_options(self):
        def count(value, low, high):
            if isinstance(value, str) and re.fullmatch(r'[0-9]{1,5}', value):
                value = int(value)
            if type(value) is not int or not low <= value <= high:
                raise CompareAdapterError('OLLAMA_INVALID_GENERATION_CONFIG', '本机报告上下文须为4096–16384 Token，输出上限须为256–8192 Token；不会静默改配置。')
            return value
        context = count(self.context_tokens, 4096, 16384)
        output = count(self.output_tokens, 256, 8192)
        if output >= context:
            raise CompareAdapterError('OLLAMA_INVALID_GENERATION_CONFIG', '报告输出上限必须小于上下文容量。')
        return {'temperature': 0, 'num_ctx': context, 'num_predict': output}

    def _client(self, timeout=None):
        return httpx.AsyncClient(timeout=timeout or self.timeout_seconds, follow_redirects=False, trust_env=False, transport=self.transport)

    async def _tags(self, client, base):
        # Availability metadata is cheap. A missing daemon must not inherit
        # the much longer token-generation timeout used by /api/chat.
        payload = await _http_json(client, "GET", base + "/api/tags", timeout=3)
        tags = payload.get("models")
        if not isinstance(tags, list):
            raise CompareAdapterError("OLLAMA_INVALID_TAGS", "Ollama 模型清单格式无效。")
        selected = []
        for record in tags:
            if not isinstance(record, dict):
                continue
            name, digest = record.get("name") or record.get("model"), record.get("digest")
            if (isinstance(name, str) and re.fullmatch(r"qwen[\w.:-]{1,180}", name, re.IGNORECASE)
                and isinstance(digest, str) and re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", digest)):
                details = record.get("details") if isinstance(record.get("details"), dict) else {}
                selected.append({"id": name, "name": name, "digest": digest, "sizeBytes": _count(record.get("size")),
                                 "parameterSize": details.get("parameter_size"), "quantization": details.get("quantization_level"),
                                 "details": {"parameter_size": details.get("parameter_size"), "quantization_level": details.get("quantization_level")}})
        return selected

    async def status(self):
        base = {"available": False, "configured": False, "actualModelId": None, "revision": None,
                "requestedModelId": self.requested_model, "selectedDefaultModel": None, "installedModels": [], "device": "ollama-managed; unmeasured",
                "licenseStatus": "local-package-license-not-reviewed", "reason": None, "errorCode": None,
                "cleanup": self.cleanup_status()}
        if self._cleanup_state == "quarantined" or self._closed:
            return {**base, "errorCode": "COMPARE_RESOURCE_QUARANTINED" if not self._closed else "COMPARE_RESOURCE_CLOSED",
                    "reason": "本地资源清理未确认或适配器已关闭；不再次调用模型服务。"}
        try:
            url = validate_ollama_url(self.base_url)
            generation_options = self._generation_options()
            base["configured"] = True
            async with self._client(timeout=3) as client:
                tags = await self._tags(client, url)
            base["installedModels"] = tags
            selected = next((model for model in tags if model["id"] == self.requested_model), None)
            if selected is None:
                return {**base, "errorCode": "OLLAMA_MODEL_NOT_INSTALLED", "reason": "本机 Ollama 可连接，但所选 Qwen 模型未安装；未自动下载。"}
            return {**base, "available": True, "actualModelId": selected["id"], "revision": selected["digest"],
                    "selectedDefaultModel": selected["id"],
                    "reason": "已从本机 /api/tags 核验模型 ID 与 digest；能力尚需真实报告生成验证。", "accessVerified": True,
                    'generationPolicy': {'contextTokens': generation_options['num_ctx'], 'outputTokenLimit': generation_options['num_predict'], 'configuredNotCapacityMeasured': True}}
        except CompareAdapterError as error:
            return {**base, "errorCode": error.code if error.code in ('OLLAMA_LOOPBACK_REQUIRED', 'OLLAMA_INVALID_GENERATION_CONFIG') else "OLLAMA_UNAVAILABLE", "reason": error.public_message}

    async def execute(self, context):
        url, messages = validate_ollama_url(self.base_url), _messages(context)
        from backend.compare_reporting import report_output_format
        report_format = report_output_format(context['snapshot'],
            report_contract=context.get('request', {}).get('reportContract', 'v1'),
            report_mode=context.get('request', {}).get('reportMode', 'deep'))
        options = self._generation_options()
        selected_id = context.get("reporterModel") or self.requested_model
        started = perf_counter()
        metrics = {"promptTokens": None, "completionTokens": None, "totalTokens": None, "cost": None,
                   "costStatus": "local-compute-cost-not-estimated", "httpResponseReceived": False,
                   "httpRequestState": "not-attempted",
                   "gpu": {"measured": False, "peakAllocatedBytes": None, "peakReservedBytes": None}}
        provenance = {"modelId": selected_id, "revision": None, "revisionSource": "ollama-api-tags-digest",
                      "runtime": "ollama", "device": "ollama-managed; unmeasured", "licenseStatus": "local-package-license-not-reviewed",
                      "reportOnly": True, "snapshotSha256": _snapshot_sha(context), "independentReport": True,
                      'generationOptions': {key: options[key] for key in ('num_ctx', 'num_predict')}}
        provenance.update(reportContract=context.get('request', {}).get('reportContract', 'v1'),
                          reportMode=context.get('request', {}).get('reportMode', 'deep'),
                          promptVersion=_json(messages[1]['content'])['promptVersion'],
                          outputFormat='server-bound-json-schema' if isinstance(report_format, dict) else 'json')
        pending_error = None
        self._begin_execution()
        self._cleanup_target, self._cleanup_confirmed = None, False
        try:
            async with self._client() as client:
                try:
                    tags = await self._tags(client, url)
                except CompareAdapterError as error:
                    if error.code in ("COMPARE_TIMEOUT", "COMPARE_CONNECTION_ERROR"):
                        raise CompareAdapterError("OLLAMA_UNAVAILABLE", "本机 Ollama 在 3 秒健康检查内不可连接；未启动生成、下载或切换模型。", metrics=error.metrics) from None
                    raise
                selected = next((record for record in tags if record["id"] == selected_id), None)
                if selected is None:
                    raise CompareAdapterError("OLLAMA_MODEL_NOT_INSTALLED", "所选 Qwen 模型不在真实本机模型清单中。")
                provenance["revision"] = selected["digest"]
                self._cleanup_target = (url, selected_id)
                metrics['httpRequestState'] = 'attempted-unconfirmed'
                payload = await _http_json(client, "POST", url + "/api/chat", payload={"model": selected_id, "messages": messages,
                              "format": report_format, "stream": False, "keep_alive": 0, "think": False, "options": options}, metrics=metrics, usage_provider='ollama')
                _report_usage(payload, "ollama", metrics)
                metrics.update(loadSeconds=_duration(payload.get('load_duration')),
                               promptEvaluationSeconds=_duration(payload.get('prompt_eval_duration')),
                               generationSeconds=_duration(payload.get("eval_duration")),
                               totalDurationSeconds=_duration(payload.get("total_duration")),
                               timeToFirstTokenSeconds=None, streaming=False)
                tokens, seconds = metrics["completionTokens"], metrics["generationSeconds"]
                metrics["tokensPerSecond"] = tokens / seconds if tokens is not None and seconds and seconds > 0 else None
                if payload.get("done") is not True or payload.get("done_reason") == "length":
                    raise CompareAdapterError("OLLAMA_REPORT_TRUNCATED", "本地报告生成尚未完成或达到输出上限；没有发布截断内容。")
                message = payload.get("message")
                if payload.get("model") != selected_id:
                    raise CompareAdapterError("OLLAMA_MODEL_CHANGED", "Ollama 返回模型 ID 与所选真实标签不一致。")
                if payload.get("done_reason") == "content_filter" or isinstance(message, dict) and message.get("refusal"):
                    raise CompareAdapterError("OLLAMA_REPORT_REFUSED", "本地模型拒绝生成该报告；没有发布拒绝原文。")
                if (not isinstance(message, dict) or message.get("content") is None
                    or isinstance(message.get("content"), str) and not message["content"].strip()):
                    raise CompareAdapterError("OLLAMA_REPORT_EMPTY", "本地报告没有返回内容；真实计量保留。")
                if message.get("role") != "assistant" or not isinstance(message.get("content"), str) or message.get("tool_calls"):
                    raise CompareAdapterError("OLLAMA_INVALID_REPORT", "本地 Qwen 没有返回完成的结构化报告。")
                report = _json(message["content"], "OLLAMA_INVALID_REPORT")
                after = await self._tags(client, url)
                if not any(record["id"] == selected_id and record["digest"] == selected["digest"] for record in after):
                    raise CompareAdapterError("OLLAMA_MODEL_CHANGED", "Qwen 模型 digest 在报告生成期间变化，报告未发布。")
                active = await _http_json(client, "GET", url + "/api/ps", timeout=3)
                models = active.get("models")
                self._cleanup_confirmed = (isinstance(models, list) and not any(isinstance(record, dict) and
                                     selected_id in (record.get("name"), record.get("model")) for record in models))
            return {"status": "completed", "data": {"rawReport": report}, "provenance": provenance, "metrics": metrics}
        except BaseException as error:
            pending_error = error
            raise
        finally:
            self._start_cleanup(self._release_ollama)
            cleanup_error = None
            try:
                await self.wait_cleanup()
            except BaseException as error:
                cleanup_error = error
                raise
            finally:
                metrics.update(wallSeconds=perf_counter() - started, resourceCleanupVerified=self.cleanup_safe)
                if pending_error is not None:
                    _error_evidence(pending_error, metrics, provenance)
                if cleanup_error is not None:
                    _error_evidence(cleanup_error, metrics, provenance)

    async def _release_ollama(self):
        if self._cleanup_target is None or self._cleanup_confirmed:
            return True
        return await self._unload(*self._cleanup_target)

    async def close(self):
        self._closed = True
        if self._cleanup_state == "running":
            self._start_cleanup(self._release_ollama)
        await self.wait_cleanup()

    async def _unload(self, url, model_id):
        async def unload():
            try:
                async with self._client(timeout=3) as client:
                    result = await _http_json(client, "POST", url + "/api/generate", payload={"model": model_id, "keep_alive": 0})
                    active = await _http_json(client, "GET", url + "/api/ps")
                models = active.get("models")
                return (result.get("done") is True and isinstance(models, list)
                        and not any(isinstance(record, dict) and model_id in (record.get("name"), record.get("model")) for record in models))
            except Exception:
                return False
        return await unload()


class DeepSeekReportAdapter:
    model_id = "deepseek-api"

    def __init__(self, configuration=None, *, config=None, transport=None):
        self.configuration, self.static_config, self.transport = configuration, config, transport
        self.request_attempted = False
        self._attempts = OrderedDict()

    @property
    def requestAttempted(self):
        return self.request_attempted

    def attempted_for(self, run_id):
        """Per-run attempt records avoid races between independently queued runs."""
        return self._attempts.get(run_id, False)

    def _mark_attempt(self, run_id, attempted):
        self.request_attempted = attempted
        if isinstance(run_id, str):
            self._attempts[run_id] = attempted
            self._attempts.move_to_end(run_id)
            while len(self._attempts) > 256:
                self._attempts.popitem(last=False)

    def _config(self):
        if self.configuration is not None:
            with self.configuration.lock:
                config = getattr(self.configuration.registry.get("deepseek"), "config", None)
        else:
            config = self.static_config
        if not isinstance(config, ModelConfig) or config.provider_id != "deepseek":
            raise CompareAdapterError("DEEPSEEK_UNCONFIGURED", "尚未配置服务端 DeepSeek 报告账户。")
        issue = config.configuration_error()
        if issue:
            raise CompareAdapterError("DEEPSEEK_UNCONFIGURED", issue)
        validate_deepseek_url(config.api_url)
        if config.api_format != "openai":
            raise CompareAdapterError("DEEPSEEK_REPORT_FORMAT_REQUIRED", "DeepSeek 报告要求 OpenAI-compatible Chat Completions 格式。")
        return config

    async def status(self):
        base = {"available": False, "configured": False, "actualModelId": None, "revision": None, "device": "provider-managed; unmeasured",
                "licenseStatus": "account-service-terms-required", "reason": None, "errorCode": None,
                "requiresCloudConsent": True, "accessVerified": False}
        try:
            config = self._config()
            # Configuration inspection does not prove account/model access and
            # never performs a paid health request.
            return {**base, "configured": True, "actualModelId": config.model_name, "revision": config.model_version,
                    "revisionSource": "server-configured; provider-version-not-independently-verified",
                    "reason": "账户已配置；供应商访问和型号权限未通过实际调用核验。每次运行须显式外传授权。",
                    "errorCode": "DEEPSEEK_ACCESS_UNVERIFIED", "callableWithConsent": True}
        except CompareAdapterError as error:
            return {**base, "errorCode": error.code, "reason": error.public_message}

    async def execute(self, context):
        run_id = context.get("runId")
        self._mark_attempt(run_id, False)
        if context.get("request", {}).get("allowCloudTransfer") is not True:
            raise CompareAdapterError("CLOUD_TRANSFER_NOT_AUTHORIZED", "未获得本次分子与受控研究事实的云端传输授权；未调用 DeepSeek。")
        config, messages = self._config(), _messages(context)
        checksum = _snapshot_sha(context)
        from backend.compare_reporting import CLOUD_CATEGORIES
        started = perf_counter()
        metrics = {"promptTokens": None, "completionTokens": None, "totalTokens": None, "tokensPerSecond": None,
                   "cost": None, "costStatus": "provider-billing-not-returned", "httpResponseReceived": False,
                   "gpu": {"measured": False, "peakAllocatedBytes": None, "peakReservedBytes": None}}
        provenance = {"modelId": config.model_name, "revision": config.model_version, "returnedModel": None,
                      "revisionSource": "server-configured", "runtime": "external-api", "reportOnly": True,
                      "snapshotSha256": checksum, "independentReport": True, "cloudTransferAuthorized": True,
                      "requestAttempted": False, "transferredCategories": list(CLOUD_CATEGORIES),
                      "rawExperimentalRowsTransferred": False, "localPathsTransferred": False}
        try:
            async with httpx.AsyncClient(timeout=config.timeout_seconds, trust_env=False, follow_redirects=False, transport=self.transport) as client:
                # An attempt is not proof of receipt, validated generation or billing.
                self._mark_attempt(run_id, True)
                provenance["requestAttempted"] = True
                metrics['httpRequestState'] = 'attempted-unconfirmed'
                payload = await _http_json(client, "POST", validate_deepseek_url(config.api_url),
                            headers={"Authorization": "Bearer " + config.api_key, "Content-Type": "application/json"},
                            payload={"model": config.model_name, "messages": messages, "stream": False,
                                     "temperature": 0, "response_format": {"type": "json_object"}}, metrics=metrics, usage_provider='deepseek')
            returned_model = payload.get("model")
            if (isinstance(returned_model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", returned_model)
                and config.api_key not in returned_model):
                provenance["returnedModel"] = returned_model
            _report_usage(payload, "deepseek", metrics)
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                raise CompareAdapterError("DEEPSEEK_REPORT_EMPTY", "DeepSeek 未返回报告选项；真实计量保留。")
            choice = choices[0]
            if not isinstance(choice, dict):
                raise CompareAdapterError("DEEPSEEK_INVALID_REPORT", "DeepSeek 报告选项格式无效。")
            finish, message = choice.get("finish_reason"), choice.get("message")
            if finish == "length":
                raise CompareAdapterError("DEEPSEEK_REPORT_TRUNCATED", "DeepSeek 达到输出上限；没有发布截断报告。")
            if finish == "content_filter" or isinstance(message, dict) and message.get("refusal"):
                raise CompareAdapterError("DEEPSEEK_REPORT_REFUSED", "DeepSeek 拒绝生成该报告；未发布拒绝原文。")
            if (not isinstance(message, dict) or message.get("content") is None
                or isinstance(message.get("content"), str) and not message["content"].strip()):
                raise CompareAdapterError("DEEPSEEK_REPORT_EMPTY", "DeepSeek 报告内容缺失；真实计量保留。")
            if (finish != "stop" or message.get("role") != "assistant" or not isinstance(message.get("content"), str)
                or message.get("tool_calls") or message.get("function_call")):
                raise CompareAdapterError("DEEPSEEK_INVALID_REPORT", "DeepSeek 未返回完整的报告 JSON；不执行生成的工具参数。")
            report = _json(message["content"], "DEEPSEEK_INVALID_REPORT")
            metrics["wallSeconds"] = perf_counter() - started
            return {"status": "completed", "data": {"rawReport": report}, "provenance": provenance, "metrics": metrics}
        except BaseException as error:
            metrics["wallSeconds"] = perf_counter() - started
            _error_evidence(error, metrics, provenance)
            raise


class UnavailableAdapter:
    """Explicit unavailable entry until a separately audited adapter is wired."""
    def __init__(self, model_id, reason="模型未配置、未授权或未完成任务合同审核。"):
        self.model_id, self.reason = model_id, reason

    async def status(self):
        return {"available": False, "configured": False, "actualModelId": None, "revision": None, "device": None,
                "licenseStatus": "not-authorized", "reason": self.reason, "errorCode": "MODEL_UNAVAILABLE"}

    async def execute(self, context):
        raise CompareAdapterError("MODEL_UNAVAILABLE", self.reason)
