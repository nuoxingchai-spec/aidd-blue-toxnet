import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from backend.explanation_report import build_report, apply_plan, plan_body, organize_with_language
from backend.explanation_service import ExplanationService
from backend.main import create_app
from backend.configuration import ModelConfigurationStore
from backend.model_client import ModelClient, ModelConfig, ModelAPIError

HASH = 'a'*64


class LocalFixture:
    def __init__(self): self._lock = threading.RLock()
    def get_public_config(self): return {'configured': True, 'providerFormat': 'local-pytorch', 'supportsImage': False}
    def get_model_info(self): return {'configured': True}
    def predict(self, *args):
        return {'endpoints': [{'id': k, 'score': v} for k,v in zip(('herg','dili','ames','clintox'),(.1,.2,.3,.4))],
                'provenance': {'modelVersion': 'unit-fixture-not-real-model', 'weightsSha256': HASH, 'artifactSha256': HASH},
                'calibration': {'status': 'unknown'}, 'applicability': {'status': 'unknown'}}


class AttrFixture:
    def explain(self, *args, **kwargs):
        return {'status': 'available', 'method': 'unit-fixture-not-real-attribution', 'atoms': [], 'fragments': [],
                'provenance': {'modelVersion': 'unit-fixture-not-real-model', 'weightsSha256': HASH, 'artifactSha256': HASH}, 'limitations': []}


class EmptyEvidence:
    def search(self, *args, **kwargs): return {'status': 'insufficient-evidence', 'results': [], 'limitations': ['unit fixture no experimental data']}


def report_fixture():
    prediction = LocalFixture().predict()
    return build_report(prediction, AttrFixture().explain(), EmptyEvidence().search(),
                        {'alerts': [], 'rulesVersion': 'unit-fixture-rules'}, endpoint='herg')


def valid_plan(report):
    return {'schema': 'aidd.llm.report-plan.v1', 'sectionOrder': [s['id'] for s in report['sections']][::-1],
            'claimOrder': {s['id']: s['claimIds'][::-1] for s in report['sections']}}


class ReportTests(unittest.TestCase):
    def test_source_kinds_separate_and_mechanisms_uncertain(self):
        r = report_fixture(); kinds = {c['sourceKind'] for c in r['claims']}
        self.assertTrue({'model-computation','human-rule','mechanism-hypothesis','insufficient-evidence'} <= kinds)
        self.assertIn('尚无充分证据', next(c['text'] for c in r['claims'] if c['sectionId']=='mechanism'))
        self.assertTrue(all('confidence' in c and 'limitations' in c and 'sourceIds' in c for c in r['claims']))
        self.assertEqual(r['mechanismAssessment']['status'], 'insufficient-evidence')
        self.assertEqual(r['mechanismAssessment']['verifiedMechanisticClaims'], [])

    def test_plan_only_reorders_facts(self):
        r = report_fixture(); before = copy.deepcopy(r)
        sections = apply_plan(r, valid_plan(r))
        self.assertEqual(r, before); self.assertEqual(sections[0]['id'], 'limits')
        self.assertEqual({s['text'] for s in sections if len(s['claimIds'])==1}, {s['text'] for s in r['sections'] if len(s['claimIds'])==1})

    def test_scores_citations_and_free_prose_rejected(self):
        for extra in ({'score': .99}, {'citation': 'invented DOI'}, {'text': 'confirmed safe'}):
            p = valid_plan(report_fixture()); p.update(extra)
            with self.assertRaises(ValueError): apply_plan(report_fixture(), p)

    def test_missing_or_cross_category_claims_rejected(self):
        r=report_fixture(); p=valid_plan(r); p['claimOrder']['mechanism']=['model:herg']
        with self.assertRaises(ValueError): apply_plan(r,p)
        p=valid_plan(r); p['sectionOrder'][0]=p['sectionOrder'][1]
        with self.assertRaises(ValueError): apply_plan(r,p)

    def test_external_request_omits_structures_labels_and_citations(self):
        r=report_fixture(); r['sources'].append({'canonicalSmiles':'PRIVATE_STRUCTURE','label':1,'reference':'PRIVATE_REFERENCE'})
        r['claims'].append({'id':'private-observation','sectionId':'experiments','text':'PRIVATE_STRUCTURE label=1','sourceKind':'experimental-observation','confidence':'observed'})
        body=json.dumps(plan_body(ModelConfig(model_name='test-only'),r))
        self.assertNotIn('PRIVATE_',body); self.assertNotIn('"label"',body)

    def test_no_configuration_still_local_report(self):
        r=report_fixture(); result=organize_with_language(r,None,None)
        self.assertEqual(result['language']['status'],'unavailable'); self.assertEqual(result['claims'],r['claims'])

    def test_real_http_adapter_contract_fixture_and_auth_not_exported(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a): pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length']))); received.append(body)
                skeleton=json.loads(body['messages'][1]['content'])['sections']
                plan={'schema':'aidd.llm.report-plan.v1','sectionOrder':[s['id'] for s in skeleton], 'claimOrder':{s['id']:[c['id'] for c in s['claims']] for s in skeleton}}
                payload=json.dumps({'model':'unit-fixture-chat-not-real-LLM','choices':[{'message':{'content':json.dumps(plan)}}]}).encode()
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(payload)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler); worker=threading.Thread(target=server.serve_forever,daemon=True); worker.start()
        try:
            config=ModelConfig(api_url=f'http://127.0.0.1:{server.server_port}',api_format='openai',api_key='fixture-key-not-secret',model_name='test-only',model_version='unit-v1')
            result=organize_with_language(report_fixture(),ModelClient(config),'custom')
            self.assertEqual(result['language']['status'],'organized'); self.assertTrue(received)
            self.assertNotIn('fixture-key-not-secret',json.dumps(result)); self.assertFalse(result['language']['sentExperimentalRows'])
        finally: server.shutdown(); server.server_close(); worker.join()

    def test_language_bad_response_does_not_change_any_fact(self):
        c=ModelClient(ModelConfig(api_url='http://127.0.0.1:1',api_format='openai',model_name='unit-only',model_version='unit-v1'))
        response=io.BytesIO(json.dumps({'choices':[{'message':{'content':'{"score":0.99,"citation":"fake"}'}}]}).encode())
        with patch.object(c.opener,'open',return_value=response): out=organize_with_language(report_fixture(),c,'custom')
        self.assertEqual(out['language']['status'],'unavailable'); self.assertEqual(out['claims'],report_fixture()['claims'])


class ExplanationAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.local=LocalFixture()
        self.config=ModelConfigurationStore(Path(self.temp.name)/'.env',registry={},env={})
        self.service=ExplanationService(self.local,self.config,evidence=EmptyEvidence(),attribution=AttrFixture())
        self.client=TestClient(create_app(registry={},env={},local_client=self.local,config_path=Path(self.temp.name)/'.env',explanation_service=self.service))
    def tearDown(self): self.temp.cleanup()

    def test_real_service_contract_unit_fixtures_marked_and_no_demo_reference(self):
        result=self.client.post('/api/research/explain',json={'smiles':'OCC'}); self.assertEqual(result.status_code,200)
        d=result.json(); self.assertEqual(d['canonicalSmiles'],'CCO'); self.assertEqual(d['status'],'partial')
        self.assertEqual(d['modelPrediction']['endpoints'],self.local.predict()['endpoints']); self.assertEqual(d['evidence']['results'],[])
        self.assertNotIn('referenceScore',json.dumps(d)); self.assertTrue(d['provenance']['researchOnly'])

    def test_input_hash_model_hash_mismatch_409(self):
        for payload in ({'expectedWeightsSha256':'b'*64},{'expectedStructureId':'c'*64}):
            self.assertEqual(self.client.post('/api/research/explain',json={'smiles':'CCO',**payload}).status_code,409)

    def test_invalid_input_and_contract_fields(self):
        for p in ({'smiles':'CC='},{'smiles':True},{'smiles':'CCO','steps':True},{'smiles':'CCO','includeLanguage':'true'},
                  {'smiles':'CCO','model':'fake'},{'smiles':'CCO','modelPrediction':{'score':.99}},{'smiles':'CCO','apiKey':'fixture-only'}):
            self.assertEqual(self.client.post('/api/research/explain',json=p).status_code,422)

    def test_model_unavailable_503_no_fake_report(self):
        with patch.object(self.local,'predict',side_effect=ModelAPIError('LOCAL_MODEL_NOT_READY','fixture unavailable',503)):
            d=self.client.post('/api/research/explain',json={'smiles':'CCO'}); self.assertEqual(d.status_code,503); self.assertNotIn('modelPrediction',d.json())

    def test_wrong_attribution_weight_version_refused(self):
        a=AttrFixture().explain(); a['provenance']['weightsSha256']='b'*64
        with patch.object(self.service.attribution,'explain',return_value=a): d=self.client.post('/api/research/explain',json={'smiles':'CCO'}).json()
        self.assertEqual(d['attribution']['status'],'unavailable'); self.assertEqual(d['attribution']['atoms'],[])

    def test_same_weights_different_calibration_manifest_refused(self):
        a=AttrFixture().explain(); a['provenance']['artifactSha256']='b'*64
        with patch.object(self.service.attribution,'explain',return_value=a):
            d=self.client.post('/api/research/explain',json={'smiles':'CCO'}).json()
        self.assertEqual(d['attribution']['status'],'unavailable')
        self.assertEqual(d['attribution']['errorCode'],'ATTRIBUTION_MODEL_CHANGED')

    def test_missing_language_configuration_does_not_prevent_report(self):
        d=self.client.post('/api/research/explain',json={'smiles':'CCO','includeLanguage':True,'languageProvider':'openai'}).json()
        self.assertEqual(d['report']['language']['status'],'unavailable'); self.assertEqual(d['modelPrediction']['endpoints'],self.local.predict()['endpoints'])

    def test_cross_origin_request_rejected(self):
        self.assertEqual(self.client.post('/api/research/explain',json={'smiles':'CCO'},headers={'Origin':'https://foreign.example'}).status_code,403)

    def test_v3_missing_no_v2_fallback(self):
        self.assertEqual(self.client.post('/api/research/explain',json={'smiles':'CCO','model':'v3'}).status_code,503)
