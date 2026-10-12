"""Offline validation helpers; no fabricated datasets, metrics, or calibration."""
from __future__ import annotations

import hashlib
import math
from collections import defaultdict

from rdkit import Chem
from rdkit.Chem import rdFingerprintGenerator
from rdkit.Chem.Scaffolds import MurckoScaffold
from rdkit import DataStructs
from backend.chemistry import canonicalize_smiles

ENDPOINTS = ("herg", "dili", "ames", "clintox")
VALIDATION_VERSION = "toxilens.validation.v1"


def canonicalize(smiles: str) -> str:
    return canonicalize_smiles(smiles)


def scaffold_split(rows: list[dict], seed: int = 42) -> dict:
    """Keep equivalent structures AND entire Bemis-Murcko scaffolds in one split."""
    groups = defaultdict(list)
    observed = {}
    duplicates = 0
    for raw in rows:
        endpoint, label = raw.get("endpoint"), raw.get("label")
        if endpoint not in ENDPOINTS or type(label) is not int or label not in (0, 1):
            raise ValueError("endpoint 必须属于四端点，label 必须为整数 0 或 1。")
        canonical = canonicalize(raw["smiles"])
        identity = (canonical, endpoint)
        if identity in observed:
            if observed[identity] != label:
                raise ValueError("同一规范结构和端点存在冲突标签；需先核查来源。")
            duplicates += 1
            continue
        observed[identity] = label
        mol = Chem.MolFromSmiles(canonical)
        scaffold = MurckoScaffold.MurckoScaffoldSmiles(mol=mol, includeChirality=False) or "[ACYCLIC]"
        groups[scaffold].append({**raw, "canonicalSmiles": canonical, "scaffold": scaffold})
    total = sum(map(len, groups.values()))
    targets = {"train": total * 0.8, "validation": total * 0.1, "test": total * 0.1}
    splits = {name: [] for name in targets}
    ordered = sorted(groups.items(), key=lambda pair: (-len(pair[1]), hashlib.sha256(f"{seed}:{pair[0]}".encode()).hexdigest()))
    for _, members in ordered:
        destination = max(targets, key=lambda name: targets[name] - len(splits[name]))
        splits[destination].extend({**row, "split": destination} for row in members)
    warnings = []
    class_counts = {}
    for name, records in splits.items():
        if not records:
            warnings.append(f"{name}: empty split; cannot establish a complete held-out evaluation")
        class_counts[name] = {}
        for endpoint in ENDPOINTS:
            labels = [record["label"] for record in records if record["endpoint"] == endpoint]
            class_counts[name][endpoint] = {"positive": sum(labels), "negative": len(labels) - sum(labels)}
            if labels and len(set(labels)) < 2:
                warnings.append(f"{name}/{endpoint}: single class; ranking metrics may be undefined")
    return {"version": VALIDATION_VERSION, "seed": seed, "method": "Bemis-Murcko scaffold; all acyclic molecules share one group", "requestedFractions": {"train": 0.8, "validation": 0.1, "test": 0.1}, "actualCounts": {name: len(records) for name, records in splits.items()}, "classCounts": class_counts, "warnings": warnings, "completeThreeWaySplit": all(splits.values()), "duplicatesRemoved": duplicates, "splits": splits}


def _ranking_metrics(records: list[dict]) -> tuple[float | None, float | None]:
    positives = sum(row["label"] for row in records)
    negatives = len(records) - positives
    if not positives or not negatives:
        return None, None
    groups = defaultdict(lambda: [0, 0])
    for row in records:
        groups[row["score"]][row["label"]] += 1
    negatives_below = 0
    wins = 0.0
    for _, (n, p) in sorted(groups.items()):
        wins += p * (negatives_below + 0.5 * n)
        negatives_below += n
    seen = true_positives = 0
    average_precision = 0.0
    for _, (n, p) in sorted(groups.items(), reverse=True):
        seen += n + p
        true_positives += p
        average_precision += (p / positives) * (true_positives / seen)
    return wins / (positives * negatives), average_precision


def evaluate_scores(rows: list[dict], score_kind: str = "uncalibrated_score", split_manifest: dict | None = None) -> dict:
    if score_kind not in ("uncalibrated_score", "probability", "binary_label"):
        raise ValueError("无效 score_kind。")
    groups = defaultdict(list)
    observed = set()
    test_membership = None
    if split_manifest is not None:
        ownership, scaffold_ownership, test_membership = {}, {}, {}
        for split, members in split_manifest["splits"].items():
            if split not in ("train", "validation", "test"):
                raise ValueError("划分清单含未知集合。")
            for member in members:
                canonical = canonicalize(member["smiles"])
                scaffold = MurckoScaffold.MurckoScaffoldSmiles(mol=Chem.MolFromSmiles(canonical), includeChirality=False) or "[ACYCLIC]"
                if ownership.setdefault(canonical, split) != split or scaffold_ownership.setdefault(scaffold, split) != split:
                    raise ValueError("划分清单发生结构或骨架泄漏。")
                if split == "test":
                    test_membership[(canonical, member["endpoint"])] = member["label"]
    for row in rows:
        if row.get("split") != "test":
            raise ValueError("最终性能评估仅允许 test 数据；校准和阈值选择使用 validation。")
        label, score, endpoint = row.get("label"), row.get("score"), row.get("endpoint")
        if endpoint not in ENDPOINTS or type(label) is not int or label not in (0, 1):
            raise ValueError("标签或端点无效。")
        if type(score) not in (int, float) or not math.isfinite(score):
            raise ValueError("分数必须为有限数字。")
        if score_kind == "probability" and not 0 <= score <= 1:
            raise ValueError("概率必须在 0..1 之间。")
        if score_kind == "binary_label" and score not in (0, 1):
            raise ValueError("binary_label 必须是 0 或 1。")
        key = (canonicalize(row["smiles"]), endpoint)
        if test_membership is not None and (key not in test_membership or test_membership[key] != label):
            raise ValueError("评估结构或标签不匹配 test 划分清单。")
        if key in observed:
            raise ValueError("测试集存在重复结构和端点。")
        observed.add(key)
        groups[endpoint].append(row)
    output = {}
    for endpoint in ENDPOINTS:
        records = groups[endpoint]
        if not records:
            output[endpoint] = {"count": 0, "status": "no-data"}
            continue
        auroc, ap = _ranking_metrics(records)
        result = {"count": len(records), "positives": sum(r["label"] for r in records), "auroc": auroc, "averagePrecision": ap, "status": "computed" if auroc is not None else "single-class"}
        if score_kind == "probability":
            result["brier"] = sum((r["score"] - r["label"]) ** 2 for r in records) / len(records)
            bins = defaultdict(list)
            for row in records:
                bins[min(9, int(row["score"] * 10))].append(row)
            result["ece10Bins"] = sum(len(bucket) / len(records) * abs(sum(r["score"] for r in bucket) / len(bucket) - sum(r["label"] for r in bucket) / len(bucket)) for bucket in bins.values())
            result["calibrationStatus"] = "measured-not-certified"
        output[endpoint] = result
    return {"version": VALIDATION_VERSION, "split": "test", "heldOutStatus": "manifest-membership-and-scaffold-separation-verified" if split_manifest is not None else "declared-not-verified", "trainingHistoryVerified": False, "scoreKind": score_kind, "endpoints": output}


def assess_applicability(smiles: str, reference_smiles: list[str], threshold: float = 0.5) -> dict:
    """A similarity heuristic only; caller must supply genuine training references."""
    canonical = canonicalize(smiles)
    if not reference_smiles:
        return {"status": "unknown", "reason": "no-training-reference-set"}
    if not 0 <= threshold <= 1:
        raise ValueError("阈值须在 0..1 之间。")
    generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    query = generator.GetFingerprint(Chem.MolFromSmiles(canonical))
    references = [generator.GetFingerprint(Chem.MolFromSmiles(canonicalize(value))) for value in reference_smiles]
    similarity = max(DataStructs.BulkTanimotoSimilarity(query, references))
    return {"status": "similarity-heuristic", "insideThreshold": similarity >= threshold, "maximumTanimoto": similarity, "threshold": threshold, "referenceCount": len(references), "method": "Morgan-radius2-2048/Tanimoto", "validated": False}
