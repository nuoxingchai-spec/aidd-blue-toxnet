"""CPU software fixtures only; no real model or performance claims."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.compare_persistence import CompareRunStore, project_record
from backend.compare_runtime import capture_runtime
from backend.compare_schemas import CompareRequest
from backend.main import create_app
from test.compare_fixture import fixture_service
from test.test_api import FakeLocalModel
from test.test_compare_persistence import fixture_record


class ReportMetadataTests(unittest.TestCase):
    def test_legacy_defaults_and_strict_new_fields(self):
        old = CompareRequest(smiles='CCO')
        self.assertEqual((old.reportContract, old.reportMode), ('v1', 'deep'))
        for change in ({'reportMode':'quick'}, {'reportContract':'v3'}, {'reportMode':False}, {'reportMode':None}, {'scores':{}}):
            with self.assertRaises(ValidationError):
                CompareRequest(smiles='CCO', **change)

    def test_additive_projection_preserves_v2_metadata_without_full_content(self):
        old = fixture_record()
        new = copy.deepcopy(old)
        new['request'].update(reportMode='fast', reportContract='v2')
        new['reports']['ollama-qwen-local'].update(reportSchema='aidd.compare.report.v2',reportMode='fast',
            factReconstruction={'status':'complete','generatedScientificFieldsAccepted':False})
        new['runtimeProvenance'] = {'schema':'aidd.compare.runtime-source.v1','sourceSha256':'e'*64,
                                  'sourceFiles': {'backend/compare_reporting.py':'f'*64}, 'secret':'must-not-save'}
        saved = project_record(new)
        self.assertEqual(saved['request']['reportMode'], 'fast')
        self.assertEqual(saved['reports']['ollama-qwen-local']['reportMode'], 'fast')
        self.assertEqual(saved['runtimeProvenance']['sourceSha256'], 'e'*64)
        self.assertNotIn('must-not-save', json.dumps(saved))
        self.assertNotIn('private generated prose', json.dumps(saved))
        self.assertNotIn('reportContract', project_record(old)['request'])

    def test_schema2_history_payloads_not_rewritten_for_new_report_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'history.sqlite3'
            first = CompareRunStore(path, clock=lambda:10)
            first.upsert(fixture_record('legacy-report'), 1000)
            original = first._db.execute('SELECT record_json FROM compare_runs').fetchone()[0]
            first.close()
            second = CompareRunStore(path, clock=lambda:10).start()
            self.assertEqual(second._db.execute('SELECT record_json FROM compare_runs').fetchone()[0], original)
            self.assertEqual(second.get('legacy-report')['reports']['ollama-qwen-local']['reportSchema'], 'aidd.compare.report.v1')
            self.assertEqual(second.diagnostics()['schemaVersion'], 2)
            second.close()

    def test_search_is_literal_metadata_only_and_paginates(self):
        store = CompareRunStore(None, clock=lambda:10)
        self.addCleanup(store.close)
        for index in range(3):
            record = fixture_record('run-'+str(index), full=True)
            record['request'].update(reportContract='v2',reportMode='fast')
            store.upsert(record, 1000)
        self.assertEqual(store.list(q='fast', limit=2)['total'], 3)
        self.assertEqual(len(store.list(q='fast',limit=2,offset=2)['items']), 1)
        self.assertEqual(store.list(q='private free question')['total'], 0)
        self.assertEqual(store.list(q="' OR 1=1 --")['total'], 0)
        self.assertEqual(store.list(q='run-1')['items'][0]['request']['reportContract'], 'v2')

    def test_runtime_receipt_is_independent_and_contains_only_relative_code_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'backend').mkdir()
            code = root/'backend/compare_test.py'
            code.write_text('original',encoding='utf-8')
            first = capture_runtime(root)
            code.write_text('changed',encoding='utf-8')
            second = capture_runtime(root)
            self.assertNotEqual(first['sourceSha256'], second['sourceSha256'])
            self.assertEqual(list(first['sourceFiles']), ['backend/compare_test.py'])
            self.assertNotIn(directory, json.dumps(first))


class ReportIntegrationTests(unittest.TestCase):
    def test_v2_fast_and_legacy_v1_http_history_export_and_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            service = fixture_service(persistence_path=Path(directory)/'runs.sqlite3')
            app = create_app(env={}, config_path=Path(directory)/'.env', local_client=FakeLocalModel(), compare_service=service)
            with TestClient(app) as client:
                diagnostics = client.get('/api/research/compare/runtime').json()
                self.assertTrue(diagnostics['persistence']['ownerAcquired'])
                self.assertFalse(any(adapter.calls for adapter in service.adapters.values()))
                for contract, mode in [('v1','deep'),('v2','fast'),('v2','deep')]:
                    submit = client.post('/api/research/compare/runs', json={'smiles':'CCO','question':'肝毒性',
                        'reportContract':contract,'reportMode':mode,'comparisonReporter':None,'allowCloudTransfer':False})
                    self.assertEqual(submit.status_code,202)
                    for _ in range(100):
                        record=client.get(submit.json()['pollUrl']).json()
                        if record['status'] not in ('pending','running'):
                            break
                        time.sleep(.01)
                    self.assertEqual(record['status'],'completed',record.get('error'))
                    report=record['reports']['ollama-qwen-local']
                    self.assertEqual(report['reportSchema'],'aidd.compare.report.'+contract)
                    self.assertFalse(record['cloudTransfer']['requestAttempted'])
                    self.assertEqual(record['snapshot']['questionInterpretation']['activeTopics'],['dili'])
                    self.assertEqual(record['runtimeProvenance']['sourceSha256'],diagnostics['runtimeProvenance']['sourceSha256'])
                    if contract=='v2':
                        self.assertEqual(report['verifiedFacts'],record['snapshot']['facts'])
                        self.assertFalse(report['sourceVerification']['newScientificFactsVerified'])
                    saved = service.store.get(record['runId'])
                    self.assertEqual(saved['request']['reportContract'], contract)
                query=client.get('/api/research/compare/runs',params={'q':'fast','limit':1}).json()
                self.assertEqual(query['total'],1)
                self.assertEqual(query['runs'][0]['request']['reportMode'],'fast')
