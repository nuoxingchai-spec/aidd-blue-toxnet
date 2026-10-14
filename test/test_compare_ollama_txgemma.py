"""Tiny invented checksum fixtures and Mock HTTP; no Google weight inference."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from backend.chemistry import canonicalize_smiles
from backend.compare_adapters import CompareAdapterError
from backend.compare_ollama_txgemma import (GGUF_REVISION, PARAMETER_COUNT,
                                          OllamaTxGemmaAdapter,
                                          validate_gguf_provenance)
from backend.compare_reporting import validate_specialist
from backend.compare_resources import CompareResourceManager
from backend.compare_tasks import BBB_TEMPLATE, TXGEMMA_REVISION, parse_task_output

MODEL_ID = "txgemma-2b:latest"


def checksum(raw):
    return hashlib.sha256(raw).hexdigest()


def context(smiles="CCO", task="tdc-bbb-martins"):
    canonical = canonicalize_smiles(smiles)
    return {"canonicalSmiles": canonical, "structureId": checksum(canonical.encode()),
            "request": {"specialistTask": task}}


class FakeOllama:
    def __init__(self, digest):
        self.digest, self.calls = digest, []
        self.tags_count = 0
        self.installed, self.active, self.unload_ok = True, False, True
        self.change_digest_after_generation = False
        self.generated = False
        self.generation_hook = None
        self.generation_gate = None
        self.unload_gate = None
        self.output = {
            "model": MODEL_ID, "response": "B", "done": True, "done_reason": "stop",
            "prompt_eval_count": 138, "eval_count": 2,
            "load_duration": 1_000_000_000, "eval_duration": 100_000_000,
            "total_duration": 1_500_000_000,
        }
        self.shown = {"details": {"format": "gguf", "family": "gemma2",
                                 "quantization_level": "Q4_K_M"},
                      "model_info": {"general.architecture": "gemma2",
                                     "general.finetune": "predict",
                                     "general.parameter_count": PARAMETER_COUNT,
                                     "gemma2.context_length": 8192,
                                     "tokenizer.ggml.bos_token_id": 2,
                                     "tokenizer.ggml.eos_token_id": 1,
                                     "tokenizer.ggml.add_bos_token": True,
                                     "tokenizer.ggml.add_eos_token": False}}

    async def handler(self, request):
        payload = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, payload))
        if request.url.path == "/api/tags":
            self.tags_count += 1
            digest = "0" * 64 if self.change_digest_after_generation and self.generated else self.digest
            return httpx.Response(200, json={"models": [{"name": MODEL_ID, "digest": digest}] if self.installed else []})
        if request.url.path == "/api/show":
            return httpx.Response(200, json=self.shown)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "alias", "digest": self.digest}] if self.active else []})
        if request.url.path == "/api/generate":
            if "prompt" not in payload:
                if self.unload_gate is not None:
                    await self.unload_gate.wait()
                if self.unload_ok:
                    self.active = False
                return httpx.Response(200, json={"model": MODEL_ID, "done": self.unload_ok})
            self.active = True
            if self.generation_gate is not None:
                await self.generation_gate.wait()
            self.generated = True
            if self.generation_hook:
                return self.generation_hook(request)
            return httpx.Response(200, json=self.output)
        raise AssertionError("Unexpected HTTP path; no pulling or chat calls permitted.")


class OllamaTxGemmaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.model_bytes = b"GGUF-test-only-not-a-real-TxGemma-model"
        self.model_hash = checksum(self.model_bytes)
        self.patches = [
            patch("backend.compare_ollama_txgemma.GGUF_SHA256", self.model_hash),
            patch("backend.compare_ollama_txgemma.GGUF_SIZE", len(self.model_bytes)),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        config = json.dumps({"model_format": "gguf", "model_family": "gemma2",
                             "file_type": "Q4_K_M"}).encode()
        self.config_hash = checksum(config)
        blobs = self.root / "blobs"
        blobs.mkdir()
        self.model_path = blobs / ("sha256-" + self.model_hash)
        self.model_path.write_bytes(self.model_bytes)
        self.config_path = blobs / ("sha256-" + self.config_hash)
        self.config_path.write_bytes(config)
        self.manifest_data = {
            "schemaVersion": 2,
            "config": {"digest": "sha256:" + self.config_hash, "size": len(config),
                       "mediaType": "application/vnd.docker.container.image.v1+json"},
            "layers": [{"digest": "sha256:" + self.model_hash, "size": len(self.model_bytes),
                        "mediaType": "application/vnd.ollama.image.model"}],
        }
        self.manifest_path = self.root / "manifests-v2" / "ollama.com" / "library" / "txgemma-2b" / "latest"
        self.manifest_path.parent.mkdir(parents=True)
        self.manifest_path.write_bytes(json.dumps(self.manifest_data).encode())
        self.manifest_hash = checksum(self.manifest_path.read_bytes())
        self.fake = FakeOllama(self.manifest_hash)
        self.env = {
            "AIDD_TXGEMMA_ENABLE": "true", "AIDD_TXGEMMA_LICENSE_ACCEPTED": "true",
            "AIDD_TXGEMMA_OLLAMA_MODEL": MODEL_ID, "AIDD_TXGEMMA_OLLAMA_DIGEST": self.manifest_hash,
            "OLLAMA_MODELS": str(self.root),
        }

    def adapter(self, changes=None):
        return OllamaTxGemmaAdapter({**self.env, **(changes or {})},
                                   transport=httpx.MockTransport(self.fake.handler))

    async def assert_code(self, code, adapter=None, ctx=None):
        with self.assertRaises(CompareAdapterError) as caught:
            await (adapter or self.adapter()).execute(ctx or context())
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    async def test_disabled_and_license_gates_do_not_call_or_start_ollama(self):
        for change, code in [({"AIDD_TXGEMMA_ENABLE": "false"}, "TXGEMMA_DISABLED"),
                             ({"AIDD_TXGEMMA_LICENSE_ACCEPTED": "false"}, "TXGEMMA_LICENSE_REQUIRED")]:
            adapter = self.adapter(change)
            self.assertEqual((await adapter.status())["errorCode"], code)
            await self.assert_code(code, adapter)
        self.assertEqual(self.fake.calls, [])

    async def test_configuration_requires_exact_id_digest_and_finite_timeout(self):
        for change in [{"AIDD_TXGEMMA_OLLAMA_DIGEST": ""},
                       {"AIDD_TXGEMMA_OLLAMA_MODEL": "../secret:latest"},
                       {"AIDD_TXGEMMA_TIMEOUT_SECONDS": "NaN"},
                       {"AIDD_TXGEMMA_TIMEOUT_SECONDS": "0"},
                       {"AIDD_TXGEMMA_TIMEOUT_SECONDS": "601"}]:
            self.assertEqual((await self.adapter(change).status())["errorCode"], "TXGEMMA_INVALID_CONFIG")
        self.assertEqual(self.fake.calls, [])

    async def test_only_loopback_no_remote_addresses_or_api_path(self):
        for url in ["https://127.0.0.1:11434", "http://evil.test:11434",
                    "http://user:secret@127.0.0.1:11434", "http://127.0.0.1:11434/anything"]:
            adapter = self.adapter({"TOXNET_OLLAMA_URL": url})
            self.assertEqual((await adapter.status())["errorCode"], "OLLAMA_LOOPBACK_REQUIRED")
            await self.assert_code("OLLAMA_LOOPBACK_REQUIRED", adapter)
        self.assertEqual(self.fake.calls, [])

    async def test_v2_status_hashes_local_source_and_metadata_without_generation(self):
        status = await self.adapter().status()
        self.assertTrue(status["available"])
        self.assertFalse(status["formalAvailability"])
        self.assertEqual(status["runtime"], "ollama-gguf")
        self.assertEqual(status["sourceKind"], "third-party-quantized")
        self.assertEqual(status["sourceRevision"], GGUF_REVISION)
        self.assertIsNone(status["upstreamRevision"])
        self.assertFalse(status["capacityVerified"])
        self.assertEqual([call[1] for call in self.fake.calls], ["/api/tags", "/api/show", "/api/tags"])
        self.assertNotIn(str(self.root), json.dumps(status))

    async def test_legacy_manifest_layout_is_supported_with_same_source_checks(self):
        legacy = self.root / "manifests" / "registry.ollama.ai" / "library" / "txgemma-2b" / "latest"
        legacy.parent.mkdir(parents=True)
        self.manifest_path.rename(legacy)
        self.assertTrue((await self.adapter().status())["available"])

    async def test_two_manifest_layouts_are_ambiguous_even_if_bytes_match(self):
        legacy = self.root / "manifests" / "registry.ollama.ai" / "library" / "txgemma-2b" / "latest"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(self.manifest_path.read_bytes())
        self.assertEqual((await self.adapter().status())["errorCode"], "TXGEMMA_GGUF_AUDIT_FAILED")
        self.assertFalse(self.fake.generated)

    async def test_missing_store_and_missing_model_return_unavailable(self):
        self.manifest_path.unlink()
        self.assertEqual((await self.adapter().status())["errorCode"], "TXGEMMA_GGUF_AUDIT_FAILED")
        self.fake.installed = False
        self.assertEqual((await self.adapter().status())["errorCode"], "TXGEMMA_NOT_INSTALLED")
        self.assertFalse(self.fake.generated)

    async def test_service_digest_mismatch_refuses_generation(self):
        self.fake.digest = "a" * 64
        await self.assert_code("TXGEMMA_MODEL_CHANGED")
        self.assertFalse(self.fake.generated)

    async def test_local_manifest_digest_mismatch_not_accepted_from_tags(self):
        self.manifest_path.write_bytes(self.manifest_path.read_bytes() + b" ")
        await self.assert_code("TXGEMMA_GGUF_AUDIT_FAILED")
        self.assertFalse(self.fake.generated)

    async def test_wrong_size_or_source_checksum_refuses_prediction(self):
        self.model_path.write_bytes(b"x" * len(self.model_bytes))
        await self.assert_code("TXGEMMA_GGUF_AUDIT_FAILED")
        self.assertFalse(self.fake.generated)

    async def test_additional_hashed_lora_adapter_or_projector_layers_are_refused(self):
        adapter_bytes = b"not-a-real-adapter-but-valid-local-checksum"
        adapter_sha = checksum(adapter_bytes)
        (self.root / "blobs" / ("sha256-" + adapter_sha)).write_bytes(adapter_bytes)
        for media_type in ["application/vnd.ollama.image.adapter",
                           "application/vnd.ollama.image.projector",
                           "application/vnd.ollama.image.lora",
                           "application/vnd.ollama.image.system",
                           "unknown-type"]:
            document = deepcopy(self.manifest_data)
            document["layers"].append({
                "digest": "sha256:" + adapter_sha, "size": len(adapter_bytes),
                "mediaType": media_type})
            raw = json.dumps(document).encode()
            self.manifest_path.write_bytes(raw)
            self.fake.digest = checksum(raw)
            adapter = self.adapter({"AIDD_TXGEMMA_OLLAMA_DIGEST": checksum(raw)})
            await self.assert_code("TXGEMMA_GGUF_AUDIT_FAILED", adapter)
        self.assertFalse(self.fake.generated)

    async def test_unsupported_identity_architecture_finetune_count_or_quantization(self):
        for container, field, value in [
            ("details", "family", "gemma"), ("details", "format", "safetensors"),
            ("details", "quantization_level", "Q8_0"),
            ("model_info", "general.architecture", "llama"),
            ("model_info", "general.finetune", "chat"),
            ("model_info", "general.parameter_count", 1000),
            ("model_info", "gemma2.context_length", 2048),
            ("model_info", "tokenizer.ggml.bos_token_id", 1),
            ("model_info", "tokenizer.ggml.eos_token_id", 2),
            ("model_info", "tokenizer.ggml.add_bos_token", False),
            ("model_info", "tokenizer.ggml.add_eos_token", True),
            ("model_info", "tokenizer.ggml.add_bos_token", 1),
            ("model_info", "tokenizer.ggml.add_eos_token", 0),
        ]:
            original = self.fake.shown[container][field]
            self.fake.shown[container][field] = value
            await self.assert_code("TXGEMMA_GGUF_IDENTITY_FAILED")
            self.fake.shown[container][field] = original
        self.assertFalse(self.fake.generated)

    async def test_absent_tokenizer_metadata_is_not_assumed_from_model_name(self):
        self.fake.shown["model_info"].pop("tokenizer.ggml.bos_token_id")
        await self.assert_code("TXGEMMA_GGUF_IDENTITY_FAILED")
        self.assertFalse(self.fake.generated)

    async def test_raw_official_prompt_and_real_metrics_categorical_result(self):
        adapter = self.adapter()
        result = await adapter.execute(context())
        request = next(call[2] for call in self.fake.calls if "prompt" in call[2])
        self.assertEqual(request["prompt"], BBB_TEMPLATE.replace("{Drug SMILES}", "CCO"))
        self.assertTrue(request["raw"])
        self.assertFalse(request["stream"])
        self.assertEqual(request["options"]["num_predict"], 8)
        self.assertEqual(request["options"]["stop"], [])
        self.assertNotIn("format", request)
        self.assertNotIn("messages", request)
        self.assertNotIn("system", request)
        self.assertEqual(result["data"]["tasks"][0]["class"], "B")
        self.assertIsNone(result["data"]["tasks"][0]["probability"])
        self.assertFalse(result["data"]["tasks"][0]["directComparable"])
        self.assertTrue(validate_gguf_provenance(result["provenance"]))
        self.assertEqual(result["metrics"]["promptTokens"], 138)
        self.assertEqual(result["metrics"]["completionTokens"], 2)
        self.assertEqual(result["metrics"]["tokensPerSecond"], 20)
        self.assertIsNone(result["metrics"]["cost"])
        self.assertFalse(result["metrics"]["gpu"]["measured"])
        self.assertTrue(result["metrics"]["resourceCleanupVerified"])
        status = await adapter.status()
        self.assertTrue(status["realExecutionVerified"])
        self.assertEqual(status["lastSuccessfulExecution"]["class"], "B")
        self.assertEqual(status["lastSuccessfulExecution"]["completionTokens"], 2)
        self.assertNotIn("尚需实际任务验收", status["reason"])
        self.assertFalse(self.fake.active)
        self.assertNotIn(str(self.root), json.dumps(result))
        validate_specialist(result["data"], context(), "tdc-bbb-martins", result["provenance"])

    async def test_salt_stereo_and_isotopes_preserved_in_official_prompt(self):
        for smiles in ["C[C@H](O)Cl", "[13CH3]CO", "CCO.[Na+]"]:
            self.fake.calls.clear()
            canonical = canonicalize_smiles(smiles)
            await self.adapter().execute(context(smiles))
            payload = next(call[2] for call in self.fake.calls if "prompt" in call[2])
            self.assertEqual(payload["prompt"], BBB_TEMPLATE.replace("{Drug SMILES}", canonical))

    async def test_illegal_smiles_noncanonical_or_structure_hash_mismatch_no_http(self):
        for value in [
            {**context(), "canonicalSmiles": "not-SMILES"},
            {**context(), "canonicalSmiles": "OCC"},
            {**context(), "structureId": "0" * 64},
        ]:
            await self.assert_code("TXGEMMA_INVALID_INPUT", ctx=value)
        self.assertEqual(self.fake.calls, [])

    async def test_unsupported_task_rejected_before_service_or_model_access(self):
        for task in ["tdc-herg", "unknown", None]:
            await self.assert_code("TXGEMMA_TASK_UNSUPPORTED", ctx=context(task=task))
        self.assertEqual(self.fake.calls, [])

    async def test_no_truncation_for_conservatively_long_prompt(self):
        with patch("backend.compare_ollama_txgemma.CONTEXT_LENGTH", 64):
            await self.assert_code("TXGEMMA_INPUT_TOO_LONG")
        self.assertEqual(self.fake.calls, [])

    async def test_invalid_or_probability_output_preserves_tokens_and_unloads(self):
        for output in ["0.9", "B because...", "A crosses the BBB", "", "B\nA"]:
            self.fake.output["response"] = output
            error = await self.assert_code("TXGEMMA_INVALID_OUTPUT")
            self.assertEqual(error.metrics["completionTokens"], 2)
            self.assertEqual(error.metrics["usageStatus"], "provider-reported")
            self.assertTrue(error.metrics["resourceCleanupVerified"])
            self.assertFalse(self.fake.active)

    async def test_truncated_or_unfinished_output_is_not_a_valid_class(self):
        for field, value in [("done", False), ("done_reason", "length"), ("done_reason", None),
                             ("model", "another-model"), ("eval_count", 9)]:
            original = self.fake.output[field]
            self.fake.output[field] = value
            code = "TXGEMMA_MODEL_CHANGED" if field == "model" else "TXGEMMA_INVALID_OUTPUT"
            await self.assert_code(code)
            self.fake.output[field] = original

    async def test_invalid_usage_keeps_other_valid_fields_without_zero_cost(self):
        self.fake.output["eval_count"] = -1
        error = await self.assert_code("COMPARE_INVALID_USAGE")
        self.assertEqual(error.metrics["promptTokens"], 138)
        self.assertIsNone(error.metrics["completionTokens"])
        self.assertIsNone(error.metrics["cost"])

    async def test_missing_usage_is_not_invented_but_category_remains_valid(self):
        self.fake.output.pop("eval_count")
        self.fake.output.pop("prompt_eval_count")
        result = await self.adapter().execute(context())
        self.assertIsNone(result["metrics"]["completionTokens"])
        self.assertIsNone(result["metrics"]["promptTokens"])
        self.assertIsNone(result["metrics"]["totalTokens"])
        self.assertIsNone(result["metrics"]["tokensPerSecond"])
        self.assertEqual(result["metrics"]["usageStatus"], "not-returned")

    async def test_digest_change_during_generation_rejects_class_and_unloads(self):
        self.fake.change_digest_after_generation = True
        error = await self.assert_code("TXGEMMA_MODEL_CHANGED")
        self.assertEqual(error.metrics["completionTokens"], 2)
        self.assertTrue(error.metrics["resourceCleanupVerified"])

    async def test_blob_change_during_generation_is_detected_by_post_hash(self):
        def change_blob(request):
            self.model_path.write_bytes(b"x" * len(self.model_bytes))
            return httpx.Response(200, json=self.fake.output)
        self.fake.generation_hook = change_blob
        await self.assert_code("TXGEMMA_GGUF_AUDIT_FAILED")
        self.assertFalse(self.fake.active)

    async def test_status_cache_invalidates_changed_same_size_blob(self):
        adapter = self.adapter()
        self.assertTrue((await adapter.status())["available"])
        self.model_path.write_bytes(b"x" * len(self.model_bytes))
        self.assertFalse((await adapter.status())["available"])
        self.assertFalse(self.fake.generated)

    async def test_status_cache_but_each_execution_rehashes_before_and_after(self):
        import backend.compare_ollama_txgemma as module
        original = module._hash_file
        calls = []
        def counted(path):
            calls.append(path)
            return original(path)
        with patch.object(module, "_hash_file", side_effect=counted):
            adapter = self.adapter()
            await adapter.status()
            first = len(calls)
            await adapter.status()
            self.assertEqual(len(calls), first)
            await adapter.execute(context())
            self.assertEqual(len(calls), first + 4)  # Config+GGUF, pre and post.

    async def test_same_mtime_restoration_still_rehashes_before_generation(self):
        adapter = self.adapter()
        self.assertTrue((await adapter.status())["available"])
        original_stat = self.model_path.stat()
        self.model_path.write_bytes(b"x" * len(self.model_bytes))
        os.utime(self.model_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        await self.assert_code("TXGEMMA_GGUF_AUDIT_FAILED", adapter)
        self.assertFalse(self.fake.generated)

    async def test_timeout_and_rate_limit_have_real_error_metrics_and_cleanup(self):
        def timeout(request):
            raise httpx.ReadTimeout("not-exported-raw-private-error", request=request)
        self.fake.generation_hook = timeout
        error = await self.assert_code("COMPARE_TIMEOUT")
        self.assertEqual(error.metrics["timeoutPhase"], "read")
        self.assertTrue(error.metrics["resourceCleanupVerified"])
        self.fake.generation_hook = lambda request: httpx.Response(429, json={"eval_count": 3})
        error = await self.assert_code("COMPARE_HTTP_RATE_LIMITED")
        self.assertEqual(error.metrics["completionTokens"], 3)
        self.assertEqual(error.metrics["httpStatusCode"], 429)
        self.assertIsNone(error.metrics["cost"])

    async def test_repeated_cancel_keeps_slot_until_tracked_unload_finishes(self):
        adapter, manager = self.adapter(), CompareResourceManager()
        self.fake.generation_gate = asyncio.Event()
        self.fake.unload_gate = asyncio.Event()
        async def run():
            async with manager.slot(adapter.id, adapter):
                await adapter.execute(context())
        running = asyncio.create_task(run())
        for _ in range(500):
            if self.fake.active:
                break
            await asyncio.sleep(0.002)
        self.assertTrue(self.fake.active)
        running.cancel()
        for _ in range(200):
            if adapter.cleanup_status()["pending"]:
                break
            await asyncio.sleep(0.002)
        running.cancel()
        await asyncio.sleep(0)
        self.assertTrue(manager.lock.locked())
        self.assertFalse(running.done())
        self.fake.unload_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertTrue(adapter.cleanup_safe)
        self.assertFalse(manager.lock.locked())
        unloads = [call for call in self.fake.calls if call[1] == "/api/generate" and "prompt" not in call[2]]
        self.assertEqual(len(unloads), 1)
        await adapter.close()

    async def test_unconfirmed_unload_quarantines_even_for_an_alias_digest(self):
        self.fake.unload_ok = False
        adapter, manager = self.adapter(), CompareResourceManager()
        async with manager.slot(adapter.id, adapter):
            result = await adapter.execute(context())
        self.assertFalse(result["metrics"]["resourceCleanupVerified"])
        self.assertFalse(adapter.cleanup_safe)
        self.assertEqual(manager.blocked_reason, "LOCAL_MODEL_CLEANUP_UNCONFIRMED")
        calls = len(self.fake.calls)
        await self.assert_code("COMPARE_RESOURCE_QUARANTINED", adapter)
        self.assertEqual((await adapter.status())["errorCode"], "COMPARE_RESOURCE_QUARANTINED")
        self.assertEqual(len(self.fake.calls), calls)

    async def test_service_returned_filesystem_path_is_never_read(self):
        self.fake.shown["modelfile"] = "FROM C:\\private\\untrusted.gguf"
        self.fake.shown["model_path"] = "C:\\private\\another.gguf"
        self.assertTrue((await self.adapter().status())["available"])
        self.assertNotIn("private", json.dumps(await self.adapter().status()))

    async def test_symlink_escaping_server_configured_root_is_refused(self):
        outside = self.root.parent / (self.root.name + "-outside")
        outside.write_bytes(self.model_bytes)
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        self.model_path.unlink()
        try:
            self.model_path.symlink_to(outside)
        except OSError:
            self.skipTest("Windows environment does not grant symlink creation.")
        await self.assert_code("TXGEMMA_GGUF_AUDIT_FAILED")
        self.assertFalse(self.fake.generated)

    async def test_runtime_specific_contract_cannot_claim_official_upstream_revision(self):
        result = await self.adapter().execute(context())
        altered = deepcopy(result["provenance"])
        altered["upstreamRevision"] = TXGEMMA_REVISION
        self.assertFalse(validate_gguf_provenance(altered))
        with self.assertRaises(ValueError):
            validate_specialist(result["data"], context(), "tdc-bbb-martins", altered)
        altered = deepcopy(result["provenance"])
        altered["ggufSha256"] = "0" * 64
        with self.assertRaises(ValueError):
            validate_specialist(result["data"], context(), "tdc-bbb-martins", altered)
        altered = deepcopy(result["provenance"])
        altered["localPackageVerified"] = False
        with self.assertRaises(ValueError):
            validate_specialist(result["data"], context(), "tdc-bbb-martins", altered)

    async def test_original_transformers_revision_gate_is_unchanged(self):
        expected = parse_task_output("tdc-bbb-martins", "A")
        data = {"structureId": context()["structureId"], "tasks": [expected]}
        self.assertEqual(validate_specialist(data, context(), "tdc-bbb-martins",
                         {"revision": TXGEMMA_REVISION, "formalAvailability": False}), data)
        with self.assertRaises(ValueError):
            validate_specialist(data, context(), "tdc-bbb-martins",
                                {"revision": GGUF_REVISION, "formalAvailability": False})
        for runtime in ["unknown-runtime", "external-api", "ollama", "ollama-gguf-unreviewed"]:
            with self.assertRaises(ValueError):
                validate_specialist(data, context(), "tdc-bbb-martins",
                                    {"revision": TXGEMMA_REVISION, "runtime": runtime,
                                     "formalAvailability": False})
        self.assertEqual(validate_specialist(data, context(), "tdc-bbb-martins",
                         {"revision": TXGEMMA_REVISION, "runtime": "local-transformers",
                          "formalAvailability": False}), data)


if __name__ == "__main__":
    unittest.main()
