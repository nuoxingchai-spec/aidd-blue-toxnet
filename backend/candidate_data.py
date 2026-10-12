"""Checksum-pinned candidate data; never change the frozen deployed model.

hERG_Karim supplies a retrospective external-source pool after conservative
leakage exclusions. Tox21 remains twelve assay-specific tasks, with absent labels
masked; it is not relabelled as the application's four clinical endpoints.
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from rdkit import Chem, rdBase
from rdkit.Chem.MolStandardize import rdMolStandardize
from rdkit.Chem.Scaffolds import MurckoScaffold

from backend.chemistry import CHEMISTRY_VERSION, canonicalize_smiles
from backend.training_data import LICENSE_NOTE, TDC_DOCUMENTATION_URL, TDC_METADATA_COMMIT, TDC_METADATA_URL

PIPELINE_VERSION = "aidd.candidate-data.v1"
TDC_METADATA_SHA256 = "f169cd7464eb5e8ba4339015c65aaf614d52356c5ff42872b89821c278cc692c"
TOX21_ASSAYS = (
    "NR-AR", "NR-AR-LBD", "NR-AhR", "NR-Aromatase", "NR-ER", "NR-ER-LBD",
    "NR-PPAR-gamma", "SR-ARE", "SR-ATAD5", "SR-HSE", "SR-MMP", "SR-p53",
)
TOX21_ENDPOINTS = tuple("tox21_" + value.lower().replace("-", "_") for value in TOX21_ASSAYS)
ALLOWED_ENDPOINTS = ("herg", *TOX21_ENDPOINTS)
SOURCES = (
    {
        "id": "herg_karim", "name": "TDC hERG_Karim", "fileId": 6822246,
        "fileName": "herg_karim.tsv", "format": "tsv", "documentedRows": 13445,
        "sha256": "b47cbff0d65bbb5c6ade5ecd4c5bab20158c6cbf631a68f355efbe7e5fafc099",
        "documentationUrl": TDC_DOCUMENTATION_URL + "#herg-karim-et-al",
        "labelMeaning": "1: hERG blocker below 10 micromolar; 0: at or above 10 micromolar, according to TDC documentation.",
        "population": "Integrated hERG literature datasets; source-population independence from earlier hERG collections is unverified.",
        "allowedUse": "Retrospective external-source hERG validation after leakage audit; no prospective or independent-source claim.",
    },
    {
        "id": "tox21", "name": "TDC Tox21", "fileId": 4259612,
        "fileName": "tox21.csv", "format": "csv", "documentedRows": 7831,
        "sha256": "6e2bf5437e3016a77c850ad49a6112f9f00e61d3f62ade110cd99172c0bd53a1",
        "documentationUrl": TDC_DOCUMENTATION_URL + "#tox21",
        "labelMeaning": "Twelve separate nuclear-receptor/stress-response assay labels: activity 1, inactivity 0; missing entries remain masked.",
        "population": "Tox21 screening chemicals; assay-specific activity is not a clinical toxicity outcome.",
        "allowedUse": "Candidate data for future twelve-task molecular training; not appended to the frozen four-endpoint model.",
    },
)
ROW_FIELDS = ("smiles", "endpoint", "label", "source_dataset", "source_id", "source_row", "source_assay", "original_smiles", "parent_smiles", "scaffold", "parent_scaffold")
AUDIT_FIELDS = (*ROW_FIELDS, "reason")
PARENT_METHOD = "RDKit LargestFragmentChooser(preferOrganic=True), Uncharger, remove stereochemistry; preserve isotopes; no tautomer merging"
EXTERNAL_STATUS = "retrospective-external-source-herg; canonical-parent-scaffold-disjoint; source-population-independence-unverified"


class ParentNormalizationError(ValueError):
    """The original structure parsed, but neutral-parent construction failed."""


class FeatureCompatibilityError(ValueError):
    """Structure exceeds the frozen model's supported feature limits."""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download_verified_bytes(url: str, expected_sha256: str, max_bytes: int) -> bytes:
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "AIDD-BlueResearch/1.0"})
            with urlopen(request, timeout=60) as response:
                content = response.read(max_bytes + 1)
            break
        except HTTPError as error:
            if error.code < 500 or attempt == 2:
                raise ValueError(f"Official candidate source unavailable ({error.code}): {url}") from error
            time.sleep(1 + attempt)
        except (URLError, TimeoutError) as error:
            if attempt == 2:
                raise ValueError(f"Official candidate source unavailable: {url}; {error}") from error
            time.sleep(1 + attempt)
    if len(content) > max_bytes or hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError(f"Official candidate source size/checksum changed: {url}")
    return content


def ensure_metadata(metadata_path: Path, offline: bool = False) -> None:
    """Download pinned metadata as inert bytes; never import/execute this file."""
    if not metadata_path.exists():
        if offline:
            raise ValueError("Missing verified TDC metadata for offline candidate preparation.")
        content = _download_verified_bytes(TDC_METADATA_URL, TDC_METADATA_SHA256, 1_000_000)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = metadata_path.with_suffix(metadata_path.suffix + ".download")
        temporary.write_bytes(content)
        temporary.replace(metadata_path)
    if sha256_file(metadata_path) != TDC_METADATA_SHA256:
        raise ValueError("Pinned TDC metadata checksum mismatch.")


def download_source(specification: dict, raw_dir: Path, offline: bool = False) -> dict:
    """Only public official files; deny checksum changes and access failures."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / specification["fileName"]
    url = f"https://dataverse.harvard.edu/api/access/datafile/{specification['fileId']}?format=original"
    downloaded = False
    if not target.exists():
        if offline:
            raise ValueError(f"Missing verified candidate source: {target}")
        content = _download_verified_bytes(url, specification["sha256"], 30_000_000)
        temporary = target.with_suffix(".download")
        temporary.write_bytes(content)
        temporary.replace(target)
        downloaded = True
    if sha256_file(target) != specification["sha256"]:
        raise ValueError(f"Candidate source checksum mismatch: {target}")
    return {**specification, "downloadUrl": url, "verifiedSha256": specification["sha256"], "bytes": target.stat().st_size,
            "downloadedThisRun": downloaded, "tdcMetadataCommit": TDC_METADATA_COMMIT,
            "tdcMetadataUrl": TDC_METADATA_URL, "license": LICENSE_NOTE}


def _label(value) -> int | None:
    if value is None or isinstance(value, str) and value.strip().lower() in ("", "nan", "na", "null"):
        return None
    if type(value) is int and value in (0, 1):
        return value
    if isinstance(value, str) and value.strip() in ("0", "1", "0.0", "1.0"):
        return int(float(value))
    raise ValueError("nonbinary_label")


def read_source_rows(path: Path, dataset: str) -> tuple[list[dict], dict]:
    """Inspect and enforce the actual official byte contract, not file extension."""
    if dataset not in ("herg_karim", "tox21"):
        raise ValueError("Unknown candidate dataset.")
    endpoints = ("herg",) if dataset == "herg_karim" else TOX21_ENDPOINTS
    counts = {endpoint: {"sourceRows": 0, "availableLabels": 0, "missingLabels": 0, "invalidLabels": 0, "positiveLabels": 0, "negativeLabels": 0} for endpoint in endpoints}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t" if dataset == "herg_karim" else ",")
        expected = {"Drug_ID", "Drug", "Y"} if dataset == "herg_karim" else {*TOX21_ASSAYS, "ID", "X"}
        if set(reader.fieldnames or ()) != expected:
            raise ValueError(f"Unexpected official candidate column contract: {dataset}")
        rows = []
        source_rows = 0
        for number, raw in enumerate(reader, start=2):
            source_rows += 1
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError(f"Malformed official candidate row: {dataset}:{number}")
            assays = (("herg", "Y"),) if dataset == "herg_karim" else tuple(zip(TOX21_ENDPOINTS, TOX21_ASSAYS))
            for endpoint, assay in assays:
                summary = counts[endpoint]
                summary["sourceRows"] += 1
                try:
                    label = _label(raw[assay])
                except ValueError:
                    summary["invalidLabels"] += 1
                    label = raw[assay]
                else:
                    if label is None:
                        summary["missingLabels"] += 1
                        continue
                    summary["availableLabels"] += 1
                    summary["positiveLabels" if label else "negativeLabels"] += 1
                rows.append({"smiles": raw["Drug" if dataset == "herg_karim" else "X"],
                             "endpoint": endpoint, "label": label, "source_dataset": dataset,
                             "source_id": raw["Drug_ID" if dataset == "herg_karim" else "ID"],
                             "source_row": number, "source_assay": assay})
    return rows, {"sourceRows": source_rows, "endpoints": counts, "missingLabelPolicy": "masked; no observation row and never imputed as zero"}


def structural_identity(smiles: str, max_atoms: int = 200) -> dict:
    canonical = canonicalize_smiles(smiles)
    with rdBase.BlockLogs():
        molecule = Chem.MolFromSmiles(canonical)
    if molecule.GetNumAtoms() > max_atoms:
        raise FeatureCompatibilityError("exceeds_model_atom_limit")
    if any(atom.GetIsotope() > 300 for atom in molecule.GetAtoms()):
        raise FeatureCompatibilityError("exceeds_model_isotope_limit")
    try:
        with rdBase.BlockLogs():
            parent = rdMolStandardize.LargestFragmentChooser(preferOrganic=True).choose(molecule)
            parent = rdMolStandardize.Uncharger().uncharge(parent)
            Chem.RemoveStereochemistry(parent)
            Chem.SanitizeMol(parent)
    except (ValueError, RuntimeError) as error:
        raise ParentNormalizationError(str(error)) from error
    return {"smiles": canonical,
            "parent_smiles": Chem.MolToSmiles(parent, canonical=True, isomericSmiles=True),
            "scaffold": MurckoScaffold.MurckoScaffoldSmiles(mol=molecule, includeChirality=False) or "[ACYCLIC]",
            "parent_scaffold": MurckoScaffold.MurckoScaffoldSmiles(mol=parent, includeChirality=False) or "[ACYCLIC]"}


def normalize_candidate_rows(rows: list[dict], max_atoms: int = 200) -> tuple[list[dict], list[dict], list[dict], dict]:
    groups = defaultdict(list)
    quarantine, duplicates = [], []
    identity_cache = {}
    failure_type_cache = {}
    invalid_structure_rows = Counter()
    invalid_structure_source_rows = set()
    structure_failure_source_rows = {kind: set() for kind in ("invalidInputStructure", "modelFeatureIncompatible", "parentNormalizationFailure")}
    conflicts = Counter()
    for raw in rows:
        row = {key: raw.get(key, "") for key in ROW_FIELDS}
        row["original_smiles"] = raw.get("smiles", "")
        try:
            label = _label(row["label"])
            if label is None or row["endpoint"] not in ALLOWED_ENDPOINTS:
                raise ValueError("missing_or_invalid_endpoint_label")
            row["label"] = label
        except ValueError as error:
            quarantine.append({**row, "reason": "invalid_label: " + str(error)})
            continue
        cache_key = row["original_smiles"]
        if cache_key not in identity_cache:
            try:
                identity_cache[cache_key] = structural_identity(cache_key, max_atoms)
            except (ValueError, TypeError, RuntimeError) as error:
                identity_cache[cache_key] = "invalid_structure: " + str(error)
                failure_type_cache[cache_key] = ("parentNormalizationFailure" if isinstance(error, ParentNormalizationError)
                                                 else "modelFeatureIncompatible" if isinstance(error, FeatureCompatibilityError)
                                                 else "invalidInputStructure")
        identity = identity_cache[cache_key]
        if isinstance(identity, str):
            quarantine.append({**row, "reason": identity})
            invalid_structure_rows[row["endpoint"]] += 1
            invalid_structure_source_rows.add((row["source_dataset"], row["source_row"]))
            structure_failure_source_rows[failure_type_cache[cache_key]].add((row["source_dataset"], row["source_row"]))
            continue
        row.update(identity)
        groups[(row["endpoint"], row["smiles"])].append(row)
    retained = []
    for (endpoint, _), members in sorted(groups.items()):
        if len({row["label"] for row in members}) > 1:
            conflicts[endpoint] += 1
            quarantine.extend({**row, "reason": "conflicting_labels_for_exact_structure_endpoint"} for row in members)
            continue
        retained.append(members[0])
        duplicates.extend({**row, "reason": "duplicate_exact_structure_endpoint_label"} for row in members[1:])
    endpoints = {}
    for endpoint in sorted({row["endpoint"] for row in rows}):
        members = [row for row in retained if row["endpoint"] == endpoint]
        endpoints[endpoint] = {
            "retainedRows": len(members), "positiveRows": sum(row["label"] for row in members),
            "negativeRows": sum(1 - row["label"] for row in members),
            "invalidStructureLabelRows": invalid_structure_rows[endpoint],
            "conflictingStructureGroups": conflicts[endpoint],
            "quarantinedLabelRows": sum(row["endpoint"] == endpoint for row in quarantine),
            "sameLabelDuplicateRows": sum(row["endpoint"] == endpoint for row in duplicates),
        }
    by_dataset = {}
    for dataset in sorted({row["source_dataset"] for row in rows}):
        members = [row for row in retained if row["source_dataset"] == dataset]
        by_dataset[dataset] = {"retainedLabelRows": len(members), "uniqueMolecules": len({row["smiles"] for row in members}),
                               "quarantinedLabelRows": sum(row["source_dataset"] == dataset for row in quarantine),
                               "invalidStructureSourceRowsWithObservedLabels": sum(source == dataset for source, _ in invalid_structure_source_rows),
                               "structureFailureSourceRowsByStage": {kind: sum(source == dataset for source, _ in failures)
                                                                     for kind, failures in structure_failure_source_rows.items()}}
    return retained, quarantine, duplicates, {"labelRows": len(retained), "uniqueMolecules": len({row["smiles"] for row in retained}),
                                           "invalidStructureSourceRows": len(invalid_structure_source_rows), "byDataset": by_dataset, "endpoints": endpoints}


def _index_splits(split_manifest: dict, max_atoms: int) -> dict:
    if set(split_manifest.get("splits", {})) != {"train", "validation", "test"}:
        raise ValueError("Frozen reference must provide all three splits.")
    indices = {}
    for name, members in split_manifest["splits"].items():
        index = {key: set() for key in ("smiles", "parent_smiles", "scaffold")}
        for smiles in sorted({row["smiles"] for row in members}):
            identity = structural_identity(smiles, max_atoms)
            index["smiles"].add(identity["smiles"])
            index["parent_smiles"].add(identity["parent_smiles"])
            index["scaffold"].update((identity["scaffold"], identity["parent_scaffold"]))
        indices[name] = index
    return indices


def audit_overlap(rows: list[dict], split_manifest: dict, max_atoms: int = 200) -> tuple[list[dict], list[dict], dict]:
    """Exclude any prior split, including other endpoints; no test-set expansion."""
    indices = _index_splits(split_manifest, max_atoms)
    overlap_rows = []
    disjoint = []
    per_split = {split: {"canonicalLabelRows": 0, "parentLabelRows": 0, "scaffoldLabelRows": 0,
                         "canonicalUniqueMolecules": set(), "parentUniqueMolecules": set(), "scaffoldUniqueMolecules": set()}
                 for split in indices}
    by_dataset = {dataset: {"candidateLabelRows": 0, "candidateUniqueMolecules": set(), "overlappingLabelRows": 0,
                            "overlappingUniqueMolecules": set(), "disjointLabelRows": 0, "disjointUniqueMolecules": set(),
                            "bySplit": {split: {"canonicalLabelRows": 0, "parentLabelRows": 0, "scaffoldLabelRows": 0,
                                                "canonicalUniqueMolecules": set(), "parentUniqueMolecules": set(), "scaffoldUniqueMolecules": set()}
                                        for split in indices}}
                  for dataset in sorted({row["source_dataset"] for row in rows})}
    for row in rows:
        reasons = []
        dataset_counts = by_dataset[row["source_dataset"]]
        dataset_counts["candidateLabelRows"] += 1
        dataset_counts["candidateUniqueMolecules"].add(row["smiles"])
        for split, index in indices.items():
            match = {"canonical": row["smiles"] in index["smiles"],
                     "parent": row["parent_smiles"] in index["parent_smiles"],
                     "scaffold": bool({row["scaffold"], row["parent_scaffold"]} & index["scaffold"])}
            for kind, matched in match.items():
                if matched:
                    per_split[split][kind + "LabelRows"] += 1
                    per_split[split][kind + "UniqueMolecules"].add(row["smiles"])
                    dataset_counts["bySplit"][split][kind + "LabelRows"] += 1
                    dataset_counts["bySplit"][split][kind + "UniqueMolecules"].add(row["smiles"])
                    reasons.append(f"{split}:{kind}")
        if reasons:
            overlap_rows.append({**row, "reason": ";".join(reasons)})
            dataset_counts["overlappingLabelRows"] += 1
            dataset_counts["overlappingUniqueMolecules"].add(row["smiles"])
        else:
            disjoint.append(row)
            dataset_counts["disjointLabelRows"] += 1
            dataset_counts["disjointUniqueMolecules"].add(row["smiles"])
    parent_groups = defaultdict(list)
    for row in rows:
        parent_groups[(row["endpoint"], row["parent_smiles"])].append(row)
    parent_conflicts = {key for key, members in parent_groups.items() if len({row["label"] for row in members}) > 1}
    herg_disjoint = [row for row in disjoint if row["endpoint"] == "herg"]
    herg_parent_groups = defaultdict(list)
    for row in herg_disjoint:
        herg_parent_groups[row["parent_smiles"]].append(row)
    pool, pool_exclusions = [], []
    for parent, members in sorted(herg_parent_groups.items()):
        if ("herg", parent) in parent_conflicts:
            pool_exclusions.extend({**row, "reason": "conflicting_herg_labels_for_parent_equivalent_structures"} for row in members)
        else:
            pool.append(members[0])
            pool_exclusions.extend({**row, "reason": "duplicate_herg_parent_equivalent_same_label"} for row in members[1:])
    for summary in per_split.values():
        for key, value in summary.items():
            if isinstance(value, set):
                summary[key] = len(value)
    for dataset_counts in by_dataset.values():
        for key, value in dataset_counts.items():
            if isinstance(value, set):
                dataset_counts[key] = len(value)
        for split_counts in dataset_counts["bySplit"].values():
            for key, value in split_counts.items():
                if isinstance(value, set):
                    split_counts[key] = len(value)
    scaffold_groups = Counter(row["parent_scaffold"] for row in rows)
    cross_source = {kind: defaultdict(set) for kind in ("smiles", "parent_smiles", "parent_scaffold")}
    for row in rows:
        for kind, groups in cross_source.items():
            groups[row[kind]].add(row["source_dataset"])
    summary = {"againstAllPriorEndpoints": True, "bySplit": per_split,
               "byDataset": by_dataset,
               "candidateLabelRows": len(rows), "overlappingLabelRows": len(overlap_rows),
               "disjointLabelRows": len(disjoint), "disjointByEndpoint": dict(Counter(row["endpoint"] for row in disjoint)),
               "withinCandidates": {"uniqueParents": len({row["parent_smiles"] for row in rows}),
                                    "parentEquivalentEndpointGroupsWithMultipleRows": sum(len(members) > 1 for members in parent_groups.values()),
                                    "parentEquivalentEndpointGroupsWithConflictingLabels": len(parent_conflicts),
                                    "canonicalStructuresSharedBetweenCandidateSources": sum(len(sources) > 1 for sources in cross_source["smiles"].values()),
                                    "parentsSharedBetweenCandidateSources": sum(len(sources) > 1 for sources in cross_source["parent_smiles"].values()),
                                    "parentScaffoldsSharedBetweenCandidateSources": sum(len(sources) > 1 for sources in cross_source["parent_scaffold"].values()),
                                    "uniqueParentScaffolds": len(scaffold_groups),
                                    "parentScaffoldsWithMultipleLabelRows": sum(count > 1 for count in scaffold_groups.values())},
               "externalHergPool": {"status": EXTERNAL_STATUS, "count": len(pool), "positive": sum(row["label"] for row in pool),
                                    "negative": sum(1 - row["label"] for row in pool), "uniqueParentScaffolds": len({row["parent_scaffold"] for row in pool}),
                                    "parentConflictExcludedRows": sum("conflicting" in row["reason"] for row in pool_exclusions),
                                    "parentDuplicateExcludedRows": sum("duplicate" in row["reason"] for row in pool_exclusions),
                                    "currentModelTrainingUse": False, "prospective": False, "sourcePopulationIndependenceVerified": False}}
    return pool, [*overlap_rows, *pool_exclusions], summary


def _write_csv(path: Path, rows: list[dict], fields: tuple = ROW_FIELDS) -> dict:
    temporary = path.with_suffix(path.suffix + ".writing")
    with temporary.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)
    return {"file": path.name, "sha256": sha256_file(path), "rows": len(rows)}


def prepare_candidates(candidate_dir: Path, split_path: Path, model_manifest_path: Path,
                       metadata_path: Path, offline: bool = False) -> dict:
    """Write candidate artifacts only. Frozen training/evaluation artifacts are read."""
    ensure_metadata(metadata_path, offline)
    paths = (split_path, model_manifest_path, metadata_path)
    hashes_before = {str(path): sha256_file(path) for path in paths}
    if hashes_before[str(metadata_path)] != TDC_METADATA_SHA256:
        raise ValueError("Pinned TDC metadata checksum mismatch.")
    model_manifest = json.loads(model_manifest_path.read_text(encoding="utf-8"))
    split = json.loads(split_path.read_text(encoding="utf-8"))
    max_atoms = model_manifest["architecture"]["maxAtoms"]
    raw_dir = candidate_dir / "raw"
    processed_dir = candidate_dir / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    source_provenance, source_counts, records = [], {}, []
    for source in SOURCES:
        source_provenance.append(download_source(source, raw_dir, offline))
        source_rows, counts = read_source_rows(raw_dir / source["fileName"], source["id"])
        if counts["sourceRows"] != source["documentedRows"]:
            raise ValueError(f"Pinned candidate row count changed: {source['id']}")
        source_counts[source["id"]] = counts
        records.extend(source_rows)
    retained, quarantined, duplicates, normalization = normalize_candidate_rows(records, max_atoms)
    pool, overlap_records, overlap = audit_overlap(retained, split, max_atoms)
    artifacts = {}
    for dataset in ("herg_karim", "tox21"):
        artifacts[dataset] = _write_csv(processed_dir / (dataset + ".csv"), [row for row in retained if row["source_dataset"] == dataset])
    artifacts["externalHergPool"] = _write_csv(processed_dir / "herg_external_pool.csv", pool)
    artifacts["quarantine"] = _write_csv(processed_dir / "quarantine.csv", quarantined, AUDIT_FIELDS)
    artifacts["duplicates"] = _write_csv(processed_dir / "duplicates.csv", duplicates, AUDIT_FIELDS)
    artifacts["overlapAudit"] = _write_csv(processed_dir / "overlap.csv", overlap_records, AUDIT_FIELDS)
    hashes_after = {str(path): sha256_file(path) for path in paths}
    if hashes_before != hashes_after:
        raise ValueError("Frozen reference artifact changed during candidate audit; rerun against an immutable model.")
    version_digest = hashlib.sha256(json.dumps({"sources": [source["sha256"] for source in SOURCES], "reference": hashes_before,
                                                "pipeline": PIPELINE_VERSION, "artifacts": artifacts}, sort_keys=True).encode()).hexdigest()
    manifest = {"schema": PIPELINE_VERSION, "candidateVersion": "aidd-candidates-" + version_digest[:16],
                "createdAtUtc": datetime.now(timezone.utc).isoformat(), "rdkitVersion": rdBase.rdkitVersion,
                "chemistryVersion": CHEMISTRY_VERSION, "maxModelAtoms": max_atoms, "maxIsotopeMass": 300,
                "parentMethod": PARENT_METHOD, "scaffoldMethod": "Bemis-Murcko, chirality removed; compare both full and neutral parent scaffolds; all acyclic share [ACYCLIC]",
                "sources": source_provenance, "sourceCounts": source_counts, "normalization": normalization, "overlap": overlap,
                "reference": {"modelVersion": model_manifest["modelVersion"], "datasetVersion": model_manifest["datasetVersion"],
                              "splitSha256": hashes_before[str(split_path)], "modelManifestSha256": hashes_before[str(model_manifest_path)],
                              "tdcMetadataSha256": TDC_METADATA_SHA256, "tdcMetadataCommit": TDC_METADATA_COMMIT,
                              "frozenArtifactsModified": False},
                "artifacts": artifacts, "currentModelTrainingUse": False,
                "limitations": ["Structural exclusions do not prove source-population independence.",
                                "hERG_Karim threshold differs from the original hERG training collection; scores are exploratory under endpoint/source shift.",
                                "Tox21 assay activity cannot substitute for hERG, DILI, Ames or ClinTox labels.",
                                "Same-parent label conflicts are excluded from the external hERG pool; parent normalization is only a leakage safeguard.",
                                "No new model weights or calibration are selected using these candidate labels."]}
    manifest_path = processed_dir / "manifest.json"
    temporary_manifest = manifest_path.with_suffix(".json.writing")
    temporary_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_manifest.replace(manifest_path)
    return manifest
