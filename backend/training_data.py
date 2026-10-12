"""Pinned public toxicity data and auditable, strict structure normalization.

Labels come from TDC's Harvard Dataverse files. Neither demonstration rules nor
commercial LLM outputs participate in this training dataset.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from rdkit import Chem, rdBase

from backend.chemistry import CHEMISTRY_VERSION, canonicalize_smiles

DATA_PIPELINE_VERSION = "aidd.toxicity-data.v1"
TDC_METADATA_COMMIT = "e70285e5ace3bb1d72eb3c57bad2e73d750f5d42"
TDC_METADATA_URL = f"https://raw.githubusercontent.com/mims-harvard/TDC/{TDC_METADATA_COMMIT}/tdc/metadata.py"
TDC_DOCUMENTATION_URL = "https://tdcommons.ai/single_pred_tasks/tox/"
LICENSE_NOTE = (
    "TDC toxicity documentation displays both 'Not Specified' and a 'CC BY 4.0' link. "
    "Dataset-specific original rights and commercial redistribution were not independently resolved. "
    "TDC's MIT software license is not a blanket license for its underlying datasets. "
    "Retain source attribution; raw data are excluded from the source repository."
)

DATASETS = (
    {
        "endpoint": "herg", "name": "TDC hERG", "fileId": 4259588,
        "sha256": "f2b00c05f12763c9da91d46246800e30925a626d5038e15eec366d633bdffe54",
        "documentedRows": 648, "positiveClass": "hERG blocker (1); non-blocker (0)",
        "sourcePopulation": "Literature-compiled drug molecules; assay thresholds follow the source dataset, not a new uniform threshold.",
        "reference": "Wang, Shuangquan, et al. ADMET evaluation in drug discovery. 16. Predicting hERG blockers by combining multiple pharmacophores and machine learning approaches. Molecular Pharmaceutics 13.8 (2016): 2855-2866.",
        "documentationUrl": TDC_DOCUMENTATION_URL + "#herg-blockers",
    },
    {
        "endpoint": "dili", "name": "TDC DILI", "fileId": 4259585,
        "sha256": "f977be424b9e509dc721e24106dd51cfb5a63ad8df0104ae51d9ab51cef7be09",
        "documentedRows": 475, "positiveClass": "Drug-induced liver injury positive (1); negative (0)",
        "sourcePopulation": "Literature-compiled drugs aggregated from US FDA National Center for Toxicological Research, per TDC documentation.",
        "reference": "Xu, Youjun, et al. Deep learning for drug-induced liver injury. Journal of Chemical Information and Modeling 55.10 (2015): 2085-2093.",
        "documentationUrl": TDC_DOCUMENTATION_URL + "#dili-drug-induced-liver-injury",
    },
    {
        "endpoint": "ames", "name": "TDC Ames", "fileId": 4259564,
        "sha256": "a17d2d6b9aa17f1cfaa1e3e4137788b2ef2700404042e285102f18543ca954b5",
        "documentedRows": 7255, "positiveClass": "Ames mutagenic (1); non-mutagenic (0)",
        "sourcePopulation": "Bacterial reverse mutation assay labels aggregated from four papers, per TDC documentation.",
        "reference": "Xu, Congying, et al. In silico prediction of chemical Ames mutagenicity. Journal of Chemical Information and Modeling 52.11 (2012): 2840-2847.",
        "documentationUrl": TDC_DOCUMENTATION_URL + "#ames-mutagenicity",
    },
    {
        "endpoint": "clintox", "name": "TDC ClinTox", "fileId": 4259572,
        "sha256": "32f8542b5ed007ca76506b7dbdaaed32c9bbe864ea1ce52d4d49f176d5c63455",
        "documentedRows": 1484,
        "positiveClass": "Clinical-trial failure due to toxicity / CT_TOX (1); comparator drug (0). This is not FDA_APPROVED.",
        "sourcePopulation": "Drugs associated with clinical-trial outcomes; negative labels do not prove absence of every possible clinical toxicity.",
        "reference": "Gayvert, Kaitlyn M., Neel S. Madhukar, and Olivier Elemento. A data-driven approach to predicting successes and failures of clinical trials. Cell Chemical Biology 23.10 (2016): 1294-1301.",
        "documentationUrl": TDC_DOCUMENTATION_URL + "#clintox",
    },
)

ENDPOINTS = tuple(item["endpoint"] for item in DATASETS)
ROW_FIELDS = ("smiles", "endpoint", "label", "source_id", "source_row")
AUDIT_FIELDS = ("smiles", "endpoint", "label", "source_id", "source_row", "canonical_smiles", "reason")
CLINTOX_REFERENCE_URL = "https://deepchemdata.s3-us-west-1.amazonaws.com/datasets/clintox.csv.gz"
CLINTOX_REFERENCE_SHA256 = "7324a84a180ad9068d3847677c85d58e8432e82b3ae6ac95cf0e98b3a58d9697"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download_source(specification: dict, raw_dir: Path, offline: bool = False) -> dict:
    """Reuse only verified bytes; source changes require an explicit code update."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / f"{specification['endpoint']}.csv"
    url = f"https://dataverse.harvard.edu/api/access/datafile/{specification['fileId']}?format=original"
    downloaded = False
    if not target.exists():
        if offline:
            raise ValueError(f"Missing verified source file: {target}")
        request = Request(url, headers={"User-Agent": "AIDD-BlueResearch/1.0", "Accept": "text/csv"})
        with urlopen(request, timeout=60) as response:
            data = response.read(10_000_001)
        if len(data) > 10_000_000:
            raise ValueError("Toxicity source exceeds the expected download size limit.")
        if hashlib.sha256(data).hexdigest() != specification["sha256"]:
            raise ValueError(f"Source checksum changed for {specification['endpoint']}; do not silently train on an unversioned replacement.")
        temporary = target.with_suffix(".download")
        temporary.write_bytes(data)
        temporary.replace(target)
        downloaded = True
    checksum = sha256_file(target)
    if checksum != specification["sha256"]:
        raise ValueError(f"Local source checksum mismatch: {target}")
    return {
        **specification, "downloadUrl": url, "localFile": str(target),
        "verifiedSha256": checksum, "bytes": target.stat().st_size,
        "downloadedThisRun": downloaded, "license": LICENSE_NOTE,
        "tdcMetadataUrl": TDC_METADATA_URL,
    }


def read_tdc_rows(path: Path, endpoint: str) -> list[dict]:
    if endpoint not in ENDPOINTS:
        raise ValueError("Unknown toxicity endpoint.")
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not {"Drug_ID", "Drug", "Y"}.issubset(reader.fieldnames or ()):
            raise ValueError(f"Unexpected TDC CSV contract: {path}")
        return [
            {"smiles": row["Drug"], "endpoint": endpoint, "label": row["Y"],
             "source_id": row["Drug_ID"], "source_row": row_number}
            for row_number, row in enumerate(reader, start=2)
        ]


def verify_clintox_target(raw_dir: Path, offline: bool = False) -> dict:
    """Verify TDC Y against MoleculeNet CT_TOX; never substitute new labels.

    The reference contains wildcard atoms and duplicate/conflicting structures.
    Parsing for this cross-check is intentionally permissive for source identity;
    the application/trainable data retain the stricter canonicalize_smiles gate.
    """
    path = raw_dir / "clintox_moleculenet.csv.gz"
    if not path.exists():
        if offline:
            raise ValueError("Missing ClinTox label verification reference for offline preparation.")
        request = Request(CLINTOX_REFERENCE_URL, headers={"User-Agent": "AIDD-BlueResearch/1.0"})
        with urlopen(request, timeout=60) as response:
            content = response.read(10_000_001)
        if hashlib.sha256(content).hexdigest() != CLINTOX_REFERENCE_SHA256:
            raise ValueError("ClinTox label reference checksum changed.")
        path.write_bytes(content)
    if sha256_file(path) != CLINTOX_REFERENCE_SHA256:
        raise ValueError("ClinTox label reference checksum mismatch.")
    reference_rows = list(csv.DictReader(io.StringIO(gzip.decompress(path.read_bytes()).decode("utf-8-sig"))))
    if not reference_rows or not {"smiles", "CT_TOX", "FDA_APPROVED"}.issubset(reference_rows[0]):
        raise ValueError("ClinTox label reference has an unexpected contract.")

    def canonical_source(smiles: str) -> str | None:
        parameters = Chem.SmilesParserParams()
        parameters.parseName = False
        with rdBase.BlockLogs():
            molecule = Chem.MolFromSmiles(smiles, parameters)
        if molecule is None:
            return None
        for atom in molecule.GetAtoms():
            atom.SetAtomMapNum(0)
        return Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)

    labels = defaultdict(set)
    for row in reference_rows:
        canonical = canonical_source(row["smiles"])
        if canonical is not None:
            labels[canonical].add(row["CT_TOX"])
    tdc_rows = read_tdc_rows(raw_dir / "clintox.csv", "clintox")
    unambiguous = ambiguous = 0
    for row in tdc_rows:
        canonical = canonical_source(row["smiles"])
        matches = labels.get(canonical, set())
        if str(row["label"]) not in matches:
            raise ValueError("TDC ClinTox label does not match the original MoleculeNet CT_TOX label; do not train until resolved.")
        if len(matches) == 1:
            unambiguous += 1
        else:
            ambiguous += 1
    return {
        "referenceUrl": CLINTOX_REFERENCE_URL, "sha256": CLINTOX_REFERENCE_SHA256,
        "referenceRows": len(reference_rows), "tdcRowsMatchedByCanonicalStructure": len(tdc_rows),
        "unambiguousCtToxMatches": unambiguous, "rowsInConflictingCtToxGroups": ambiguous,
        "labelMismatchRows": 0, "trainingUse": "verification only; reference does not supply new training labels",
    }


def normalize_rows(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict], dict]:
    """Reject missing labels; quarantine *all* rows for conflicting identities.

    No salt stripping, tautomer merging, charge neutralization or stereochemistry
    removal occurs. The same chemistry contract is used during training/serving.
    """
    groups = defaultdict(list)
    quarantine = []
    duplicates = []
    raw_counts = Counter(row.get("endpoint") for row in rows)
    for raw in rows:
        row = {key: raw.get(key, "") for key in ROW_FIELDS}
        label = row["label"]
        valid_label = (
            type(label) is int and label in (0, 1)
            or isinstance(label, str) and label in ("0", "1", "0.0", "1.0")
        )
        if row["endpoint"] not in ENDPOINTS or not valid_label:
            quarantine.append({**row, "canonical_smiles": "", "reason": "invalid_endpoint_or_missing_nonbinary_label"})
            continue
        row["label"] = int(float(label))
        try:
            canonical = canonicalize_smiles(row["smiles"])
        except (ValueError, TypeError) as error:
            quarantine.append({**row, "canonical_smiles": "", "reason": "invalid_structure: " + str(error)})
            continue
        groups[(row["endpoint"], canonical)].append(row)
    retained = []
    conflict_groups = Counter()
    for (endpoint, canonical), members in sorted(groups.items()):
        if len({row["label"] for row in members}) > 1:
            conflict_groups[endpoint] += 1
            quarantine.extend({**row, "canonical_smiles": canonical, "reason": "conflicting_labels_for_same_structure_and_endpoint"} for row in members)
            continue
        retained.append({**members[0], "smiles": canonical})
        duplicates.extend({**row, "canonical_smiles": canonical, "reason": "duplicate_same_structure_endpoint_label"} for row in members[1:])
    summary = {}
    for endpoint in ENDPOINTS:
        clean = [row for row in retained if row["endpoint"] == endpoint]
        excluded = [row for row in quarantine if row["endpoint"] == endpoint]
        summary[endpoint] = {
            "rawRows": raw_counts[endpoint], "retainedRows": len(clean),
            "positiveRows": sum(row["label"] for row in clean),
            "negativeRows": sum(1 - row["label"] for row in clean),
            "duplicatesRemoved": sum(row["endpoint"] == endpoint for row in duplicates),
            "conflictingStructureGroups": conflict_groups[endpoint],
            "quarantinedRows": len(excluded),
            "quarantineReasons": dict(Counter(row["reason"].split(":", 1)[0] for row in excluded)),
        }
    return retained, quarantine, duplicates, summary


def write_csv(path: Path, rows: list[dict], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def prepare_data(data_dir: Path, offline: bool = False) -> dict:
    raw_dir = data_dir / "raw"
    output_dir = data_dir / "processed"
    sources = [download_source(spec, raw_dir, offline) for spec in DATASETS]
    label_validation = verify_clintox_target(raw_dir, offline)
    rows = []
    for source in sources:
        rows.extend(read_tdc_rows(Path(source["localFile"]), source["endpoint"]))
    retained, quarantine, duplicates, summary = normalize_rows(rows)
    target = output_dir / "toxicity.csv"
    write_csv(target, retained, ROW_FIELDS)
    write_csv(output_dir / "quarantine.csv", quarantine, AUDIT_FIELDS)
    write_csv(output_dir / "duplicates.csv", duplicates, AUDIT_FIELDS)
    checksum = sha256_file(target)
    result = {
        "schemaVersion": DATA_PIPELINE_VERSION,
        "datasetVersion": "aidd-toxicity-" + checksum[:16],
        "preparedAtUtc": datetime.now(timezone.utc).isoformat(),
        "chemistryVersion": CHEMISTRY_VERSION, "rdkitVersion": rdBase.rdkitVersion,
        "sourceRegistry": "TDC official metadata pinned at " + TDC_METADATA_COMMIT,
        "sources": sources, "counts": summary, "clintoxLabelValidation": label_validation,
        "normalization": "Strict canonical isomeric SMILES; atom maps removed; salts, isotopes, stereochemistry and charge preserved. All canonical identity/endpoint label conflicts quarantined; same-label duplicates removed. No missing labels imputed.",
        "output": {"path": str(target), "sha256": checksum, "rows": len(retained),
                   "quarantinePath": str(output_dir / "quarantine.csv"),
                   "quarantineSha256": sha256_file(output_dir / "quarantine.csv"),
                   "duplicatesPath": str(output_dir / "duplicates.csv"),
                   "duplicatesSha256": sha256_file(output_dir / "duplicates.csv")},
        "licenseCaveat": LICENSE_NOTE,
        "limitations": [
            "Heterogeneous literature/assay and clinical outcome labels are dataset-level labels, not universal in vivo toxicity truth.",
            "Downloaded row counts can differ from TDC documentation; actual counts are reported separately.",
            "This preparation step does not train a model, fit calibration, or assess performance.",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return result
