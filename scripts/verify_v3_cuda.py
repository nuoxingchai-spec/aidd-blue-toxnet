"""Real offline CUDA acceptance, without changing a trained model on disk.

Exit 0 means every requested real-model batch passed; 2 means CUDA/dependencies
were unavailable; 1 means a verification failed (including an actual OOM).
There is no synthetic model, download, fitting on validation, or CPU fallback.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import gc
import hashlib
import importlib
from importlib import metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCHEMA = "aidd.toxnet.v3-cuda-verification.v1"
DEFAULT_BATCHES = (1, 2, 4, 8)


class VerificationError(ValueError):
    pass


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _label(path):
    path = Path(path).resolve()
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)


def driver_information():
    """A read-only official driver query; no shell or environment modifications."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return {"status": "available" if result.returncode == 0 else "query-failed",
                "returnCode": result.returncode, "rows": result.stdout.strip().splitlines(),
                "error": result.stderr.strip()[:1000]}
    except (OSError, subprocess.SubprocessError) as error:
        return {"status": "unavailable", "errorType": type(error).__name__, "error": str(error)}


def environment_information(torch=None):
    information = {"pythonVersion": platform.python_version(), "pythonExecutable": sys.executable,
                   "platform": platform.platform(), "driver": driver_information(),
                   "torchVersion": None, "cudaBuildVersion": None, "cudaAvailable": False,
                   "gpuDevices": [], "dependencies": {}}
    for name in ("torch", "rdkit", "numpy", "transformers", "tokenizers", "safetensors", "scikit-learn"):
        try:
            information["dependencies"][name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            information["dependencies"][name] = None
    try:
        torch = torch if torch is not None else importlib.import_module("torch")
        information["torchVersion"] = str(torch.__version__)
        information["cudaBuildVersion"] = torch.version.cuda
        information["cudaAvailable"] = bool(torch.cuda.is_available())
        information["pytorchWheelKind"] = "cpu" if torch.version.cuda is None else "cuda"
        if information["cudaAvailable"]:
            for index in range(torch.cuda.device_count()):
                device = torch.cuda.get_device_properties(index)
                information["gpuDevices"].append({"index": index, "name": device.name,
                                                   "totalMemoryBytes": int(device.total_memory),
                                                   "computeCapability": [device.major, device.minor]})
    except (ImportError, OSError, RuntimeError) as error:
        information["environmentError"] = {"type": type(error).__name__, "message": str(error)}
    return information


def validate_batch_sizes(values):
    if not values or any(type(value) is not int or not 1 <= value <= 8 for value in values) or len(set(values)) != len(values):
        raise VerificationError("Use distinct batch sizes in 1..8; the acceptance defaults are 1 2 4 8.")
    return tuple(sorted(values))


def is_cuda_oom(error, torch):
    """EncoderError retains the genuine torch OOM as its exception cause."""
    cursor, visited = error, set()
    while cursor is not None and id(cursor) not in visited:
        visited.add(id(cursor))
        if isinstance(cursor, torch.cuda.OutOfMemoryError):
            return True
        cursor = cursor.__cause__ or cursor.__context__
    return False


def measure_cuda_phase(torch, operation):
    """Measure actual synchronized execution; null metrics mean not measured."""
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    record = {"status": "failed", "cudaDevice": "cuda:0", "oom": False,
              "baselineAllocatedBytes": int(torch.cuda.memory_allocated()),
              "baselineReservedBytes": int(torch.cuda.memory_reserved()),
              "peakAllocatedBytes": None, "peakReservedBytes": None, "executionSeconds": None}
    started = time.perf_counter()
    value = None
    try:
        value = operation()
        torch.cuda.synchronize()
        record["status"] = "passed"
    except Exception as error:
        record.update({"oom": is_cuda_oom(error, torch), "errorType": type(error).__name__, "error": str(error)})
        # OOM keeps the CUDA context usable; other errors may not. Never pretend
        # an unsuccessful synchronization measured a successful execution.
        try:
            torch.cuda.synchronize()
        except RuntimeError:
            pass
    finally:
        record["executionSeconds"] = round(time.perf_counter() - started, 6)
        record["peakAllocatedBytes"] = int(torch.cuda.max_memory_allocated())
        record["peakReservedBytes"] = int(torch.cuda.max_memory_reserved())
        record["peakAdditionalAllocatedBytes"] = record["peakAllocatedBytes"] - record["baselineAllocatedBytes"]
        record["peakAdditionalReservedBytes"] = record["peakReservedBytes"] - record["baselineReservedBytes"]
    return record, value


def basic_cuda_test(torch):
    """Random tensors test GPU arithmetic only, never stand in for MoLFormer."""
    torch.manual_seed(42)
    left, right = torch.randn(64, 64, device="cuda"), torch.randn(64, 64, device="cuda")
    multiplied = left @ right
    model = torch.nn.Linear(64, 8).to("cuda")
    before = [parameter.detach().clone() for parameter in model.parameters()]
    optimizer = torch.optim.AdamW(model.parameters(), lr=.001)
    output = model(multiplied)
    loss = output.square().mean()
    loss.backward()
    gradients = [parameter.grad for parameter in model.parameters()]
    if not all(value is not None and value.device.type == "cuda" and bool(torch.isfinite(value).all()) for value in gradients):
        raise VerificationError("GPU basic gradients are invalid or not CUDA tensors.")
    optimizer.step()
    if not bool(torch.isfinite(output).all()) or not any(not torch.equal(old, new) for old, new in zip(before, model.parameters())):
        raise VerificationError("GPU basic forward/update verification failed.")
    return {"matmulDevice": multiplied.device.type, "forwardDevice": output.device.type,
            "backwardDevice": gradients[0].device.type, "optimizerUpdated": True,
            "loss": float(loss.detach().cpu()), "purpose": "GPU arithmetic; not a molecular-model benchmark"}


def state_digest(model):
    checksum = hashlib.sha256()
    for name, tensor in sorted(model.state_dict().items()):
        value = tensor.detach().cpu().contiguous()
        checksum.update(name.encode())
        checksum.update(str(value.dtype).encode())
        checksum.update(str(tuple(value.shape)).encode())
        checksum.update(value.numpy().tobytes())
    return checksum.hexdigest()


def artifact_files(model_dir, encoder_dir):
    files = {path.resolve() for directory in (model_dir, encoder_dir) for path in directory.rglob("*") if path.is_file()}
    protected = ROOT / "artifacts/v3/v2-preservation.json"
    if protected.is_file():
        for entry in json.loads(protected.read_text(encoding="utf-8"))["files"]:
            path = (ROOT / entry["file"]).resolve()
            if not path.is_relative_to(ROOT) or digest(path) != entry["afterSha256"]:
                raise VerificationError("Protected v2/history bytes do not match the established preservation receipt.")
            files.add(path)
    return {path: digest(path) for path in sorted(files)}


def verify_development_binding(data, manifest, data_dir, tasks):
    if (manifest.get("formalAvailability") is not False or manifest.get("status") != "trained-completed"
        or manifest.get("tasks") != list(tasks) or manifest.get("architecture", {}).get("featureBranches") != ["molformer"]
        or manifest.get("architecture", {}).get("headType") != "shared-mlp"
        or manifest.get("dataset", {}).get("datasetVersion") != data.get("datasetVersion")
        or manifest.get("dataset", {}).get("manifestSha256") != digest(Path(data_dir) / "manifest.json")
        or manifest.get("dataset", {}).get("splitSha256") != data.get("splitSha256")
        or any(manifest["training"]["endpointStatus"].get(task) != "trained" for task in tasks)):
        raise VerificationError("Require the actual fully trained frozen-MoLFormer B16 bound to this audited development split.")


def train_head_probe(torch, experiments, loaded, features, samples, positive_weights, *, seed=42):
    """One optimizer step on a memory-only clone and true TRAIN observations."""
    import numpy as np
    if not samples or any(row.get("split") != "train" for row in samples):
        raise VerificationError("The CUDA training probe accepts audited train rows only.")
    manifest = loaded["manifest"]
    labels, masks = experiments.labels_and_masks(samples, tasks=experiments.ALL_ENDPOINTS)
    values = ((features - loaded["mean"]) / loaded["scale"]).astype(np.float32)
    if values.shape != (len(samples), manifest["architecture"]["inputDim"]) or not np.isfinite(values).all():
        raise VerificationError("Real frozen features/preprocessing have invalid dimensions or values.")
    torch.manual_seed(seed)
    clone = copy.deepcopy(loaded["head"]).to("cuda").requires_grad_(True).train()
    before = state_digest(clone)
    weights = torch.tensor([manifest["training"]["taskWeights"][task] for task in experiments.ALL_ENDPOINTS], device="cuda")
    positive = torch.from_numpy(positive_weights).to("cuda")
    inputs = torch.from_numpy(values).to("cuda")
    targets, mask = torch.from_numpy(labels).to("cuda"), torch.from_numpy(masks).to("cuda")
    optimizer = torch.optim.AdamW(clone.parameters(), lr=.001, weight_decay=.001)
    output = clone(inputs)
    if output.shape != (len(samples), 16) or output.device.type != "cuda" or not bool(torch.isfinite(output).all()):
        raise VerificationError("Actual B16 forward failed the sixteen-task finite CUDA output contract.")
    loss = experiments.weighted_masked_bce(output, targets, mask, weights=weights, positive_weights=positive)
    if loss is None or not bool(torch.isfinite(loss)):
        raise VerificationError("No real observed labels, or a nonfinite masked BCE loss.")
    loss.backward()
    gradients = [parameter.grad for parameter in clone.parameters()]
    if not all(value is not None and value.device.type == "cuda" and bool(torch.isfinite(value).all()) for value in gradients):
        raise VerificationError("Actual B16 CUDA gradients failed finite/device checks.")
    # Skipped tasks have an exact zero gradient; missing labels are not negatives.
    skipped = [index for index in range(16) if masks[:, index].sum() == 0]
    if any(not bool((parameter.grad == 0).all()) for index in skipped for parameter in clone.heads[index].parameters()):
        raise VerificationError("An unobserved task unexpectedly received a supervised gradient.")
    optimizer.step()
    if state_digest(clone) == before:
        raise VerificationError("The memory-only B16 clone did not update.")
    if any(not bool(torch.isfinite(parameter).all()) for parameter in clone.parameters()):
        raise VerificationError("Optimizer produced nonfinite B16 parameters.")
    result = {"outputShape": list(output.shape), "device": output.device.type, "loss": float(loss.detach().cpu()),
              "observedLabels": int(masks.sum()), "missingLabels": int(masks.size - masks.sum()),
              "observedPerTask": {task: int(masks[:, index].sum()) for index, task in enumerate(experiments.ALL_ENDPOINTS)},
              "tasksSkippedWithoutObservations": [experiments.ALL_ENDPOINTS[index] for index in skipped],
              "finiteGradients": True, "optimizerUpdatedMemoryClone": True,
              "optimizerSaved": False, "split": "train", "seed": seed,
              "classWeightsFitSplit": "train", "taskWeights": weights.detach().cpu().tolist()}
    del clone, optimizer, inputs, output, loss, gradients
    return result


def run_verification(*, encoder_dir, model_dir, data_dir, batch_sizes=DEFAULT_BATCHES, threads=6, atol=.001, rtol=.001):
    batches = validate_batch_sizes(batch_sizes)
    if type(threads) is not int or threads < 1 or not 0 < atol <= .01 or not 0 < rtol <= .01:
        raise VerificationError("Positive CPU threads and CPU/GPU tolerances in (0,.01] are required.")
    report = {"schema": SCHEMA, "generatedAt": datetime.now(timezone.utc).isoformat(),
              "status": "not-tested-no-cuda-device", "formalAvailability": False,
              "validationCodeSha256": digest(Path(__file__)), "environment": environment_information(),
              "requestedBatchSizes": list(batches), "basicCuda": {"status": "not-run"},
              "encoderBatches": [], "headTrainingBatches": [],
              "maximumSuccessfulEncoderBatchSize": None, "maximumSuccessfulHeadTrainingBatchSize": None,
              "maximumSuccessfulCombinedBatchSize": None, "diskArtifactsUnchanged": None,
              "limitations": ["One-step clone training validates engineering only; it is not a new training run or a new evaluation.",
                              "Only fixed audited train rows are used. Historical test/Karim labels do not fit or select any parameter.",
                              "Measured sizes 1..8 do not certify capacity on longer molecules, fine-tuning, or other workloads.",
                              "CUDA success does not validate clinical toxicity, causal mechanisms, calibration, or scientific generalization."]}
    if not report["environment"]["cudaAvailable"]:
        report["reason"] = "CPU-only PyTorch wheel" if report["environment"].get("pytorchWheelKind") == "cpu" else "No usable CUDA device/driver or PyTorch dependency"
        return report
    originals = {}
    try:
        import numpy as np
        import torch
        from backend.multitask_data import load_multitask_data
        from backend.multitask_model import MultitaskModelClient
        from backend import multitask_experiments as experiments
        from backend.pretrained_encoder import EMBEDDING_DIMENSION, MODEL_REVISION, WEIGHTS_SHA256
        torch.set_num_threads(threads)
        torch.cuda.set_device(0)
        report["basicCuda"], basic = measure_cuda_phase(torch, lambda: basic_cuda_test(torch))
        if basic is None:
            raise VerificationError("Real CUDA arithmetic test failed; molecular tests were not started.")
        report["basicCuda"]["verification"] = basic
        encoder_dir, model_dir, data_dir = Path(encoder_dir).resolve(), Path(model_dir).resolve(), Path(data_dir).resolve()
        originals = artifact_files(model_dir, encoder_dir)
        data, all_samples = load_multitask_data(data_dir)  # Full existing source/label/leakage/hash audit.
        cpu_client = MultitaskModelClient(model_dir, encoder_dir=encoder_dir, device="cpu", allow_research=True)
        cpu = cpu_client._ensure_loaded()  # Strict loader; no handcrafted checkpoint shortcut.
        manifest = cpu["manifest"]
        verify_development_binding(data, manifest, data_dir, experiments.ALL_ENDPOINTS)
        train = [sample for sample in all_samples if sample["split"] == "train"]
        if len(train) < max(batches):
            raise VerificationError("Not enough real audited train molecules for every requested batch; no repetitions/substitutions allowed.")
        labels, masks = experiments.labels_and_masks(train)
        positive = experiments.positive_weights_train_only(labels, masks, np.ones(len(train), dtype=bool))
        probes = train[:max(batches)]  # Deterministic molecular order, no selection on endpoint labels.
        probe_hash = hashlib.sha256(json.dumps([row["smiles"] for row in probes], separators=(",", ":")).encode()).hexdigest()
        report["data"] = {"datasetVersion": data["datasetVersion"], "manifestSha256": digest(data_dir / "manifest.json"),
                          "splitSha256": data["splitSha256"], "sourceSplit": "train", "trainingRows": len(train),
                          "probeStructureOrderSha256": probe_hash, "probeCount": len(probes),
                          "sourceHashes": manifest["dataset"]["sourceHashes"], "trainingPermission": data["trainingPermission"],
                          "validationLabelsUsedForFitting": False, "oldHeldoutLabelsUsedForFitting": False}
        report["model"] = {"version": manifest["modelVersion"], "manifestSha256": cpu["manifestSha256"],
                           "weightsSha256": manifest["artifacts"]["weights"]["sha256"], "parameterCount": cpu["parameterCount"],
                           "tasks": manifest["tasks"], "taskContracts": manifest["taskContracts"],
                           "taskContractVersion": manifest["taskContractVersion"], "formalAvailability": False,
                           "encoderRevision": MODEL_REVISION, "encoderWeightsSha256": WEIGHTS_SHA256}
        references = {batch: cpu["encoder"].encode([row["smiles"] for row in probes[:batch]], batch_size=batch) for batch in batches}
        gpu_client = MultitaskModelClient(model_dir, encoder_dir=encoder_dir, device="cuda", allow_research=True)
        gpu = gpu_client._ensure_loaded()
        encoder = gpu["encoder"]
        encoder_before, head_before = state_digest(encoder.model), state_digest(gpu["head"])
        if any(parameter.requires_grad for parameter in encoder.model.parameters()) or encoder.model.training:
            raise VerificationError("The real pretrained encoder is not frozen/in evaluation mode.")
        embeddings = {}
        for batch in batches:
            record, features = measure_cuda_phase(torch, lambda b=batch: encoder.encode([row["smiles"] for row in probes[:b]], batch_size=b))
            record.update({"batchSize": batch, "operation": "frozen-MoLFormer-extraction", "encoderResidentOnCuda": True,
                           "cpuGpuTolerance": {"atol": atol, "rtol": rtol}})
            if features is not None:
                delta = np.abs(features - references[batch])
                agreement = bool(np.allclose(features, references[batch], atol=atol, rtol=rtol))
                record.update({"embeddingShape": list(features.shape), "finite": bool(np.isfinite(features).all()),
                               "maximumAbsoluteCpuGpuDifference": float(delta.max()), "meanAbsoluteCpuGpuDifference": float(delta.mean()),
                               "cpuGpuAgreement": agreement})
                if not agreement or features.shape != (batch, EMBEDDING_DIMENSION) or not np.isfinite(features).all():
                    record.update({"status": "failed", "error": "CPU/GPU agreement, dimension or finite-value check failed"})
                else:
                    embeddings[batch] = features
            report["encoderBatches"].append(record)
        # Train the existing frozen-feature head only. Offload the encoder so
        # training memory is independently observed, not added to encoder memory.
        encoder.model.to("cpu")
        encoder.device = "cpu"
        gc.collect()
        torch.cuda.empty_cache()
        for batch in batches:
            if batch not in embeddings:
                report["headTrainingBatches"].append({"batchSize": batch, "status": "not-run", "oom": False,
                                                      "reason": "No verified real CUDA embedding from this batch",
                                                      "peakAllocatedBytes": None, "peakReservedBytes": None, "executionSeconds": None})
                continue
            record, result = measure_cuda_phase(torch, lambda b=batch: train_head_probe(torch, experiments, gpu, embeddings[b], probes[:b], positive))
            record.update({"batchSize": batch, "operation": "real-B16-memory-clone-train-step", "encoderResidentOnCuda": False})
            if result is not None:
                record["verification"] = result
            report["headTrainingBatches"].append(record)
        frozen_ok = (state_digest(encoder.model) == encoder_before and state_digest(gpu["head"]) == head_before
                     and all(not parameter.requires_grad and parameter.grad is None for parameter in encoder.model.parameters()))
        report["frozenEncoderAndLoadedHeadUnchanged"] = frozen_ok
        if not frozen_ok:
            raise VerificationError("Frozen encoder or original loaded head changed during memory-only clone validation.")
        successful_encoder = {row["batchSize"] for row in report["encoderBatches"] if row["status"] == "passed"}
        successful_head = {row["batchSize"] for row in report["headTrainingBatches"] if row["status"] == "passed"}
        report["maximumSuccessfulEncoderBatchSize"] = max(successful_encoder, default=None)
        report["maximumSuccessfulHeadTrainingBatchSize"] = max(successful_head, default=None)
        report["maximumSuccessfulCombinedBatchSize"] = max(successful_encoder & successful_head, default=None)
        report["status"] = "passed" if successful_encoder == successful_head == set(batches) else "failed-or-partial"
    except Exception as error:
        report.update({"status": "unavailable" if isinstance(error, ImportError) else "failed",
                       "errorType": type(error).__name__, "error": str(error)})
    finally:
        if originals:
            changes = [path for path, checksum in originals.items() if not path.is_file() or digest(path) != checksum]
            report["protectedFiles"] = [{"file": _label(path), "beforeSha256": checksum,
                                          "afterSha256": digest(path) if path.is_file() else None} for path, checksum in originals.items()]
            report["diskArtifactsUnchanged"] = not changes
            if changes:
                report.update({"status": "failed", "error": "Model/encoder/formal-v2 artifact bytes changed during verification."})
    return report


def save_report(output, report, *, encoder_dir, model_dir, data_dir):
    output = Path(output).resolve()
    if output.exists():
        raise VerificationError("Output exists; choose a new path and preserve prior verification history.")
    blocked = [ROOT / "models", ROOT / "data", Path(encoder_dir), Path(model_dir), Path(data_dir)]
    if any(output.is_relative_to(path.resolve()) for path in blocked):
        raise VerificationError("Verification output must be separate from model and data artifacts.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--encoder-dir", type=Path, default=ROOT / "models/pretrained/molformer")
    parser.add_argument("--model-dir", type=Path, default=ROOT / "artifacts/v3-multitask/baselines-20261013/B16-seed-42")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/v3-multitask-audited")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=list(DEFAULT_BATCHES))
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--cpu-gpu-atol", type=float, default=.001)
    parser.add_argument("--cpu-gpu-rtol", type=float, default=.001)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output exists; preserve history and choose a new path.")
    try:
        report = run_verification(encoder_dir=args.encoder_dir, model_dir=args.model_dir, data_dir=args.data_dir,
                                  batch_sizes=args.batch_sizes, threads=args.threads, atol=args.cpu_gpu_atol, rtol=args.cpu_gpu_rtol)
        save_report(args.output, report, encoder_dir=args.encoder_dir, model_dir=args.model_dir, data_dir=args.data_dir)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps({"status": report["status"], "cudaAvailable": report["environment"]["cudaAvailable"],
                      "maximumSuccessfulCombinedBatchSize": report["maximumSuccessfulCombinedBatchSize"],
                      "output": str(args.output)}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 2 if report["status"] in ("not-tested-no-cuda-device", "unavailable") else 1


if __name__ == "__main__":
    raise SystemExit(main())
