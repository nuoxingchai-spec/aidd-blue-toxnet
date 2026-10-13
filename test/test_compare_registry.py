"""Role registry is not evidence of actual model availability."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from backend.compare_adapters import UnavailableAdapter
from backend.compare_registry import CompareRegistry, MODEL_SPECS


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_four_ids_required(self):
        with self.assertRaises(ValueError):
            CompareRegistry({"toxnet-v3-b16": UnavailableAdapter("toxnet-v3-b16")})

    async def test_static_roles_do_not_imply_availability(self):
        registry = CompareRegistry({key: UnavailableAdapter(key) for key in MODEL_SPECS})
        records = await registry.describe()
        self.assertEqual([record["id"] for record in records], list(MODEL_SPECS))
        self.assertTrue(all(record["available"] is False and record["formalAvailability"] is False for record in records))
        self.assertEqual(records[0]["capabilities"], ["toxicity-prediction", "tox21-pathways"])
        self.assertEqual(records[2]["type"], "report-generator")

    async def test_one_check_failure_does_not_replace_other_model_statuses(self):
        adapters = {key: UnavailableAdapter(key) for key in MODEL_SPECS}
        adapters["toxnet-v3-b16"].status = AsyncMock(return_value={"available": True, "configured": True, "actualModelId": "verified-fixture-only"})
        adapters["ollama-qwen-local"].status = AsyncMock(side_effect=RuntimeError("private server details"))
        records = await CompareRegistry(adapters).describe()
        self.assertTrue(records[0]["available"])
        self.assertFalse(records[2]["available"])
        self.assertNotIn("private server", str(records))

    async def test_defaults_keep_ollama_separate_from_qwen_api(self):
        registry = CompareRegistry.defaults(env={})
        self.assertEqual(registry.adapters["ollama-qwen-local"].base_url, "http://127.0.0.1:11434")
        self.assertEqual((await registry.adapters["deepseek-api"].status())["errorCode"], "DEEPSEEK_UNCONFIGURED")


if __name__ == "__main__":
    unittest.main()
