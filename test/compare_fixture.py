"""Explicit MOCK adapters: synthetic software fixtures, never model evidence.

For browser QA only: python -m test.compare_fixture (loopback port 4184).
No model files, keys, Ollama, network APIs or paid calls are used.
"""
import asyncio
import copy

from backend.compare_adapters import CompareAdapterError
from backend.compare_orchestrator import CompareOrchestrator
from backend.compare_registry import CompareRegistry
from backend.compare_schemas import MODEL_IDS
from backend.compare_tasks import parse_task_output, TXGEMMA_REVISION
from backend.multitask_model import ALL_TASKS


def fixture_prediction(context):
    return {'canonicalSmiles': context['canonicalSmiles'], 'structureId': context['structureId'],
            'endpoints': [{'id': task, 'score': (i + 1) / 20, 'label': task} for i, task in enumerate(ALL_TASKS)],
            'provenance': {'modelVersion': 'MOCK-software-fixture-NOT-REAL', 'weightsSha256': 'f' * 64,
                           'taskContractVersion': 'MOCK-only', 'formalAvailability': False}}


def fixture_report(snapshot):
    return {'schema': 'aidd.compare.report.v1', 'snapshotSha256': snapshot['snapshotSha256'],
            'sectionOrder': [s['id'] for s in snapshot['sections']],
            'claimOrder': {s['id']: s['claimIds'] for s in snapshot['sections']},
            'factChecks': [{'claimId': f['id'], 'value': copy.deepcopy(f['value']), 'sourceIds': f['sourceIds']} for f in snapshot['facts']],
            'commentary': [{'sectionId': 'limits', 'text': 'MOCK 测试文字，不是科学机制或真实模型报告。'}]}


class FixtureAdapter:
    def __init__(self, identifier, delay=0, error=None):
        self.id, self.delay, self.error = identifier, delay, error
        self.calls, self.cancelled, self.cleanup_safe = [], False, True
        self.last_partial = None

    async def status(self):
        return {'id': self.id, 'available': True, 'configured': True, 'actualModelId': 'MOCK-not-real-' + self.id,
                'revision': 'MOCK', 'device': 'mock-cpu', 'licenseStatus': 'test-only',
                'reason': 'Mock 软件测试夹具，没有真实模型、实验结果或付费调用。',
                'installedModels': [{'id': 'MOCK-qwen', 'name': 'MOCK-qwen', 'digest': 'MOCK'}],
                'selectedDefaultModel': 'MOCK-qwen'}

    async def execute(self, context):
        self.calls.append(copy.deepcopy(context))
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error:
            raise CompareAdapterError(self.error, 'MOCK failure')
        if self.id == MODEL_IDS[0]:
            data = {'prediction': fixture_prediction(context), 'evidence': {'status': 'insufficient-evidence', 'results': []}}
        elif self.id == MODEL_IDS[1]:
            data = {'tasks': [parse_task_output(context['request']['specialistTask'], 'B')], 'structureId': context['structureId']}
        else:
            if context['snapshot']['structure']['canonicalSmiles'] == 'N#N' and self.id == MODEL_IDS[2]:
                raise CompareAdapterError('MOCK_REPORT_FAILURE', 'MOCK reporter failure to test partial result retention')
            data = {'rawReport': fixture_report(context['snapshot'])}
        return {'status': 'completed', 'data': data, 'provenance': {'testFixture': True,
                'revision': TXGEMMA_REVISION if self.id == MODEL_IDS[1] else 'MOCK', 'formalAvailability': False},
                'metrics': {'cost': None, 'gpu': {'measured': False}, 'promptTokens': None, 'completionTokens': None}}


class BrowserFixtureService(CompareOrchestrator):
    async def describe(self):
        result = await super().describe()
        result['testFixture'] = True
        return result


def fixture_service(delay=0, **kwargs):
    adapters = {identifier: FixtureAdapter(identifier, delay=delay) for identifier in MODEL_IDS}
    return BrowserFixtureService(CompareRegistry(adapters), **kwargs)


if __name__ == '__main__':
    import uvicorn
    from backend.main import create_app
    uvicorn.run(create_app(env={}, compare_service=fixture_service(delay=1)), host='127.0.0.1', port=4184)
