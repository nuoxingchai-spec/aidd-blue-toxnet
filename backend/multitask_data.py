"""Isolated 16-task development: four toxicity labels plus 12 Tox21 assays.

Task names never substitute for each other. The exposed four-task development
validation remains development data, not a newly independent performance test.
All tasks share indivisible chemistry groups and the same forbidden references.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from rdkit import rdBase

from backend.chemistry import CHEMISTRY_VERSION, canonicalize_smiles
from backend.training_data import DATASETS, ENDPOINTS, LICENSE_NOTE, sha256_file
from backend.candidate_data import (SOURCES, TOX21_ASSAYS, TOX21_ENDPOINTS,
                                    normalize_candidate_rows, read_source_rows)
from backend.v3_data import (IDENTITY_POLICY, IDENTITY_VERSION, V2_MANIFEST_SHA256,
                             V2_SPLIT_SHA256, EXPOSED_POOL_SHA256, FDA_HASHES, _components,
                             _digest, _fda_audit, _json_bytes, _load_json, _read_csv,
                             _required_hash, _verified_inputs, _verify_source_role_contract,
                             build_forbidden_index, identity_tokens, leakage_identity)

FOUR_ENDPOINTS = ENDPOINTS
ALL_ENDPOINTS = (*FOUR_ENDPOINTS, *TOX21_ENDPOINTS)
DATA_SCHEMA = "aidd.toxnet.multitask-development-data.v1"
TASK_CONTRACT_VERSION = "aidd.toxnet.four-toxicity-twelve-assay-tasks.v1"
SPLIT_SCHEMA = "aidd.toxnet.multitask-development-split.v1"
TOX21_SPEC = next(source for source in SOURCES if source["id"] == "tox21")
TOX21_PROCESSED_SHA256 = "6546b23e114788a0e5c2ca41122b83b88e6a0ba1cedcf760c03db005d831059c"
PREVIOUS_DEVELOPMENT_VERSION = "aidd-toxnet-v3-development-c1d448002df99aa3"
PREVIOUS_DEVELOPMENT_MANIFEST_SHA256 = "1fffa35d1da6e4f58e269a0d74d6dc3473d98251ae419b8c8c762a7bbe4b4bf6"
PREVIOUS_DEVELOPMENT_SPLIT_SHA256 = "7701f6c3d8b69a7b1ed918e1365d39e79726414ce2898dca157f7a42134cbc91"


def task_contracts() -> dict:
    result = {spec["endpoint"]: {"group": "four-toxicity-tasks", "positiveClass": spec["positiveClass"],
                                 "sourceSha256": spec["sha256"], "reference": spec["reference"]}
              for spec in DATASETS}
    result.update({endpoint: {"group": "tox21-assay-activity", "sourceAssay": assay,
                             "positiveClass": f"Tox21 {assay} assay active (1); inactive (0); absent label remains unknown",
                             "sourceSha256": TOX21_SPEC["sha256"], "sourceFileId": TOX21_SPEC["fileId"],
                             "documentationUrl": TOX21_SPEC["documentationUrl"],
                             "clinicalToxicityInterpretation": False}
                   for endpoint, assay in zip(TOX21_ENDPOINTS, TOX21_ASSAYS)})
    return result


def training_permission() -> dict:
    return {"status": "research-only-source-rights-unresolved", "allowedRoles": ["train-optimization", "validation-model-selection"],
            "approvedLabelSources": [spec["name"] for spec in DATASETS] + ["TDC Tox21"],
            "commercialRedistributionApproved": False, "externalOrHeldoutLabelsAllowed": False,
            "tox21LabelsSubstitutedForFourEndpoints": False, "note": LICENSE_NOTE}


def class_counts(samples: list[dict]) -> dict:
    return {split: {endpoint: {"positive": sum(sample["labels"][endpoint] for sample in samples if sample["split"] == split and endpoint in sample["labels"]),
                              "negative": sum(1 - sample["labels"][endpoint] for sample in samples if sample["split"] == split and endpoint in sample["labels"])}
                    for endpoint in ALL_ENDPOINTS} for split in ("train", "validation")}


def observation_provenance(row: dict) -> dict:
    endpoint = row["endpoint"]
    return {"endpoint": endpoint, "sourceDataset": "tox21" if endpoint in TOX21_ENDPOINTS else "tdc-four-task",
            "source_id": str(row.get("source_id", "")), "source_row": str(row.get("source_row", "")),
            "sourceAssay": row.get("source_assay", "") if endpoint in TOX21_ENDPOINTS else endpoint}


def split_multitask_data(rows: list[dict], forbidden: dict[str, list[str]], *, seed: int = 42,
                         validation_fraction: float = 0.2,
                         label_blind_filter: Callable[[str], str] | None = None) -> tuple[list[dict], list[dict], dict, dict]:
    """One molecular split for all tasks; missing observations are not rows."""
    if type(seed) is not int or not math.isfinite(validation_fraction) or not 0 < validation_fraction < 1:
        raise ValueError("Invalid fixed multitask development split arguments")
    forbidden_index, forbidden_counts = build_forbidden_index(forbidden)
    molecules, input_structures, exclusions, canonical_cache, incompatibilities = {}, set(), [], {}, {}
    for row in rows:
        endpoint, label = row.get("endpoint"), row.get("label")
        if endpoint not in ALL_ENDPOINTS or type(label) is not int or label not in (0, 1):
            raise ValueError("Only 16 independent observed binary tasks are permitted; absent labels stay absent")
        if endpoint in TOX21_ENDPOINTS:
            expected_assay = TOX21_ASSAYS[TOX21_ENDPOINTS.index(endpoint)]
            if row.get("source_dataset") != "tox21" or row.get("source_assay") != expected_assay:
                raise ValueError("Tox21 source assay/task contract mismatch; no endpoint relabelling")
        elif row.get("source_dataset") == "tox21":
            raise ValueError("Tox21 assay activity cannot supply a four-endpoint label")
        try:
            original = row["smiles"]
            if original not in canonical_cache:
                canonical_cache[original] = canonicalize_smiles(original)
            canonical = canonical_cache[original]
            input_structures.add(canonical)
            if canonical in incompatibilities:
                raise ValueError(incompatibilities[canonical])
            if canonical not in molecules:
                try:
                    if label_blind_filter is not None and label_blind_filter(canonical) != canonical:
                        raise ValueError("Encoder compatibility policy changed chemical identity")
                    molecules[canonical] = {"smiles": canonical, "labels": {}, "provenance": [],
                                            "auditIdentity": leakage_identity(canonical)}
                except (ValueError, RuntimeError, TypeError) as error:
                    incompatibilities[canonical] = str(error)
                    raise
        except (ValueError, RuntimeError, TypeError) as error:
            exclusions.append({"smiles": row.get("smiles"), "endpoint": endpoint, "label": label,
                               "reason": "label-blind-input-or-audit-incompatible", "detail": str(error)})
            continue
        sample = molecules[canonical]
        if endpoint in sample["labels"]:
            raise ValueError("Duplicate/conflicting canonical task observations must be resolved at the verified source stage")
        sample["labels"][endpoint] = label
        sample["provenance"].append(observation_provenance(row))
    eligible, direct_overlaps = [], Counter()
    for members in _components([molecules[key] for key in sorted(molecules)]):
        reasons = set()
        for member in members:
            direct = {role + ":" + token.split(":", 1)[0]
                      for token in identity_tokens(member["auditIdentity"]) for role in forbidden_index.get(token, ())}
            direct_overlaps.update(direct)
            reasons.update(direct)
        group_id = _digest(sorted(sample["smiles"] for sample in members))
        if reasons:
            exclusions.extend({"smiles": sample["smiles"], "labels": sample["labels"], "groupId": group_id,
                               "reason": "forbidden-overlap-connected-component", "overlapReasons": sorted(reasons)} for sample in members)
        else:
            eligible.append((group_id, members))
    total = sum(len(sample["labels"]) for _, members in eligible for sample in members)
    targets = {"train": total * (1 - validation_fraction), "validation": total * validation_fraction}
    allocated = {name: 0 for name in targets}
    samples = []
    ordered = sorted(eligible, key=lambda group: (-sum(len(sample["labels"]) for sample in group[1]),
                                                  hashlib.sha256(f"{seed}:{group[0]}".encode()).hexdigest()))
    for group_id, members in ordered:
        destination = max(targets, key=lambda name: targets[name] - allocated[name])
        for member in members:
            samples.append({**member, "split": destination, "groupId": group_id})
            allocated[destination] += len(member["labels"])
    samples.sort(key=lambda sample: sample["smiles"])
    counts = class_counts(samples)
    warnings = [f"{split}/{endpoint}: missing or single class; ranking metrics cannot be estimated"
                for split, tasks in counts.items() for endpoint, values in tasks.items() if min(values.values()) == 0]
    usable = all(any(sample["split"] == name for sample in samples) for name in targets)
    if not usable:
        warnings.append("Indivisible chemistry groups cannot establish a usable train/validation split")
    audit = {"status": "prepared-development-only" if usable else "blocked-insufficient-disjoint-groups",
             "identityVersion": IDENTITY_VERSION, "identityPolicy": IDENTITY_POLICY,
             "forbiddenRoles": forbidden_counts, "exposedHeldoutLabelsUsedForSelection": False,
             "inputLabelRows": len(rows), "inputUniqueStructuresBeforeCompatibility": len(input_structures),
             "inputUniqueStructuresAfterCompatibility": len(molecules), "eligibleUniqueStructures": len(samples),
             "eligibleLabelRows": total, "excludedUniqueStructures": len({row["smiles"] for row in exclusions}),
             "excludedLabelRows": sum(len(row.get("labels", {})) or 1 for row in exclusions),
             "exclusionReasons": dict(Counter(row["reason"] for row in exclusions)),
             "directOverlapMoleculesByRoleAndIdentity": dict(sorted(direct_overlaps.items())),
             "groupCount": len(eligible), "largestGroupMolecules": max((len(members) for _, members in eligible), default=0),
             "acyclicGroupSizes": [len(members) for _, members in eligible if any("[ACYCLIC]" in member["auditIdentity"]["scaffolds"] for member in members)],
             "acyclicPolicy": "one indivisible connected component; never fracture to improve class balance",
             "sharedTaskSplit": True, "taskLabelSubstitution": False, "classCounts": counts, "warnings": warnings,
             "newBlindTestClaimed": False, "calibrationCertificationAllowed": False}
    verify_multitask_samples(samples, forbidden_index)
    return samples, exclusions, audit, forbidden_index


def verify_multitask_samples(samples: list[dict], forbidden_index: dict) -> None:
    seen, ownership = set(), {}
    for sample in samples:
        if sample.get("split") not in ("train", "validation"):
            raise ValueError("Multitask data are development train/validation only")
        smiles = canonicalize_smiles(sample.get("smiles"))
        if sample["smiles"] != smiles or smiles in seen:
            raise ValueError("Multitask inputs must be unique strict canonical complete structures")
        seen.add(smiles)
        labels = sample.get("labels")
        if not isinstance(labels, dict) or not labels or any(endpoint not in ALL_ENDPOINTS or type(label) is not int or label not in (0, 1)
                                                          for endpoint, label in labels.items()):
            raise ValueError("Invalid 16-task observed-label mask contract")
        identity = leakage_identity(smiles)
        if sample.get("auditIdentity") != identity:
            raise ValueError("Multitask chemistry identity mismatch")
        group = sample.get("groupId")
        if not isinstance(group, str) or len(group) != 64:
            raise ValueError("Invalid shared multitask component ID")
        for token in identity_tokens(identity):
            if token in forbidden_index:
                raise ValueError("Multitask structures overlap forbidden old validation/test/exposed Karim")
            if ownership.setdefault(token, (sample["split"], group)) != (sample["split"], group):
                raise ValueError("Cross-task canonical/parent/tautomer/scaffold component leakage")
    for members in _components(samples):
        expected = _digest(sorted(sample["smiles"] for sample in members))
        if any(sample["groupId"] != expected for sample in members):
            raise ValueError("Shared multitask component ID does not match its entire structure group")


def verify_observation_membership(samples: list[dict], frozen_split: dict, tox21_rows: list[dict]) -> None:
    canonical_cache = {}

    def canonical(smiles):
        if smiles not in canonical_cache:
            canonical_cache[smiles] = canonicalize_smiles(smiles)
        return canonical_cache[smiles]

    allowed = {(canonical(row["smiles"]), row["endpoint"]): row for row in frozen_split["splits"]["train"]}
    for row in tox21_rows:
        key = canonical(row["smiles"]), row["endpoint"]
        if key in allowed or row["endpoint"] not in TOX21_ENDPOINTS:
            raise ValueError("Tox21 cannot overwrite four-endpoint experimental observations")
        allowed[key] = {**row, "label": int(row["label"])}
    for sample in samples:
        provenance = sample.get("provenance", [])
        by_task = {row["endpoint"]: row for row in provenance}
        if len(by_task) != len(provenance) or set(by_task) != set(sample["labels"]):
            raise ValueError("Observation provenance does not match the 16-task missing-label mask")
        for endpoint, label in sample["labels"].items():
            original = allowed.get((sample["smiles"], endpoint))
            if original is None or original["label"] != label or by_task[endpoint] != observation_provenance(original):
                raise ValueError("Multitask observation is not an unchanged verified old TRAIN/Tox21 raw experimental label")


def _verified_tox21_rows(candidate_dir: Path, historical_rows: list[dict]) -> tuple[list[dict], dict]:
    path = candidate_dir / "processed/tox21.csv"
    _required_hash(path, TOX21_PROCESSED_SHA256)
    raw, summary = read_source_rows(candidate_dir / "raw/tox21.csv", "tox21")
    if summary["sourceRows"] != TOX21_SPEC["documentedRows"]:
        raise ValueError("Pinned Tox21 raw row count mismatch")
    retained, quarantine, duplicates, cleaning = normalize_candidate_rows(raw)

    def key(row):
        return row["smiles"], row["endpoint"], int(row["label"]), observation_provenance(row)

    regenerated = { (row["smiles"], row["endpoint"]): key(row) for row in retained }
    historical = { (row["smiles"], row["endpoint"]): key(row) for row in historical_rows }
    if len(regenerated) != len(retained) or len(historical) != len(historical_rows) or regenerated != historical:
        raise ValueError("Tox21 clean labels/provenance cannot be reproduced from pinned raw assay columns")
    return retained, {"sourceCounts": summary, "normalization": cleaning, "quarantinedLabelRows": len(quarantine),
                      "rawMissingAssayCells": sum(counts["missingLabels"] for counts in summary["endpoints"].values()),
                      "rawObservedAssayCells": len(raw), "retainedCandidateObservedAssayCells": len(retained),
                      "retainedCandidateMissingAssayCells": len({row["smiles"] for row in retained}) * len(TOX21_ENDPOINTS) - len(retained),
                      "sameLabelDuplicatesRemoved": len(duplicates), "rawLabelsRebuiltAndMatched": True,
                      "missingLabelPolicy": "absent assay cells remain absent, never negative or four-task proxy labels"}


def _previous_development(directory: Path, sources: list) -> dict:
    if not directory.exists():
        return {"datasetVersion": PREVIOUS_DEVELOPMENT_VERSION,
                "manifestSha256": PREVIOUS_DEVELOPMENT_MANIFEST_SHA256, "splitSha256": PREVIOUS_DEVELOPMENT_SPLIT_SHA256,
                "role": "previously-exposed-development", "allowedForNewDevelopment": True,
                "selectionValidationAlreadyExposed": True, "newBlindTestClaimed": False,
                "sameSplitHistoricalMetricsComparable": False, "structuresBySplit": None,
                "structureAudit": "not-assessable-historical-structures-not-distributed", "unknownStructureReuse": True}
    manifest_path, split_path = directory / "manifest.json", directory / "split.json"
    _required_hash(manifest_path, PREVIOUS_DEVELOPMENT_MANIFEST_SHA256)
    _required_hash(split_path, PREVIOUS_DEVELOPMENT_SPLIT_SHA256)
    manifest, split = _load_json(manifest_path), _load_json(split_path)
    if manifest.get("datasetVersion") != PREVIOUS_DEVELOPMENT_VERSION or set(split.get("splits", {})) != {"train", "validation"}:
        raise ValueError("Unsupported previously exposed four-task development reference")
    sources.extend((("previously-exposed-v3-development-manifest", manifest_path.resolve(), PREVIOUS_DEVELOPMENT_MANIFEST_SHA256),
                    ("previously-exposed-v3-development-split", split_path.resolve(), PREVIOUS_DEVELOPMENT_SPLIT_SHA256)))
    return {"datasetVersion": manifest["datasetVersion"], "manifestSha256": PREVIOUS_DEVELOPMENT_MANIFEST_SHA256,
            "splitSha256": PREVIOUS_DEVELOPMENT_SPLIT_SHA256, "role": "previously-exposed-development",
            "allowedForNewDevelopment": True, "selectionValidationAlreadyExposed": True,
            "newBlindTestClaimed": False, "sameSplitHistoricalMetricsComparable": False,
            "structuresBySplit": split["splits"]}


def previous_development_overlap(samples: list[dict], reference: dict) -> dict:
    if reference.get("structuresBySplit") is None:
        return None
    index = {name: set(structures) for name, structures in reference["structuresBySplit"].items()}
    return {current: {previous: sum(sample["split"] == current and sample["smiles"] in structures for sample in samples)
                      for previous, structures in index.items()} for current in ("train", "validation")}


def _data_version(files: dict, sources: list, encoder_policy: dict) -> str:
    return "aidd-toxnet-16task-development-" + _digest({"schema": DATA_SCHEMA, "taskContractVersion": TASK_CONTRACT_VERSION,
           "chemistryVersion": CHEMISTRY_VERSION, "identityVersion": IDENTITY_VERSION, "rdkitVersion": rdBase.rdkitVersion,
           "files": files, "sources": [{"role": row["role"], "sha256": row["sha256"], "useInThisProtocol": row["useInThisProtocol"]} for row in sources],
           "encoderCompatibility": encoder_policy})[:16]


def source_usage(role: str) -> str:
    """Legacy verification role names remain unchanged; new usage is explicit."""
    if role in {"audit-only-source-tox21", "audit-only-tox21-clean-structures"}:
        return "multitask-experimental-label-source"
    if role in {"previously-exposed-herg-pool", "previously-exposed-herg-evidence", "audit-only-source-herg_karim"}:
        return "structural-exclusion-only"
    if role == "frozen-v2-split":
        return "old-train-membership-and-heldout-exclusion; heldout-labels-never-optimized"
    if role.startswith("experimental-source-") or role == "clintox-label-verification":
        return "original-four-task-label-verification"
    if role.startswith("previously-exposed-v3-development-"):
        return "exposure-audit-only"
    if role.startswith("audit-only-fda-"):
        return "audit-only-pending-structure-mapping"
    if role == "verified-experimental-labels":
        return "old-train-four-task-experimental-label-membership"
    if role.startswith("frozen-v2-"):
        return "frozen-v2-preservation-reference"
    return "pinned-source-provenance-verification"


def _multitask_source_roles(records: list[dict], directory: Path) -> dict[str, Path]:
    """Only reviewed source roles may appear; required roles remain pinned."""
    unique_roles = {
        "frozen-v2-model-manifest", "frozen-v2-split", "frozen-v2-weights", "frozen-v2-trainingReferences", "frozen-v2-evaluation",
        "previously-exposed-herg-evidence", "processed-data-manifest", "verified-experimental-labels", "clintox-label-verification",
        *("experimental-source-" + endpoint for endpoint in FOUR_ENDPOINTS), "candidate-audit-manifest",
        "audit-only-source-herg_karim", "audit-only-source-tox21", "previously-exposed-herg-pool",
        "audit-only-tox21-clean-structures", "audit-only-fda-provenance",
        "previously-exposed-v3-development-manifest", "previously-exposed-v3-development-split",
    }
    counts = Counter(entry.get("role") for entry in records)
    if any(role not in unique_roles and role != "audit-only-fda-raw" for role in counts):
        raise ValueError("Unsupported multitask source provenance role")
    if any(counts[role] != 1 for role in unique_roles if role in counts):
        raise ValueError("Duplicate multitask source provenance role")
    if any(entry.get("useInThisProtocol") != source_usage(entry["role"]) for entry in records):
        raise ValueError("Multitask source usage contract changed; inherited audit-only role is not its new training usage")
    fda_records = [entry for entry in records if entry["role"] == "audit-only-fda-raw"]
    if len(fda_records) > 2 or len({entry.get("sha256") for entry in fda_records}) != len(fda_records) or any(entry.get("sha256") not in FDA_HASHES.values() for entry in fda_records):
        raise ValueError("Unsupported/duplicate FDA audit source snapshot")
    roles = _verify_source_role_contract(records, directory)
    return roles


def prepare_multitask_data(data_dir: Path, model_dir: Path, candidate_dir: Path, output_dir: Path,
                           external_evidence: Path, *, previous_development_dir: Path | None = None,
                           seed: int = 42, validation_fraction: float = 0.2,
                           encoder_compatible: str = "molformer") -> dict:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise ValueError("Refusing to overwrite multitask development data; select a fresh output directory")
    if encoder_compatible not in ("none", "molformer"):
        raise ValueError("Unsupported label-blind encoder compatibility policy")
    encoder_policy, input_filter = {"id": "none", "labelsUsedForFiltering": False}, None
    if encoder_compatible == "molformer":
        from backend.pretrained_encoder import ENCODER_VERSION, MODEL_REVISION, MAX_TOKENS, validate_encoder_smiles
        input_filter = validate_encoder_smiles
        encoder_policy = {"id": ENCODER_VERSION, "modelRevision": MODEL_REVISION, "maxTokens": MAX_TOKENS,
                          "labelsUsedForFiltering": False, "appliedBeforeSharedSplit": True, "appliesToAllComparators": True,
                          "policy": "strict complete canonical input; no UNK or truncation"}
    four_rows, forbidden, model, candidates, sources = _verified_inputs(data_dir, model_dir, candidate_dir, external_evidence)
    tox21_rows, tox21_reconstruction = _verified_tox21_rows(candidate_dir, candidates["tox21Rows"])
    previous = _previous_development(previous_development_dir or data_dir / "v3-development", sources)
    samples, exclusions, audit, forbidden_index = split_multitask_data(four_rows + tox21_rows, forbidden, seed=seed,
        validation_fraction=validation_fraction, label_blind_filter=input_filter)
    audit["tox21RawVerification"] = tox21_reconstruction
    audit["encoderCompatibility"] = encoder_policy
    audit["previouslyExposedDevelopment"] = {key: value for key, value in previous.items() if key != "structuresBySplit"}
    audit["previouslyExposedDevelopment"]["canonicalOverlapWithCurrentSplits"] = previous_development_overlap(samples, previous)
    audit["fda"] = _fda_audit(candidate_dir, sources)
    frozen = _load_json(model_dir / "split.json")
    verify_observation_membership(samples, frozen, tox21_rows)
    output_dir.mkdir(parents=True, exist_ok=False)
    split = {"schema": SPLIT_SCHEMA, "seed": seed, "taskContractVersion": TASK_CONTRACT_VERSION,
             "requestedFractions": {"train": 1 - validation_fraction, "validation": validation_fraction},
             "method": "all 16 tasks share indivisible conservative chemistry components; size-greedy fixed-hash seed",
             "splits": {name: [sample["smiles"] for sample in samples if sample["split"] == name] for name in ("train", "validation")},
             "groupIds": {sample["smiles"]: sample["groupId"] for sample in samples}, "classCounts": audit["classCounts"],
             "sharedTaskSplit": True, "blindTest": False, "calibrationCertificationAllowed": False}
    payloads = {"split": ("split.json", _json_bytes(split)), "audit": ("audit.json", _json_bytes(audit)),
                "forbiddenIdentities": ("forbidden-identities.json", _json_bytes(forbidden_index)),
                "samples": ("samples.jsonl", b"".join((json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8") for row in samples)),
                "exclusions": ("exclusions.jsonl", b"".join((json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8") for row in exclusions))}
    files = {}
    for name, (filename, content) in payloads.items():
        (output_dir / filename).write_bytes(content)
        files[name] = {"file": filename, "sha256": sha256_file(output_dir / filename)}
    source_records = [{"role": role, "pathFromData": os.path.relpath(path, output_dir).replace("\\", "/"), "sha256": checksum,
                       "useInThisProtocol": source_usage(role)}
                      for role, path, checksum in sources]
    manifest = {"schema": DATA_SCHEMA, "datasetVersion": _data_version(files, source_records, encoder_policy),
                "createdAtUtc": datetime.now(timezone.utc).isoformat(), "status": audit["status"],
                "taskContractVersion": TASK_CONTRACT_VERSION, "taskContracts": task_contracts(), "endpoints": list(ALL_ENDPOINTS),
                "fourEndpoints": list(FOUR_ENDPOINTS), "tox21Endpoints": list(TOX21_ENDPOINTS),
                "chemistryVersion": CHEMISTRY_VERSION, "identityVersion": IDENTITY_VERSION, "rdkitVersion": rdBase.rdkitVersion,
                "trainingPermission": training_permission(), "encoderCompatibility": encoder_policy,
                "sourceFiles": source_records, "files": files, "splitSha256": files["split"]["sha256"],
                "sourceRoleMeaning": "role is the inherited immutable source-verification role; useInThisProtocol is the explicit 16-task usage",
                "sampleCounts": {name: len(members) for name, members in split["splits"].items()},
                "classCounts": audit["classCounts"], "warnings": audit["warnings"],
                "fourTaskMoleculeCounts": {name: sum(sample["split"] == name and any(task in FOUR_ENDPOINTS for task in sample["labels"]) for sample in samples) for name in split["splits"]},
                "previouslyExposedDevelopment": audit["previouslyExposedDevelopment"],
                "frozenReference": {"modelVersion": model["modelVersion"], "manifestSha256": V2_MANIFEST_SHA256,
                                    "splitSha256": V2_SPLIT_SHA256, "previouslyExposedHergPoolSha256": EXPOSED_POOL_SHA256,
                                    "heldoutLabelsAllowed": False, "v2Modified": False, "fourTaskV3Modified": False},
                "evaluationStatus": "not-evaluated", "calibrationStatus": "not-fitted", "calibrationCertificationAllowed": False,
                "applicabilityValidated": False, "newBlindTestClaimed": False,
                "limitations": ["Tox21 labels are assay activity, never clinical toxicity or replacement four-endpoint labels.",
                                "This is development validation used for model selection; no independent new test or calibration certification.",
                                "Previously exposed four-task v3 development structures may enter this new development split and are counted explicitly.",
                                "Compare models only on this shared new split; historical v3 metrics are not same-split controls.",
                                "FDA has no verified structural mapping; it supplies no observations.",
                                "Underlying dataset redistribution/commercial rights and source-population independence remain unverified."]}
    (output_dir / "manifest.json").write_bytes(_json_bytes(manifest))
    for _, path, checksum in sources:
        _required_hash(path, checksum)
    return manifest


def load_multitask_data(directory: Path) -> tuple[dict, list[dict]]:
    directory = directory.resolve()
    manifest = _load_json(directory / "manifest.json")
    if (manifest.get("schema") != DATA_SCHEMA or manifest.get("taskContractVersion") != TASK_CONTRACT_VERSION
        or manifest.get("taskContracts") != task_contracts() or manifest.get("endpoints") != list(ALL_ENDPOINTS)
        or manifest.get("chemistryVersion") != CHEMISTRY_VERSION or manifest.get("identityVersion") != IDENTITY_VERSION
        or manifest.get("rdkitVersion") != rdBase.rdkitVersion):
        raise ValueError("Unsupported 16-task data, chemistry, dependency or task contract")
    if manifest.get("status") != "prepared-development-only":
        raise ValueError("Multitask development data are blocked or incomplete")
    reference = manifest.get("frozenReference", {})
    if (reference.get("manifestSha256") != V2_MANIFEST_SHA256 or reference.get("splitSha256") != V2_SPLIT_SHA256
        or reference.get("previouslyExposedHergPoolSha256") != EXPOSED_POOL_SHA256
        or reference.get("heldoutLabelsAllowed") is not False or reference.get("v2Modified") is not False
        or reference.get("fourTaskV3Modified") is not False):
        raise ValueError("Multitask frozen-reference provenance mismatch")
    if (manifest.get("trainingPermission") != training_permission() or manifest.get("newBlindTestClaimed") is not False
        or manifest.get("calibrationCertificationAllowed") is not False or manifest.get("calibrationStatus") != "not-fitted"):
        raise ValueError("Multitask development does not authorize hidden labels, relabelling or calibration certification")
    files = manifest.get("files", {})
    if set(files) != {"split", "audit", "forbiddenIdentities", "samples", "exclusions"}:
        raise ValueError("Incomplete multitask development artifacts")
    for entry in files.values():
        filename = entry.get("file", "")
        if not filename or Path(filename).name != filename or "\\" in filename:
            raise ValueError("Multitask artifact path escapes data directory")
        _required_hash(directory / filename, entry["sha256"])
    if manifest.get("splitSha256") != files["split"]["sha256"]:
        raise ValueError("Multitask split SHA256 mismatch")
    sources = manifest.get("sourceFiles", [])
    roles = _multitask_source_roles(sources, directory)
    previous_roles = {"previously-exposed-v3-development-manifest", "previously-exposed-v3-development-split"}
    present_previous = previous_roles & set(roles)
    if present_previous and present_previous != previous_roles:
        raise ValueError("Incomplete optional previously exposed development reference")
    additional_required = [("audit-only-tox21-clean-structures", TOX21_PROCESSED_SHA256)]
    if present_previous:
        additional_required.extend((("previously-exposed-v3-development-manifest", PREVIOUS_DEVELOPMENT_MANIFEST_SHA256),
                                    ("previously-exposed-v3-development-split", PREVIOUS_DEVELOPMENT_SPLIT_SHA256)))
    for role, checksum in additional_required:
        matching = [entry for entry in sources if entry["role"] == role]
        if len(matching) != 1 or matching[0]["sha256"] != checksum:
            raise ValueError("Multitask immutable Tox21/previous development source contract changed: " + role)
    for entry in sources:
        _required_hash(directory / entry["pathFromData"], entry["sha256"])
    encoder_policy = manifest.get("encoderCompatibility", {})
    input_filter = None
    if encoder_policy.get("id") != "none":
        from backend.pretrained_encoder import ENCODER_VERSION, MODEL_REVISION, MAX_TOKENS, validate_encoder_smiles
        if (encoder_policy.get("id") != ENCODER_VERSION or encoder_policy.get("modelRevision") != MODEL_REVISION
            or encoder_policy.get("maxTokens") != MAX_TOKENS or encoder_policy.get("labelsUsedForFiltering") is not False):
            raise ValueError("Unsupported multitask encoder compatibility policy")
        input_filter = validate_encoder_smiles
    if manifest.get("datasetVersion") != _data_version(files, sources, encoder_policy):
        raise ValueError("Multitask dataset version does not match its immutable files and source hashes")
    with (directory / files["samples"]["file"]).open(encoding="utf-8") as source:
        samples = [json.loads(line) for line in source if line.strip()]
    frozen_split = _load_json(roles["frozen-v2-split"])
    forbidden = {"old-validation": [row["smiles"] for row in frozen_split["splits"]["validation"]],
                 "old-test": [row["smiles"] for row in frozen_split["splits"]["test"]],
                 "previously-exposed-herg-karim": [row["smiles"] for row in _read_csv(roles["previously-exposed-herg-pool"])]}
    fresh_forbidden, _ = build_forbidden_index(forbidden)
    if _load_json(directory / files["forbiddenIdentities"]["file"]) != fresh_forbidden:
        raise ValueError("Recorded forbidden identity index differs from fixed historical references; hashes cannot authorize leakage")
    verify_multitask_samples(samples, fresh_forbidden)
    verify_observation_membership(samples, frozen_split, _read_csv(roles["audit-only-tox21-clean-structures"]))
    if samples != sorted(samples, key=lambda sample: sample["smiles"]):
        raise ValueError("Multitask molecular order is not canonical; feature caches cannot be reused")
    if input_filter is not None and any(input_filter(sample["smiles"]) != sample["smiles"] for sample in samples):
        raise ValueError("Multitask encoder changed chemical identity")
    split = _load_json(directory / files["split"]["file"])
    expected = {name: [sample["smiles"] for sample in samples if sample["split"] == name] for name in ("train", "validation")}
    if (split.get("schema") != SPLIT_SCHEMA or split.get("taskContractVersion") != TASK_CONTRACT_VERSION
        or split.get("splits") != expected or split.get("groupIds") != {sample["smiles"]: sample["groupId"] for sample in samples}
        or split.get("classCounts") != class_counts(samples) or manifest.get("classCounts") != class_counts(samples)
        or manifest.get("sampleCounts") != {name: len(members) for name, members in expected.items()}
        or split.get("sharedTaskSplit") is not True or split.get("blindTest") is not False):
        raise ValueError("Multitask sample/task membership differs from its shared split contract")
    prior = manifest.get("previouslyExposedDevelopment", {})
    if (prior.get("role") != "previously-exposed-development" or prior.get("newBlindTestClaimed") is not False
        or prior.get("datasetVersion") != PREVIOUS_DEVELOPMENT_VERSION
        or prior.get("manifestSha256") != PREVIOUS_DEVELOPMENT_MANIFEST_SHA256
        or prior.get("splitSha256") != PREVIOUS_DEVELOPMENT_SPLIT_SHA256
        or prior.get("allowedForNewDevelopment") is not True or prior.get("sameSplitHistoricalMetricsComparable") is not False):
        raise ValueError("Previously exposed development overlap/role audit is invalid")
    if present_previous:
        previous_split = _load_json(roles["previously-exposed-v3-development-split"])
        expected_overlap = previous_development_overlap(samples, {"structuresBySplit": previous_split["splits"]})
        if prior.get("canonicalOverlapWithCurrentSplits") != expected_overlap:
            raise ValueError("Previously exposed development overlap count is invalid")
    elif (prior.get("structureAudit") != "not-assessable-historical-structures-not-distributed"
          or prior.get("unknownStructureReuse") is not True or prior.get("canonicalOverlapWithCurrentSplits") is not None):
        raise ValueError("Missing historical development structures cannot be labelled overlap-free or a new blind test")
    return manifest, samples
