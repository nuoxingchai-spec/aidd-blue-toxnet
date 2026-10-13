"""Serial local-model scheduling with cleanup quarantine and real measurements."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import time


class ResourceUnavailable(Exception):
    pass


class CompareResourceManager:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.blocked_reason = None
        self.active_model = None

    def public(self):
        return {'localModelConcurrency': 1, 'gpuScheduling': 'serial-owned-model-stages',
                'activeModel': self.active_model, 'blocked': bool(self.blocked_reason),
                'blockedReason': self.blocked_reason,
                'externalApplicationsManaged': False,
                'capacityExtrapolatedFromMolformer': False}

    @asynccontextmanager
    async def slot(self, model_id, adapter=None):
        started = time.perf_counter()
        async with self.lock:
            if self.blocked_reason:
                raise ResourceUnavailable(self.blocked_reason)
            self.active_model = model_id
            metadata = {'queueWaitMs': round((time.perf_counter() - started) * 1000, 3)}
            try:
                yield metadata
            finally:
                # Closing an HTTP connection does not prove Ollama stopped its
                # GPU work. The adapter must confirm cleanup or block reuse.
                if adapter is not None and getattr(adapter, 'cleanup_safe', True) is False:
                    self.blocked_reason = 'LOCAL_MODEL_CLEANUP_UNCONFIRMED; verify daemon cleanup and restart compare service'
                self.active_model = None
