"""Real deployment regression: bounded room for prompt plus structured report."""
import json
import unittest
import httpx
from backend.compare_adapters import OllamaQwenAdapter, CompareAdapterError
from test.test_compare_adapters import context, chat, tags


class GenerationPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_budget_sent_to_real_request_shape_and_provenance(self):
        requests = []
        def handler(request):
            if request.url.path == '/api/chat':
                requests.append(json.loads(request.content))
                return httpx.Response(200, json=chat())
            return httpx.Response(200, json={'models': []} if request.url.path == '/api/ps' else tags())
        adapter = OllamaQwenAdapter(transport=httpx.MockTransport(handler))
        result = await adapter.execute(context())
        self.assertEqual(requests[0]['options'], {'temperature': 0, 'num_ctx': 8192, 'num_predict': 4096})
        self.assertEqual(result['provenance']['generationOptions'], {'num_ctx': 8192, 'num_predict': 4096})
        self.assertEqual(requests[0]['messages'], context()['messages'])
        self.assertTrue(result['metrics']['resourceCleanupVerified'])

    async def test_env_overrides_are_validated_without_automatic_capacity_claim(self):
        adapter = OllamaQwenAdapter.from_env({'TOXNET_OLLAMA_CONTEXT_TOKENS': '12288', 'TOXNET_OLLAMA_OUTPUT_TOKENS': '4096'})
        self.assertEqual(adapter._generation_options()['num_ctx'], 12288)
        for context_count, output in [(True, 4096), ('-1', 4096), (32768, 4096), (8192, 8192), (8192, 'invalid')]:
            bad = OllamaQwenAdapter(context_tokens=context_count, output_tokens=output)
            with self.assertRaises(CompareAdapterError): bad._generation_options()

    async def test_bad_config_refuses_generation_before_any_http_request(self):
        calls = []
        adapter = OllamaQwenAdapter(context_tokens=2048, transport=httpx.MockTransport(lambda r: calls.append(r)))
        self.assertEqual((await adapter.status())['errorCode'], 'OLLAMA_INVALID_GENERATION_CONFIG')
        with self.assertRaises(CompareAdapterError): await adapter.execute(context())
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
