"""Separate research API. No automatic promotion into the four-endpoint UI."""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from starlette.concurrency import run_in_threadpool

from backend.model_client import ModelAPIError
from backend.multitask_model import MultitaskModelClient


class Tox21ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    smiles: StrictStr = Field(min_length=1, max_length=4096)


def create_research_router(*, env=None, client=None):
    router = APIRouter(prefix="/api/research", tags=["research"])
    model = client if client is not None else MultitaskModelClient.from_env(env)

    @router.get("/tox21/status")
    async def status():
        return await run_in_threadpool(model.get_status)

    @router.post("/tox21")
    async def predict(payload: Tox21ResearchRequest, request: Request):
        try:
            result = await run_in_threadpool(model.predict_tox21, payload.smiles)
            result["requestId"] = getattr(request.state, "request_id", uuid4().hex)
            return result
        except ModelAPIError as error:
            return JSONResponse({"status": "unavailable" if error.status_code == 503 else "invalid-input",
                                 "error": {"code": error.code, "message": error.public_message,
                                           "requestId": getattr(request.state, "request_id", uuid4().hex)}}, status_code=error.status_code)

    return router
