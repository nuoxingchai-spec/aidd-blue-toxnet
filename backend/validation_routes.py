"""Strict same-origin scientific-validation contracts; no path-taking routes."""
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from backend.validation_service import ID_PATTERN, MODELS, SCHEMA, ValidationError


class AuditRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    datasetId: StrictStr = Field(pattern=ID_PATTERN)


class EvaluateRequest(AuditRequest):
    protocolId: StrictStr = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$')
    modelIds: list[Literal['A4','B16','C_Morgan']] = Field(min_length=3,max_length=3)
    seed: StrictInt = 42

    @field_validator('modelIds')
    @classmethod
    def models(cls,value):
        if set(value) != set(MODELS): raise ValueError('Fixed three-model comparison required')
        return value

    @field_validator('seed')
    @classmethod
    def fixed_seed(cls,value):
        if value != 42: raise ValueError('Seed is fixed before independent evaluation')
        return value


def create_validation_router(service):
    router=APIRouter(prefix='/api/research/validation',tags=['scientific-validation'])
    def failure(error):
        return JSONResponse({'schema':SCHEMA,'error':{'code':error.code,'message':error.public_message}},status_code=error.status_code)

    @router.get('/datasets')
    async def datasets():
        return service.catalog()

    @router.post('/audit',status_code=202)
    async def audit(payload:AuditRequest):
        try: return await service.submit_async('audit',payload.model_dump())
        except ValidationError as error: return failure(error)

    @router.post('/evaluate',status_code=202)
    async def evaluate(payload:EvaluateRequest):
        try: return await service.submit_async('evaluate',payload.model_dump())
        except ValidationError as error: return failure(error)

    @router.get('/runs')
    async def runs():
        return service.list_runs()

    @router.get('/runs/{run_id}')
    async def get_run(run_id:str):
        try: return service.get(run_id)
        except ValidationError as error: return failure(error)

    @router.delete('/runs/{run_id}')
    async def cancel(run_id:str):
        try: return await service.cancel(run_id)
        except ValidationError as error: return failure(error)

    @router.get('/reports/{run_id}')
    async def report(run_id:str):
        try:
            record=service.get(run_id)
            if record['status'] != 'completed' or record.get('report') is None:
                raise ValidationError('VALIDATION_REPORT_UNAVAILABLE','尚无完成且通过校验的科研报告。')
            return {**record['report'],'schema':SCHEMA,'runId':run_id,'formalAvailability':False,'resultSha256':record['resultSha256']}
        except ValidationError as error: return failure(error)

    return router
