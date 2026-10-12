"""Configurable real HTTP clients. No synthetic prediction fallback."""
from __future__ import annotations

import json
import math
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

ENDPOINT_IDS = ("herg", "dili", "ames", "clintox")
PROMPT_VERSION = "toxilens.multimodel.prompt.v2"
PROVIDER_URLS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/chat/completions",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "glm": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
}


class ModelAPIError(Exception):
    def __init__(self, code: str, public_message: str, status_code: int = 502):
        super().__init__(public_message)
        self.code, self.public_message, self.status_code = code, public_message, status_code


@dataclass(frozen=True)
class ModelConfig:
    provider_id: str = "custom"
    api_url: str = ""
    api_key: str = ""
    model_name: str = ""
    model_version: str = ""
    api_format: str = "json"
    timeout_seconds: float = 30
    score_kind: str = "uncalibrated_score"
    send_image: bool = False
    temperature: float | None = None
    json_mode: bool = True

    @classmethod
    def from_env(cls, prefix="TOXILENS_MODEL_", provider_id="custom", env=None):
        values = os.environ if env is None else env
        try:
            timeout = float(values.get(prefix + "TIMEOUT_SECONDS", "30"))
        except ValueError:
            timeout = float("nan")
        temperature_value = values.get(prefix + "TEMPERATURE", "").strip().lower()
        try:
            temperature = None if temperature_value in ("", "omit", "default") else float(temperature_value)
        except ValueError:
            temperature = float("nan")
        return cls(provider_id=provider_id, api_url=values.get(prefix + "API_URL", PROVIDER_URLS.get(provider_id, "")).strip(), api_key=values.get(prefix + "API_KEY", "").strip(), model_name=values.get(prefix + "MODEL_NAME", "").strip(), model_version=values.get(prefix + "MODEL_VERSION", "").strip(), api_format=values.get(prefix + "API_FORMAT", "openai" if provider_id in PROVIDER_URLS else "json").strip(), timeout_seconds=timeout, score_kind=values.get(prefix + "SCORE_KIND", "uncalibrated_score").strip(), send_image=values.get(prefix + "SEND_IMAGE", "false").lower() in ("1", "true", "yes"), temperature=temperature, json_mode=values.get(prefix + "JSON_MODE", "true").lower() not in ("0", "false", "no"))

    def configuration_error(self):
        if not self.api_url or not self.model_name or not self.model_version:
            return "请配置 API 地址、模型名称和可追溯的模型版本。"
        if self.provider_id in PROVIDER_URLS and not self.api_key:
            return "所选供应商尚未配置 API 密钥。"
        try:
            parsed = urlsplit(self.api_url)
            if not parsed.hostname or parsed.username or parsed.password:
                return "API 地址无效，认证信息应使用服务端环境变量。"
            if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
                return "API 地址须使用 HTTPS；本机推理服务可使用 HTTP。"
            _ = parsed.port
        except ValueError:
            return "API 地址格式无效。"
        if self.api_format not in ("json", "openai") or self.score_kind not in ("uncalibrated_score", "probability", "binary_label"):
            return "API 格式或分数类型配置无效。"
        if self.api_format == "openai" and self.score_kind != "uncalibrated_score":
            return "通用对话模型仅支持未校准分数；概率或二元标签需使用专用预测服务。"
        if not math.isfinite(self.timeout_seconds) or not 0.1 <= self.timeout_seconds <= 120:
            return "模型超时须在 0.1 到 120 秒之间。"
        if self.temperature is not None and (not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2):
            return "temperature 参数配置无效。"
        if any("\n" in text or "\r" in text for text in (self.api_key, self.api_url)):
            return "API 认证或地址配置无效。"
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class ModelClient:
    def __init__(self, config: ModelConfig):
        self.config = config
        self.opener = urllib.request.build_opener(_NoRedirect())

    def get_public_config(self):
        return {"configured": self.config.configuration_error() is None, "providerFormat": self.config.api_format, "modelName": self.config.model_name or None, "version": self.config.model_version or None, "supportsImage": self.config.send_image}

    def _body(self, canonical, identity, image_data_url):
        if self.config.api_format == "json":
            return {"schema": "toxilens.model.request.v1", "model": self.config.model_name, "smiles": canonical, "structureId": identity, "endpoints": list(ENDPOINT_IDS), "scoreKind": self.config.score_kind, **({"imageDataUrl": image_data_url} if image_data_url else {})}
        prompt = (
            "You are assisting an exploratory molecular screening workflow. Return only one JSON object with an endpoints array containing exactly herg, dili, ames and clintox. "
            "Each endpoint has id, score (finite 0..1, an UNCALIBRATED exploratory score, never a certified probability), and optional drivers [{label,detail}]. "
            "Positive classes: herg=hERG channel inhibition; dili=drug-induced liver injury; ames=Ames mutagenicity positive; clintox=clinical toxicity, NOT FDA approval. "
            "Use the molecular structure, distinguish structural hypotheses from experimental facts, and do not invent assays, citations, calibration, training sets, applicability or model versions. "
            "Do not claim these estimates are TxGemma predictions or clinically validated."
        )
        text = f"Canonical isomeric SMILES: {canonical}\nStructure identifier: {identity}\nPrompt version: {PROMPT_VERSION}"
        content = [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": image_data_url}}] if image_data_url else text
        return {"model": self.config.model_name, **({"temperature": self.config.temperature} if self.config.temperature is not None else {}), **({"response_format": {"type": "json_object"}} if self.config.json_mode else {}), "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": content}]}

    def _validate_endpoints(self, payload):
        records = payload.get("endpoints") if isinstance(payload, dict) else None
        if not isinstance(records, list) or len(records) != 4:
            raise ModelAPIError("MODEL_INVALID_RESPONSE", "模型响应必须完整包含四个毒性端点。")
        result, seen = [], set()
        for record in records:
            if not isinstance(record, dict) or record.get("id") not in ENDPOINT_IDS or record["id"] in seen:
                raise ModelAPIError("MODEL_INVALID_RESPONSE", "模型响应含未知或重复端点。")
            seen.add(record["id"])
            value = record.get("score")
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ModelAPIError("MODEL_INVALID_RESPONSE", "模型分数须为 0 到 1 的有限数字。")
            if self.config.score_kind == "binary_label" and value not in (0, 1):
                raise ModelAPIError("MODEL_INVALID_RESPONSE", "二元预测标签只能为 0 或 1。")
            drivers = record.get("drivers", [])
            if not isinstance(drivers, list) or len(drivers) > 8:
                raise ModelAPIError("MODEL_INVALID_RESPONSE", "模型解释字段格式无效。")
            explanations = []
            for driver in drivers:
                if not isinstance(driver, dict) or not isinstance(driver.get("label"), str) or not isinstance(driver.get("detail"), str) or len(driver["label"]) > 160 or len(driver["detail"]) > 1600:
                    raise ModelAPIError("MODEL_INVALID_RESPONSE", "模型解释内容不符合接口契约。")
                explanations.append({"label": driver["label"], "detail": driver["detail"], "source": "model-generated" if self.config.api_format == "openai" else "model-provided"})
            result.append({"id": record["id"], "score": float(value), "drivers": explanations})
        return sorted(result, key=lambda record: ENDPOINT_IDS.index(record["id"]))

    def predict(self, canonical_smiles, structure_id, image_data_url=None):
        issue = self.config.configuration_error()
        if issue:
            raise ModelAPIError("MODEL_NOT_CONFIGURED", issue, 503)
        image = image_data_url if self.config.send_image else None
        if self.config.send_image and (not isinstance(image, str) or not image.startswith("data:image/png;base64,")):
            raise ModelAPIError("MODEL_INPUT_ERROR", "视觉模型需要标准 PNG 分子结构图。", 422)
        request = urllib.request.Request(self.config.api_url, data=json.dumps(self._body(canonical_smiles, structure_id, image), ensure_ascii=False, allow_nan=False).encode("utf-8"), headers={"Content-Type": "application/json", "Accept": "application/json", **({"Authorization": "Bearer " + self.config.api_key} if self.config.api_key else {})}, method="POST")
        try:
            with self.opener.open(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ModelAPIError("MODEL_INVALID_RESPONSE", "模型响应超过允许大小。")
                payload = json.loads(raw)
        except urllib.error.HTTPError as error:
            raise ModelAPIError("MODEL_HTTP_ERROR", f"模型服务请求失败（HTTP {error.code}），请检查模型名称、权限和接口配置。") from None
        except (socket.timeout, TimeoutError):
            raise ModelAPIError("MODEL_TIMEOUT", "模型服务响应超时。", 504) from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (socket.timeout, TimeoutError)):
                raise ModelAPIError("MODEL_TIMEOUT", "模型服务响应超时。", 504) from None
            raise ModelAPIError("MODEL_CONNECTION_ERROR", "无法连接模型服务，请检查 API 地址和网络。") from None
        except (UnicodeError, json.JSONDecodeError, ValueError, OSError):
            raise ModelAPIError("MODEL_INVALID_RESPONSE", "模型服务返回了无效响应。") from None
        if not isinstance(payload, dict):
            raise ModelAPIError("MODEL_INVALID_RESPONSE", "模型响应必须是 JSON 对象。")
        outer = payload
        if self.config.api_format == "openai":
            try:
                content = payload["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    raise ValueError()
                payload = json.loads(content)
            except (KeyError, IndexError, TypeError, ValueError):
                raise ModelAPIError("MODEL_INVALID_RESPONSE", "对话模型未返回符合契约的结构化 JSON。") from None
        endpoints = self._validate_endpoints(payload)
        provenance = {"providerFormat": self.config.api_format, "modelName": self.config.model_name, "modelVersion": self.config.model_version, "modelVersionSource": "configured", "returnedModel": outer.get("model") if isinstance(outer.get("model"), str) else None, "scoreKind": self.config.score_kind, "promptVersion": PROMPT_VERSION if self.config.api_format == "openai" else None, "endpointContractVersion": "toxilens.endpoints.v1", "requestId": outer.get("id") if isinstance(outer.get("id"), str) else None, "temperature": self.config.temperature if self.config.api_format == "openai" else None, "jsonMode": self.config.json_mode if self.config.api_format == "openai" else None}
        # Versions or validation claims inside generated chat content are not evidence.
        if self.config.api_format == "json":
            version = outer.get("modelVersion")
            if isinstance(version, str) and version.strip() and len(version) <= 200:
                provenance.update(modelVersion=version, modelVersionSource="provider-response")
        calibration = {"status": "unknown", "reason": "no-independent-calibration-evidence"}
        applicability = {"status": "unknown", "reason": "no-training-reference-domain"}
        if self.config.api_format == "json":
            claimed = payload.get("calibration")
            if self.config.score_kind == "probability" and isinstance(claimed, dict) and claimed.get("status") == "validated" and all(isinstance(claimed.get(key), str) and claimed[key].strip() for key in ("evidenceId", "datasetVersion")):
                calibration = {key: claimed[key] for key in ("status", "evidenceId", "datasetVersion")}
                calibration["source"] = "prediction-service-declaration"
            claimed = payload.get("applicability")
            if isinstance(claimed, dict) and claimed.get("status") in ("in_domain", "out_of_domain") and all(isinstance(claimed.get(key), str) and claimed[key].strip() for key in ("evidenceId", "referenceDatasetVersion", "method")):
                applicability = {key: claimed[key] for key in ("status", "evidenceId", "referenceDatasetVersion", "method")}
                applicability["source"] = "prediction-service-declaration"
        return {"endpoints": endpoints, "provenance": provenance, "scoreKind": self.config.score_kind, "calibration": calibration, "applicability": applicability}


def get_model_registry(env=None):
    values = os.environ if env is None else env
    registry = {provider: ModelClient(ModelConfig.from_env(f"TOXILENS_{provider.upper()}_", provider, values)) for provider in PROVIDER_URLS}
    prefix = "TOXILENS_CUSTOM_" if any(key.startswith("TOXILENS_CUSTOM_") for key in values) else "TOXILENS_MODEL_"
    registry["custom"] = ModelClient(ModelConfig.from_env(prefix, "custom", values))
    return registry
