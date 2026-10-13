"""Isolated molecular inference and independent report-only HTTP adapters.

No model is pulled or downloaded. Report providers never create trusted model
scores. Cloud calls require per-run permission and server-side credentials.
"""
from __future__ import annotations

import asyncio
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

ROOT = Path(__file__).resolve().parents[1]
MAX_HTTP_BYTES = 2 * 1024 * 1024
MAX_WORKER_BYTES = 24 * 1024 * 1024
WORKER_SCHEMA = "aidd.compare.b16.worker.v1"


class CompareAdapterError(Exception):
    def __init__(self, code, public_message):
        super().__init__(public_message)
        self.code, self.public_message = code, public_message


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
        or encoded(messages) != encoded(report_messages(snapshot))):
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


async def _http_json(client, method, url, *, payload=None, headers=None, timeout=None):
    try:
        async with client.stream(method, url, json=payload, headers=headers,
                                 **({"timeout": timeout} if timeout is not None else {})) as response:
            if not 200 <= response.status_code < 300:
                raise CompareAdapterError("COMPARE_HTTP_ERROR", f"模型服务请求失败（HTTP {response.status_code}）；请检查服务、模型名及权限。")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_HTTP_BYTES:
                    raise CompareAdapterError("COMPARE_RESPONSE_TOO_LARGE", "模型响应超过允许大小。")
            return _json(bytes(raw))
    except httpx.TimeoutException:
        raise CompareAdapterError("COMPARE_TIMEOUT", "模型服务响应超时。") from None
    except httpx.HTTPError:
        raise CompareAdapterError("COMPARE_CONNECTION_ERROR", "无法连接模型服务；未自动切换到其他模型。") from None


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


class B16Adapter:
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
        self.cleanup_safe = True

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
            payload.update(canonicalSmiles=context["canonicalSmiles"], structureId=context["structureId"],
                           includeTox21=request.get("includeTox21", True), includeEvidence=request.get("includeEvidence", False),
                           explainEndpoint=request.get("explainEndpoint"))
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
            self.cleanup_safe = False
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(str(self.python_executable), "-m", "backend.compare_worker", cwd=str(ROOT),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                        env=worker_env, limit=MAX_WORKER_BYTES,
                        **({"creationflags": 0x08000000} if os.name == "nt" else {})))
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                # Retain the child handle even if cancellation races process
                # creation. The outer finally kills/waits that exact process.
                try:
                    process = await asyncio.shield(spawning)
                except (OSError, NotImplementedError):
                    pass
                raise
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
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                # Cancellation never leaves a live worker occupying CUDA.
                await asyncio.shield(process.wait())
            if process is not None and process.returncode is not None:
                self.cleanup_safe = True
            elif spawning is not None and spawning.done() and not spawning.cancelled() and spawning.exception() is not None:
                # No child was created at all.
                self.cleanup_safe = True

    async def status(self):
        # Status is advisory; every execute re-verifies actual file bytes.
        if self._status_cache and monotonic() - self._status_cache[0] < 30:
            return dict(self._status_cache[1])
        base = self._base()
        try:
            response = await self._worker("status")
            result = {**base, **response["info"]}
        except CompareAdapterError as error:
            result = {**base, "errorCode": error.code, "reason": error.public_message}
        self._status_cache = (monotonic(), result)
        return dict(result)

    async def execute(self, context):
        started = perf_counter()
        response = await self._worker("execute", context)
        result = response.get("result")
        if not isinstance(result, dict) or result.get("status") not in ("completed", "partial") or not isinstance(result.get("data"), dict):
            raise CompareAdapterError("COMPARE_WORKER_INVALID", "B16 worker 结果不符合 Compare 合同。")
        result.setdefault("metrics", {})["wallSeconds"] = perf_counter() - started
        self._status_cache = (monotonic(), {**self._base(), **response.get("info", {})})
        return result


class OllamaQwenAdapter:
    model_id = "ollama-qwen-local"

    def __init__(self, base_url="http://127.0.0.1:11434", model_id="qwen3.5:4b", *, timeout_seconds=180, transport=None):
        self.base_url, self.requested_model = base_url, model_id
        self.timeout_seconds, self.transport = timeout_seconds, transport
        self.cleanup_safe = True

    @classmethod
    def from_env(cls, env=None):
        values = os.environ if env is None else env
        return cls(values.get("TOXNET_OLLAMA_URL", "http://127.0.0.1:11434"),
                   values.get("TOXNET_OLLAMA_MODEL", "qwen3.5:4b"))

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
                "licenseStatus": "local-package-license-not-reviewed", "reason": None, "errorCode": None}
        try:
            url = validate_ollama_url(self.base_url)
            base["configured"] = True
            async with self._client(timeout=3) as client:
                tags = await self._tags(client, url)
            base["installedModels"] = tags
            selected = next((model for model in tags if model["id"] == self.requested_model), None)
            if selected is None:
                return {**base, "errorCode": "OLLAMA_MODEL_NOT_INSTALLED", "reason": "本机 Ollama 可连接，但所选 Qwen 模型未安装；未自动下载。"}
            return {**base, "available": True, "actualModelId": selected["id"], "revision": selected["digest"],
                    "selectedDefaultModel": selected["id"],
                    "reason": "已从本机 /api/tags 核验模型 ID 与 digest；能力尚需真实报告生成验证。", "accessVerified": True}
        except CompareAdapterError as error:
            return {**base, "errorCode": error.code if error.code == "OLLAMA_LOOPBACK_REQUIRED" else "OLLAMA_UNAVAILABLE", "reason": error.public_message}

    async def execute(self, context):
        self.cleanup_safe = True
        url, messages = validate_ollama_url(self.base_url), _messages(context)
        selected_id = context.get("reporterModel") or self.requested_model
        started = perf_counter()
        async with self._client() as client:
            try:
                tags = await self._tags(client, url)
            except CompareAdapterError as error:
                if error.code in ("COMPARE_TIMEOUT", "COMPARE_CONNECTION_ERROR"):
                    raise CompareAdapterError("OLLAMA_UNAVAILABLE", "本机 Ollama 在 3 秒健康检查内不可连接；未启动生成、下载或切换模型。") from None
                raise
            selected = next((record for record in tags if record["id"] == selected_id), None)
            if selected is None:
                raise CompareAdapterError("OLLAMA_MODEL_NOT_INSTALLED", "所选 Qwen 模型不在真实本机模型清单中。")
            try:
                payload = await _http_json(client, "POST", url + "/api/chat", payload={"model": selected_id, "messages": messages,
                              "format": "json", "stream": False, "keep_alive": 0, "think": False, "options": {"temperature": 0}})
                message = payload.get("message")
                if (payload.get("done") is not True or payload.get("model") != selected_id
                    or not isinstance(message, dict) or message.get("role") != "assistant" or not isinstance(message.get("content"), str)):
                    raise CompareAdapterError("OLLAMA_INVALID_REPORT", "本地 Qwen 没有返回完成的结构化报告。")
                report = _json(message["content"], "OLLAMA_INVALID_REPORT")
                after = await self._tags(client, url)
                if not any(record["id"] == selected_id and record["digest"] == selected["digest"] for record in after):
                    raise CompareAdapterError("OLLAMA_MODEL_CHANGED", "Qwen 模型 digest 在报告生成期间变化，报告未发布。")
                active = await _http_json(client, "GET", url + "/api/ps", timeout=3)
                models = active.get("models")
                self.cleanup_safe = (isinstance(models, list) and not any(isinstance(record, dict) and
                                     selected_id in (record.get("name"), record.get("model")) for record in models))
            except BaseException:
                # Best-effort local unload on cancellation/timeout. It cannot
                # kill an independently managed Ollama daemon, so the resource
                # manager must treat cleanup failure as a busy/unavailable GPU.
                self.cleanup_safe = await self._unload(url, selected_id)
                raise
        wall = perf_counter() - started
        prompt_tokens, completion_tokens = _count(payload.get("prompt_eval_count")), _count(payload.get("eval_count"))
        evaluation_seconds = _duration(payload.get("eval_duration"))
        return {"status": "completed", "data": {"rawReport": report},
                "provenance": {"modelId": selected_id, "revision": selected["digest"], "revisionSource": "ollama-api-tags-digest",
                               "runtime": "ollama", "device": "ollama-managed; unmeasured", "licenseStatus": "local-package-license-not-reviewed",
                               "reportOnly": True, "snapshotSha256": _snapshot_sha(context), "independentReport": True},
                "metrics": {"wallSeconds": wall, "promptTokens": prompt_tokens, "completionTokens": completion_tokens,
                            "generationSeconds": evaluation_seconds, "totalDurationSeconds": _duration(payload.get("total_duration")),
                            "resourceCleanupVerified": self.cleanup_safe,
                            "tokensPerSecond": completion_tokens / evaluation_seconds if completion_tokens is not None and evaluation_seconds and evaluation_seconds > 0 else None,
                            "cost": None, "costStatus": "local-compute-cost-not-estimated", "gpu": {"measured": False, "peakAllocatedBytes": None, "peakReservedBytes": None}}}

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
        return await asyncio.shield(unload())


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
        async with httpx.AsyncClient(timeout=config.timeout_seconds, trust_env=False, follow_redirects=False, transport=self.transport) as client:
            # This proves an HTTP request was attempted, not that the provider
            # received it, generated a valid report, or billed any amount.
            self._mark_attempt(run_id, True)
            payload = await _http_json(client, "POST", validate_deepseek_url(config.api_url),
                        headers={"Authorization": "Bearer " + config.api_key, "Content-Type": "application/json"},
                        payload={"model": config.model_name, "messages": messages, "stream": False,
                                 "temperature": 0, "response_format": {"type": "json_object"}})
        try:
            message = payload["choices"][0]["message"]
            finish = payload["choices"][0].get("finish_reason")
            if not isinstance(message, dict) or message.get("role") != "assistant" or not isinstance(message.get("content"), str) or finish != "stop":
                raise ValueError()
            report = _json(message["content"], "DEEPSEEK_INVALID_REPORT")
        except (KeyError, IndexError, TypeError, ValueError):
            raise CompareAdapterError("DEEPSEEK_INVALID_REPORT", "DeepSeek 未返回完整结构化报告。") from None
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return {"status": "completed", "data": {"rawReport": report},
                "provenance": {"modelId": config.model_name, "revision": config.model_version,
                               "returnedModel": payload.get("model") if isinstance(payload.get("model"), str) else None,
                               "revisionSource": "server-configured", "runtime": "external-api", "reportOnly": True,
                               "snapshotSha256": checksum, "independentReport": True,
                               "cloudTransferAuthorized": True, "requestAttempted": True, "transferredCategories": list(CLOUD_CATEGORIES),
                               "rawExperimentalRowsTransferred": False, "localPathsTransferred": False},
                "metrics": {"wallSeconds": perf_counter() - started, "promptTokens": _count(usage.get("prompt_tokens")),
                            "completionTokens": _count(usage.get("completion_tokens")), "totalTokens": _count(usage.get("total_tokens")),
                            "tokensPerSecond": None, "cost": None, "costStatus": "provider-billing-not-returned",
                            "gpu": {"measured": False, "peakAllocatedBytes": None, "peakReservedBytes": None}}}


class UnavailableAdapter:
    """Explicit unavailable entry until a separately audited adapter is wired."""
    def __init__(self, model_id, reason="模型未配置、未授权或未完成任务合同审核。"):
        self.model_id, self.reason = model_id, reason

    async def status(self):
        return {"available": False, "configured": False, "actualModelId": None, "revision": None, "device": None,
                "licenseStatus": "not-authorized", "reason": self.reason, "errorCode": "MODEL_UNAVAILABLE"}

    async def execute(self, context):
        raise CompareAdapterError("MODEL_UNAVAILABLE", self.reason)
