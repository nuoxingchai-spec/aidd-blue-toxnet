"""Deterministic cancellation regressions; no model/GPU/service is used."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from backend.compare_adapters import B16Adapter, OllamaQwenAdapter
from backend.compare_resources import CompareResourceManager, ResourceUnavailable
from test.test_compare_adapters import context, tags


class CleanupRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_shield_only_reproduces_false_safe_and_unfinished_cleanup(self):
        """A negative control recreates the exact pre-fix assignment ordering."""
        generation_started, cleanup_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        legacy = SimpleNamespace(cleanup_safe=True, cleanup_task=None)
        async def cleanup():
            cleanup_started.set()
            await release.wait()
            return False
        async def old_execute():
            try:
                generation_started.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                legacy.cleanup_task = asyncio.create_task(cleanup())
                legacy.cleanup_safe = await asyncio.shield(legacy.cleanup_task)
                raise
        operation = asyncio.create_task(old_execute())
        await generation_started.wait()
        operation.cancel()
        await cleanup_started.wait()
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertTrue(legacy.cleanup_safe)
        self.assertFalse(legacy.cleanup_task.done())
        release.set()
        await legacy.cleanup_task

    async def _exercise_ollama_double_cancel(self, *, confirm):
        generation_started, cleanup_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        entered_next = asyncio.Event()
        unload_calls = 0
        async def handler(request):
            nonlocal unload_calls
            if request.url.path == "/api/chat":
                generation_started.set()
                await asyncio.Event().wait()
            if request.url.path == "/api/generate":
                unload_calls += 1
                cleanup_started.set()
                await release.wait()
                return httpx.Response(200, json={"done": True})
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": []} if confirm else tags())
            return httpx.Response(200, json=tags())
        adapter = OllamaQwenAdapter(transport=httpx.MockTransport(handler))
        manager = CompareResourceManager()
        async def first():
            async with manager.slot("ollama", adapter):
                await adapter.execute(context())
        async def next_model():
            async with manager.slot("b16"):
                entered_next.set()
        operation = asyncio.create_task(first())
        await generation_started.wait()
        self.assertFalse(adapter.cleanup_safe)
        operation.cancel()
        await cleanup_started.wait()
        next_operation = asyncio.create_task(next_model())
        operation.cancel()
        await asyncio.sleep(0)
        self.assertFalse(operation.done())
        self.assertFalse(entered_next.is_set())
        self.assertFalse(adapter.cleanup_safe)
        self.assertEqual(adapter.cleanup_status()["state"], "cleaning")
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertEqual(unload_calls, 1)
        await adapter.close()
        await adapter.close()
        self.assertEqual(unload_calls, 1)
        if confirm:
            await next_operation
            self.assertTrue(entered_next.is_set())
            self.assertEqual(adapter.cleanup_status()["state"], "released")
            self.assertFalse(manager.public()["blocked"])
        else:
            with self.assertRaises(ResourceUnavailable):
                await next_operation
            self.assertFalse(entered_next.is_set())
            self.assertEqual(adapter.cleanup_status()["state"], "quarantined")
            self.assertTrue(manager.public()["blocked"])

    async def test_repeat_cancel_waits_one_cleanup_and_keeps_resource_lock(self):
        await self._exercise_ollama_double_cancel(confirm=True)

    async def test_repeat_cancel_unconfirmed_cleanup_quarantines_next_model(self):
        await self._exercise_ollama_double_cancel(confirm=False)

    async def test_b16_repeat_cancel_during_spawn_waits_exact_child_exit(self):
        spawning_started, release_spawn, waiting_exit, release_exit = [asyncio.Event() for _ in range(4)]
        child = SimpleNamespace(returncode=None, killed=0)
        def kill():
            child.killed += 1
        async def wait():
            waiting_exit.set()
            await release_exit.wait()
            child.returncode = -9
            return -9
        child.kill, child.wait = kill, wait
        async def spawn(*args, **kwargs):
            spawning_started.set()
            await release_spawn.wait()
            return child
        adapter = B16Adapter(model_dir="test-only", encoder_dir="test-only", enabled=True)
        manager = CompareResourceManager()
        async def execute():
            async with manager.slot("b16", adapter):
                await adapter.execute(context())
        with patch("backend.compare_adapters.asyncio.create_subprocess_exec", spawn):
            task = asyncio.create_task(execute())
            await spawning_started.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertFalse(adapter.cleanup_safe)
            release_spawn.set()
            await waiting_exit.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(child.killed, 1)
            self.assertEqual(manager.active_model, "b16")
            release_exit.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(adapter.cleanup_safe)
        self.assertIsNone(manager.active_model)
        self.assertEqual(adapter.cleanup_status()["state"], "released")


if __name__ == "__main__":
    unittest.main()
