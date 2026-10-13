"""Same-origin, bounded, opt-in four-model research compare routes."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from backend.compare_orchestrator import CompareOrchestrator, CompareRunError
from backend.compare_persistence import ComparePersistenceError
from backend.compare_schemas import CompareRequest, ComparePresetRequest


def build_compare_service(configuration):
    from backend.compare_registry import create_default_registry
    from backend.compare_txgemma import TxGemmaPredictAdapter
    from backend.compare_persistence import default_history_path
    values = configuration.startup_values
    registry = create_default_registry(configuration, env=values, txgemma_adapter=TxGemmaPredictAdapter(env=values))
    return CompareOrchestrator(registry, env=values,
        presets_path=configuration.path.parent / 'data/compare-presets.json',
        persistence_path=default_history_path(configuration.path.parent))


def create_compare_router(service):
    router = APIRouter(prefix='/api/research', tags=['multi-model-compare'])
    def failure(error):
        return JSONResponse({'error': {'code': error.code, 'message': error.public_message}}, status_code=getattr(error, 'status_code', 503))

    @router.get('/models')
    async def models():
        try:
            return await service.describe()
        except (asyncio.TimeoutError, TimeoutError):
            return JSONResponse({'error': {'code': 'COMPARE_STATUS_TIMEOUT', 'message': '真实模型状态检查超时。'}}, status_code=504)
        except Exception:
            return JSONResponse({'error': {'code': 'COMPARE_STATUS_UNAVAILABLE', 'message': '模型状态不可用；请检查本机配置和资源清理。'}}, status_code=503)

    @router.post('/compare/runs', status_code=202)
    async def create_run(payload: CompareRequest, request: Request):
        try:
            return service.submit(payload, request_id=request.state.request_id)
        except CompareRunError as error:
            return failure(error)

    @router.get('/compare/runs')
    async def list_runs(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0, le=1000000)):
        try:
            return service.list_runs(limit=limit, offset=offset)
        except ComparePersistenceError as error:
            return failure(error)

    @router.get('/compare/runs/{run_id}')
    async def get_run(run_id: str):
        try:
            return service.get(run_id)
        except (CompareRunError, ComparePersistenceError) as error:
            return failure(error)

    @router.delete('/compare/runs/{run_id}')
    async def cancel_run(run_id: str):
        try:
            return await service.cancel(run_id)
        except (CompareRunError, ComparePersistenceError) as error:
            return failure(error)

    @router.get('/compare/presets')
    async def presets():
        return service.list_presets()

    @router.post('/compare/presets', status_code=201)
    async def save_preset(payload: ComparePresetRequest):
        try:
            return service.save_preset(payload)
        except CompareRunError as error:
            return failure(error)

    @router.patch('/compare/presets/{preset_id}')
    async def update_preset(preset_id: str, payload: ComparePresetRequest):
        try:
            return service.update_preset(preset_id, payload)
        except CompareRunError as error:
            return failure(error)

    @router.delete('/compare/presets/{preset_id}')
    async def delete_preset(preset_id: str):
        try:
            return service.delete_preset(preset_id)
        except CompareRunError as error:
            return failure(error)
    return router
