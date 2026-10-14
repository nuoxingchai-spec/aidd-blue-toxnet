"""Explicit runtime routing preserves the original official model contract."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from backend.compare_routes import build_compare_service
from backend.compare_txgemma import TxGemmaPredictAdapter
from backend.compare_ollama_txgemma import OllamaTxGemmaAdapter
from backend.compare_adapters import UnavailableAdapter


class RuntimeFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_and_opt_in_quantized_runtime_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            for runtime, expected in [(None, TxGemmaPredictAdapter), ('transformers', TxGemmaPredictAdapter),
                                      ('ollama', OllamaTxGemmaAdapter), ('unknown', UnavailableAdapter)]:
                values = {} if runtime is None else {'AIDD_TXGEMMA_RUNTIME': runtime}
                config = SimpleNamespace(startup_values=values, path=Path(directory)/'.env')
                service = build_compare_service(config)
                self.assertIsInstance(service.adapters['txgemma-2b-predict'], expected)
                self.assertIsNotNone(service.adapters['toxnet-v3-b16'])
                await service.close()


if __name__ == '__main__':
    unittest.main()
