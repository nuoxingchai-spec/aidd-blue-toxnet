import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from backend.compare_tasks import (BBB_TEMPLATE, TXGEMMA_REVISION, get_task,
                                  parse_task_output, task_registry)
from backend.compare_txgemma import (TxGemmaAdapterError, TxGemmaPredictAdapter,
                                    file_sha256, git_blob_oid, lfs_pointer_oid,
                                    verify_snapshot_receipt)
from backend.txgemma_worker import WorkerFailure, validate_request
from scripts.audit_txgemma_snapshot import audit_snapshot
from backend.chemistry import canonicalize_smiles


def fixture_snapshot(root):
    """Tiny self-contained checksum fixture; NEVER presented as Google weights."""
    data = {
        "config.json": json.dumps({"model_type": "gemma2", "architectures": ["Gemma2ForCausalLM"]}).encode(),
        "tokenizer_config.json": json.dumps({"tokenizer_class": "GemmaTokenizerFast"}).encode(),
        "tdc_prompts.json": json.dumps({"BBB_Martins": BBB_TEMPLATE}).encode(),
        "model.safetensors.index.json": json.dumps({"weight_map": {"test.weight": "fixture.safetensors"}}).encode(),
        "fixture.safetensors": b"unit test checksum data, not an executable model",
    }
    records = {}
    for name, value in data.items():
        (root / name).write_bytes(value)
        is_lfs = name.endswith(".safetensors")
        digest = hashlib.sha256(value).hexdigest()
        records[name] = (len(value), lfs_pointer_oid(digest, len(value)) if is_lfs else git_blob_oid(value), is_lfs)
    return records


class TaskContractTests(unittest.TestCase):
    def test_only_reviewed_bbb_is_supported_and_no_task_directly_comparable(self):
        tasks = task_registry()
        self.assertEqual([x["id"] for x in tasks if x["supported"]], ["tdc-bbb-martins"])
        self.assertTrue(all(x["directComparable"] is False for x in tasks))
        self.assertEqual(get_task("tdc-bbb-martins").template_sha256,
                         "0b8139032fee96df468b8cab46fd3ca09d9d957558b528be766be41002d93edb")

    def test_categorical_parser_never_turns_letter_into_probability(self):
        for answer, positive in (("A", False), ("B", True), ("(B) crosses the BBB", True)):
            output = parse_task_output("tdc-bbb-martins", answer)
            self.assertEqual(output["positive"], positive)
            self.assertIsNone(output["probability"])
            self.assertEqual(output["outputType"], "categorical-text")

    def test_free_text_probabilities_and_contradictory_option_rejected(self):
        for output in ("0.9", "90%", "B because I think it is toxic", "A crosses the BBB", "C", "B\nA", "(B", "B)"):
            with self.assertRaises(ValueError):
                parse_task_output("tdc-bbb-martins", output)
        with self.assertRaises(ValueError):
            parse_task_output("tdc-herg", "A")
        with self.assertRaises(ValueError):
            get_task("__import__('os')")

    def test_worker_preserves_salts_stereochemistry_and_isotopes(self):
        base = {"snapshotDir": "not-loaded", "receiptPath": "not-loaded", "revision": TXGEMMA_REVISION,
                "device": "cpu", "dtype": "float32", "taskId": "tdc-bbb-martins"}
        for value in ("C[C@H](O)C(=O)O", "[13CH3]CO", "C[NH3+].[Cl-]"):
            canonical = canonicalize_smiles(value)
            request = {**base, "canonicalSmiles": canonical}
            self.assertEqual(validate_request(request).name, "BBB_Martins")
            self.assertEqual(request["canonicalSmiles"], canonical)
        for value in ("not a molecule", "OCC"):
            with self.assertRaises((ValueError, WorkerFailure)):
                validate_request({**base, "canonicalSmiles": value})
        with self.assertRaises(WorkerFailure):
            validate_request({**base, "canonicalSmiles": "CCO", "remoteCode": True})


class SnapshotAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.root = self.directory / "snapshot"
        self.root.mkdir()
        self.receipt = self.directory / "receipt.json"
        self.official = fixture_snapshot(self.root)
        self.patches = [patch("backend.compare_txgemma.OFFICIAL_FILES", self.official),
                        patch("scripts.audit_txgemma_snapshot.OFFICIAL_FILES", self.official)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def audit(self):
        return audit_snapshot(self.root, self.receipt, license_accepted=True)

    def test_offline_complete_audit_reconstructs_pointer_and_exclusive_output(self):
        result = self.audit()
        self.assertEqual(result["status"], "audited")
        self.assertFalse(result["formalAvailability"])
        self.assertEqual(result["capacityValidation"], "not-measured")
        self.assertEqual(verify_snapshot_receipt(self.root, self.receipt)["revision"], TXGEMMA_REVISION)
        with self.assertRaisesRegex(ValueError, "overwritten"):
            self.audit()

    def test_license_revision_and_missing_snapshot_refuse(self):
        with self.assertRaisesRegex(ValueError, "personally accept"):
            audit_snapshot(self.root, self.receipt, license_accepted=False)
        with self.assertRaisesRegex(ValueError, "revision"):
            audit_snapshot(self.root, self.receipt, license_accepted=True, revision="main")
        with self.assertRaisesRegex(ValueError, "missing"):
            audit_snapshot(self.directory / "missing", self.receipt, license_accepted=True)

    def test_changed_weights_or_coherently_rehashed_receipt_refused(self):
        self.audit()
        weight = self.root / "fixture.safetensors"
        weight.write_bytes(b"x" * weight.stat().st_size)
        with self.assertRaisesRegex(ValueError, "SHA256"):
            verify_snapshot_receipt(self.root, self.receipt)
        record = json.loads(self.receipt.read_text())
        record["files"]["fixture.safetensors"]["sha256"] = file_sha256(weight)
        self.receipt.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "official Git pointer"):
            verify_snapshot_receipt(self.root, self.receipt)

    def test_mismatched_directory_and_unfinished_receipt_refused(self):
        self.audit()
        record = json.loads(self.receipt.read_text())
        for field, value in (("status", "pending"), ("revision", "main"),
                             ("formalAvailability", True), ("licenseAcceptedByOperator", False),
                             ("snapshotDir", str(self.directory))):
            changed = {**record, field: value}
            self.receipt.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):
                verify_snapshot_receipt(self.root, self.receipt)

    def test_remote_python_and_pickle_prohibited(self):
        self.audit()
        for name in ("custom.py", "weights.bin", "weights.pkl"):
            path = self.root / name
            path.write_text("fixture")
            with self.assertRaisesRegex(ValueError, "prohibited"):
                verify_snapshot_receipt(self.root, self.receipt)
            path.unlink()

    def test_metadata_and_prompt_identity_mismatch(self):
        (self.root / "tdc_prompts.json").write_text(json.dumps({"BBB_Martins": "invented template"}))
        # Even rebuilding our fixture's Git identities cannot pass task identity.
        self.official["tdc_prompts.json"] = ((self.root / "tdc_prompts.json").stat().st_size,
                                            git_blob_oid((self.root / "tdc_prompts.json").read_bytes()), False)
        with self.assertRaisesRegex(ValueError, "reviewed official notebook"):
            self.audit()

    def test_receipt_output_inside_model_directory_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            audit_snapshot(self.root, self.root / "receipt.json", license_accepted=True)

    def test_malformed_receipt_and_file_records_refused(self):
        self.audit()
        record = json.loads(self.receipt.read_text())
        self.receipt.write_text("[]")
        with self.assertRaises(ValueError):
            verify_snapshot_receipt(self.root, self.receipt)
        record["files"]["fixture.safetensors"] = []
        self.receipt.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "JSON object"):
            verify_snapshot_receipt(self.root, self.receipt)

    def test_path_escape_refused(self):
        from backend.compare_txgemma import _safe_file
        for filename in ("../receipt.json", str(self.directory / "outside"), "..\\receipt.json"):
            with self.assertRaises(ValueError):
                _safe_file(self.root.resolve(), filename)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_not_enabled_or_license_not_accepted_never_runs_worker(self):
        disabled = TxGemmaPredictAdapter(env={})
        self.assertFalse((await disabled.status())["available"])
        with self.assertRaisesRegex(TxGemmaAdapterError, "启用"):
            await disabled.execute({})
        enabled = TxGemmaPredictAdapter(env={"AIDD_TXGEMMA_ENABLE": "true"})
        self.assertEqual((await enabled.status())["errorCode"], "TXGEMMA_LICENSE_REQUIRED")

    async def test_unknown_revision_config_and_missing_installation_unavailable(self):
        base = {"AIDD_TXGEMMA_ENABLE": "true", "AIDD_TXGEMMA_LICENSE_ACCEPTED": "true"}
        for overrides, expected in (({"AIDD_TXGEMMA_REVISION": "main"}, "TXGEMMA_UNSUPPORTED_REVISION"),
                                    ({"AIDD_TXGEMMA_DEVICE": "auto"}, "TXGEMMA_INVALID_CONFIG"),
                                    ({"AIDD_TXGEMMA_DTYPE": "float16"}, "TXGEMMA_INVALID_CONFIG"),
                                    ({"AIDD_TXGEMMA_TIMEOUT_SECONDS": "NaN"}, "TXGEMMA_INVALID_CONFIG"),
                                    ({}, "TXGEMMA_NOT_INSTALLED")):
            self.assertEqual((await TxGemmaPredictAdapter(env={**base, **overrides}).status())["errorCode"], expected)

    async def _mock_execute(self, response, returncode=0):
        adapter = TxGemmaPredictAdapter(env={})
        adapter.status = AsyncMock(return_value={"available": True})
        process = AsyncMock()
        process.kill = Mock()
        process.returncode = returncode
        process.communicate.return_value = (json.dumps(response).encode(), None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text("test-only receipt")
            adapter.receipt_path = str(path)
            with patch("backend.compare_txgemma.asyncio.create_subprocess_exec", return_value=process):
                result = await adapter.execute({"canonicalSmiles": "CCO", "structureId": "fixture-identity",
                                                "request": {"specialistTask": "tdc-bbb-martins"}})
        return result

    async def test_mock_worker_categorical_result_and_metrics(self):
        result = await self._mock_execute({"status": "completed", "answer": "B", "revision": TXGEMMA_REVISION,
                                           "metrics": {"inputTokens": 12, "outputTokens": 1, "gpu": None}})
        self.assertIsNone(result["data"]["tasks"][0]["probability"])
        self.assertEqual(result["data"]["structureId"], "fixture-identity")
        self.assertEqual(result["metrics"]["outputTokens"], 1)
        self.assertFalse(result["provenance"]["formalAvailability"])

    async def test_invalid_worker_output_and_oom_are_real_failures(self):
        for response in ({"status": "completed", "answer": "0.9", "revision": TXGEMMA_REVISION},
                         {"status": "completed", "answer": "B", "revision": "main"}):
            with self.assertRaises(TxGemmaAdapterError) as caught:
                await self._mock_execute(response)
            self.assertEqual(caught.exception.code, "TXGEMMA_INVALID_OUTPUT")
        with self.assertRaises(TxGemmaAdapterError) as caught:
            await self._mock_execute({"status": "failed", "code": "TXGEMMA_OOM"}, 1)
        self.assertEqual(caught.exception.code, "TXGEMMA_OOM")

    async def test_unsupported_task_does_not_spawn_worker(self):
        adapter = TxGemmaPredictAdapter(env={})
        adapter.status = AsyncMock(return_value={"available": True})
        with patch("backend.compare_txgemma.asyncio.create_subprocess_exec") as spawn:
            with self.assertRaises(TxGemmaAdapterError) as caught:
                await adapter.execute({"request": {"specialistTask": "tdc-herg"}})
            self.assertEqual(caught.exception.code, "TXGEMMA_TASK_UNSUPPORTED")
            spawn.assert_not_called()

    async def test_timeout_terminates_disposable_worker(self):
        adapter = TxGemmaPredictAdapter(env={})
        adapter.status = AsyncMock(return_value={"available": True})
        process = AsyncMock()
        process.kill = Mock()
        process.returncode = None
        process.communicate.side_effect = asyncio.TimeoutError()
        with patch("backend.compare_txgemma.asyncio.create_subprocess_exec", return_value=process):
            with self.assertRaises(TxGemmaAdapterError) as caught:
                await adapter.execute({"canonicalSmiles": "CCO", "structureId": "fixture",
                                       "request": {"specialistTask": "tdc-bbb-martins"}})
        self.assertEqual(caught.exception.code, "TXGEMMA_TIMEOUT")
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_cancellation_terminates_worker_and_remains_cancelled(self):
        adapter = TxGemmaPredictAdapter(env={})
        adapter.status = AsyncMock(return_value={"available": True})
        process = AsyncMock()
        process.kill = Mock()
        process.returncode = None
        process.communicate.side_effect = asyncio.CancelledError()
        with patch("backend.compare_txgemma.asyncio.create_subprocess_exec", return_value=process):
            with self.assertRaises(asyncio.CancelledError):
                await adapter.execute({"canonicalSmiles": "CCO", "structureId": "fixture",
                                       "request": {"specialistTask": "tdc-bbb-martins"}})
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_explicit_cuda_without_available_gpu_never_falls_back(self):
        adapter = TxGemmaPredictAdapter(env={"AIDD_TXGEMMA_ENABLE": "true", "AIDD_TXGEMMA_LICENSE_ACCEPTED": "true",
                                            "AIDD_TXGEMMA_DEVICE": "cuda", "AIDD_TXGEMMA_SNAPSHOT_DIR": "fixture",
                                            "AIDD_TXGEMMA_AUDIT_RECEIPT": "fixture"})
        with patch.object(adapter, "_verify", return_value={}), patch("torch.cuda.is_available", return_value=False):
            status = await adapter.status()
        self.assertFalse(status["available"])
        self.assertEqual(status["errorCode"], "TXGEMMA_CUDA_UNAVAILABLE")
        self.assertEqual(status["device"], "cuda")

    async def test_missing_dependency_and_unreviewed_transformers_fail_closed(self):
        adapter = TxGemmaPredictAdapter(env={"AIDD_TXGEMMA_ENABLE": "true", "AIDD_TXGEMMA_LICENSE_ACCEPTED": "true",
                                            "AIDD_TXGEMMA_SNAPSHOT_DIR": "fixture", "AIDD_TXGEMMA_AUDIT_RECEIPT": "fixture"})
        with patch.object(adapter, "_verify", return_value={}), patch("backend.compare_txgemma.importlib.util.find_spec", return_value=None):
            self.assertEqual((await adapter.status())["errorCode"], "TXGEMMA_DEPENDENCIES_MISSING")
        with patch.object(adapter, "_verify", return_value={}), patch("backend.compare_txgemma.importlib.metadata.version", return_value="unreviewed"):
            status = await adapter.status()
        self.assertFalse(status["available"])
        self.assertEqual(status["errorCode"], "TXGEMMA_DEPENDENCIES_UNREVIEWED")

    async def test_unknown_or_nonfinite_worker_metrics_rejected(self):
        for metrics in ({"inputTokens": 12, "outputTokens": 1, "apiKey": "forbidden-fixture"},
                        {"inputTokens": 12, "outputTokens": 1, "generationSeconds": float("nan")},
                        {"inputTokens": 12, "outputTokens": 90}):
            with self.assertRaises(TxGemmaAdapterError) as caught:
                await self._mock_execute({"status": "completed", "answer": "B", "revision": TXGEMMA_REVISION,
                                          "metrics": metrics})
            self.assertEqual(caught.exception.code, "TXGEMMA_INVALID_OUTPUT")

    async def test_cancellation_during_spawn_cleans_up_child_handle(self):
        adapter = TxGemmaPredictAdapter(env={})
        adapter.status = AsyncMock(return_value={"available": True})
        process = AsyncMock()
        process.returncode = None
        process.kill = Mock()
        started, release = asyncio.Event(), asyncio.Event()
        async def spawning(*args, **kwargs):
            started.set()
            await release.wait()
            return process
        with patch("backend.compare_txgemma.asyncio.create_subprocess_exec", side_effect=spawning):
            running = asyncio.create_task(adapter.execute({"canonicalSmiles": "CCO", "structureId": "fixture",
                                                          "request": {"specialistTask": "tdc-bbb-martins"}}))
            await started.wait()
            running.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await running
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
