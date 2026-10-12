"""Same-origin application and strict model API gateway. Keys remain server-side."""
from __future__ import annotations

import base64
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictStr, field_validator
from rdkit import Chem, rdBase
from rdkit.Chem import Draw
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.chemistry import CHEMISTRY_VERSION, RULES_VERSION, analyze_chemistry
from backend.configuration import ConfigurationError, ModelConfigurationStore
from backend.model_client import ModelAPIError
from backend.local_model import LocalModelClient
from backend.research_evidence import read_external_evidence

ROOT = Path(__file__).resolve().parents[1]
API_VERSION = "2.0.0"
CONTRACT_VERSION = "toxilens.api.v2"
PROVIDER_LABELS = {"openai": "ChatGPT / OpenAI", "deepseek": "DeepSeek", "qwen": "千问 / Qwen", "glm": "GLM / 智谱", "custom": "自定义模型 API", "local": "蓝研 ToxNet · 自训练"}
ENDPOINT_DEFINITIONS = {
    "herg": {"positiveClass": "hERG channel inhibition", "unit": "model score; not an IC50 value"},
    "dili": {"positiveClass": "drug-induced liver injury", "unit": "model score"},
    "ames": {"positiveClass": "Ames assay mutagenicity positive", "unit": "model score"},
    "clintox": {"positiveClass": "clinical toxicity", "unit": "model score; distinct from FDA approval"},
}
MODEL_ENDPOINT_DESCRIPTIONS = {
    "herg": "来源数据中的 hERG 通道阻断标签",
    "dili": "来源数据中的药物性肝损伤标签",
    "ames": "来源 Ames 致突变试验标签",
    "clintox": "临床毒性标签；区别于 FDA 批准状态",
}


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    smiles: StrictStr = Field(min_length=1, max_length=4096)
    mode: Literal["model", "rules"] = "model"
    provider: StrictStr | None = Field(default=None, min_length=1, max_length=40)


class ProviderConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apiUrl: StrictStr | None = Field(default=None, min_length=1, max_length=2048)
    apiKey: StrictStr | None = Field(default=None, max_length=4096)
    modelName: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    modelVersion: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    apiFormat: Literal["json", "openai"] | None = None
    scoreKind: Literal["uncalibrated_score", "probability", "binary_label"] | None = None
    sendImage: StrictBool | None = None
    timeoutSeconds: StrictFloat | None = Field(default=None, ge=0.1, le=120, allow_inf_nan=False)
    jsonMode: StrictBool | None = None
    temperature: StrictFloat | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)

    @field_validator("apiUrl", "apiKey", "modelName", "modelVersion", "apiFormat", "scoreKind", "sendImage", "timeoutSeconds", "jsonMode", "temperature", mode="before")
    @classmethod
    def validate_values(cls, value, info):
        if value is None and info.field_name not in ("apiKey", "temperature"):
            raise ValueError("Configuration fields cannot be null")
        if isinstance(value, str) and any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("Configuration fields cannot contain control characters")
        return value


def _tier(score: float, mode: str) -> dict:
    tiers = [(0.33, "low", "低分区间", "blue"), (0.58, "moderate", "中分区间", "amber"), (0.78, "high", "高分区间", "orange"), (1.01, "critical", "重点关注", "red")]
    _, identifier, label, color = next(t for t in tiers if score < t[0])
    return {"id": identifier, "label": label, "color": color, "action": "结合实验与适用域证据复核", "meaning": f"{mode} composite score tier; not a calibrated risk category"}


def _structure_image(canonical: str) -> str:
    image = Draw.MolToImage(Chem.MolFromSmiles(canonical), size=(700, 360))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _compose(smiles: str, mode: str, provider: str | None, registry: dict, default_provider: str, request_id: str) -> dict:
    result = analyze_chemistry(smiles)
    result.update(mode=mode, generatedAt=datetime.now(timezone.utc).isoformat(), requestId=request_id, endpointDefinitions=ENDPOINT_DEFINITIONS)
    result["provenance"].update(contractVersion=CONTRACT_VERSION, apiVersion=API_VERSION, mode=mode, requestId=request_id)
    result["provenance"]["chemistry"] = {"version": CHEMISTRY_VERSION, "rulesVersion": RULES_VERSION, "rdkitVersion": rdBase.rdkitVersion}
    result["provenance"]["rules"] = result["rules"]
    result["calibration"] = {"status": "unknown", "reason": "no-independent-calibration-evidence"}
    result["applicability"] = {"status": "unknown", "reason": "no-training-reference-domain"}
    result["scoreKind"] = "uncalibrated_score"
    if mode == "rules":
        for endpoint in result["endpoints"]:
            endpoint.update(score=endpoint["ruleScore"], modelScore=None, drivers=endpoint["ruleDrivers"], evidence="本地规则分数（未校准）")
        result["overall"] = result["ruleOverall"]
        result["evidenceLabel"] = "本地规则基线 · 未校准"
        result["provenance"]["model"] = None
        result["provenance"]["aggregation"] = {"method": "0.62*max + 0.28*mean + 0.10*alertPressure", "version": "rule-composite.v2", "meaning": "demonstration composite index; not probability"}
    else:
        provider_id = provider or default_provider
        if provider_id not in registry:
            raise ValueError("未知的模型供应商，请从已配置列表选择。")
        client = registry[provider_id]
        metadata = client.get_public_config()
        image = _structure_image(result["canonicalSmiles"]) if metadata.get("supportsImage") else None
        prediction = client.predict(result["canonicalSmiles"], result["structureId"], image_data_url=image)
        predictions = {endpoint["id"]: endpoint for endpoint in prediction["endpoints"]}
        for endpoint in result["endpoints"]:
            predicted = predictions[endpoint["id"]]
            explanation = ("model-provided" if prediction["provenance"].get("providerFormat") == "json" else "model-generated") if predicted.get("drivers") else "none"
            endpoint.update(score=predicted["score"], modelScore=predicted["score"], scoreSource="model", explanationSource=explanation, drivers=predicted.get("drivers", []), description=MODEL_ENDPOINT_DESCRIPTIONS[endpoint["id"]], evidence="本地自主模型输出" if provider_id == "local" else "模型 API 输出", source=PROVIDER_LABELS.get(provider_id, provider_id))
        scores = [item["score"] for item in result["endpoints"]]
        result["overall"] = round(0.7 * max(scores) + 0.3 * sum(scores) / len(scores), 3)
        result["scoreKind"] = prediction.get("scoreKind", prediction.get("provenance", {}).get("scoreKind", "uncalibrated_score"))
        result["calibration"] = prediction.get("calibration", result["calibration"])
        result["applicability"] = prediction.get("applicability", result["applicability"])
        result["evidenceLabel"] = ("本地自主模型输出 · " if provider_id == "local" else "模型 API 输出 · ") + ("二元标签" if result["scoreKind"] == "binary_label" else "概率输出（需校准证据）" if result["scoreKind"] == "probability" else "未校准分数")
        result["provenance"]["model"] = {**prediction["provenance"], "providerId": provider_id, "imageIncluded": image is not None}
        result["provenance"]["aggregation"] = {"method": "0.70*max + 0.30*mean", "version": "model-composite.v1", "meaning": "composite index; not probability; excludes rule and reference scores"}
    result["tier"] = _tier(result["overall"], mode)
    result["provenance"].update(scoreKind=result["scoreKind"], calibration=result["calibration"], applicability=result["applicability"])
    return result


def create_app(registry: dict | None = None, default_provider: str | None = None, config_path: Path | None = None, env: dict | None = None, local_client=None) -> FastAPI:
    configuration = ModelConfigurationStore(config_path or ROOT / ".env", registry=registry, env=env)
    registry = configuration.registry
    local = registry.get("local") or local_client or LocalModelClient(model_dir=config_path.parent / "models/aidd-toxnet" if config_path else None, env=configuration.startup_values)
    registry.setdefault("local", local)
    active_provider = default_provider or configuration.startup_values.get("TOXILENS_DEFAULT_PROVIDER") or next((key for key, value in registry.items() if value.get_public_config()["configured"]), "custom")
    application = FastAPI(title="AIDD 蓝研 API", version=API_VERSION, docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        request.state.request_id = uuid4().hex
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
                return JSONResponse({"error": {"code": "CROSS_ORIGIN_REQUEST", "message": "仅允许来自本工作台的请求。", "requestId": request.state.request_id}}, status_code=403)
            size = request.headers.get("content-length", "0")
            if not size.isdigit() or int(size) > 16384:
                return JSONResponse({"error": {"code": "REQUEST_TOO_LARGE", "message": "请求内容过大。", "requestId": request.state.request_id}}, status_code=413)
            if request.url.path.startswith("/api/providers/") and request.url.path.endswith("/config") and request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                return JSONResponse({"error": {"code": "JSON_CONTENT_TYPE_REQUIRED", "message": "API 配置必须使用 JSON 请求。", "requestId": request.state.request_id}}, status_code=415)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        return response

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        message = "API 配置字段格式无效，请检查地址、模型和参数类型。" if request.url.path.startswith("/api/providers/") else "请求格式无效：需提供 SMILES、有效模式及供应商。"
        return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": message, "requestId": request.state.request_id}}, status_code=422)

    @application.exception_handler(ConfigurationError)
    async def invalid_configuration(request, error):
        return JSONResponse({"error": {"code": error.code, "message": error.message, "requestId": request.state.request_id}}, status_code=error.status_code)

    @application.get("/api/providers/{provider}/config")
    def provider_configuration(provider: str):
        return configuration.public(provider)

    @application.post("/api/providers/{provider}/config")
    def save_provider_configuration(provider: str, payload: ProviderConfigurationRequest):
        return configuration.save(provider, payload.model_dump(exclude_unset=True))

    @application.get("/api/health")
    def health():
        providers = [{"id": key, "label": PROVIDER_LABELS.get(key, key), **value.get_public_config()} for key, value in registry.items()]
        model = next((provider for provider in providers if provider["id"] == active_provider), {"configured": False, "providerFormat": None, "modelName": None, "version": None, "supportsImage": False})
        return {"status": "ok", "contractVersion": CONTRACT_VERSION, "apiVersion": API_VERSION, "chemistry": {"version": CHEMISTRY_VERSION, "rulesVersion": RULES_VERSION, "rdkitVersion": rdBase.rdkitVersion}, "model": model, "modelProviders": providers, "defaultProvider": active_provider, "capabilities": {"rules": True, "model": any(provider["configured"] for provider in providers)}}

    @application.get("/api/local-model/info")
    def local_model_info():
        info = local.get_model_info()
        external = read_external_evidence(info, (config_path.parent if config_path else ROOT) / "artifacts/external-herg-evaluation.json")
        if external:
            info["externalEvaluation"] = external
        return info

    @application.post("/api/analyze")
    async def analyze(payload: AnalyzeRequest, request: Request):
        try:
            return await run_in_threadpool(_compose, payload.smiles, payload.mode, payload.provider, registry, active_provider, request.state.request_id)
        except ValueError as error:
            return JSONResponse({"error": {"code": "INVALID_SMILES" if not payload.provider or payload.provider in registry else "INVALID_PROVIDER", "message": str(error), "requestId": request.state.request_id}}, status_code=422)
        except ModelAPIError as error:
            return JSONResponse({"error": {"code": error.code, "message": error.public_message, "requestId": request.state.request_id}}, status_code=error.status_code)
        except Exception:
            return JSONResponse({"error": {"code": "INTERNAL_ERROR", "message": "服务处理失败，请检查服务配置和版本。", "requestId": request.state.request_id}}, status_code=500)

    @application.get("/")
    def index():
        return FileResponse(ROOT / "index.html")

    @application.get("/{asset}")
    def static_asset(asset: str):
        if asset not in ("app.js", "toxicity-core.js", "styles.css"):
            return JSONResponse({"error": {"code": "NOT_FOUND", "message": "未找到该资源。"}}, status_code=404)
        return FileResponse(ROOT / asset)

    return application


app = create_app()
