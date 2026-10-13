"""Report failures preserve real response counters without upstream text."""
from __future__ import annotations

import json
import unittest

import httpx

from backend.compare_adapters import CompareAdapterError, DeepSeekReportAdapter, OllamaQwenAdapter
from test.test_compare_adapters import chat, context, deepseek_config, tags


class ErrorEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def _deep_failure(self, payload, code):
        adapter = DeepSeekReportAdapter(config=deepseek_config(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
        with self.assertRaises(CompareAdapterError) as caught:
            await adapter.execute(context(True))
        error = caught.exception
        self.assertEqual(error.code, code)
        self.assertTrue(error.provenance["requestAttempted"])
        self.assertEqual(error.metrics["httpStatusCode"], 200)
        self.assertIsNone(error.metrics["cost"])
        self.assertNotIn("not-real-test-key", json.dumps(error.__dict__))
        self.assertNotIn("DO-NOT-PUBLISH-UPSTREAM-TEXT", json.dumps(error.__dict__))
        return error

    async def test_deep_bad_json_keeps_usage_and_model_provenance(self):
        payload = {"model": "returned-test-model", "usage": {"prompt_tokens": 100, "completion_tokens": 12, "total_tokens": 112},
                   "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "DO-NOT-PUBLISH-UPSTREAM-TEXT"}}]}
        error = await self._deep_failure(payload, "DEEPSEEK_INVALID_REPORT")
        self.assertEqual(error.metrics["promptTokens"], 100)
        self.assertEqual(error.metrics["completionTokens"], 12)
        self.assertEqual(error.provenance["returnedModel"], "returned-test-model")
        self.assertGreaterEqual(error.metrics["wallSeconds"], 0)

    async def test_deep_truncated_refused_empty_are_distinct_with_usage(self):
        for finish, message, code in (
            ("length", {"role": "assistant", "content": "{}"}, "DEEPSEEK_REPORT_TRUNCATED"),
            ("stop", {"role": "assistant", "content": None, "refusal": "DO-NOT-PUBLISH-UPSTREAM-TEXT"}, "DEEPSEEK_REPORT_REFUSED"),
            ("stop", {"role": "assistant", "content": None}, "DEEPSEEK_REPORT_EMPTY"),
        ):
            with self.subTest(code=code):
                error = await self._deep_failure({"usage": {"prompt_tokens": 5, "completion_tokens": 2}, "choices": [{"finish_reason": finish, "message": message}]}, code)
                self.assertEqual(error.metrics["completionTokens"], 2)

    async def test_negative_usage_stays_null_other_valid_fields_kept(self):
        error = await self._deep_failure({"usage": {"prompt_tokens": 5, "completion_tokens": -2, "total_tokens": 3},
                    "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}]}, "COMPARE_INVALID_USAGE")
        self.assertEqual(error.metrics["promptTokens"], 5)
        self.assertIsNone(error.metrics["completionTokens"])
        self.assertEqual(error.metrics["invalidUsageFields"], ["completion_tokens"])

    async def test_http429_and500_keep_status_but_no_upstream_body(self):
        for status, code in ((429, "COMPARE_HTTP_RATE_LIMITED"), (500, "COMPARE_HTTP_SERVER_ERROR")):
            adapter = DeepSeekReportAdapter(config=deepseek_config(), transport=httpx.MockTransport(
                lambda _, status=status: httpx.Response(status, text="DO-NOT-PUBLISH-UPSTREAM-TEXT not-real-test-key")))
            with self.assertRaises(CompareAdapterError) as caught:
                await adapter.execute(context(True))
            error = caught.exception
            self.assertEqual(error.code, code)
            self.assertEqual(error.metrics["httpStatusCode"], status)
            self.assertTrue(error.metrics["httpResponseReceived"])
            self.assertTrue(error.provenance["requestAttempted"])
            self.assertNotIn("DO-NOT-PUBLISH-UPSTREAM-TEXT", json.dumps(error.__dict__))
            self.assertNotIn("not-real-test-key", json.dumps(error.__dict__))

    async def test_network_timeout_phase_retained_without_fake_tokens(self):
        def handler(request):
            raise httpx.ReadTimeout("DO-NOT-PUBLISH-UPSTREAM-TEXT", request=request)
        adapter = DeepSeekReportAdapter(config=deepseek_config(), transport=httpx.MockTransport(handler))
        with self.assertRaises(CompareAdapterError) as caught:
            await adapter.execute(context(True))
        error = caught.exception
        self.assertEqual(error.code, "COMPARE_TIMEOUT")
        self.assertEqual(error.metrics["timeoutPhase"], "read")
        self.assertIsNone(error.metrics["completionTokens"])
        self.assertTrue(error.provenance["requestAttempted"])

    async def test_http_error_json_usage_kept_without_error_text_or_zero_cost(self):
        for status in (429, 500):
            adapter = DeepSeekReportAdapter(config=deepseek_config(), transport=httpx.MockTransport(
                lambda _, status=status: httpx.Response(status, json={
                    'error': {'message': 'DO-NOT-PUBLISH-UPSTREAM-TEXT'},
                    'usage': {'prompt_tokens': 8, 'completion_tokens': 3, 'total_tokens': 11}})))
            with self.assertRaises(CompareAdapterError) as caught:
                await adapter.execute(context(True))
            self.assertEqual(caught.exception.metrics['totalTokens'], 11)
            self.assertEqual(caught.exception.metrics['httpStatusCode'], status)
            self.assertIsNone(caught.exception.metrics['cost'])
            self.assertNotIn('DO-NOT-PUBLISH-UPSTREAM-TEXT', json.dumps(caught.exception.__dict__))

    async def test_ollama_bad_report_keeps_token_counts_and_cleans_once(self):
        unloads = []
        def handler(request):
            if request.url.path == "/api/chat":
                value = chat()
                value["message"]["content"] = "DO-NOT-PUBLISH-UPSTREAM-TEXT"
                return httpx.Response(200, json=value)
            if request.url.path == "/api/generate":
                unloads.append(True)
                return httpx.Response(200, json={"done": True})
            return httpx.Response(200, json={"models": []} if request.url.path == "/api/ps" else tags())
        adapter = OllamaQwenAdapter(transport=httpx.MockTransport(handler))
        with self.assertRaises(CompareAdapterError) as caught:
            await adapter.execute(context())
        error = caught.exception
        self.assertEqual(error.code, "OLLAMA_INVALID_REPORT")
        self.assertEqual(error.metrics["completionTokens"], 6)
        self.assertTrue(error.metrics["resourceCleanupVerified"])
        self.assertEqual(error.provenance["revision"], "a" * 64)
        self.assertEqual(unloads, [True])
        self.assertNotIn("DO-NOT-PUBLISH-UPSTREAM-TEXT", json.dumps(error.__dict__))

    async def test_ollama_refusal_empty_and_negative_usage_distinct(self):
        for field, value, code in (("refusal", "DO-NOT-PUBLISH-UPSTREAM-TEXT", "OLLAMA_REPORT_REFUSED"),
                                   ("content", "   ", "OLLAMA_REPORT_EMPTY"),
                                   ("negative-usage", -1, "COMPARE_INVALID_USAGE")):
            def handler(request):
                if request.url.path == "/api/chat":
                    payload = chat()
                    if field == "negative-usage":
                        payload["eval_count"] = value
                    else:
                        payload["message"][field] = value
                    return httpx.Response(200, json=payload)
                if request.url.path == "/api/generate":
                    return httpx.Response(200, json={"done": True})
                return httpx.Response(200, json={"models": []} if request.url.path == "/api/ps" else tags())
            adapter = OllamaQwenAdapter(transport=httpx.MockTransport(handler))
            with self.assertRaises(CompareAdapterError) as caught:
                await adapter.execute(context())
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.metrics["promptTokens"], 12)
            self.assertTrue(caught.exception.metrics["resourceCleanupVerified"])


if __name__ == "__main__":
    unittest.main()
