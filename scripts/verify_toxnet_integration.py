"""Read-only acceptance of the integrated, real-weight ToxNet research system.

Explicit local paths are required. No .env, external provider or synthetic
training sample is used; exported evidence contains aggregate checks only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCHEMA = "aidd.toxnet.integration-runtime-verification.v1"
FOUR_TASKS = ("herg", "dili", "ames", "clintox")
TOX21_TASKS = (
    "tox21_nr_ar", "tox21_nr_ar_lbd", "tox21_nr_ahr", "tox21_nr_aromatase",
    "tox21_nr_er", "tox21_nr_er_lbd", "tox21_nr_ppar_gamma", "tox21_sr_are",
    "tox21_sr_atad5", "tox21_sr_hse", "tox21_sr_mmp", "tox21_sr_p53",
)
FORBIDDEN_EXPORT_KEYS = {
    "smiles", "canonicalsmiles", "label", "labels", "source_id", "source_row",
    "observations", "trainingrows", "validationpredictions", "apikey", "api_key",
    "authorization", "apiurl", "api_url", "password", "secret",
}


class IntegrationCheckError(RuntimeError):
    """A machine-readable failure without molecular rows or configuration data."""


def require(condition, code):
    if not condition:
        raise IntegrationCheckError(code)


def checksum(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verify_protected_files(root, snapshot, origin_ref="origin/main"):
    """Compare fixed local hashes and, when requested, independent Git bytes."""
    root = Path(root).resolve()
    ledger = read_json(snapshot)
    require(ledger.get("status") == "unchanged" and bool(ledger.get("files")), "PROTECTED_LEDGER_INVALID")
    records, seen, origin_paths = [], set(), set()
    if origin_ref:
        tree = subprocess.run(["git", "ls-tree", "-r", "--name-only", origin_ref],
                              cwd=root, capture_output=True, check=False)
        require(tree.returncode == 0, "PROTECTED_GIT_REFERENCE_UNAVAILABLE")
        origin_paths = set(tree.stdout.decode("utf-8").splitlines())
        require("models/aidd-toxnet/weights.pt" in origin_paths, "PROTECTED_GIT_V2_WEIGHTS_UNAVAILABLE")
    for record in ledger["files"]:
        relative = Path(record["file"])
        path = (root / relative).resolve()
        require(not relative.is_absolute() and path.is_relative_to(root) and path != root,
                "PROTECTED_PATH_ESCAPE")
        require(record["file"] not in seen, "PROTECTED_DUPLICATE_PATH")
        seen.add(record["file"])
        expected = record["beforeSha256"]
        require(expected == record["afterSha256"], "PROTECTED_LEDGER_CHANGED")
        require(path.is_file() and checksum(path) == expected, "PROTECTED_FILE_CHANGED")
        origin_hash = None
        if origin_ref and relative.as_posix() in origin_paths:
            proc = subprocess.run(["git", "show", f"{origin_ref}:{relative.as_posix()}"],
                                  cwd=root, capture_output=True, check=False)
            require(proc.returncode == 0, "PROTECTED_GIT_REFERENCE_UNAVAILABLE")
            origin_hash = hashlib.sha256(proc.stdout).hexdigest()
            require(origin_hash == expected, "PROTECTED_GIT_BYTES_CHANGED")
        records.append({"file": relative.as_posix(), "sha256": expected,
                        "matchesProtectedSnapshot": True, "originMainSha256": origin_hash,
                        "originMainComparison": "identical" if origin_hash else "not-distributed; protected-snapshot-only"})
    require("models/aidd-toxnet/weights.pt" in seen, "PROTECTED_V2_WEIGHTS_NOT_INCLUDED")
    return records


def validate_scores(items, expected_tasks):
    require(isinstance(items, list) and [item.get("id") for item in items] == list(expected_tasks),
            "OUTPUT_TASK_ORDER_MISMATCH")
    for item in items:
        score = item.get("score")
        require(type(score) in (float, int) and math.isfinite(score) and 0 <= score <= 1,
                "OUTPUT_SCORE_INVALID")
    return {item["id"]: item["score"] for item in items}


def validate_report(packet, original_scores, weights_sha):
    require(packet.get("contractVersion") == "aidd.research.explanation.api.v1", "EXPLANATION_CONTRACT_MISMATCH")
    require(validate_scores(packet["modelPrediction"]["endpoints"], FOUR_TASKS) == original_scores,
            "REPORT_CHANGED_MODEL_SCORES")
    require(packet["provenance"].get("weightsSha256") == weights_sha and
            packet["provenance"].get("researchOnly") is True and
            packet["provenance"].get("humanRiskProbability") is False, "REPORT_MODEL_PROVENANCE_MISMATCH")
    attribution = packet["attribution"]
    require(attribution.get("status") in ("available", "computed-with-warning"), "REAL_ATTRIBUTION_UNAVAILABLE")
    require(attribution["provenance"].get("weightsSha256") == weights_sha and
            attribution["provenance"].get("predictionBeforeAfterIdentical") is True,
            "ATTRIBUTION_MODEL_MISMATCH")
    require(attribution.get("atoms") and attribution.get("imageDataUrl", "").startswith("data:image/png;base64,"),
            "ATTRIBUTION_VISUALIZATION_MISSING")
    report = packet["report"]
    require(report.get("language", {}).get("status") == "unavailable", "EXTERNAL_LANGUAGE_PROVIDER_UNEXPECTED")
    kinds = {claim["sourceKind"] for claim in report["claims"]}
    require({"model-computation", "human-rule", "mechanism-hypothesis"} <= kinds, "REPORT_SOURCE_KINDS_MIXED")
    require(all(set(("sourceIds", "confidence", "limitations")) <= set(claim) for claim in report["claims"]),
            "REPORT_TRACEABILITY_MISSING")
    require(report["mechanismAssessment"]["status"] == "insufficient-evidence" and
            report["mechanismAssessment"]["verifiedMechanisticClaims"] == [], "UNSUPPORTED_MECHANISM_CLAIM")
    require(any("尚无充分证据" in section["text"] for section in report["sections"]), "CHINESE_UNCERTAINTY_MISSING")
    require(packet["structuralAlerts"].get("sourceKind") == "human-rule" and
            packet["structuralAlerts"].get("notNeuralAttribution") is True, "RULES_MISREPRESENTED_AS_NEURAL")
    return {"scoresUnchanged": True, "sourceKinds": sorted(kinds), "localChineseReport": True,
            "mechanismUncertain": True, "languageUnavailableLocalReportPreserved": True,
            "attributionStatus": attribution["status"], "attributionMethod": attribution["method"],
            "atomCount": len(attribution["atoms"]), "weightsSha256": weights_sha,
            "attributionDiagnostics": attribution.get("diagnostics", {})}


def validate_tox21(packet, all_outputs, weights_sha):
    require(packet.get("contractVersion") == "aidd.research.tox21.api.v1", "TOX21_CONTRACT_MISMATCH")
    values = validate_scores(packet["pathways"], TOX21_TASKS)
    all_values = validate_scores(all_outputs["endpoints"], (*FOUR_TASKS, *TOX21_TASKS))
    require(all(all_values[task] == values[task] for task in TOX21_TASKS), "TOX21_ROUTE_CHANGED_SCORES")
    semantics = packet["semantics"]
    require(semantics.get("clinicalToxicityProbability") is False and semantics.get("aggregateRiskProvided") is False,
            "TOX21_CLINICAL_AGGREGATE_FORBIDDEN")
    require(packet["provenance"].get("formalAvailability") is False and
            packet["provenance"].get("researchOnly") is True and
            packet["provenance"].get("weightsSha256") == weights_sha,
            "TOX21_RESEARCH_PROVENANCE_MISMATCH")
    require(packet["calibration"].get("status") == "not-fitted" and
            packet["applicability"].get("validated") is False, "TOX21_CERTIFICATION_UNSUPPORTED")
    return {"taskCount": len(all_values), "pathwayCount": len(values), "routeScoresUnchanged": True,
            "formalAvailability": False, "researchOnly": True, "calibrationStatus": "not-fitted",
            "applicabilityValidated": False, "clinicalAggregateProvided": False}


def _safe_create_app():
    # backend.main constructs a global app during import. Prevent that import's
    # side effect from reading the user's dotenv or process provider settings.
    with patch.dict(os.environ, {}, clear=True), patch("backend.configuration.dotenv_values", return_value={}):
        from backend.main import create_app
    return create_app


def verify_runtime(*, model_dir, encoder_dir, data_dir, index_dir, device="cpu", root=ROOT,
                   protected_snapshot=None, origin_ref="origin/main", threads=6):
    started = time.monotonic()
    root, model_dir, encoder_dir, data_dir, index_dir = map(Path, (root, model_dir, encoder_dir, data_dir, index_dir))
    protected_snapshot = protected_snapshot or root / "artifacts/v3-multitask/v2-preservation.json"
    protected_before = verify_protected_files(root, protected_snapshot, origin_ref)
    from backend.local_model import LocalModelClient
    from backend.multitask_model import MultitaskModelClient
    from backend.multitask_data import load_multitask_data
    from backend.evidence_search import EvidenceSearch
    from backend.configuration import ModelConfigurationStore
    from backend.explanation_service import ExplanationService
    from fastapi.testclient import TestClient
    import torch

    torch.set_num_threads(threads)
    require(device in ("cpu", "cuda"), "DEVICE_UNSUPPORTED")
    require(device != "cuda" or torch.cuda.is_available(), "CUDA_UNAVAILABLE")
    data_manifest, samples = load_multitask_data(data_dir)
    # Validation reconstructs identities/source membership; do not export rows.
    data_proof = {"datasetVersion": data_manifest["datasetVersion"], "manifestSha256": checksum(data_dir / "manifest.json"),
                  "splitSha256": data_manifest["splitSha256"], "sampleCounts": data_manifest["sampleCounts"],
                  "actualSampleCount": len(samples), "sourceMembershipAndLeakageChecksPassed": True,
                  "newBlindTestClaimed": data_manifest["newBlindTestClaimed"]}
    del samples
    local = LocalModelClient(model_dir=root / "models/aidd-toxnet", env={})
    research = MultitaskModelClient(model_dir, encoder_dir=encoder_dir, device=device, allow_research=True)
    local_info, research_info = local.get_model_info(), research.get_model_info()
    require(local_info.get("configured") is True, "V2_MODEL_UNAVAILABLE")
    require(research_info.get("configured") is True, "B16_MODEL_UNAVAILABLE")
    require(research_info.get("formalAvailability") is False and research_info["tasks"] == list((*FOUR_TASKS, *TOX21_TASKS)),
            "B16_NOT_A_COMPLETE_RESEARCH_16_TASK_MODEL")
    require(research_info["dataset"]["datasetVersion"] == data_proof["datasetVersion"] and
            research_info["dataset"]["manifestSha256"] == data_proof["manifestSha256"] and
            research_info["dataset"]["splitSha256"] == data_proof["splitSha256"], "B16_DATA_IDENTITY_MISMATCH")
    local_loaded = local._ensure_loaded()
    parameter_hash = lambda: hashlib.sha256(b"".join(value.detach().cpu().contiguous().numpy().tobytes()
                                        for value in local_loaded["model"].state_dict().values())).hexdigest()
    before_parameter_hash = parameter_hash()
    evidence = EvidenceSearch(index_dir)
    with evidence._lock:
        index_manifest, structures, _, observations, _ = evidence._load()
        known = next(structure for structure in structures if any(row["endpoint"] == "herg" for row in observations[structure]))
    retrieved = evidence.search(known, endpoint="herg")
    require(retrieved.get("status") == "available" and retrieved["results"][0]["exactMatch"] is True,
            "AUDITED_EXPERIMENT_RETRIEVAL_UNAVAILABLE")
    require(all(row.get("sourceSha256") and row.get("use") == "explanation-only" and
                row.get("probabilityInterpretation") == "not-a-prediction" for row in retrieved["results"]),
            "EXPERIMENT_SOURCE_CONTRACT_MISMATCH")
    index_proof = {"datasetVersion": index_manifest["datasetVersion"], "sha256": index_manifest["records"]["sha256"],
                   "observationCount": index_manifest["records"]["count"], "uniqueStructureCount": index_manifest["uniqueStructures"],
                   "sourceLedgerAndMembershipVerified": True, "actualExactMatchVerified": True,
                   "similarityIsConfidence": False, "optimizationUse": False}
    with tempfile.TemporaryDirectory(prefix="toxnet-integration-") as temporary:
        temporary = Path(temporary)
        isolated_path = temporary / "nonexistent-config.env"
        registry = {"local": local}
        config = ModelConfigurationStore(isolated_path, registry=registry, env={})
        service = ExplanationService(local, config, research_client=research, evidence=evidence)
        values = {"TOXNET_MULTITASK_ENABLE": "true", "TOXNET_MULTITASK_MODEL_DIR": str(model_dir.resolve()),
                  "TOXNET_MULTITASK_ENCODER_DIR": str(encoder_dir.resolve()), "TOXNET_MULTITASK_DEVICE": device}
        # Router client and attribution share a verified snapshot to avoid a
        # second ~47M-parameter encoder allocation on the 8 GB target device.
        with patch("backend.research_routes.MultitaskModelClient.from_env", return_value=research):
            app = _safe_create_app()(registry=registry, default_provider="local", config_path=isolated_path,
                                     env=values, local_client=local, explanation_service=service)
        with TestClient(app) as client:
            health, info = client.get("/api/health"), client.get("/api/local-model/info")
            require(health.status_code == info.status_code == 200, "V2_INFO_HTTP_FAILED")
            require(info.json()["weightsSha256"] == local_info["weightsSha256"], "V2_HTTP_MODEL_IDENTITY_MISMATCH")
            predicted = client.post("/api/analyze", json={"smiles": "CCO", "mode": "model", "provider": "local"})
            require(predicted.status_code == 200, "V2_FOUR_POINT_HTTP_FAILED")
            original = predicted.json()
            scores = validate_scores(original["endpoints"], FOUR_TASKS)
            explained = client.post("/api/research/explain", json={"smiles": "CCO", "model": "v2", "endpoint": "herg",
                      "includeLanguage": True, "expectedWeightsSha256": local_info["weightsSha256"],
                      "expectedStructureId": original["structureId"]})
            require(explained.status_code == 200, "REAL_EXPLANATION_HTTP_FAILED")
            report_proof = validate_report(explained.json(), scores, local_info["weightsSha256"])
            require(validate_scores(client.post("/api/analyze", json={"smiles": "CCO", "provider": "local"}).json()["endpoints"],
                                    FOUR_TASKS) == scores, "V2_POST_EXPLANATION_SCORES_CHANGED")
            tox21 = client.post("/api/research/tox21", json={"smiles": "CCO"})
            require(tox21.status_code == 200, "B16_TOX21_HTTP_UNAVAILABLE")
            b16_proof = validate_tox21(tox21.json(), research.predict_all("CCO"), research_info["weightsSha256"])
            status = client.get("/api/research/tox21/status")
            require(status.status_code == 200 and status.json()["configured"] is True and
                    status.json()["formalAvailability"] is False, "B16_RESEARCH_STATUS_INVALID")
            for route, body in (("/api/analyze", {"smiles": "CC=", "provider": "local"}),
                                ("/api/research/explain", {"smiles": "CC="}),
                                ("/api/research/tox21", {"smiles": "CC="})):
                require(client.post(route, json=body).status_code == 422, "INVALID_SMILES_NOT_REJECTED")
            mismatch = client.post("/api/research/explain", json={"smiles": "CCO", "expectedWeightsSha256": "a" * 64})
            require(mismatch.status_code == 409, "EXPLANATION_WRONG_SHA_NOT_REJECTED")
            require(client.post("/api/analyze", json={"smiles": "CCO", "provider": "custom"}).status_code == 422,
                    "UNCONFIGURED_PROVIDER_NOT_REJECTED")
            missing = MultitaskModelClient(model_dir, encoder_dir=None, allow_research=True)
            require(missing.get_status().get("errorCode") == "MULTITASK_ENCODER_MISSING", "MISSING_ENCODER_NOT_REJECTED")
            with patch.object(research, "predict_tox21", side_effect=lambda smiles: missing.predict_tox21(smiles)):
                unavailable = client.post("/api/research/tox21", json={"smiles": "CCO"})
            require(unavailable.status_code == 503 and "pathways" not in unavailable.json(), "MISSING_ENCODER_HTTP_FAKED_OUTPUT")
            copied = temporary / "corrupted-copy"
            shutil.copytree(model_dir, copied)
            copied_manifest = read_json(copied / "manifest.json")
            with (copied / copied_manifest["artifacts"]["weights"]["file"]).open("ab") as handle:
                handle.write(b"integration-check-only")
            bad = MultitaskModelClient(copied, encoder_dir=encoder_dir, allow_research=True)
            require(bad.get_status().get("errorCode") == "MULTITASK_CHECKSUM_MISMATCH", "BAD_B16_SHA_NOT_REJECTED")
            with patch.object(research, "predict_tox21", side_effect=lambda smiles: bad.predict_tox21(smiles)):
                corrupted = client.post("/api/research/tox21", json={"smiles": "CCO"})
            require(corrupted.status_code == 503 and "pathways" not in corrupted.json(), "BAD_SHA_HTTP_FAKED_OUTPUT")
    require(before_parameter_hash == parameter_hash(), "V2_PARAMETERS_CHANGED")
    require(all(parameter.grad is None for parameter in local_loaded["model"].parameters()), "V2_PARAMETER_GRADIENTS_CHANGED")
    protected_after = verify_protected_files(root, protected_snapshot, origin_ref)
    require(protected_after == protected_before, "PROTECTED_HISTORY_CHANGED_DURING_RUN")
    versions = {name: importlib.metadata.version(name) for name in ("torch", "numpy", "rdkit", "transformers", "fastapi")}
    return {"schema": SCHEMA, "status": "passed", "generatedAt": datetime.now(timezone.utc).isoformat(),
            "durationSeconds": round(time.monotonic() - started, 3), "runtime": {"device": device, "dependencies": versions,
                  "pythonVersion": sys.version.split()[0], "v2InferenceDevice": "cpu", "cudaAvailable": torch.cuda.is_available()},
            "v2": {"modelVersion": local_info["version"], "weightsSha256": local_info["weightsSha256"],
                  "parameterCount": local_info["parameterCount"], "httpFourEndpointsPassed": True,
                  "parameterTensorsUnchanged": True, "parameterGradientsUnmodified": True},
            "data": data_proof, "experimentalIndex": index_proof, "explanation": report_proof,
            "b16": {**b16_proof, "modelVersion": research_info["modelVersion"], "weightsSha256": research_info["weightsSha256"],
                     "encoderRevision": research_info["encoder"]["revision"], "encoderWeightsSha256": research_info["encoder"]["weightsSha256"],
                     "embeddingDimension": research_info["encoder"]["dimension"], "headParameterCount": research_info["parameterCount"]},
            "negativeCases": {"invalidSmiles422AllRoutes": True, "wrongExplanationSha409": True,
                  "missingEncoder503NoFakeOutput": True, "corruptedCopySha503NoFakeOutput": True,
                  "unconfiguredProviderRejected": True}, "protectedV2AndHistory": protected_after,
            "externalProvidersInvoked": False, "userDotenvRead": False, "trainingPerformed": False,
            "scientificMechanismValidated": False, "clinicalUseCertified": False,
            "exportContainsExperimentalRows": False}


def write_summary(output, summary):
    """Exclusive output creation; nested sensitive/observation keys are denied."""
    def inspect(value):
        if isinstance(value, dict):
            require(not any(str(key).lower() in FORBIDDEN_EXPORT_KEYS for key in value), "SUMMARY_SENSITIVE_CONTENT_FORBIDDEN")
            for member in value.values():
                inspect(member)
        elif isinstance(value, (list, tuple)):
            for member in value:
                inspect(member)
    inspect(summary)
    require(summary.get("schema") == SCHEMA, "SUMMARY_SCHEMA_INVALID")
    payload = json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("model-dir", "encoder-dir", "data-dir", "index-dir", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--protected-snapshot", type=Path, default=ROOT / "artifacts/v3-multitask/v2-preservation.json")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Output already exists; choose a new file to preserve historical verification")
    if not 1 <= args.threads <= 32:
        parser.error("--threads must be 1..32")
    try:
        summary = verify_runtime(model_dir=args.model_dir, encoder_dir=args.encoder_dir, data_dir=args.data_dir,
                                 index_dir=args.index_dir, device=args.device, protected_snapshot=args.protected_snapshot,
                                 threads=args.threads)
    except (IntegrationCheckError, OSError, ValueError, KeyError, ImportError, RuntimeError) as error:
        # Never print arbitrary exception text: it can include molecular rows,
        # provider URLs or environment configuration from a dependency.
        summary = {"schema": SCHEMA, "status": "failed", "generatedAt": datetime.now(timezone.utc).isoformat(),
                   "failureCode": str(error) if isinstance(error, IntegrationCheckError) else type(error).__name__,
                   "externalProvidersInvoked": False, "trainingPerformed": False, "exportContainsExperimentalRows": False}
    write_summary(args.output, summary)
    print(json.dumps({"status": summary["status"], "output": str(args.output), "failureCode": summary.get("failureCode")}, ensure_ascii=False))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
