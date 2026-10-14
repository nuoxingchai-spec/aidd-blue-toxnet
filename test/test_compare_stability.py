"""Cross-module software fixtures: restoration never re-executes any model."""
import asyncio
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from backend.chemistry import analyze_chemistry
from backend.compare_adapters import DeepSeekReportAdapter
from backend.compare_logging import diagnostic
from backend.compare_orchestrator import CompareOrchestrator, CompareRunError
from backend.compare_persistence import CompareRunStore, ComparePersistenceError
from backend.compare_registry import CompareRegistry
from backend.compare_resources import wait_tracked_cleanup
from backend.compare_schemas import CompareRequest, ComparePresetRequest, MODEL_IDS
from backend.compare_worker import WORKER_SCHEMA, run_worker
from backend.main import create_app
from backend.model_client import ModelConfig
from test.compare_fixture import fixture_service, fixture_prediction, fixture_report
from test.test_api import FakeLocalModel


class StabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = fixture_service()

    async def asyncTearDown(self):
        await self.service.close()

    async def test_repeated_cancel_is_one_owned_stop_operation(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        adapter = self.service.adapters[MODEL_IDS[0]]
        cancellations = 0
        async def execute(context):
            nonlocal cancellations
            entered.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancellations += 1
                adapter.cleanup_safe = False
                cleaning.set()
                await wait_tracked_cleanup(asyncio.create_task(release.wait()))
                adapter.cleanup_safe = True
                raise
        adapter.execute = execute
        ack = self.service.submit(CompareRequest(smiles='CCO'))
        await entered.wait()
        first = asyncio.create_task(self.service.cancel(ack['runId']))
        await cleaning.wait()
        second = asyncio.create_task(self.service.cancel(ack['runId']))
        await asyncio.sleep(.005)
        self.assertTrue(self.service.resources.lock.locked())
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        release.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(cancellations, 1)
        self.assertEqual([r['status'] for r in results], ['cancelled', 'cancelled'])
        self.assertFalse(self.service.resources.lock.locked())
        self.assertTrue(self.service.cancel_tasks[ack['runId']].done())

    async def test_global_timeout_and_manual_cancel_wait_for_cleanup(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        adapter = self.service.adapters[MODEL_IDS[0]]
        self.service.run_seconds = .02
        async def execute(context):
            entered.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                adapter.cleanup_safe = False
                cleaning.set()
                try:
                    await wait_tracked_cleanup(asyncio.create_task(release.wait()))
                finally:
                    adapter.cleanup_safe = True
                raise
        adapter.execute = execute
        ack = self.service.submit(CompareRequest(smiles='CCO'))
        await entered.wait()
        await asyncio.wait_for(cleaning.wait(), 1)
        cancel = asyncio.create_task(self.service.cancel(ack['runId']))
        await asyncio.sleep(.005)
        self.assertTrue(self.service.resources.lock.locked())
        release.set()
        await asyncio.wait_for(cancel, 1)
        self.assertTrue(self.service.tasks[ack['runId']].done())
        self.assertFalse(self.service.resources.lock.locked())

    async def test_shutdown_drains_active_task_and_rejects_new_submission(self):
        adapter = self.service.adapters[MODEL_IDS[0]]
        adapter.delay = 60
        ack = self.service.submit(CompareRequest(smiles='CCO'))
        while not adapter.calls:
            await asyncio.sleep(.001)
        await asyncio.gather(self.service.close(), self.service.close())
        self.assertTrue(self.service.tasks[ack['runId']].done())
        self.assertTrue(adapter.cancelled)
        self.assertIsNone(self.service.resources.active_model)
        with self.assertRaises(CompareRunError): self.service.submit(CompareRequest(smiles='CCO'))

    async def test_restart_recovers_completed_minimum_history_without_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite3'
            first = fixture_service(persistence_path=path)
            await first.start()
            ack = first.submit(CompareRequest(smiles='C/C=C\\C', question='private research text'))
            await first.tasks[ack['runId']]
            await first.close()
            second = fixture_service(persistence_path=path)
            await second.start()
            try:
                record = second.get(ack['runId'])
                self.assertTrue(record['restored'])
                self.assertFalse(record['recovery']['contentAvailable'])
                self.assertNotIn('canonicalSmiles', record['structure'])
                self.assertNotIn('question', record['request'])
                self.assertEqual(len(record['models'][MODEL_IDS[0]]['data']['prediction']['endpoints']), 16)
                self.assertTrue(record['models'][MODEL_IDS[0]]['provenance']['testFixture'])
                self.assertFalse(record['request']['allowCloudTransfer'])
                self.assertTrue(all(not adapter.calls for adapter in second.adapters.values()))
                self.assertEqual(second.list_runs()['total'], 1)
            finally:
                await second.close()

    async def test_full_content_requires_consent_and_preserves_stereo_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite3'
            first = fixture_service(persistence_path=path)
            await first.start()
            ack = first.submit(CompareRequest(smiles='C/C=C\\C', persistSensitiveContent=True))
            await first.tasks[ack['runId']]
            original = first.get(ack['runId'])
            await first.close()
            second = fixture_service(persistence_path=path)
            await second.start()
            try:
                record = second.get(ack['runId'])
                self.assertTrue(record['contentSaved'])
                self.assertEqual(record['structure']['canonicalSmiles'], original['structure']['canonicalSmiles'])
                self.assertEqual(record['snapshot'], original['snapshot'])
                self.assertEqual(record['reports'][MODEL_IDS[2]], original['reports'][MODEL_IDS[2]])
                self.assertTrue(all(not adapter.calls for adapter in second.adapters.values()))
            finally:
                await second.close()

    async def test_interrupted_history_preserves_prediction_and_never_calls_cloud(self):
        ack = self.service.submit(CompareRequest(smiles='CCO'))
        await self.service.tasks[ack['runId']]
        record = self.service.get(ack['runId'])
        record['status'] = 'running'
        record['models'][MODEL_IDS[2]] = {'status': 'running', 'data': None, 'metrics': {}, 'provenance': {}, 'error': None}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite3'
            store = CompareRunStore(path)
            store.upsert(record, 0)
            store.close()
            second = fixture_service(persistence_path=path)
            await second.start()
            try:
                restored = second.get(ack['runId'])
                self.assertEqual(restored['status'], 'partial')
                self.assertEqual(restored['error']['code'], 'INTERRUPTED')
                self.assertEqual(restored['models'][MODEL_IDS[0]]['status'], 'completed')
                self.assertEqual(restored['models'][MODEL_IDS[2]]['error']['code'], 'INTERRUPTED')
                self.assertTrue(all(not adapter.calls for adapter in second.adapters.values()))
            finally:
                await second.close()

    async def test_invalid_cloud_report_usage_survives_orchestration_and_storage(self):
        config = ModelConfig(provider_id='deepseek', api_url='https://api.deepseek.com/chat/completions',
                             api_key='fixture-only-not-real-key', model_name='fixture-model', model_version='fixture-v1', api_format='openai')
        def respond(request):
            return httpx.Response(200, json={'model': 'fixture-model', 'choices': [{'finish_reason': 'stop',
                 'message': {'role': 'assistant', 'content': 'invalid inner JSON'}}],
                 'usage': {'prompt_tokens': 8, 'completion_tokens': 3, 'total_tokens': 11}})
        self.service.adapters[MODEL_IDS[3]] = DeepSeekReportAdapter(config=config, transport=httpx.MockTransport(respond))
        ack = self.service.submit(CompareRequest(smiles='CCO', allowCloudTransfer=True))
        await self.service.tasks[ack['runId']]
        record = self.service.get(ack['runId'])
        item = record['models'][MODEL_IDS[3]]
        self.assertEqual(item['status'], 'failed')
        self.assertIsNone(item['data'])
        self.assertEqual(item['metrics']['totalTokens'], 11)
        self.assertTrue(record['cloudTransfer']['requestAttempted'])
        self.assertFalse(record['cloudTransfer']['executed'])
        self.assertIsNone(item['metrics']['cost'])
        self.assertEqual(item['metrics']['httpRequestState'], 'response-received')
        stored = self.service.store.get(ack['runId'])
        self.assertEqual(stored['models'][MODEL_IDS[3]]['metrics']['totalTokens'], 11)
        self.assertNotIn('fixture-only-not-real-key', str(stored))

    async def test_valid_report_json_with_wrong_fact_retains_usage_and_failure_type(self):
        adapter = self.service.adapters[MODEL_IDS[3]]
        async def wrong_fact(context):
            raw = fixture_report(context['snapshot'])
            raw['factChecks'][0]['claimId'] = 'invented-fact'
            return {'status': 'completed', 'data': {'rawReport': raw},
                'metrics': {'promptTokens': 8, 'completionTokens': 3, 'totalTokens': 11,
                            'httpRequestState': 'response-received', 'cost': None},
                'provenance': {'requestAttempted': True, 'testFixture': True}}
        adapter.execute = wrong_fact
        ack = self.service.submit(CompareRequest(smiles='CCO', allowCloudTransfer=True))
        await self.service.tasks[ack['runId']]
        item = self.service.get(ack['runId'])['models'][MODEL_IDS[3]]
        self.assertEqual(item['status'], 'failed')
        self.assertEqual(item['error']['code'], 'COMPARE_REPORT_FACT_CHECK_FAILED')
        self.assertEqual(item['metrics']['reportValidation'], 'fact-check-failed')
        self.assertEqual(item['metrics']['totalTokens'], 11)
        self.assertIsNone(item['data'])
        self.assertEqual(self.service.store.get(ack['runId'])['models'][MODEL_IDS[3]]['metrics']['totalTokens'], 11)

    async def test_explicit_multi_worker_configuration_rejected(self):
        service = fixture_service(env={'WEB_CONCURRENCY': '2'})
        with self.assertRaises(CompareRunError): await service.start()
        await service.close()

    async def test_interrupted_local_stage_stays_quarantined_after_restart(self):
        ack = self.service.submit(CompareRequest(smiles='CCO'))
        await self.service.tasks[ack['runId']]
        record = self.service.get(ack['runId'])
        record['status'] = 'running'
        record['models'][MODEL_IDS[2]]['status'] = 'running'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite3'
            store = CompareRunStore(path)
            store.upsert(record, 0)  # Expired history still cannot prove release.
            store.close()
            service = fixture_service(persistence_path=path)
            await service.start()
            self.assertTrue(service.resources.blocked_reason)
            self.assertEqual(service.store.get_quarantine(), 'INTERRUPTED_LOCAL_EXECUTION')
            with self.assertRaises(Exception): await service.describe()
            self.assertTrue(all(not adapter.calls for adapter in service.adapters.values()))
            await service.close()
            reopened = fixture_service(persistence_path=path)
            await reopened.start()
            self.assertTrue(reopened.resources.blocked_reason)
            await reopened.close()

    async def test_probe_and_shutdown_drain_other_adapters_if_one_cleanup_raises(self):
        for closing in (False, True):
            service = fixture_service()
            drained = []
            async def failed():
                drained.append('first')
                raise RuntimeError('fixture failure')
            async def clean():
                drained.append('second')
            operation = 'close' if closing else 'wait_cleanup'
            setattr(service.adapters[MODEL_IDS[0]], operation, failed)
            setattr(service.adapters[MODEL_IDS[2]], operation, clean)
            if closing:
                await service.close()
            else:
                await service.describe()
                self.assertEqual(service.store.get_quarantine(), 'LOCAL_MODEL_CLEANUP_UNCONFIRMED')
                await service.close()
            self.assertEqual(drained[:2], ['first', 'second'])
            self.assertTrue(service.resources.blocked_reason)


class ResearchStepsTests(unittest.TestCase):
    def test_legacy_contract_and_explicit_independent_choices(self):
        old = CompareRequest(smiles='CCO', includeEvidence=True, explainEndpoint='dili')
        self.assertTrue(old.includeAttribution)
        self.assertEqual(old.evidenceEndpoint, 'dili')
        new = CompareRequest(smiles='CCO', includeEvidence=True, evidenceEndpoint='herg', includeAttribution=False, explainEndpoint='dili')
        self.assertFalse(new.includeAttribution)
        self.assertEqual(new.evidenceEndpoint, 'herg')

    def test_evidence_can_run_without_attribution_and_with_different_endpoint(self):
        chemistry = analyze_chemistry('CCO')
        client = Mock(device='cpu')
        prediction = fixture_prediction(chemistry)
        client.predict_all.return_value = prediction
        search = Mock()
        search.search.return_value = {'status': 'available', 'results': []}
        attribution = Mock()
        attribution.explain.return_value = {'status': 'available', 'provenance': prediction['provenance']}
        payload = {'schema': WORKER_SCHEMA, 'action': 'execute', 'device': 'cpu', 'enabled': True,
                   'canonicalSmiles': chemistry['canonicalSmiles'], 'structureId': chemistry['structureId'],
                   'includeEvidence': True, 'evidenceEndpoint': 'herg', 'includeAttribution': False,
                   'explainEndpoint': 'dili', 'evidenceIndex': 'fixture-only'}
        with patch('backend.compare_worker.MultitaskModelClient', return_value=client), patch('backend.compare_worker._require_b16', return_value={}), patch('backend.compare_worker._public_info', return_value={}), patch('backend.evidence_search.EvidenceSearch', return_value=search), patch('backend.attribution.AttributionEngine', return_value=attribution) as engine:
            result = run_worker(payload)
            engine.assert_not_called()
            search.search.assert_called_with('CCO', endpoint='herg', limit=3)
            self.assertEqual(result['result']['data']['attribution']['status'], 'not-requested')
            payload['includeAttribution'] = True
            result = run_worker(payload)
            attribution.explain.assert_called_with('CCO', endpoint='dili', model='multitask')
            self.assertEqual(result['result']['provenance']['researchSteps']['evidenceEndpoint'], 'herg')

    def test_structured_logs_allowlist_excludes_private_strings(self):
        with self.assertLogs('aidd.compare', level='INFO') as logs:
            value = diagnostic(stage='model-finished', request_id='Bearer sensitive-token', run_id='a'*32,
                               cleanup_state={'state': 'quarantined', 'apiKey': 'private-secret'}, duration_ms=1)
        self.assertIsNone(value['requestId'])
        self.assertNotIn('sensitive-token', str(logs.output))
        self.assertNotIn('private-secret', str(logs.output))


class StabilityAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = fixture_service()
        self.context = TestClient(create_app(registry={'local': FakeLocalModel()}, env={},
            config_path=Path(self.directory.name)/'.env', compare_service=self.service))
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.directory.cleanup()

    def test_history_pagination_and_preset_update_delete_conflicts(self):
        self.client.post('/api/research/compare/runs', json={'smiles':'CCO'})
        result = self.client.get('/api/research/compare/runs?limit=1&offset=0')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['total'], 1)
        self.assertNotIn('canonicalSmiles', str(result.json()))
        self.assertEqual(self.client.get('/api/research/compare/runs?limit=0').status_code, 422)
        row = self.client.post('/api/research/compare/presets', json={'name':'first','settings':{}}).json()
        self.assertIn('updatedAt', row)
        self.assertEqual(self.client.post('/api/research/compare/presets', json={'name':'FIRST','settings':{}}).status_code, 409)
        updated = self.client.patch('/api/research/compare/presets/'+row['id'], json={'name':'renamed','settings':{'evidenceEndpoint':'herg','includeEvidence':True}})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()['createdAt'], row['createdAt'])
        self.assertEqual(self.client.delete('/api/research/compare/presets/'+row['id']).json()['deleted'], True)
        self.assertEqual(self.client.get('/api/research/compare/presets').json()['presets'], [])

    def test_actual_chunked_body_limit_and_patch_origin(self):
        response = self.client.post('/api/research/compare/runs', content=iter([b'x'*10000,b'y'*10000]), headers={'content-type':'application/json'})
        self.assertEqual(response.status_code, 413)
        response = self.client.post('/api/research/compare/runs', content=iter([b'{"smiles":',b'"CCO"}']), headers={'content-type':'application/json'})
        self.assertEqual(response.status_code, 202)
        response = self.client.patch('/api/research/compare/presets/unknown', json={'name':'x','settings':{}}, headers={'origin':'https://evil.example'})
        self.assertEqual(response.status_code, 403)

    def test_history_storage_failure_is_safe_structured_503(self):
        error = ComparePersistenceError('COMPARE_PERSISTENCE_READ_FAILED', '本机记录读取失败。')
        with patch.object(self.service.store, 'prune', side_effect=error):
            for method, path in [('get', '/api/research/compare/runs'),
                                 ('get', '/api/research/compare/runs/unknown'),
                                 ('delete', '/api/research/compare/runs/unknown')]:
                result = getattr(self.client, method)(path)
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.json()['error']['code'], 'COMPARE_PERSISTENCE_READ_FAILED')
