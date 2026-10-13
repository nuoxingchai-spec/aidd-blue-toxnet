"""Independent strict research comparison contracts; no credential fields."""
from __future__ import annotations

from typing import Literal
import re

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator
from backend.chemistry import canonicalize_smiles

CONTRACT_VERSION = 'aidd.multimodel.compare.v1'
MODEL_IDS = ('toxnet-v3-b16', 'txgemma-2b-predict', 'ollama-qwen-local', 'deepseek-api')


class CompareSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    mode: Literal['compare'] = 'compare'
    primaryPredictor: Literal['toxnet-v3-b16'] = 'toxnet-v3-b16'
    specialistPredictor: Literal['txgemma-2b-predict'] = 'txgemma-2b-predict'
    specialistTask: StrictStr | None = Field(default=None, max_length=80)
    primaryReporter: Literal['ollama-qwen-local'] = 'ollama-qwen-local'
    comparisonReporter: Literal['deepseek-api'] | None = 'deepseek-api'
    includeTox21: StrictBool = True
    includeEvidence: StrictBool = False
    evidenceEndpoint: Literal['herg', 'dili', 'ames', 'clintox'] | None = None
    includeAttribution: StrictBool | None = None
    explainEndpoint: Literal['herg', 'dili', 'ames', 'clintox'] | None = None
    ollamaModel: StrictStr | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode='after')
    def compatible_research_steps(self):
        if self.includeAttribution is None:
            self.includeAttribution = self.explainEndpoint is not None
        if self.includeAttribution and self.explainEndpoint is None:
            raise ValueError('Attribution requires one explicit endpoint')
        # Historical v1 clients used explainEndpoint for both options. New
        # clients explicitly send evidenceEndpoint, including null for all.
        if 'evidenceEndpoint' not in self.model_fields_set:
            self.evidenceEndpoint = self.explainEndpoint
        return self

    @field_validator('specialistTask')
    @classmethod
    def task_identifier(cls, value):
        if value is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
            raise ValueError('Task must be an exact registered identifier')
        return value

    @field_validator('ollamaModel')
    @classmethod
    def model_identifier(cls, value):
        if value is not None and (any(ord(c) < 32 for c in value) or value.strip() != value):
            raise ValueError('Invalid installed model identifier')
        return value


class CompareRequest(CompareSettings):
    smiles: StrictStr = Field(min_length=1, max_length=4096)
    question: StrictStr = Field(default='', max_length=1000)
    allowCloudTransfer: StrictBool = False
    persistSensitiveContent: StrictBool = False

    @field_validator('smiles')
    @classmethod
    def valid_smiles(cls, value):
        canonicalize_smiles(value)
        return value.strip()

    @field_validator('question')
    @classmethod
    def valid_question(cls, value):
        if any(ord(c) < 32 and c not in '\n\t' for c in value):
            raise ValueError('Question contains control characters')
        return value.strip()


class ComparePresetRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: StrictStr = Field(min_length=1, max_length=80)
    settings: CompareSettings

    @field_validator('name')
    @classmethod
    def valid_name(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid preset name')
        return value.strip()
