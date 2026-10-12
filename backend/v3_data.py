"""Independent v3 development data: old TRAIN labels, never exposed test labels.

Audit identities deliberately over-group salts, stereo, isotopes and tautomers.
They are leakage safeguards, never replacements for the chemistry used as input.
No download, label generation, v2 modification or performance selection occurs.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from typing import Callable
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from rdkit import Chem, rdBase
from rdkit.Chem.MolStandardize import rdMolStandardize
from rdkit.Chem.Scaffolds import MurckoScaffold

from backend.chemistry import CHEMISTRY_VERSION, canonicalize_smiles
from backend.training_data import (CLINTOX_REFERENCE_SHA256, DATASETS, ENDPOINTS,
                                   LICENSE_NOTE, normalize_rows, read_tdc_rows,
                                   sha256_file, verify_clintox_target)
from backend.candidate_data import SOURCES, TOX21_ENDPOINTS

DATA_SCHEMA = "aidd.toxnet.development-data.v3"
TASK_CONTRACT_VERSION = "aidd.toxnet.four-real-label-tasks.v3.1"
IDENTITY_VERSION = "aidd.v3.leakage-identities.v1"
V2_VERSION = "aidd-toxnet-v2-5c22f6cc5dd3"
V2_MANIFEST_SHA256 = "6225acc5414233756159bb1015489badb5f4bc5ba484c72367da5bac678af850"
V2_SPLIT_SHA256 = "39be266ac00d9af84f858ccdc2471c9fa13972fba326dc1838913ab942440193"
EXTERNAL_EVIDENCE_SHA256 = "0c186f3933653a832c6b7e2dea83ea6f19b8d93592bc79aa6e9a1923c0212476"
EXPOSED_POOL_SHA256 = "7e1df556c8674898e48b37c44f9bfdbcb9a4f60aba8df00dadff7564ce957262"
FDA_HASHES = {
    "dilirank_fda.xlsx": "1ca1352ff727af68e68e250eae2ed775bca8492335140ac0afd2233248694993",
    "dilist_fda.xlsx": "4331ee9d16ae7641488161e4dc2c603c29e06baa8667dd16e7f3d635366e7e5e",
}
IDENTITY_POLICY = (
    "Strict canonical input retained. Audit only: remove stereo and isotopes; "
    "LargestFragmentChooser(preferOrganic=True), Uncharger, tautomer canonicalizer; "
    "compare original/full and neutral-parent Bemis-Murcko scaffolds. "
    "All acyclic structures share [ACYCLIC]. Connected components are indivisible."
)


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _required_hash(path: Path, expected: str) -> str:
    if not path.is_file():
        raise ValueError(f"Missing verified v3 dependency: {path}. Prepare original real data first; no synthetic fallback.")
    actual = sha256_file(path)
    if actual != expected:
        raise ValueError(f"SHA256 mismatch for v3 dependency: {path}; replacement data require a new audited protocol.")
    return actual


def _load_json(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"Missing verified v3 dependency: {path}; no synthetic fallback.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"Invalid JSON v3 dependency: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def _without_stereo_isotopes(molecule):
    result = Chem.Mol(molecule)
    Chem.RemoveStereochemistry(result)
    for atom in result.GetAtoms():
        atom.SetIsotope(0)
        atom.SetAtomMapNum(0)
    Chem.SanitizeMol(result)
    return result


@lru_cache(maxsize=40000)
def _cached_leakage_identity(canonical: str) -> str:
    """Cache immutable JSON so callers cannot mutate a later identity lookup."""
    molecule = Chem.MolFromSmiles(canonical)
    if molecule.GetNumAtoms() > 200 or any(atom.GetIsotope() > 300 for atom in molecule.GetAtoms()):
        raise ValueError("Structure exceeds the existing supported atom/isotope feature limits")
    try:
        with rdBase.BlockLogs():
            full_near = _without_stereo_isotopes(molecule)
            parent = rdMolStandardize.LargestFragmentChooser(preferOrganic=True).choose(molecule)
            parent = rdMolStandardize.Uncharger().uncharge(parent)
            parent_near = _without_stereo_isotopes(parent)
            tautomer = rdMolStandardize.TautomerEnumerator().Canonicalize(parent_near)
            Chem.SanitizeMol(tautomer)
            scaffold = MurckoScaffold.MurckoScaffoldSmiles(mol=full_near, includeChirality=False) or "[ACYCLIC]"
            parent_scaffold = MurckoScaffold.MurckoScaffoldSmiles(mol=parent_near, includeChirality=False) or "[ACYCLIC]"
            tautomer_scaffold = MurckoScaffold.MurckoScaffoldSmiles(mol=tautomer, includeChirality=False) or "[ACYCLIC]"
    except (ValueError, RuntimeError) as error:
        raise ValueError("v3 leakage audit normalization failed; structure cannot enter development") from error
    return json.dumps({
        "canonical": canonical,
        "stereoIsotopeEquivalent": Chem.MolToSmiles(full_near, canonical=True, isomericSmiles=True),
        "parent": Chem.MolToSmiles(parent_near, canonical=True, isomericSmiles=True),
        "tautomerParent": Chem.MolToSmiles(tautomer, canonical=True, isomericSmiles=True),
        "scaffolds": sorted({scaffold, parent_scaffold, tautomer_scaffold}),
    }, sort_keys=True)


def leakage_identity(smiles: str) -> dict:
    """Versioned conservative comparison keys; preserve input chemistry exactly."""
    canonical = canonicalize_smiles(smiles)
    return json.loads(_cached_leakage_identity(canonical))


def identity_tokens(identity: dict) -> set[str]:
    tokens = {f"{kind}:{identity[kind]}" for kind in ("canonical", "stereoIsotopeEquivalent", "parent", "tautomerParent")}
    tokens.update("scaffold:" + value for value in identity["scaffolds"])
    return tokens


def build_forbidden_index(forbidden: dict[str, list[str]]) -> tuple[dict[str, list[str]], dict]:
    index = defaultdict(set)
    counts = {}
    cache = {}
    for role, structures in sorted(forbidden.items()):
        unique = sorted(set(structures))
        for smiles in unique:
            if smiles not in cache:
                cache[smiles] = leakage_identity(smiles)
            for token in identity_tokens(cache[smiles]):
                index[token].add(role)
        counts[role] = {"structures": len(unique), "labelsReadForSelection": False}
    return {token: sorted(roles) for token, roles in sorted(index.items())}, counts


def _components(samples: list[dict]) -> list[list[dict]]:
    parent = list(range(len(samples)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[max(a, b)] = min(a, b)

    ownership = {}
    for position, sample in enumerate(samples):
        for token in identity_tokens(sample["auditIdentity"]):
            if token in ownership:
                union(position, ownership[token])
            else:
                ownership[token] = position
    groups = defaultdict(list)
    for index, sample in enumerate(samples):
        groups[find(index)].append(sample)
    return list(groups.values())


def class_counts(samples: list[dict]) -> dict:
    return {
        split: {
            endpoint: {
                "positive": sum(record["labels"][endpoint] for record in samples if record.get("split") == split and endpoint in record["labels"]),
                "negative": sum(1 - record["labels"][endpoint] for record in samples if record.get("split") == split and endpoint in record["labels"]),
            }
            for endpoint in ENDPOINTS
        }
        for split in ("train", "validation")
    }


def split_development(train_label_rows: list[dict], forbidden: dict[str, list[str]],
                      seed: int = 42, validation_fraction: float = 0.2,
                      label_blind_filter: Callable[[str], str] | None = None) -> tuple[list[dict], list[dict], dict, dict]:
    """Split only permitted old TRAIN by components; labels are never imputed.

    Allocation uses group size and a fixed hash tie break, never validation
    performance or exposed labels. A giant acyclic group is never fractured.
    """
    if type(seed) is not int or not 0 < validation_fraction < 1 or not math.isfinite(validation_fraction):
        raise ValueError("Invalid fixed development seed or validation fraction")
    forbidden_index, forbidden_counts = build_forbidden_index(forbidden)
    molecules = {}
    exclusions = []
    for row in train_label_rows:
        endpoint, label = row.get("endpoint"), row.get("label")
        if endpoint not in ENDPOINTS or type(label) is not int or label not in (0, 1):
            raise ValueError("v3 allows only original four-endpoint observed binary labels; missing labels are absent")
        try:
            canonical = canonicalize_smiles(row["smiles"])
            if label_blind_filter is not None:
                try:
                    checked = label_blind_filter(canonical)
                except (ValueError, RuntimeError, TypeError) as error:
                    exclusions.append({"smiles": canonical, "endpoint": endpoint, "label": label,
                                       "reason": "label-blind-encoder-input-incompatible", "detail": str(error)})
                    continue
                if checked != canonical:
                    raise ValueError("Encoder compatibility filter changed chemical identity")
            if canonical not in molecules:
                molecules[canonical] = {"smiles": canonical, "labels": {}, "provenance": [],
                                        "auditIdentity": leakage_identity(canonical)}
        except (ValueError, RuntimeError, TypeError) as error:
            exclusions.append({"smiles": row.get("smiles"), "endpoint": endpoint, "label": label,
                               "reason": "audit-identity-unavailable", "detail": str(error)})
            continue
        sample = molecules[canonical]
        if endpoint in sample["labels"]:
            if sample["labels"][endpoint] != label:
                raise ValueError("Conflicting canonical endpoint labels in permitted training data")
            raise ValueError("Duplicate canonical endpoint labels in permitted training data")
        sample["labels"][endpoint] = label
        sample["provenance"].append({"endpoint": endpoint, "source_id": row.get("source_id", ""),
                                     "source_row": row.get("source_row", "")})
    groups = _components([molecules[key] for key in sorted(molecules)])
    eligible = []
    direct_overlaps = Counter()
    for members in groups:
        reasons = set()
        for member in members:
            direct = {role + ":" + token.split(":", 1)[0]
                      for token in identity_tokens(member["auditIdentity"])
                      for role in forbidden_index.get(token, ())}
            direct_overlaps.update(direct)
            reasons.update(direct)
        group_id = _digest(sorted(item["smiles"] for item in members))
        if reasons:
            exclusions.extend({"smiles": item["smiles"], "labels": item["labels"], "groupId": group_id,
                               "reason": "forbidden-overlap-connected-component", "overlapReasons": sorted(reasons)}
                              for item in members)
        else:
            eligible.append((group_id, members))
    total_labels = sum(len(item["labels"]) for _, members in eligible for item in members)
    targets = {"train": total_labels * (1 - validation_fraction), "validation": total_labels * validation_fraction}
    allocated = {key: 0 for key in targets}
    samples = []
    ordered = sorted(eligible, key=lambda pair: (-sum(len(item["labels"]) for item in pair[1]),
                                                  hashlib.sha256(f"{seed}:{pair[0]}".encode()).hexdigest()))
    for group_id, members in ordered:
        destination = max(targets, key=lambda name: targets[name] - allocated[name])
        for member in members:
            samples.append({**member, "groupId": group_id, "split": destination})
            allocated[destination] += len(member["labels"])
    samples.sort(key=lambda item: item["smiles"])
    counts = class_counts(samples)
    warnings = []
    for split in counts:
        if not any(item["split"] == split for item in samples):
            warnings.append(f"{split}: empty; indivisible chemistry groups cannot establish a usable two-way split")
        for endpoint, values in counts[split].items():
            if min(values.values()) == 0:
                warnings.append(f"{split}/{endpoint}: missing or single class; ranking metrics are not estimable")
    acyclic_sizes = [len(members) for _, members in eligible
                    if any("[ACYCLIC]" in member["auditIdentity"]["scaffolds"] for member in members)]
    audit = {
        "identityVersion": IDENTITY_VERSION, "identityPolicy": IDENTITY_POLICY,
        "forbiddenRoles": forbidden_counts, "exposedLabelsUsedForDevelopment": False,
        "oldTrainLabelRows": len(train_label_rows), "oldTrainUniqueStructures": len(molecules),
        "eligibleUniqueStructures": len(samples), "eligibleLabelRows": total_labels,
        "excludedUniqueStructures": len({item["smiles"] for item in exclusions}),
        "exclusionReasons": dict(Counter(item["reason"] for item in exclusions)),
        "directOverlapMoleculesByRoleAndIdentity": dict(sorted(direct_overlaps.items())),
        "groupCount": len(eligible), "largestGroupMolecules": max((len(members) for _, members in eligible), default=0),
        "acyclicGroupSizes": acyclic_sizes, "acyclicPolicy": "one indivisible connected group; never split to improve class balance",
        "classCounts": counts, "warnings": warnings,
        "status": "prepared-development-only" if all(any(item["split"] == split for item in samples) for split in counts) else "blocked-insufficient-disjoint-groups",
        "prospectiveBlindTest": False, "heldoutLabelsUsedForSelection": False,
    }
    verify_development_samples(samples, forbidden_index)
    return samples, exclusions, audit, forbidden_index


def verify_development_samples(samples: list[dict], forbidden_index: dict[str, list[str]]) -> None:
    seen = set()
    ownership, groups = {}, defaultdict(list)
    for sample in samples:
        if sample.get("split") not in ("train", "validation"):
            raise ValueError("v3 data must contain train/validation only; no relabelled exposed test")
        canonical = canonicalize_smiles(sample.get("smiles"))
        if canonical != sample["smiles"] or canonical in seen:
            raise ValueError("v3 samples must be unique strict canonical inputs")
        seen.add(canonical)
        labels = sample.get("labels")
        if not isinstance(labels, dict) or not labels or any(endpoint not in ENDPOINTS or type(label) is not int or label not in (0, 1)
                                                          for endpoint, label in labels.items()):
            raise ValueError("Invalid observed-label mask/endpoint contract")
        actual_identity = leakage_identity(canonical)
        if sample.get("auditIdentity") != actual_identity:
            raise ValueError("v3 chemistry identity version or representation mismatch")
        group_id = sample.get("groupId")
        if not isinstance(group_id, str) or len(group_id) != 64:
            raise ValueError("Invalid v3 connected-component ID")
        groups[group_id].append(canonical)
        for token in identity_tokens(actual_identity):
            if token in forbidden_index:
                raise ValueError("v3 data overlap a forbidden old heldout/exposed structure or scaffold")
            existing = ownership.setdefault(token, (sample["split"], group_id))
            if existing != (sample["split"], group_id):
                raise ValueError("v3 canonical/parent/tautomer/scaffold connected-component leakage")
    actual_components = _components(samples)
    for members in actual_components:
        expected_id = _digest(sorted(item["smiles"] for item in members))
        if any(item["groupId"] != expected_id for item in members):
            raise ValueError("v3 connected-component ID does not match the full structure group")


def _verified_inputs(data_dir: Path, model_dir: Path, candidate_dir: Path, external_evidence: Path) -> tuple[list[dict], dict, dict, dict, list[tuple[str, Path, str]]]:
    sources = []

    def verify(role, path, expected):
        actual = _required_hash(path, expected)
        sources.append((role, path.resolve(), actual))
        return actual

    verify("frozen-v2-model-manifest", model_dir / "manifest.json", V2_MANIFEST_SHA256)
    verify("frozen-v2-split", model_dir / "split.json", V2_SPLIT_SHA256)
    model = _load_json(model_dir / "manifest.json")
    if model.get("modelVersion") != V2_VERSION or model.get("endpoints") != list(ENDPOINTS):
        raise ValueError("Unsupported frozen model/task reference")
    for key, entry in model["artifacts"].items():
        verify("frozen-v2-" + key, model_dir / entry["file"], entry["sha256"])
    verify("previously-exposed-herg-evidence", external_evidence, EXTERNAL_EVIDENCE_SHA256)
    split = _load_json(model_dir / "split.json")
    if set(split.get("splits", {})) != {"train", "validation", "test"}:
        raise ValueError("Invalid frozen v2 split contract")
    processed_manifest = _load_json(data_dir / "processed/manifest.json")
    sources.append(("processed-data-manifest", (data_dir / "processed/manifest.json").resolve(), sha256_file(data_dir / "processed/manifest.json")))
    labels_path = data_dir / "processed/toxicity.csv"
    verify("verified-experimental-labels", labels_path, model["training"]["sourceHashes"]["labels"])
    if processed_manifest.get("output", {}).get("sha256") != model["training"]["sourceHashes"]["labels"]:
        raise ValueError("Processed data manifest does not describe the frozen real-label snapshot")
    original_rows = []
    for specification in DATASETS:
        path = data_dir / "raw" / (specification["endpoint"] + ".csv")
        verify("experimental-source-" + specification["endpoint"], path, specification["sha256"])
        original_rows.extend(read_tdc_rows(path, specification["endpoint"]))
    verify("clintox-label-verification", data_dir / "raw/clintox_moleculenet.csv.gz", CLINTOX_REFERENCE_SHA256)
    verify_clintox_target(data_dir / "raw", offline=True)
    regenerated, _, _, _ = normalize_rows(original_rows)
    cleaned = _read_csv(labels_path)

    def row_key(row):
        return (row["smiles"], row["endpoint"], int(row["label"]), str(row.get("source_id", "")), str(row.get("source_row", "")))

    if sorted(map(row_key, regenerated)) != sorted(map(row_key, cleaned)):
        raise ValueError("Processed labels/provenance cannot be reproduced from pinned real experimental sources")
    permitted = {(row["smiles"], row["endpoint"], int(row["label"])): row for row in cleaned}
    frozen_keys = []
    old_train = []
    forbidden = {"old-validation": [], "old-test": []}
    for name, members in split["splits"].items():
        for row in members:
            key = (canonicalize_smiles(row["smiles"]), row["endpoint"], row["label"])
            if key not in permitted:
                raise ValueError("Frozen split contains a label not in the verified experimental source snapshot")
            frozen_keys.append(key)
            if name == "train":
                old_train.append({**permitted[key], "label": row["label"]})
            else:
                forbidden["old-" + name].append(key[0])
    if len(frozen_keys) != len(set(frozen_keys)) or set(frozen_keys) != set(permitted):
        raise ValueError("Frozen split does not partition the verified experimental labels exactly")
    candidate_manifest = _load_json(candidate_dir / "processed/manifest.json")
    sources.append(("candidate-audit-manifest", (candidate_dir / "processed/manifest.json").resolve(), sha256_file(candidate_dir / "processed/manifest.json")))
    if (candidate_manifest.get("reference", {}).get("splitSha256") != V2_SPLIT_SHA256
            or candidate_manifest.get("reference", {}).get("modelManifestSha256") != V2_MANIFEST_SHA256):
        raise ValueError("Candidate snapshot refers to a different frozen v2 version")
    for specification in SOURCES:
        verify("audit-only-source-" + specification["id"], candidate_dir / "raw" / specification["fileName"], specification["sha256"])
    external = candidate_dir / "processed/herg_external_pool.csv"
    verify("previously-exposed-herg-pool", external, EXPOSED_POOL_SHA256)
    if candidate_manifest.get("artifacts", {}).get("externalHergPool", {}).get("sha256") != EXPOSED_POOL_SHA256:
        raise ValueError("Candidate manifest does not describe the previously exposed hERG pool")
    external_rows = _read_csv(external)
    if len(external_rows) != 9978:
        raise ValueError("Previously exposed hERG pool row count changed")
    # Only structures are used: external labels never enter train, selection or calibration.
    forbidden["previously-exposed-herg-karim"] = [row["smiles"] for row in external_rows]
    tox21_entry = candidate_manifest["artifacts"]["tox21"]
    tox21_path = candidate_dir / "processed" / tox21_entry["file"]
    verify("audit-only-tox21-clean-structures", tox21_path, tox21_entry["sha256"])
    tox21_rows = _read_csv(tox21_path)
    if any(row["endpoint"] not in TOX21_ENDPOINTS for row in tox21_rows):
        raise ValueError("Tox21 audit input contains substituted four-endpoint labels")
    return old_train, forbidden, model, {"tox21Rows": tox21_rows, "candidateManifest": candidate_manifest}, sources


def _tox21_audit(rows: list[dict], samples: list[dict], forbidden_index: dict) -> dict:
    sample_index = defaultdict(set)
    for sample in samples:
        for token in identity_tokens(sample["auditIdentity"]):
            sample_index[token].add(sample["split"])
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["smiles"]].append(row)
    overlaps = Counter()
    unavailable = 0
    for smiles in sorted(grouped):
        try:
            tokens = identity_tokens(leakage_identity(smiles))
        except (ValueError, TypeError, RuntimeError):
            unavailable += 1
            continue
        matched = {role for token in tokens for role in forbidden_index.get(token, ())}
        matched.update("v3-" + role for token in tokens for role in sample_index.get(token, ()))
        overlaps.update(matched)
    return {"status": "structure-role-overlap-audit-only", "trainingImported": False,
            "taskContract": "12 assay-specific activity tasks, never substituted for four toxicity endpoints",
            "uniqueStructures": len(grouped), "observedLabelRows": len(rows), "identityUnavailableStructures": unavailable,
            "overlapUniqueStructuresByRole": dict(sorted(overlaps.items())),
            "multiTaskIntegrationStatus": "deferred-requires-independent-role-and-split-contract",
            "missingLabelPolicy": "no row for an absent assay label; never imputed as negative"}


def _fda_audit(candidate_dir: Path, sources: list) -> dict:
    provenance_path = candidate_dir / "fda-provenance.json"
    if not provenance_path.exists():
        return {"status": "missing-audit-only-dependency", "trainingImported": False,
                "missing": str(provenance_path), "structureOverlap": "not-assessable-no-verified-structure-mapping"}
    provenance = _load_json(provenance_path)
    sources.append(("audit-only-fda-provenance", provenance_path.resolve(), sha256_file(provenance_path)))
    results = []
    for entry in provenance.get("datasets", []):
        name = Path(entry["file"]).name
        expected = FDA_HASHES.get(name)
        if not expected or entry["sha256"] != expected:
            raise ValueError("Unsupported FDA source or changed provenance hash")
        path = candidate_dir / "raw" / name
        _required_hash(path, expected)
        sources.append(("audit-only-fda-raw", path.resolve(), expected))
        if entry.get("hasSmilesColumn") is not False or entry.get("structureMappingStatus") != "not-attempted":
            raise ValueError("FDA mapping requires a new audited contract, not automatic import")
        results.append({"dataset": entry["datasetName"], "sha256": expected, "rawDrugRows": entry["rawDrugRows"],
                        "role": "audit-only-not-training", "structureOverlap": "not-assessable-no-verified-structure-mapping",
                        "reason": "Drug names are not structures; no guessed SMILES or endpoint labels imported",
                        "license": entry.get("license")})
    return {"status": "role-audited-overlap-pending-structure-mapping", "trainingImported": False, "datasets": results}


def _verify_source_role_contract(source_records: list[dict], data_dir: Path) -> dict[str, Path]:
    """A mutable manifest cannot replace the pinned experiments or old split."""
    required = {
        "frozen-v2-model-manifest": V2_MANIFEST_SHA256,
        "frozen-v2-split": V2_SPLIT_SHA256,
        "previously-exposed-herg-evidence": EXTERNAL_EVIDENCE_SHA256,
        "previously-exposed-herg-pool": EXPOSED_POOL_SHA256,
        "verified-experimental-labels": "75e5f2c8766efe24cf6cbca57ce1e72bc9028705303b29fff8ad6016c6b0bd6b",
        "clintox-label-verification": CLINTOX_REFERENCE_SHA256,
        **{"experimental-source-" + spec["endpoint"]: spec["sha256"] for spec in DATASETS},
        **{"audit-only-source-" + spec["id"]: spec["sha256"] for spec in SOURCES},
    }
    roles = defaultdict(list)
    for record in source_records:
        roles[record["role"]].append(record)
    for role, checksum in required.items():
        if len(roles[role]) != 1 or roles[role][0].get("sha256") != checksum:
            raise ValueError("v3 immutable experimental/heldout source contract missing or changed: " + role)
    return {role: (data_dir / records[0]["pathFromData"]).resolve() for role, records in roles.items()}


def _verify_old_train_membership(samples: list[dict], frozen_split: dict) -> None:
    allowed = {(canonicalize_smiles(row["smiles"]), row["endpoint"]): row for row in frozen_split["splits"]["train"]}
    for sample in samples:
        provenance = {record["endpoint"]: record for record in sample.get("provenance", [])}
        if set(provenance) != set(sample["labels"]):
            raise ValueError("v3 observation provenance does not match observed-label mask")
        for endpoint, label in sample["labels"].items():
            original = allowed.get((sample["smiles"], endpoint))
            if (original is None or type(label) is not int or original["label"] != label
                    or str(provenance[endpoint].get("source_id", "")) != str(original.get("source_id", ""))
                    or str(provenance[endpoint].get("source_row", "")) != str(original.get("source_row", ""))):
                raise ValueError("v3 observation is not an unchanged verified old TRAIN experimental label")


def prepare_v3_data(data_dir: Path, model_dir: Path, candidate_dir: Path, output_dir: Path,
                    external_evidence: Path, seed: int = 42, validation_fraction: float = 0.2,
                    encoder_compatible: str = "molformer") -> dict:
    """Offline-only verified preparation. Existing output directories are refused."""
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise ValueError(f"Refusing to overwrite v3 data directory: {output_dir}; use a new versioned output")
    if encoder_compatible not in ("none", "molformer"):
        raise ValueError("Unsupported label-blind encoder compatibility policy")
    encoder_policy = {"id": "none", "appliedBeforeSplit": True, "labelsUsedForFiltering": False}
    input_filter = None
    if encoder_compatible == "molformer":
        from backend.pretrained_encoder import ENCODER_VERSION, MAX_TOKENS, MODEL_REVISION, validate_encoder_smiles
        input_filter = validate_encoder_smiles
        encoder_policy = {"id": ENCODER_VERSION, "modelRevision": MODEL_REVISION, "maxTokens": MAX_TOKENS,
                          "appliedBeforeSplit": True, "labelsUsedForFiltering": False,
                          "appliesToAllBaselines": True, "policy": "strict canonical input, no truncation, no UNK; rejected observations quarantined before common split"}
    old_train, forbidden, model, candidates, sources = _verified_inputs(data_dir, model_dir, candidate_dir, external_evidence)
    samples, exclusions, audit, forbidden_index = split_development(old_train, forbidden, seed, validation_fraction, input_filter)
    audit["encoderCompatibility"] = encoder_policy
    audit["tox21"] = _tox21_audit(candidates["tox21Rows"], samples, forbidden_index)
    audit["fda"] = _fda_audit(candidate_dir, sources)
    import os
    source_records = [{"role": role, "pathFromData": os.path.relpath(path, output_dir).replace("\\", "/"), "sha256": checksum}
                      for role, path, checksum in sources]
    output_dir.mkdir(parents=True, exist_ok=False)
    split = {"schema": "aidd.toxnet.development-split.v3", "seed": seed,
             "requestedFractions": {"train": 1 - validation_fraction, "validation": validation_fraction},
             "method": "indivisible connected conservative chemistry groups; size-greedy fixed-seed-hash order",
             "splits": {name: [record["smiles"] for record in samples if record["split"] == name] for name in ("train", "validation")},
             "groupIds": {record["smiles"]: record["groupId"] for record in samples},
             "classCounts": audit["classCounts"], "blindTest": False, "oldExposedLabelsUsedForSelection": False}
    serialized = {
        "samples": ("samples.jsonl", b"".join((json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8") for record in samples)),
        "split": ("split.json", _json_bytes(split)),
        "audit": ("audit.json", _json_bytes(audit)),
        "exclusions": ("exclusions.jsonl", b"".join((json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8") for record in exclusions)),
        "forbiddenIdentities": ("forbidden-identities.json", _json_bytes(forbidden_index)),
    }
    files = {}
    for name, (filename, content) in serialized.items():
        path = output_dir / filename
        path.write_bytes(content)
        files[name] = {"file": filename, "sha256": sha256_file(path)}
    version_hash = _digest({"files": files, "sources": [{"role": row["role"], "sha256": row["sha256"]} for row in source_records],
                            "schema": DATA_SCHEMA, "chemistryVersion": CHEMISTRY_VERSION,
                            "rdkitVersion": rdBase.rdkitVersion, "taskContractVersion": TASK_CONTRACT_VERSION,
                            "encoderCompatibility": encoder_policy})
    manifest = {
        "schema": DATA_SCHEMA, "datasetVersion": "aidd-toxnet-v3-development-" + version_hash[:16],
        "taskContractVersion": TASK_CONTRACT_VERSION, "createdAtUtc": datetime.now(timezone.utc).isoformat(),
        "status": audit["status"], "chemistryVersion": CHEMISTRY_VERSION, "rdkitVersion": rdBase.rdkitVersion,
        "identityVersion": IDENTITY_VERSION, "endpoints": list(ENDPOINTS),
        "encoderCompatibility": encoder_policy,
        "taskContracts": {spec["endpoint"]: {"positiveClass": spec["positiveClass"], "sourceSha256": spec["sha256"], "reference": spec["reference"]} for spec in DATASETS},
        "trainingPermission": {"status": "research-only-source-rights-unresolved", "allowedRoles": ["train-optimization", "validation-model-selection"],
                               "approvedLabelSources": [spec["name"] for spec in DATASETS], "commercialRedistributionApproved": False,
                               "externalOrHeldoutLabelsAllowed": False, "note": LICENSE_NOTE},
        "inputSourceRole": "verified frozen-v2 TRAIN labels only; new two-way development split; no new blind test",
        "frozenReference": {"modelVersion": model["modelVersion"], "manifestSha256": V2_MANIFEST_SHA256, "splitSha256": V2_SPLIT_SHA256,
                            "previouslyExposedHergPoolSha256": EXPOSED_POOL_SHA256, "labelsUsedForDevelopment": False, "v2Modified": False},
        "files": files, "splitSha256": files["split"]["sha256"], "sourceFiles": source_records,
        "classCounts": audit["classCounts"], "warnings": audit["warnings"],
        "sampleCounts": {name: len(split["splits"][name]) for name in split["splits"]},
        "evaluationStatus": "not-evaluated", "calibrationStatus": "not-fitted", "applicabilityValidated": False,
        "limitations": ["Development validation is available for model selection, not unbiased final performance claims.",
                        "Exposed v2 validation/test and hERG_Karim are structural exclusion references only; their labels cannot tune v3.",
                        "Conservative equivalent/scaffold exclusions can sharply reduce sample size and remove positive examples.",
                        "Source-population independence and commercial source rights remain unverified.",
                        "Tox21/FDA supply no labels to these four tasks; FDA structure overlap awaits audited mapping."],
    }
    (output_dir / "manifest.json").write_bytes(_json_bytes(manifest))
    # Recheck originals after all processing; neither replacements nor silent races are permitted.
    for _, path, checksum in sources:
        _required_hash(path, checksum)
    return manifest


def load_v3_data(data_dir: Path) -> tuple[dict, list[dict]]:
    """Verify bytes, source provenance, chemical version, labels and split again."""
    data_dir = data_dir.resolve()
    manifest = _load_json(data_dir / "manifest.json")
    if (manifest.get("schema") != DATA_SCHEMA or manifest.get("taskContractVersion") != TASK_CONTRACT_VERSION
            or manifest.get("identityVersion") != IDENTITY_VERSION or manifest.get("chemistryVersion") != CHEMISTRY_VERSION
            or manifest.get("rdkitVersion") != rdBase.rdkitVersion or manifest.get("endpoints") != list(ENDPOINTS)):
        raise ValueError("Unsupported v3 data, chemistry, dependency or task contract version; rebuild a new audited data version")
    if manifest.get("status") != "prepared-development-only":
        raise ValueError("v3 development data are blocked/incomplete; no valid training/validation split")
    expected_contract = {spec["endpoint"]: {"positiveClass": spec["positiveClass"], "sourceSha256": spec["sha256"], "reference": spec["reference"]} for spec in DATASETS}
    if manifest.get("taskContracts") != expected_contract:
        raise ValueError("v3 positive-class/source contract mismatch")
    permission = manifest.get("trainingPermission", {})
    if (permission.get("externalOrHeldoutLabelsAllowed") is not False or permission.get("status") != "research-only-source-rights-unresolved"
            or permission.get("commercialRedistributionApproved") is not False
            or permission.get("allowedRoles") != ["train-optimization", "validation-model-selection"]
            or permission.get("approvedLabelSources") != [spec["name"] for spec in DATASETS]):
        raise ValueError("v3 training permission does not preserve research-only and heldout-label exclusions")
    reference = manifest.get("frozenReference", {})
    if (reference.get("manifestSha256") != V2_MANIFEST_SHA256 or reference.get("splitSha256") != V2_SPLIT_SHA256
            or reference.get("previouslyExposedHergPoolSha256") != EXPOSED_POOL_SHA256 or reference.get("v2Modified") is not False):
        raise ValueError("v3 frozen-reference provenance mismatch")
    encoder_policy = manifest.get("encoderCompatibility", {})
    input_filter = None
    if encoder_policy.get("id") != "none":
        from backend.pretrained_encoder import ENCODER_VERSION, MAX_TOKENS, MODEL_REVISION, validate_encoder_smiles
        if (encoder_policy.get("id") != ENCODER_VERSION or encoder_policy.get("modelRevision") != MODEL_REVISION
                or encoder_policy.get("maxTokens") != MAX_TOKENS or encoder_policy.get("labelsUsedForFiltering") is not False):
            raise ValueError("Unsupported v3 encoder compatibility revision/policy")
        input_filter = validate_encoder_smiles
    files = manifest.get("files", {})
    if set(files) != {"samples", "split", "audit", "exclusions", "forbiddenIdentities"}:
        raise ValueError("Incomplete v3 development artifacts")
    for entry in files.values():
        filename = entry.get("file", "")
        if not filename or Path(filename).name != filename or "\\" in filename:
            raise ValueError("v3 artifact path escapes development data directory")
        _required_hash(data_dir / filename, entry["sha256"])
    if manifest.get("splitSha256") != files["split"]["sha256"]:
        raise ValueError("v3 split SHA256 mismatch")
    if not manifest.get("sourceFiles"):
        raise ValueError("Missing v3 real-data provenance")
    role_paths = _verify_source_role_contract(manifest["sourceFiles"], data_dir)
    for entry in manifest["sourceFiles"]:
        _required_hash(data_dir / entry["pathFromData"], entry["sha256"])
    version_hash = _digest({"files": files, "sources": [{"role": row["role"], "sha256": row["sha256"]} for row in manifest["sourceFiles"]],
                            "schema": DATA_SCHEMA, "chemistryVersion": CHEMISTRY_VERSION,
                            "rdkitVersion": rdBase.rdkitVersion, "taskContractVersion": TASK_CONTRACT_VERSION,
                            "encoderCompatibility": encoder_policy})
    if manifest.get("datasetVersion") != "aidd-toxnet-v3-development-" + version_hash[:16]:
        raise ValueError("v3 dataset version does not match its immutable artifact/source hashes")
    with (data_dir / files["samples"]["file"]).open(encoding="utf-8") as source:
        samples = [json.loads(line) for line in source if line.strip()]
    forbidden_index = _load_json(data_dir / files["forbiddenIdentities"]["file"])
    verify_development_samples(samples, forbidden_index)
    _verify_old_train_membership(samples, _load_json(role_paths["frozen-v2-split"]))
    if input_filter is not None and any(input_filter(sample["smiles"]) != sample["smiles"] for sample in samples):
        raise ValueError("v3 encoder compatibility validation changed chemical identity")
    split = _load_json(data_dir / files["split"]["file"])
    membership = {name: [sample["smiles"] for sample in samples if sample["split"] == name] for name in ("train", "validation")}
    if (split.get("splits") != membership or split.get("groupIds") != {sample["smiles"]: sample["groupId"] for sample in samples}
            or split.get("classCounts") != class_counts(samples) or manifest.get("classCounts") != class_counts(samples)
            or manifest.get("sampleCounts") != {name: len(values) for name, values in membership.items()}):
        raise ValueError("v3 sample membership/class counts do not match the frozen development split")
    if samples != sorted(samples, key=lambda sample: sample["smiles"]):
        raise ValueError("v3 input order is not canonical; embedding cache may not be reused")
    return manifest, samples
