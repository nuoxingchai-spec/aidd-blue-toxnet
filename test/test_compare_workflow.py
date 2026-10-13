"""CPU-only contracts, independence, security and lifecycle; all models MOCK."""
import asyncio
import copy
from pathlib import Path
import tempfile
import time
import unittest

from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.chemistry import analyze_chemistry
from backend.compare_orchestrator import CompareRunError
from backend.compare_reporting import build_snapshot, validate_b16, validate_report, validate_specialist
from backend.compare_resources import CompareResourceManager, ResourceUnavailable
from backend.compare_schemas import CompareRequest, ComparePresetRequest, MODEL_IDS
from backend.compare_tasks import parse_task_output, TXGEMMA_REVISION
from backend.main import create_app
from test.compare_fixture import fixture_service, fixture_prediction, fixture_report
from test.test_api import FakeLocalModel


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.chemistry = analyze_chemistry('OCC')
        self.structure = {k: self.chemistry[k] for k in ('canonicalSmiles', 'structureId')}
        self.models = {key: {'status': 'unavailable', 'data': None} for key in MODEL_IDS}
        self.models[MODEL_IDS[0]] = {'status': 'completed', 'data': {'prediction': fixture_prediction(self.structure)}}
        self.snapshot = build_snapshot(self.structure, self.chemistry, self.models, question='herg private-path /secret raw data')

    def test_snapshot_contains_scores_semantics_but_no_question_rows_paths(self):
        self.assertEqual(self.snapshot['questionScope'], 'herg')
        self.assertNotIn('/secret', str(self.snapshot))
        self.assertEqual(len([f for f in self.snapshot['facts'] if f['id'].startswith('model:')]), 16)
        self.assertFalse(self.snapshot['experimentalRowsIncluded'])

    def test_include_tox21_false_only_changes_report_not_prediction(self):
        snapshot = build_snapshot(self.structure, self.chemistry, self.models, include_tox21=False)
        self.assertEqual(len([f for f in snapshot['facts'] if f['id'].startswith('model:')]), 4)
        self.assertEqual(len(self.models[MODEL_IDS[0]]['data']['prediction']['endpoints']), 16)

    def test_report_free_text_always_unverified(self):
        report = fixture_report(self.snapshot)
        report['commentary'][0]['text'] = 'invented clinical mechanism and paper'
        verified = validate_report(report, self.snapshot)
        self.assertEqual(verified['unverifiedAdditions'][0]['verification'], 'unverified')
        self.assertFalse(verified['sourceVerification']['newScientificFactsVerified'])

    def test_score_source_bool_omission_duplicate_section_and_extra_rejected(self):
        for mutation in ('score', 'source', 'bool', 'omit', 'duplicate', 'section', 'extra', 'nan', 'hash'):
            with self.subTest(mutation=mutation):
                raw = fixture_report(self.snapshot)
                if mutation == 'score': raw['factChecks'][0]['value']['score'] = .999
                elif mutation == 'source': raw['factChecks'][0]['sourceIds'] = ['invented-paper']
                elif mutation == 'bool': raw['factChecks'][0]['value']['score'] = True
                elif mutation == 'omit': raw['factChecks'].pop()
                elif mutation == 'duplicate': raw['factChecks'].append(raw['factChecks'][0])
                elif mutation == 'section': raw['claimOrder']['limits'].append(raw['claimOrder']['prediction'].pop())
                elif mutation == 'extra': raw['tools'] = ['execute']
                elif mutation == 'nan': raw['factChecks'][0]['value']['score'] = float('nan')
                else: raw['snapshotSha256'] = 'a' * 64
                with self.assertRaises(ValueError): validate_report(raw, self.snapshot)

    def test_mutated_snapshot_rejected(self):
        raw = fixture_report(self.snapshot)
        self.snapshot['facts'][0]['value']['score'] = .999
        with self.assertRaises(ValueError): validate_report(raw, self.snapshot)

    def test_prediction_wrong_identity_incomplete_or_invalid_score_rejected(self):
        for field, value in [('structureId', 'wrong'), ('endpoints', []), ('provenance', {})]:
            raw = fixture_prediction(self.structure)
            raw[field] = value
            with self.assertRaises(ValueError): validate_b16(raw, self.structure)
        raw = fixture_prediction(self.structure)
        raw['endpoints'][0]['score'] = float('inf')
        with self.assertRaises(ValueError): validate_b16(raw, self.structure)

    def test_tx_class_semantics_and_identity_are_strict(self):
        data = {'tasks': [parse_task_output('tdc-bbb-martins', 'B')], 'structureId': self.structure['structureId']}
        provenance = {'revision': TXGEMMA_REVISION, 'formalAvailability': False}
        validate_specialist(data, self.structure, 'tdc-bbb-martins', provenance)
        self.models[MODEL_IDS[1]] = {'status': 'completed', 'data': data, 'provenance': provenance}
        snap = build_snapshot(self.structure, self.chemistry, self.models)
        fact = next(f for f in snap['facts'] if f['id'] == 'txgemma:class')
        self.assertEqual(fact['value']['class'], 'B')
        self.assertIsNone(fact['value']['probability'])
        data['tasks'][0]['probability'] = 1
        with self.assertRaises(ValueError): validate_specialist(data, self.structure, 'tdc-bbb-martins', provenance)


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = fixture_service()

    async def asyncTearDown(self):
        await self.service.close()

    async def finish(self, **kwargs):
        ack = self.service.submit(CompareRequest(smiles='OCC', **kwargs))
        await self.service.tasks[ack['runId']]
        return self.service.get(ack['runId'])

    async def test_four_independent_outputs_and_shared_snapshot(self):
        record = await self.finish(specialistTask='tdc-bbb-martins', allowCloudTransfer=True)
        self.assertEqual(record['status'], 'completed')
        qwen, deep = [self.service.adapters[i].calls[0] for i in MODEL_IDS[2:]]
        self.assertEqual(qwen['messages'], deep['messages'])
        self.assertEqual(qwen['snapshot'], deep['snapshot'])
        self.assertNotIn('reports', deep)
        self.assertFalse(record['comparison']['scientificAccuracyRanked'])
        self.assertFalse(record['comparison']['directPredictionComparison'])
        self.assertEqual(len(record['reports']), 2)

    async def test_no_consent_means_no_deepseek_call(self):
        record = await self.finish()
        self.assertEqual(self.service.adapters[MODEL_IDS[3]].calls, [])
        self.assertEqual(record['models'][MODEL_IDS[3]]['status'], 'skipped')
        self.assertFalse(record['cloudTransfer']['executed'])

    async def test_first_reporter_cannot_pollute_second(self):
        original = self.service.adapters[MODEL_IDS[2]].execute
        async def mutate(context):
            result = await original(context)
            context['snapshot']['facts'][0]['value']['score'] = .999
            context['messages'][0]['content'] = 'polluted'
            return result
        self.service.adapters[MODEL_IDS[2]].execute = mutate
        record = await self.finish(allowCloudTransfer=True)
        self.assertEqual(len(record['reports']), 2)
        self.assertNotIn('polluted', str(self.service.adapters[MODEL_IDS[3]].calls))

    async def test_partial_failure_retains_prediction_and_other_report(self):
        self.service.adapters[MODEL_IDS[2]].error = 'COMPARE_INVALID_RESPONSE'
        record = await self.finish(allowCloudTransfer=True)
        self.assertEqual(record['status'], 'partial')
        self.assertEqual(record['models'][MODEL_IDS[0]]['status'], 'completed')
        self.assertIn(MODEL_IDS[3], record['reports'])

    async def test_invalid_report_retains_measured_tokens_without_publishing_text(self):
        adapter = self.service.adapters[MODEL_IDS[2]]
        original = adapter.execute
        async def tamper(context):
            result = await original(context)
            result['data']['rawReport']['factChecks'][0]['value']['score'] = .999
            result['metrics']['completionTokens'] = 12
            result['provenance']['revision'] = 'measured-fixture-digest'
            return result
        adapter.execute = tamper
        record = await self.finish()
        item = record['models'][MODEL_IDS[2]]
        self.assertEqual(item['status'], 'failed')
        self.assertIsNone(item['data'])
        self.assertEqual(item['metrics']['completionTokens'], 12)
        self.assertEqual(item['provenance']['revision'], 'measured-fixture-digest')
        self.assertNotIn(MODEL_IDS[2], record['reports'])

    async def test_cancel_queued_model_never_reuses_previous_partial(self):
        adapter = self.service.adapters[MODEL_IDS[0]]
        chemistry = analyze_chemistry('CCO')
        adapter.last_partial = {'data': {'prediction': fixture_prediction(chemistry)}}
        await self.service.resources.lock.acquire()
        try:
            ack = self.service.submit(CompareRequest(smiles='CCO'))
            await asyncio.sleep(.005)
            record = await self.service.cancel(ack['runId'])
            self.assertEqual(record['models'][MODEL_IDS[0]]['status'], 'cancelled')
            self.assertIsNone(record['models'][MODEL_IDS[0]]['data'])
        finally:
            self.service.resources.lock.release()

    async def test_all_failures_do_not_create_demo_scores(self):
        for adapter in self.service.adapters.values(): adapter.error = 'MODEL_NOT_INSTALLED'
        record = await self.finish(specialistTask='tdc-bbb-martins', allowCloudTransfer=True)
        self.assertEqual(record['status'], 'failed')
        self.assertIsNone(record['models'][MODEL_IDS[0]]['data'])
        self.assertFalse(record['cloudTransfer']['executed'])

    async def test_unknown_tasks_rejected_unsupported_never_execute(self):
        with self.assertRaises(CompareRunError):
            self.service.submit(CompareRequest(smiles='CCO', specialistTask='evil'))
        record = await self.finish(specialistTask='tdc-herg')
        self.assertEqual(record['models'][MODEL_IDS[1]]['status'], 'unsupported')
        self.assertEqual(self.service.adapters[MODEL_IDS[1]].calls, [])

    async def test_cancel_pending_and_running_waits_for_cleanup(self):
        ack = self.service.submit(CompareRequest(smiles='CCO'))
        record = await self.service.cancel(ack['runId'])
        self.assertEqual(record['status'], 'cancelled')
        adapter = self.service.adapters[MODEL_IDS[0]]
        adapter.delay = 20
        ack = self.service.submit(CompareRequest(smiles='CCO'))
        for _ in range(100):
            if adapter.calls: break
            await asyncio.sleep(.001)
        await self.service.cancel(ack['runId'])
        self.assertTrue(adapter.cancelled)
        self.assertTrue(self.service.tasks[ack['runId']].done())
        self.assertIsNone(self.service.resources.active_model)

    async def test_b16_optional_timeout_retains_verified_prediction(self):
        self.service.stage_seconds = .01
        adapter = self.service.adapters[MODEL_IDS[0]]
        async def slow(context):
            adapter.last_partial = {'data': {'prediction': fixture_prediction(context)}, 'provenance': {'testFixture': True}}
            await asyncio.sleep(20)
        adapter.execute = slow
        record = await self.finish()
        self.assertEqual(record['models'][MODEL_IDS[0]]['status'], 'partial')
        self.assertEqual(record['models'][MODEL_IDS[0]]['error']['code'], 'OPTIONAL_STAGE_TIMEOUT')

    async def test_capacity_ttl_and_copy_protection(self):
        self.service.maximum_records = 1
        record = await self.finish()
        record['status'] = 'invented'
        self.assertNotEqual(self.service.get(record['runId'])['status'], 'invented')
        with self.assertRaises(CompareRunError): self.service.submit(CompareRequest(smiles='CCO'))
        self.service.expiry[record['runId']] = time.monotonic() - 1
        with self.assertRaises(CompareRunError): self.service.get(record['runId'])
        self.assertEqual(len(self.service.runs), 0)

    async def test_presets_only_settings_never_cloud_molecule_or_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            self.service.presets_path = Path(directory) / 'presets.json'
            row = self.service.save_preset(ComparePresetRequest(name='local', settings={}))
            self.assertNotIn('allowCloudTransfer', str(row))
            self.assertNotIn('smiles', str(row))
            self.assertFalse(self.service.list_presets()['cloudConsentPersisted'])
            self.assertTrue(self.service.presets_path.exists())
        for forbidden in ('apiKey', 'allowCloudTransfer', 'smiles', 'question'):
            with self.assertRaises(ValidationError): ComparePresetRequest(name='x', settings={forbidden: 'bad'})

    async def test_resource_quarantine_prevents_reuse(self):
        manager = CompareResourceManager()
        adapter = self.service.adapters[MODEL_IDS[2]]
        async with manager.slot('first', adapter): adapter.cleanup_safe = False
        with self.assertRaises(ResourceUnavailable):
            async with manager.slot('second'): pass
        self.assertTrue(manager.public()['blocked'])

    async def test_local_stages_serial_even_with_multiple_runs(self):
        active, maximum = 0, 0
        manager = CompareResourceManager()
        async def probe():
            nonlocal active, maximum
            async with manager.slot('probe'):
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(.005)
                active -= 1
        await asyncio.gather(probe(), probe(), probe())
        self.assertEqual(maximum, 1)


class CompareAPITests(unittest.TestCase):
    def setUp(self):
        self.service = fixture_service()
        self.directory = tempfile.TemporaryDirectory()
        self.client_context = TestClient(create_app(registry={'local': FakeLocalModel()}, env={},
            config_path=Path(self.directory.name) / '.env', compare_service=self.service))
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.directory.cleanup()

    def test_actual_routes_async_contract_json_export_and_static(self):
        models = self.client.get('/api/research/models')
        self.assertEqual(models.status_code, 200)
        self.assertEqual(len(models.json()['models']), 4)
        ack = self.client.post('/api/research/compare/runs', json={'smiles': 'OCC'})
        self.assertEqual(ack.status_code, 202)
        url = ack.json()['pollUrl']
        for _ in range(100):
            result = self.client.get(url)
            if result.json()['status'] in ('completed', 'partial', 'failed'): break
            time.sleep(.01)
        self.assertEqual(result.json()['structure']['canonicalSmiles'], 'CCO')
        self.assertEqual(result.json()['contractVersion'], 'aidd.multimodel.compare.v1')
        self.assertEqual(self.client.delete(url).json()['status'], result.json()['status'])
        for path in ('/compare', '/compare-app.js', '/compare-core.js', '/compare.css'):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get('/backend/compare_adapters.py').status_code, 404)

    def test_strict_fields_invalid_smiles_and_csrf_rejected(self):
        for change in ({'smiles': 'C(C)(C)(C)(C)C'}, {'allowCloudTransfer': 1}, {'extra': 1},
                       {'apiKey': 'do-not-accept'}, {'primaryPredictor': 'v2'}, {'question': 'x' * 1001}):
            self.assertEqual(self.client.post('/api/research/compare/runs', json={'smiles': 'CCO', **change}).status_code, 422)
        response = self.client.post('/api/research/compare/runs', content='{}', headers={'content-type': 'text/plain'})
        self.assertEqual(response.status_code, 415)
        ack = self.client.post('/api/research/compare/runs', json={'smiles': 'CCO'}).json()
        self.assertEqual(self.client.delete(ack['pollUrl'], headers={'origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/research/compare/runs/unknown').status_code, 404)

    def test_old_v2_http_semantics_unchanged(self):
        result = self.client.post('/api/analyze', json={'smiles': 'OCC', 'provider': 'local'}).json()
        self.assertEqual(result['provenance']['contractVersion'], 'toxilens.api.v2')
        self.assertEqual(len(result['endpoints']), 4)
        self.assertEqual(result['provenance']['model']['modelVersion'], 'test-fixture-v1')

    def test_presets_cannot_persist_authorization(self):
        result = self.client.post('/api/research/compare/presets', json={'name': 'local', 'settings': {}})
        self.assertEqual(result.status_code, 201)
        self.assertFalse(self.client.get('/api/research/compare/presets').json()['cloudConsentPersisted'])
        self.assertEqual(self.client.post('/api/research/compare/presets', json={'name': 'bad', 'settings': {'allowCloudTransfer': True}}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
