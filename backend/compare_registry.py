"""Fixed capabilities describe roles, while adapter checks describe availability."""
from __future__ import annotations

import asyncio
from copy import deepcopy

from backend.compare_adapters import B16Adapter, DeepSeekReportAdapter, OllamaQwenAdapter, UnavailableAdapter

MODEL_SPECS = {
    "toxnet-v3-b16": {"label": "蓝研 ToxNet v3 B16", "type": "molecular-predictor", "runtime": "local-pytorch",
                      "capabilities": ["toxicity-prediction", "tox21-pathways"], "researchOnly": True,
                      "source": "verified-local-aidd-multitask-artifact", "semantics": "Four toxicity labels and twelve separate Tox21 assay activities; uncalibrated, not clinical probabilities."},
    "txgemma-2b-predict": {"label": "TxGemma-2B-Predict", "type": "therapeutic-task-predictor", "runtime": "local-transformers",
                          "capabilities": ["verified-tdc-tasks-only"], "researchOnly": True,
                          "source": "Google official Hugging Face model; gated terms and pinned revision required",
                          "semantics": "Only reviewed official task templates; categorical output is not a probability."},
    "ollama-qwen-local": {"label": "本地 Qwen · Ollama", "type": "report-generator", "runtime": "ollama",
                          "capabilities": ["chinese-report", "structured-generation"], "researchOnly": True,
                          "source": "actual-loopback-ollama-model-list", "semantics": "Organizes verified facts; never changes predictor scores or validates mechanisms."},
    "deepseek-api": {"label": "DeepSeek · 独立报告", "type": "report-generator", "runtime": "external-api",
                     "capabilities": ["independent-report", "structured-generation"], "researchOnly": True,
                     "source": "server-side-deepseek-account-configuration", "semantics": "Independent report of the same minimal facts, with explicit per-run cloud consent."},
}


class CompareRegistry:
    def __init__(self, adapters):
        if set(adapters) != set(MODEL_SPECS):
            raise ValueError("Compare registry must contain exactly the four contracted model IDs")
        self.adapters = dict(adapters)

    @classmethod
    def defaults(cls, env=None, configuration=None, txgemma_adapter=None):
        return cls({"toxnet-v3-b16": B16Adapter.from_env(env),
                    "txgemma-2b-predict": txgemma_adapter or UnavailableAdapter("txgemma-2b-predict"),
                    "ollama-qwen-local": OllamaQwenAdapter.from_env(env),
                    "deepseek-api": DeepSeekReportAdapter(configuration)})

    async def describe(self):
        # Sequential checks prevent two local loading probes using CUDA at once.
        # The outer orchestrator also holds its shared local-model resource lock.
        result = []
        for model_id, spec in MODEL_SPECS.items():
            try:
                status = await self.adapters[model_id].status()
                if not isinstance(status, dict) or type(status.get("available")) is not bool:
                    raise ValueError("Invalid adapter status")
            except asyncio.CancelledError:
                raise
            except Exception:
                status = {"available": False, "configured": False, "actualModelId": None, "revision": None,
                          "device": None, "licenseStatus": "unknown", "errorCode": "MODEL_STATUS_UNAVAILABLE", "reason": "真实模型状态检查失败。"}
            result.append({**deepcopy(spec), **deepcopy(status), "id": model_id, "formalAvailability": False})
        return result


def create_default_registry(configuration=None, *, env=None, txgemma_adapter=None):
    return CompareRegistry.defaults(env, configuration, txgemma_adapter)
