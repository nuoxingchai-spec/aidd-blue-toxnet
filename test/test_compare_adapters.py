"""No external model, paid request, downloaded weights or GPU in these tests."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend.compare_adapters import (B16Adapter, CompareAdapterError, DeepSeekReportAdapter,
                                     OllamaQwenAdapter, _json, validate_deepseek_url, validate_ollama_url)
from backend.compare_worker import WORKER_SCHEMA, _cuda_oom, _require_b16, run_worker
from backend.model_client import ModelConfig, ModelAPIError
from backend.multitask_model import ALL_TASKS
from backend.chemistry import analyze_chemistry
from backend.compare_reporting import build_snapshot, report_messages, report_output_format, validate_report

DIGEST = "a" * 64
TEST_STRUCTURE = {"canonicalSmiles": "CCO", "structureId": hashlib.sha256(b"CCO").hexdigest()}
TEST_SNAPSHOT = build_snapshot(TEST_STRUCTURE, analyze_chemistry("CCO"),
                {"toxnet-v3-b16": {"status": "unavailable", "data": None}, "txgemma-2b-predict": {"status": "skipped", "data": None}})
MESSAGES = report_messages(TEST_SNAPSHOT)


def context(cloud=False):
    return {"canonicalSmiles": "CCO", "structureId": hashlib.sha256(b"CCO").hexdigest(),
            "request": {"allowCloudTransfer": cloud, "includeTox21": True, "includeEvidence": False, "explainEndpoint": None},
            "messages": MESSAGES, "snapshot": TEST_SNAPSHOT, "runId": "test-run-only"}


def tags(digest=DIGEST, name="qwen3.5:4b"):
    return {"models": [{"name": name, "model": name, "digest": digest, "size": 100,
                        "details": {"parameter_size": "4.0B", "quantization_level": "Q4_K_M"}}]}


def chat(content=None):
    return {"model": "qwen3.5:4b", "done": True,
            "message": {"role": "assistant", "content": json.dumps(content or {"schema": "test-only"})},
            "prompt_eval_count": 12, "eval_count": 6, "eval_duration": 2_000_000_000, "total_duration": 3_000_000_000}


def deepseek_config(**changes):
    return ModelConfig(**{"provider_id": "deepseek", "api_url": "https://api.deepseek.com/chat/completions",
                         "api_key": "not-real-test-key", "model_name": "account-configured-test-model",
                         "model_version": "configured-test-version", "api_format": "openai", **changes})


class FakeProcess:
    def __init__(self, records, *, hang=False):
        self.records = [json.dumps(record).encode() + b"\n" for record in records]
        self.hang, self.killed, self.returncode = hang, False, None
        self.stdin = SimpleNamespace(write=lambda value: setattr(self, "written", value), drain=AsyncMock(), close=lambda: None)
        self.stdout = SimpleNamespace(readline=self.readline)

    async def readline(self):
        if self.records:
            return self.records.pop(0)
        if self.hang:
            await asyncio.Event().wait()
        self.returncode = 0
        return b""

    def kill(self):
        self.killed, self.returncode = True, -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class URLValidationTests(unittest.TestCase):
    def test_ollama_literal_loopback_and_no_remote_or_credentials(self):
        self.assertEqual(validate_ollama_url("http://localhost:11434/"), "http://127.0.0.1:11434")
        self.assertEqual(validate_ollama_url("http://[::1]:11434"), "http://[::1]:11434")
        for url in ("http://evil.test:11434", "http://127.0.0.1.evil.test", "https://127.0.0.1:11434",
                    "http://user:password@127.0.0.1:11434", "http://127.0.0.1:11434/api", "http://127.0.0.1:11434/?token=secret",
                    "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://2130706433:11434", "http://127.0.0.2:11434"):
            with self.subTest(url=url), self.assertRaises(CompareAdapterError):
                validate_ollama_url(url)

    def test_deepseek_official_host_only(self):
        self.assertEqual(validate_deepseek_url("https://api.deepseek.com:443/v1/chat/completions"), "https://api.deepseek.com/v1/chat/completions")
        for url in ("http://api.deepseek.com/chat/completions", "https://evil.test/chat/completions",
                    "https://api.deepseek.com.evil.test/chat/completions", "https://api.deepseek.com:8443/chat/completions",
                    "https://user:secret@api.deepseek.com/chat/completions", "https://api.deepseek.com/chat/completions?q=1",
                    "https://api.deepseek.com/embeddings", "https://127.0.0.1/chat/completions"):
            with self.subTest(url=url), self.assertRaises(CompareAdapterError):
                validate_deepseek_url(url)

    def test_strict_json_no_nan_no_array(self):
        for value in ('{"score":NaN}', '{"score":1e999}', '{"score":1,"score":2}', '[1,2]', 'not JSON'):
            with self.subTest(value=value), self.assertRaises(CompareAdapterError):
                _json(value)


class OllamaAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_v2_payload_uses_snapshot_bound_schema_for_fast_and_deep(self):
        """Real adapter with MockTransport; no Ollama or model is contacted."""
        for mode in ('fast', 'deep'):
            with self.subTest(mode=mode):
                supplied = deepcopy(context())
                supplied['request'].update(reportContract='v2', reportMode=mode)
                supplied['messages'] = report_messages(supplied['snapshot'], report_contract='v2', report_mode=mode)
                template = json.loads(supplied['messages'][1]['content'])['responseTemplate']
                submitted = []
                def handler(request):
                    if request.url.path == '/api/chat':
                        submitted.append(json.loads(request.content))
                        return httpx.Response(200, json=chat(template))
                    return httpx.Response(200, json={'models': []} if request.url.path == '/api/ps' else tags())
                adapter = OllamaQwenAdapter(transport=httpx.MockTransport(handler))
                result = await adapter.execute(supplied)
                self.assertEqual(len(submitted), 1)
                body = submitted[0]
                self.assertEqual(body['messages'], supplied['messages'])
                self.assertIsInstance(body['format'], dict)
                self.assertEqual(body['format'], report_output_format(supplied['snapshot'], report_contract='v2', report_mode=mode))
                properties = body['format']['properties']
                self.assertEqual(properties['snapshotSha256']['const'], supplied['snapshot']['snapshotSha256'])
                self.assertEqual(properties['claimOrder']['type'], 'object')
                self.assertFalse(properties['claimOrder']['additionalProperties'])
                for section in supplied['snapshot']['sections']:
                    member_schema = properties['claimOrder']['properties'][section['id']]
                    self.assertEqual(member_schema['items']['enum'], section['claimIds'])
                    self.assertEqual(member_schema['minItems'], len(section['claimIds']))
                    self.assertEqual(member_schema['maxItems'], len(section['claimIds']))
                for forbidden in ('score', 'sources', 'sourceIds', 'modelVersion', 'structureId', 'factChecks'):
                    self.assertNotIn(forbidden, properties)
                verified = validate_report(result['data']['rawReport'], supplied['snapshot'], report_contract='v2', report_mode=mode)
                self.assertEqual(verified['verifiedFacts'], supplied['snapshot']['facts'])
                self.assertTrue(result['metrics']['resourceCleanupVerified'])

    async def test_v2_prompt_and_output_format_provenance_track_actual_payload(self):
        supplied = deepcopy(context())
        supplied['request'].update(reportContract='v2', reportMode='fast')
        supplied['messages'] = report_messages(supplied['snapshot'], report_contract='v2', report_mode='fast')
        message_payload = json.loads(supplied['messages'][1]['content'])
        def handler(request):
            if request.url.path == '/api/chat':
                return httpx.Response(200, json=chat(message_payload['responseTemplate']))
            return httpx.Response(200, json={'models': []} if request.url.path == '/api/ps' else tags())
        result = await OllamaQwenAdapter(transport=httpx.MockTransport(handler)).execute(supplied)
        provenance = result['provenance']
        self.assertEqual(provenance['promptVersion'], message_payload['promptVersion'])
        self.assertEqual(provenance['promptVersion'], 'aidd.compare.independent-report.v2.1')
        self.assertEqual(provenance['outputFormat'], 'server-bound-json-schema')
        self.assertEqual(provenance['reportContract'], 'v2')
        self.assertEqual(provenance['reportMode'], 'fast')
        self.assertEqual(provenance['snapshotSha256'], TEST_SNAPSHOT['snapshotSha256'])
        self.assertEqual(provenance['revision'], DIGEST)

    async def test_v1_default_and_explicit_contract_keep_json_generation_and_provenance(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                supplied = deepcopy(context())
                if explicit:
                    supplied['request'].update(reportContract='v1', reportMode='deep')
                bodies = []
                def handler(request):
                    if request.url.path == '/api/chat':
                        bodies.append(json.loads(request.content))
                        return httpx.Response(200, json=chat())
                    return httpx.Response(200, json={'models': []} if request.url.path == '/api/ps' else tags())
                result = await OllamaQwenAdapter(transport=httpx.MockTransport(handler)).execute(supplied)
                self.assertEqual(bodies[0]['format'], 'json')
                self.assertEqual(bodies[0]['messages'], MESSAGES)
                self.assertEqual(result['provenance']['promptVersion'], 'aidd.compare.independent-report.v1')
                self.assertEqual(result['provenance']['outputFormat'], 'json')
                self.assertEqual(result['provenance']['reportContract'], 'v1')
                self.assertEqual(result['provenance']['reportMode'], 'deep')

    async def test_v2_invalid_or_truncated_json_keeps_usage_generation_provenance_and_cleanup(self):
        for failure, expected_code in (('invalid', 'OLLAMA_INVALID_REPORT'), ('truncated', 'OLLAMA_REPORT_TRUNCATED')):
            with self.subTest(failure=failure):
                supplied = deepcopy(context())
                supplied['request'].update(reportContract='v2', reportMode='fast')
                supplied['messages'] = report_messages(supplied['snapshot'], report_contract='v2', report_mode='fast')
                unloads = []
                def handler(request):
                    if request.url.path == '/api/chat':
                        payload = chat()
                        payload['message']['content'] = 'DO-NOT-PUBLISH-INVALID-MODEL-TEXT'
                        if failure == 'truncated':
                            payload['done_reason'] = 'length'
                        return httpx.Response(200, json=payload)
                    if request.url.path == '/api/generate':
                        unloads.append(True)
                        return httpx.Response(200, json={'done': True})
                    return httpx.Response(200, json={'models': []} if request.url.path == '/api/ps' else tags())
                adapter = OllamaQwenAdapter(transport=httpx.MockTransport(handler))
                with self.assertRaises(CompareAdapterError) as caught:
                    await adapter.execute(supplied)
                error = caught.exception
                self.assertEqual(error.code, expected_code)
                self.assertEqual(error.metrics['promptTokens'], 12)
                self.assertEqual(error.metrics['completionTokens'], 6)
                self.assertEqual(error.metrics['generationSeconds'], 2.0)
                self.assertEqual(error.provenance['promptVersion'], 'aidd.compare.independent-report.v2.1')
                self.assertEqual(error.provenance['outputFormat'], 'server-bound-json-schema')
                self.assertEqual(error.provenance['snapshotSha256'], TEST_SNAPSHOT['snapshotSha256'])
                self.assertEqual(error.provenance['revision'], DIGEST)
                self.assertTrue(error.metrics['resourceCleanupVerified'])
                self.assertEqual(unloads, [True])
                self.assertNotIn('DO-NOT-PUBLISH-INVALID-MODEL-TEXT', json.dumps(error.__dict__))

    async def test_status_queries_actual_tags_and_reports_digest(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=tags())
        adapter = OllamaQwenAdapter(transport=httpx.MockTransport(handler))
        status = await adapter.status()
        self.assertTrue(status["available"])
        self.assertEqual(status["actualModelId"], "qwen3.5:4b")
        self.assertEqual(status["revision"], DIGEST)
        self.assertEqual(status["installedModels"][0]["name"], "qwen3.5:4b")
        self.assertEqual(status["selectedDefaultModel"], "qwen3.5:4b")
        self.assertEqual([str(item.url) for item in calls], ["http://127.0.0.1:11434/api/tags"])

    async def test_missing_model_unavailable_no_auto_pull(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"models": []})
        status = await OllamaQwenAdapter(transport=httpx.MockTransport(handler)).status()
        self.assertFalse(status["available"])
        self.assertEqual(status["errorCode"], "OLLAMA_MODEL_NOT_INSTALLED")
        self.assertEqual(len(calls), 1)

    async def test_connection_failure_is_unavailable(self):
        def handler(request):
            raise httpx.ConnectError("diagnostic-private-path-never-public", request=request)
        status = await OllamaQwenAdapter(transport=httpx.MockTransport(handler)).status()
        self.assertEqual(status["errorCode"], "OLLAMA_UNAVAILABLE")
        self.assertNotIn("diagnostic-private", json.dumps(status))

    async def test_execute_health_check_three_seconds_generation_180_seconds(self):
        timeouts = {}
        def handler(request):
            timeouts[request.url.path] = dict(request.extensions["timeout"])
            if request.url.path == "/api/chat":
                return httpx.Response(200, json=chat())
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": []})
            return httpx.Response(200, json=tags())
        await OllamaQwenAdapter(transport=httpx.MockTransport(handler)).execute(context())
        self.assertTrue(all(value == 3 for value in timeouts["/api/tags"].values()))
        self.assertTrue(all(value == 180 for value in timeouts["/api/chat"].values()))

    async def test_execute_missing_daemon_fast_check_unavailable_no_chat(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            self.assertEqual(request.extensions["timeout"]["connect"], 3)
            raise httpx.ConnectTimeout("test-only missing daemon", request=request)
        adapter = OllamaQwenAdapter(transport=httpx.MockTransport(handler))
        with self.assertRaises(CompareAdapterError) as error:
            await adapter.execute(context())
        self.assertEqual(error.exception.code, "OLLAMA_UNAVAILABLE")
        self.assertEqual(calls, ["/api/tags"])
        self.assertTrue(adapter.cleanup_safe)

    async def test_unverified_digest_and_non_qwen_not_accepted(self):
        for response in (tags(digest="unknown"), tags(name="llama3:8b")):
            status = await OllamaQwenAdapter(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))).status()
            self.assertFalse(status["available"])
            self.assertEqual(status["installedModels"], [])

    async def test_execute_independent_json_no_score_fallback_and_real_response_metrics(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=chat({"claims": ["test"]}) if request.url.path == "/api/chat" else tags())
        result = await OllamaQwenAdapter(transport=httpx.MockTransport(handler)).execute(context())
        self.assertEqual(result["data"], {"rawReport": {"claims": ["test"]}})
        self.assertEqual(result["metrics"]["completionTokens"], 6)
        self.assertEqual(result["metrics"]["tokensPerSecond"], 3.0)
        self.assertFalse(result["metrics"]["gpu"]["measured"])
        self.assertIsNone(result["metrics"]["cost"])
        body = json.loads(calls[1].content)
        self.assertEqual(body["messages"], MESSAGES)
        self.assertEqual(body["keep_alive"], 0)
        self.assertEqual(body["format"], "json")
        self.assertFalse(body["think"])
        self.assertEqual(result["provenance"]["snapshotSha256"], TEST_SNAPSHOT["snapshotSha256"])

    async def test_selected_model_must_be_installed_qwen(self):
        adapter = OllamaQwenAdapter(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=tags())))
        with self.assertRaises(CompareAdapterError) as error:
            await adapter.execute({**context(), "reporterModel": "not-installed:latest"})
        self.assertEqual(error.exception.code, "OLLAMA_MODEL_NOT_INSTALLED")

    async def test_digest_changed_discards_report(self):
        tag_calls = 0
        def handler(request):
            nonlocal tag_calls
            if request.url.path == "/api/chat":
                return httpx.Response(200, json=chat())
            tag_calls += 1
            return httpx.Response(200, json=tags(DIGEST if tag_calls == 1 else "c" * 64))
        with self.assertRaises(CompareAdapterError) as error:
            await OllamaQwenAdapter(transport=httpx.MockTransport(handler)).execute(context())
        self.assertEqual(error.exception.code, "OLLAMA_MODEL_CHANGED")

    async def test_redirect_is_rejected(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://evil.test/secret"})
        status = await OllamaQwenAdapter(transport=httpx.MockTransport(handler)).status()
        self.assertFalse(status["available"])
        self.assertEqual(len(calls), 1)

    async def test_invalid_report_rejected(self):
        def handler(request):
            bad = chat()
            bad["message"]["content"] = '{"score":NaN}'
            return httpx.Response(200, json=bad if request.url.path == "/api/chat" else tags())
        with self.assertRaises(CompareAdapterError):
            await OllamaQwenAdapter(transport=httpx.MockTransport(handler)).execute(context())

    async def test_timeout_triggers_local_unload(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/api/chat":
                raise httpx.ReadTimeout("private diagnostic", request=request)
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": []})
            return httpx.Response(200, json={"done": True} if request.url.path == "/api/generate" else tags())
        adapter = OllamaQwenAdapter(transport=httpx.MockTransport(handler))
        with self.assertRaises(CompareAdapterError) as error:
            await adapter.execute(context())
        self.assertEqual(error.exception.code, "COMPARE_TIMEOUT")
        self.assertTrue(adapter.cleanup_safe)
        self.assertIn("/api/generate", calls)

    async def test_cancel_closes_http_and_unloads_or_marks_unsafe(self):
        called = asyncio.Event()
        async def handler(request):
            if request.url.path == "/api/chat":
                called.set()
                await asyncio.Event().wait()
            if request.url.path == "/api/generate":
                return httpx.Response(503, json={})
            return httpx.Response(200, json=tags())
        adapter = OllamaQwenAdapter(transport=httpx.MockTransport(handler))
        task = asyncio.create_task(adapter.execute(context()))
        await called.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(adapter.cleanup_safe)

    async def test_normal_completion_verifies_unloaded_model(self):
        def handler(request):
            if request.url.path == "/api/chat":
                return httpx.Response(200, json=chat())
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": []})
            return httpx.Response(200, json=tags())
        adapter = OllamaQwenAdapter(transport=httpx.MockTransport(handler))
        result = await adapter.execute(context())
        self.assertTrue(adapter.cleanup_safe)
        self.assertTrue(result["metrics"]["resourceCleanupVerified"])

    async def test_mutated_snapshot_or_prompt_never_sent_to_local_reporter(self):
        handler = unittest.mock.Mock(side_effect=AssertionError("no unverified snapshot"))
        adapter = OllamaQwenAdapter(transport=httpx.MockTransport(handler))
        changed = deepcopy(context())
        changed["snapshot"]["facts"][0]["value"] = {"fake": True}
        with self.assertRaises(CompareAdapterError) as error:
            await adapter.execute(changed)
        self.assertEqual(error.exception.code, "COMPARE_SNAPSHOT_MISMATCH")
        changed = deepcopy(context())
        changed["messages"][1]["content"] += " also invent a study"
        with self.assertRaises(CompareAdapterError) as error:
            await adapter.execute(changed)
        self.assertEqual(error.exception.code, "COMPARE_REPORT_CONTEXT_INVALID")
        handler.assert_not_called()


class DeepSeekAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_does_not_call_network_or_claim_access(self):
        handler = unittest.mock.Mock(side_effect=AssertionError("no status network"))
        adapter = DeepSeekReportAdapter(config=deepseek_config(), transport=httpx.MockTransport(handler))
        status = await adapter.status()
        self.assertTrue(status["configured"])
        self.assertFalse(status["available"])
        self.assertFalse(status["accessVerified"])
        self.assertEqual(status["errorCode"], "DEEPSEEK_ACCESS_UNVERIFIED")
        self.assertNotIn("not-real-test-key", json.dumps(status))
        handler.assert_not_called()

    async def test_no_cloud_permission_no_request(self):
        handler = unittest.mock.Mock(side_effect=AssertionError("cloud not authorized"))
        adapter = DeepSeekReportAdapter(config=deepseek_config(), transport=httpx.MockTransport(handler))
        with self.assertRaises(CompareAdapterError) as error:
            await adapter.execute(context())
        self.assertEqual(error.exception.code, "CLOUD_TRANSFER_NOT_AUTHORIZED")
        self.assertFalse(adapter.request_attempted)
        self.assertFalse(adapter.attempted_for("test-run-only"))
        handler.assert_not_called()

    async def test_real_contract_mock_report_keeps_scores_out_of_adapter(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"model": "returned-test-model", "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"claims":["independent-test"]}'}}],
                                            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        result = await DeepSeekReportAdapter(config=deepseek_config(), transport=httpx.MockTransport(handler)).execute(context(True))
        self.assertEqual(result["data"], {"rawReport": {"claims": ["independent-test"]}})
        self.assertEqual(result["metrics"]["totalTokens"], 15)
        self.assertIsNone(result["metrics"]["cost"])
        self.assertFalse(result["provenance"]["rawExperimentalRowsTransferred"])
        self.assertEqual(result["provenance"]["returnedModel"], "returned-test-model")
        self.assertEqual(result["provenance"]["snapshotSha256"], TEST_SNAPSHOT["snapshotSha256"])
        self.assertTrue(result["provenance"]["requestAttempted"])
        self.assertEqual(json.loads(calls[0].content)["messages"], MESSAGES)
        self.assertEqual(calls[0].headers["authorization"], "Bearer not-real-test-key")
        self.assertNotIn("not-real-test-key", json.dumps(result))

    async def test_untrusted_url_never_called(self):
        handler = unittest.mock.Mock(side_effect=AssertionError("SSRF"))
        adapter = DeepSeekReportAdapter(config=deepseek_config(api_url="https://evil.test/v1/chat/completions"), transport=httpx.MockTransport(handler))
        with self.assertRaises(CompareAdapterError) as error:
            await adapter.execute(context(True))
        self.assertEqual(error.exception.code, "DEEPSEEK_TRUSTED_URL_REQUIRED")
        self.assertFalse(adapter.request_attempted)
        handler.assert_not_called()

    async def test_truncated_or_non_json_output_rejected(self):
        for choice in ({"finish_reason": "length", "message": {"role": "assistant", "content": "{}"}},
                       {"finish_reason": "stop", "message": {"role": "assistant", "content": "I think it is safe."}}):
            adapter = DeepSeekReportAdapter(config=deepseek_config(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": [choice]})))
            with self.assertRaises(CompareAdapterError):
                await adapter.execute(context(True))

    async def test_live_configuration_replacement_used(self):
        import threading
        store = SimpleNamespace(lock=threading.RLock(), registry={"deepseek": SimpleNamespace(config=deepseek_config())})
        adapter = DeepSeekReportAdapter(store)
        self.assertEqual((await adapter.status())["actualModelId"], "account-configured-test-model")
        store.registry["deepseek"] = SimpleNamespace(config=deepseek_config(model_name="new-account-model"))
        self.assertEqual((await adapter.status())["actualModelId"], "new-account-model")

    async def test_http_failure_retains_attempt_but_configuration_failure_does_not(self):
        def handler(request):
            raise httpx.ConnectError("private diagnostic", request=request)
        adapter = DeepSeekReportAdapter(config=deepseek_config(), transport=httpx.MockTransport(handler))
        with self.assertRaises(CompareAdapterError):
            await adapter.execute({**context(True), "runId": "attempted-failure"})
        self.assertTrue(adapter.attempted_for("attempted-failure"))
        self.assertTrue(adapter.requestAttempted)
        with self.assertRaises(CompareAdapterError):
            await adapter.execute({**context(False), "runId": "not-authorized"})
        self.assertFalse(adapter.attempted_for("not-authorized"))
        self.assertFalse(adapter.requestAttempted)
        self.assertTrue(adapter.attempted_for("attempted-failure"))

    async def test_bad_snapshot_refuses_cloud_attempt(self):
        handler = unittest.mock.Mock(side_effect=AssertionError("no cloud transfer"))
        adapter = DeepSeekReportAdapter(config=deepseek_config(), transport=httpx.MockTransport(handler))
        changed = {**context(True), "snapshotSha256": "c" * 64}
        with self.assertRaises(CompareAdapterError) as error:
            await adapter.execute(changed)
        self.assertEqual(error.exception.code, "COMPARE_SNAPSHOT_MISMATCH")
        self.assertFalse(adapter.attempted_for("test-run-only"))
        handler.assert_not_called()

    async def test_attempt_flags_isolated_across_overlapping_runs(self):
        first_started = asyncio.Event()
        release = asyncio.Event()
        async def handler(request):
            first_started.set()
            await release.wait()
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}]})
        adapter = DeepSeekReportAdapter(config=deepseek_config(), transport=httpx.MockTransport(handler))
        first = asyncio.create_task(adapter.execute({**context(True), "runId": "allowed"}))
        await first_started.wait()
        with self.assertRaises(CompareAdapterError):
            await adapter.execute({**context(False), "runId": "refused"})
        self.assertTrue(adapter.attempted_for("allowed"))
        self.assertFalse(adapter.attempted_for("refused"))
        release.set()
        result = await first
        self.assertTrue(result["provenance"]["requestAttempted"])
        self.assertTrue(adapter.attempted_for("allowed"))
        self.assertFalse(adapter.attempted_for("refused"))


class B16AdapterTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, **changes):
        return B16Adapter(**{"model_dir": "test-does-not-exist", "encoder_dir": "test-does-not-exist-encoder", "enabled": True, **changes})

    async def test_disabled_never_starts_worker(self):
        with patch("backend.compare_adapters.asyncio.create_subprocess_exec") as spawn:
            status = await B16Adapter().status()
        self.assertFalse(status["available"])
        self.assertEqual(status["errorCode"], "MULTITASK_RESEARCH_DISABLED")
        spawn.assert_not_called()

    async def test_worker_only_trusted_server_paths_and_offline_flags(self):
        process = FakeProcess([{"kind": "final", "ok": True, "info": {"available": True, "configured": True, "actualModelId": "test-only"}}])
        with patch("backend.compare_adapters.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            adapter = self.adapter()
            first, second = await adapter.status(), await adapter.status()
        self.assertTrue(first["available"])
        self.assertEqual(first, second)
        self.assertEqual(spawn.await_count, 1)
        kwargs = spawn.call_args.kwargs
        self.assertEqual(kwargs["env"]["HF_HUB_OFFLINE"], "1")
        self.assertEqual(kwargs["env"]["TRANSFORMERS_OFFLINE"], "1")
        sent = json.loads(process.written)
        self.assertEqual(sent["action"], "status")
        self.assertNotIn("apiKey", sent)

    async def test_worker_invalid_or_failed_response_not_available(self):
        process = FakeProcess([{"kind": "final", "ok": False, "error": {"code": "MULTITASK_CHECKSUM_MISMATCH", "message": "hash failed"}}])
        with patch("backend.compare_adapters.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            status = await self.adapter().status()
        self.assertFalse(status["available"])
        self.assertEqual(status["errorCode"], "MULTITASK_CHECKSUM_MISMATCH")

    async def test_execute_timeout_retains_real_prediction_event(self):
        prediction = {"kind": "prediction", "ok": True, "info": {"available": True},
                      "result": {"status": "partial", "data": {"prediction": {"endpoints": [{"id": "herg", "score": 0.25}]},
                                     "evidence": {"status": "pending"}, "attribution": {"status": "pending"}}, "provenance": {}, "metrics": {}}}
        process = FakeProcess([prediction], hang=True)
        with patch("backend.compare_adapters.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            result = await self.adapter(timeout_seconds=0.02).execute(context())
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["prediction"]["endpoints"][0]["score"], 0.25)
        self.assertEqual(result["data"]["evidence"]["errorCode"], "COMPARE_OPTIONAL_AUDIT_TIMEOUT")
        self.assertTrue(process.killed)

    async def test_cancel_kills_and_waits_worker(self):
        process = FakeProcess([], hang=True)
        with patch("backend.compare_adapters.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            task = asyncio.create_task(self.adapter().execute(context()))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.killed)
        self.assertIsNotNone(process.returncode)

    async def test_cancellation_while_creating_child_still_kills_exact_child(self):
        process = FakeProcess([], hang=True)
        creation_started, release_creation = asyncio.Event(), asyncio.Event()
        async def spawn(*args, **kwargs):
            creation_started.set()
            await release_creation.wait()
            return process
        adapter = self.adapter()
        with patch("backend.compare_adapters.asyncio.create_subprocess_exec", spawn):
            task = asyncio.create_task(adapter.execute(context()))
            await creation_started.wait()
            task.cancel()
            release_creation.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(process.killed)
        self.assertTrue(adapter.cleanup_safe)

    async def test_prediction_is_reverified_execute_not_status_cache(self):
        adapter = self.adapter()
        adapter._status_cache = (float("inf"), {"available": True})
        with patch.object(adapter, "_worker", AsyncMock(side_effect=CompareAdapterError("MULTITASK_CHECKSUM_MISMATCH", "hash failed"))) as worker:
            with self.assertRaises(CompareAdapterError):
                await adapter.execute(context())
        worker.assert_awaited_once()


class B16WorkerTests(unittest.TestCase):
    def test_genuine_oom_wrapper_is_distinguished_from_generic_runtime_failure(self):
        test_oom_class = type("OutOfMemoryError", (RuntimeError,), {"__module__": "torch"})
        try:
            try:
                raise test_oom_class("test-only GPU memory error")
            except RuntimeError:
                raise ModelAPIError("MULTITASK_INFERENCE_UNAVAILABLE", "safe wrapped error") from None
        except ModelAPIError as error:
            self.assertTrue(_cuda_oom(error))
        self.assertFalse(_cuda_oom(RuntimeError("generic CUDA error")))

    def test_research_disabled_and_extra_fields_rejected_before_loading(self):
        base = {"schema": WORKER_SCHEMA, "action": "status", "enabled": False, "device": "cpu"}
        with patch("backend.compare_worker.MultitaskModelClient") as client:
            with self.assertRaises(ModelAPIError) as error:
                run_worker(base)
            self.assertEqual(error.exception.code, "MULTITASK_RESEARCH_DISABLED")
            with self.assertRaises(ModelAPIError):
                run_worker({**base, "browserPath": "private"})
        client.assert_not_called()

    def test_incomplete_or_wrong_branches_cannot_be_called_b16(self):
        manifest = {"status": "trained-completed", "tasks": list(ALL_TASKS),
                    "architecture": {"featureBranches": ["morgan"], "headType": "logistic-regression"},
                    "training": {"endpointStatus": {task: "trained" for task in ALL_TASKS}}}
        client = SimpleNamespace(_ensure_loaded=lambda: {"manifest": manifest}, _require_tox21=lambda _: None)
        with self.assertRaises(ModelAPIError) as error:
            _require_b16(client)
        self.assertEqual(error.exception.code, "COMPARE_B16_REQUIRED")

    def test_complete_molformer_head_passes_task_guard(self):
        manifest = {"status": "trained-completed", "tasks": list(ALL_TASKS),
                    "architecture": {"featureBranches": ["molformer"], "headType": "shared-mlp"},
                    "training": {"endpointStatus": {task: "trained" for task in ALL_TASKS}}}
        loaded = {"manifest": manifest}
        client = SimpleNamespace(_ensure_loaded=lambda: loaded, _require_tox21=lambda _: None)
        self.assertIs(_require_b16(client), loaded)


if __name__ == "__main__":
    unittest.main()
