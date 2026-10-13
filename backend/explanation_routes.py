"""Strict same-origin research explanation contract."""
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from starlette.concurrency import run_in_threadpool
from backend.model_client import ModelAPIError


class ExplainRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    smiles: StrictStr = Field(min_length=1, max_length=4096)
    model: Literal['v2', 'v3'] = 'v2'
    endpoint: StrictStr = Field(default='herg', min_length=1, max_length=64)
    steps: StrictInt = Field(default=32, ge=8, le=128)
    includeLanguage: StrictBool = False
    languageProvider: StrictStr | None = Field(default=None, max_length=64)
    expectedWeightsSha256: StrictStr | None = Field(default=None, pattern='^[0-9a-f]{64}$')
    expectedStructureId: StrictStr | None = Field(default=None, pattern='^[0-9a-f]{64}$')


def create_explanation_router(service):
    router = APIRouter(prefix='/api/research')

    @router.post('/explain')
    async def explain(payload: ExplainRequest, request: Request):
        try:
            result = await run_in_threadpool(service.explain, payload.smiles, endpoint=payload.endpoint, model=payload.model,
                    include_language=payload.includeLanguage, language_provider=payload.languageProvider, steps=payload.steps,
                    expected_weights=payload.expectedWeightsSha256, expected_structure=payload.expectedStructureId)
            result['requestId'] = getattr(request.state, 'request_id', uuid4().hex)
            return result
        except ModelAPIError as e:
            return JSONResponse({'status': 'unavailable', 'error': {'code': e.code, 'message': e.public_message,
                                  'requestId': getattr(request.state, 'request_id', uuid4().hex)}}, status_code=e.status_code)
        except ValueError as e:
            return JSONResponse({'status': 'invalid-input', 'error': {'code': 'INVALID_SMILES', 'message': str(e),
                                  'requestId': getattr(request.state, 'request_id', uuid4().hex)}}, status_code=422)
        except Exception:
            return JSONResponse({'status': 'unavailable', 'error': {'code': 'EXPLANATION_UNAVAILABLE',
                                  'message': '真实解释链路不可用；不返回模拟报告。', 'requestId': getattr(request.state, 'request_id', uuid4().hex)}}, status_code=503)
    return router
