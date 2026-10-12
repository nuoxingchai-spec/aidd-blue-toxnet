"""Expose separately recorded research evidence for the exact loaded weights."""
import hashlib
import json
import math
from pathlib import Path


def read_external_evidence(info: dict, path: Path) -> dict | None:
    if not info.get("configured") or not info.get("weightsSha256"):
        return None
    try:
        if path.stat().st_size > 256_000:
            return None
        raw = path.read_bytes()
        record = json.loads(raw)
        if (record.get("schema") != "aidd.external-herg-evaluation.v1" or record.get("status") != "completed"
                or record.get("modelVersion") != info.get("version")
                or record.get("weightsSha256") != info.get("weightsSha256")
                or record.get("modelManifestSha256") != info.get("artifactSha256")
                or any(record.get(key) is not False for key in ("modelUpdated", "calibrationRefit", "thresholdSelected", "prospective"))):
            return None
        for key in ("neuralModel", "fingerprintBaseline"):
            metrics = record[key]
            if type(metrics.get("count")) is not int or metrics["count"] <= 0 or type(metrics.get("positives")) is not int or not 0 <= metrics["positives"] <= metrics["count"]:
                return None
            for field in ("auroc", "averagePrecision", "brier", "ece10Bins"):
                value = metrics.get(field)
                if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1:
                    return None
        if record["neuralModel"]["count"] != record["fingerprintBaseline"]["count"] or record["neuralModel"]["positives"] != record["fingerprintBaseline"]["positives"]:
            return None
        return {"datasetName": "TDC hERG_Karim", "endpoint": "herg", "modelVersion": record["modelVersion"],
                "candidateVersion": record["candidateVersion"],
                "neuralModel": {field: record["neuralModel"][field] for field in ("count", "positives", "auroc", "averagePrecision", "brier", "ece10Bins")},
                "fingerprintBaseline": {field: record["fingerprintBaseline"][field] for field in ("count", "positives", "auroc", "averagePrecision", "brier", "ece10Bins")}, "poolSha256": record["poolSha256"],
                "evidenceSha256": hashlib.sha256(raw).hexdigest(), "prospective": False,
                "sourcePopulationIndependenceVerified": False, "calibrationRefit": False,
                "meaning": "外部来源回顾性评估；已排除旧结构、母体和骨架重叠，但来源人群独立性未验证。",
                "limitation": "hERG 标签阈值与原训练来源不完全一致；外部来源下概率误差较大，当前仍为研究原型。"}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
