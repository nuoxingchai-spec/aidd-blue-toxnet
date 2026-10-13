"""Disposable local-only TxGemma process, invoked by the Compare adapter.

stdin contains one validated request. stdout is one small JSON object. A killed
worker releases its own model/GPU allocations; it never downloads anything.
"""
from __future__ import annotations

import contextlib
import io
import json
import importlib.metadata
import os
import sys
import time

from backend.compare_tasks import TXGEMMA_REVISION, get_task, parse_task_output
from backend.compare_txgemma import (REVIEWED_TRANSFORMERS_VERSION,
                                    check_snapshot_identity, verify_snapshot_receipt)
from backend.chemistry import canonicalize_smiles


class WorkerFailure(Exception):
    def __init__(self, code):
        self.code = code


def validate_request(request):
    required = {"snapshotDir", "receiptPath", "revision", "device", "dtype", "taskId", "canonicalSmiles"}
    if not isinstance(request, dict) or set(request) != required:
        raise WorkerFailure("TXGEMMA_INVALID_OUTPUT")
    if request["revision"] != TXGEMMA_REVISION:
        raise WorkerFailure("TXGEMMA_AUDIT_FAILED")
    task = get_task(request["taskId"])
    if not task.supported:
        raise WorkerFailure("TXGEMMA_INVALID_OUTPUT")
    if request["device"] not in ("cpu", "cuda") or request["dtype"] not in ("float32", "float16", "bfloat16"):
        raise WorkerFailure("TXGEMMA_LOAD_FAILED")
    if request["device"] == "cpu" and request["dtype"] != "float32":
        raise WorkerFailure("TXGEMMA_LOAD_FAILED")
    smiles = request["canonicalSmiles"]
    # Do not strip salts, isotopes, or stereochemistry. The orchestrator's RDKit
    # canonical identity must remain exactly the model input identity.
    if canonicalize_smiles(smiles) != smiles:
        raise WorkerFailure("TXGEMMA_INVALID_OUTPUT")
    return task


def execute_request(request):
    task = validate_request(request)
    from pathlib import Path
    root = Path(request["snapshotDir"]).resolve()
    verify_snapshot_receipt(root, request["receiptPath"])
    template = check_snapshot_identity(root)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    if importlib.metadata.version("transformers") != REVIEWED_TRANSFORMERS_VERSION:
        raise WorkerFailure("TXGEMMA_LOAD_FAILED")
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if request["device"] == "cuda" and not torch.cuda.is_available():
        raise WorkerFailure("TXGEMMA_CUDA_UNAVAILABLE")
    if request["device"] == "cuda" and request["dtype"] == "bfloat16" and not torch.cuda.is_bf16_supported():
        raise WorkerFailure("TXGEMMA_LOAD_FAILED")
    started = time.perf_counter()
    model = tokenizer = None
    try:
        if request["device"] == "cuda":
            torch.cuda.reset_peak_memory_stats()
        tokenizer = AutoTokenizer.from_pretrained(str(root), local_files_only=True, trust_remote_code=False)
        model = AutoModelForCausalLM.from_pretrained(
            str(root), local_files_only=True, trust_remote_code=False, use_safetensors=True,
            torch_dtype=getattr(torch, request["dtype"]))
        model.to(request["device"]).eval()
        prompt = template.replace("{Drug SMILES}", request["canonicalSmiles"])
        inputs = tokenizer(prompt, return_tensors="pt", truncation=False, padding=False,
                           return_attention_mask=True)
        token_count = int(inputs["input_ids"].shape[1])
        if token_count + 8 > min(2048, int(model.config.max_position_embeddings)):
            raise WorkerFailure("TXGEMMA_INPUT_TOO_LONG")
        if tokenizer.unk_token_id is not None and bool((inputs["input_ids"] == tokenizer.unk_token_id).any()):
            raise WorkerFailure("TXGEMMA_INVALID_OUTPUT")
        inputs = {key: value.to(request["device"]) for key, value in inputs.items()}
        loaded = time.perf_counter()
        if request["device"] == "cuda":
            torch.cuda.synchronize()
        generated_at = time.perf_counter()
        with torch.inference_mode():
            output = model.generate(**inputs, max_new_tokens=8, do_sample=False,
                                    pad_token_id=tokenizer.pad_token_id,
                                    eos_token_id=tokenizer.eos_token_id)
        if request["device"] == "cuda":
            torch.cuda.synchronize()
        generation_seconds = time.perf_counter() - generated_at
        output_ids = output[0, token_count:]
        answer = tokenizer.decode(output_ids, skip_special_tokens=True)
        parse_task_output(task.id, answer)
        output_tokens = int(output_ids.shape[0])
        gpu = None
        if request["device"] == "cuda":
            gpu = {"name": torch.cuda.get_device_name(),
                   "peakAllocatedMiB": torch.cuda.max_memory_allocated() / 1048576,
                   "peakReservedMiB": torch.cuda.max_memory_reserved() / 1048576,
                   "measurementScope": "this-disposable-worker-PyTorch-only"}
        return {"status": "completed", "answer": answer, "revision": TXGEMMA_REVISION,
                "metrics": {"loadSeconds": loaded - started, "generationSeconds": generation_seconds,
                            "inputTokens": token_count, "outputTokens": output_tokens,
                            "tokensPerSecond": output_tokens / generation_seconds if generation_seconds > 0 else None,
                            "device": request["device"], "dtype": request["dtype"], "gpu": gpu,
                            "cost": None, "costStatus": "local-no-provider-billing"}}
    finally:
        del model, tokenizer
        if request["device"] == "cuda" and torch.cuda.is_available():
            torch.cuda.empty_cache()


def main():
    try:
        raw = sys.stdin.buffer.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise WorkerFailure("TXGEMMA_INVALID_OUTPUT")
        request = json.loads(raw)
        # Third-party progress messages never become model outputs or expose
        # local paths. The parent also suppresses stderr.
        with contextlib.redirect_stdout(io.StringIO()):
            result = execute_request(request)
    except WorkerFailure as exc:
        result = {"status": "failed", "code": exc.code}
    except Exception as exc:
        if type(exc).__name__ in ("OutOfMemoryError", "CUDAOutOfMemoryError") or "out of memory" in str(exc).lower():
            code = "TXGEMMA_OOM"
        elif isinstance(exc, ValueError):
            code = "TXGEMMA_AUDIT_FAILED"
        else:
            code = "TXGEMMA_LOAD_FAILED"
        result = {"status": "failed", "code": code}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
