"""Short-lived, offline B16 inference worker; never accepts browser file paths.

The parent supplies trusted server configuration over stdin. All model loading
uses the existing strict MultitaskModelClient. Exiting the process releases its
CUDA context, including after cancellation by the parent.
"""
from __future__ import annotations

import contextlib
import json
import sys
from time import perf_counter

from backend.chemistry import RULES_VERSION, analyze_chemistry
from backend.model_client import ModelAPIError
from backend.multitask_model import ALL_TASKS, MultitaskModelClient

WORKER_SCHEMA = "aidd.compare.b16.worker.v1"
MAX_INPUT_BYTES = 64 * 1024


def _cuda_oom(error):
    """Find genuine OOM types even when the existing loader wraps an error."""
    pending, seen = [error], set()
    while pending and len(seen) < 20:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if type(current).__name__ == "OutOfMemoryError" and type(current).__module__.startswith("torch"):
            return True
        pending.extend(value for value in (current.__cause__, current.__context__) if isinstance(value, BaseException))
    return False


def _require_b16(client):
    loaded = client._ensure_loaded()
    client._require_tox21(loaded)
    manifest = loaded["manifest"]
    architecture = manifest["architecture"]
    if (manifest["status"] != "trained-completed" or manifest["tasks"] != list(ALL_TASKS)
        or architecture.get("featureBranches") != ["molformer"]
        or architecture.get("headType") != "shared-mlp"
        or any(value != "trained" for value in manifest["training"]["endpointStatus"].values())):
        raise ModelAPIError("COMPARE_B16_REQUIRED", "Compare 主预测要求完整训练的 MoLFormer B16；不接受 A4、Morgan 或禁用通路的消融工件。", 503)
    return loaded


def _public_info(client, loaded):
    manifest = loaded["manifest"]
    encoder = manifest.get("encoder") or {}
    return {"available": True, "configured": True, "actualModelId": manifest["modelVersion"],
            "revision": loaded["manifestSha256"], "weightsSha256": manifest["artifacts"]["weights"]["sha256"],
            "device": client.device, "licenseStatus": "project-research-head; encoder-Apache-2.0",
            "encoderSource": encoder.get("source"), "encoderRevision": encoder.get("revision"),
            "taskContractVersion": manifest["taskContractVersion"], "tasks": manifest["tasks"],
            "taskContracts": manifest["taskContracts"], "formalAvailability": False,
            "reason": "真实离线 B16 工件、化学合同、任务状态及 SHA256 已校验。", "errorCode": None}


def run_worker(payload, emit=None):
    if (not isinstance(payload, dict) or payload.get("schema") != WORKER_SCHEMA
        or set(payload) - {"schema", "action", "modelDir", "encoderDir", "device", "enabled", "canonicalSmiles", "structureId", "includeTox21", "includeEvidence", "includeAttribution", "evidenceEndpoint", "explainEndpoint", "evidenceIndex"}
        or payload.get("action") not in ("status", "execute") or type(payload.get("enabled")) is not bool
        or payload.get("device") not in ("cpu", "cuda", "cuda:0")):
        raise ModelAPIError("COMPARE_WORKER_INVALID", "Compare 离线 worker 配置无效。", 503)
    if not payload["enabled"]:
        raise ModelAPIError("MULTITASK_RESEARCH_DISABLED", "必须显式启用 TOXNET_MULTITASK_ENABLE=true。", 503)
    started = perf_counter()
    torch = None
    gpu_metrics = {"peakAllocatedBytes": None, "peakReservedBytes": None, "measured": False}
    if payload["device"].startswith("cuda"):
        import torch as torch_module
        torch = torch_module
        if not torch.cuda.is_available():
            raise ModelAPIError("COMPARE_CUDA_UNAVAILABLE", "所配置的 CUDA 设备不可用；不会自动改用云端或模拟模型。", 503)
        torch.cuda.reset_peak_memory_stats()
    client = MultitaskModelClient(payload.get("modelDir"), encoder_dir=payload.get("encoderDir"),
                                  device=payload["device"], allow_research=payload["enabled"])
    loaded = _require_b16(client)
    info = _public_info(client, loaded)
    if payload["action"] == "status":
        return {"ok": True, "info": info}
    prediction_started = perf_counter()
    prediction = client.predict_all(payload["canonicalSmiles"])
    if prediction["structureId"] != payload.get("structureId"):
        raise ModelAPIError("COMPARE_STRUCTURE_MISMATCH", "B16 输出结构标识与 Compare 输入不一致。", 409)
    prediction_seconds = perf_counter() - prediction_started
    explain_endpoint = payload.get('explainEndpoint') if payload.get('includeAttribution', payload.get('explainEndpoint') is not None) else None
    # B16 always computes/preserves all sixteen outputs. includeTox21 controls
    # snapshot/UI scope in the parent, never the true model output contract.
    chemistry = analyze_chemistry(payload["canonicalSmiles"])
    alerts = {"status": "available", "alerts": chemistry["alerts"], "rulesVersion": RULES_VERSION,
              "sourceKind": "human-rule", "notNeuralAttribution": True}
    if emit is not None:
        # Publish verified primary output before potentially expensive optional
        # source/fragment audits. The parent can retain this on audit timeout.
        emit({"kind": "prediction", "ok": True, "info": info,
              "result": {"status": "partial", "data": {"prediction": prediction, "structuralAlerts": alerts,
                       "evidence": {"status": "pending" if payload.get("includeEvidence") else "not-requested", "results": []},
                       "attribution": {"status": "pending" if explain_endpoint else "not-requested", "atoms": [], "fragments": []}},
                       "provenance": {**prediction["provenance"], "device": client.device, "isolatedWorker": True},
                       "metrics": {"loadAndExecutionSeconds": perf_counter() - started, "predictionSeconds": prediction_seconds,
                                   "gpu": gpu_metrics, "promptTokens": None, "completionTokens": None, "cost": None}}})
    evidence = {"status": "not-requested", "results": []}
    attribution = {"status": "not-requested", "atoms": [], "fragments": []}
    partial = False
    if payload.get("includeEvidence"):
        try:
            from backend.evidence_search import EvidenceSearch
            evidence = EvidenceSearch(payload["evidenceIndex"]).search(payload["canonicalSmiles"], endpoint=payload.get("evidenceEndpoint", payload.get("explainEndpoint")), limit=3)
        except Exception:
            evidence = {"status": "insufficient-evidence", "results": [], "errorCode": "COMPARE_EVIDENCE_UNAVAILABLE"}
        partial |= evidence["status"] != "available"
    if explain_endpoint:
        try:
            from backend.attribution import AttributionEngine
            attribution = AttributionEngine(None, client).explain(payload["canonicalSmiles"], endpoint=explain_endpoint, model="multitask")
            if attribution.get("status") in ("available", "computed-with-warning") and any(
                attribution.get("provenance", {}).get(key) != prediction["provenance"].get(key)
                for key in ("weightsSha256", "manifestSha256", "modelVersion")):
                attribution = {"status": "unavailable", "atoms": [], "fragments": [], "errorCode": "ATTRIBUTION_MODEL_CHANGED"}
        except Exception:
            attribution = {"status": "unavailable", "atoms": [], "fragments": [], "errorCode": "COMPARE_ATTRIBUTION_UNAVAILABLE"}
        partial |= attribution["status"] != "available"
    if torch is not None:
        torch.cuda.synchronize()
        gpu_metrics = {"peakAllocatedBytes": torch.cuda.max_memory_allocated(),
                       "peakReservedBytes": torch.cuda.max_memory_reserved(), "measured": True,
                       "scope": "this isolated B16 process including load and requested attribution; not total board memory"}
    return {"ok": True, "result": {"status": "partial" if partial else "completed",
            "data": {"prediction": prediction, "evidence": evidence, "attribution": attribution,
                     "structuralAlerts": alerts},
            "provenance": {**prediction["provenance"], "device": client.device, "isolatedWorker": True,
                           "researchSteps": {"evidenceRequested": payload.get('includeEvidence') is True,
                                             "evidenceEndpoint": payload.get('evidenceEndpoint', payload.get('explainEndpoint')),
                                             "attributionRequested": explain_endpoint is not None,
                                             "attributionEndpoint": explain_endpoint}},
            "metrics": {"loadAndExecutionSeconds": perf_counter() - started, "predictionSeconds": prediction_seconds,
                        "promptTokens": None, "completionTokens": None, "cost": None, "gpu": gpu_metrics}}, "info": info}


def main():
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    protocol_output = sys.stdout
    def emit(value):
        protocol_output.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        protocol_output.flush()
    try:
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("oversized")
        payload = json.loads(raw)
        # Library progress output is diagnostic, never mixed into protocol JSON.
        with contextlib.redirect_stdout(sys.stderr):
            result = run_worker(payload, emit)
    except ModelAPIError as error:
        result = {"ok": False, "error": {"code": "COMPARE_CUDA_OOM" if _cuda_oom(error) else error.code,
                  "message": "B16 GPU 显存不足；隔离进程退出释放本次资源。请关闭其他 GPU 程序或显式配置 CPU，未切换模型。" if _cuda_oom(error) else error.public_message}}
    except Exception as error:
        # Never serialize exception strings containing paths, data or secrets.
        result = {"ok": False, "error": {"code": "COMPARE_CUDA_OOM" if _cuda_oom(error) else "COMPARE_B16_UNAVAILABLE",
                  "message": "B16 GPU 显存不足；隔离进程退出释放本次资源。未自动切换模型或云端。" if _cuda_oom(error) else "B16 真实推理不可用；请核验工件、依赖、设备和离线编码器。"}}
    emit({"kind": "final", **result})
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
