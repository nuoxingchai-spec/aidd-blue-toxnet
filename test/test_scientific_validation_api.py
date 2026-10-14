"""Software fixtures only: no experimental data, downloads or paid inference."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from fastapi import FastAPI

from backend.validation_routes import create_validation_router
from backend.validation_service import (SCHEMA,ValidationService,ValidationError,
                                        atomic_json,file_sha,public_aggregate,trusted_file)


class ScientificValidationAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        async def fixture(payload):
            return {'schema':SCHEMA,'formalAvailability':False,'testFixture':True,
                    'datasetAudit':{'status':'not-independently-qualified','qualified':False},
                    'fairComparison':{'status':'not-evaluated','endpoints':[]}}
        self.service=ValidationService(root=self.root,data_root=self.root/'private',env={},runner=fixture)
        app=FastAPI(); app.include_router(create_validation_router(self.service))
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://testserver')

    async def asyncTearDown(self):
        await self.service.close()
        await self.client.aclose()
        self.temp.cleanup()

    async def terminal(self,identifier):
        await self.service.tasks[identifier]
        return self.service.get(identifier)

    def register(self):
        root=self.service.data_root
        root.mkdir(parents=True,exist_ok=True)
        for name in ('metadata.json','protocol.json'): (root/name).write_text('{}',encoding='utf-8')
        (root/'rows.jsonl').write_text('',encoding='utf-8')
        row={'id':'fixture-dataset','name':'Explicit software fixture','version':'software-only',
             'metadataFile':'metadata.json','rowsFile':'rows.jsonl','protocolFile':'protocol.json',
             'metadataSha256':file_sha(root/'metadata.json'),'rowsSha256':file_sha(root/'rows.jsonl'),
             'protocolSha256':file_sha(root/'protocol.json')}
        atomic_json(root/'registry.json',{'schema':'aidd.validation.registry.v1','datasets':[row]})
        return row

    async def test_empty_catalog_is_not_scientifically_qualified_or_inference(self):
        response=await self.client.get('/api/research/validation/datasets')
        value=response.json()
        self.assertEqual(response.status_code,200)
        self.assertEqual(value['schema'],SCHEMA)
        self.assertFalse(value['formalAvailability'])
        self.assertTrue(all(d['qualification']=='not-independently-qualified' for d in value['datasets']))
        self.assertFalse(self.service.data_root.exists())

    async def test_browser_paths_urls_extra_fields_and_coercion_rejected(self):
        base={'datasetId':'fixture-dataset','protocolId':'independent-v1','modelIds':['A4','B16','C_Morgan'],'seed':42}
        for changes in ({'path':'C:/private/labels.json'},{'datasetId':'../labels'},
                        {'datasetId':'https://external.example/data'}, {'seed':True},{'seed':43},
                        {'modelIds':['A4','A4','B16']},{'modelIds':['B16']}):
            response=await self.client.post('/api/research/validation/evaluate',json={**base,**changes})
            self.assertEqual(response.status_code,422)
        self.assertFalse(self.service.tasks)

    async def test_unknown_dataset_and_unsealed_dataset_reject_before_runner(self):
        body={'datasetId':'unknown','protocolId':'independent-v1','modelIds':['A4','B16','C_Morgan'],'seed':42}
        response=await self.client.post('/api/research/validation/evaluate',json=body)
        self.assertEqual(response.status_code,409)
        self.register(); body['datasetId']='fixture-dataset'
        response=await self.client.post('/api/research/validation/evaluate',json=body)
        self.assertEqual(response.status_code,403)
        self.service.env['TOXNET_VALIDATION_ALLOW_LABELS']='true'
        response=await self.client.post('/api/research/validation/evaluate',json=body)
        self.assertEqual(response.status_code,409)
        self.assertFalse(self.service.tasks)

    async def test_audit_202_sha_report_history_recovery_and_no_replay(self):
        response=await self.client.post('/api/research/validation/audit',json={'datasetId':'historical-reference-inventory'})
        self.assertEqual(response.status_code,202)
        run_id=response.json()['runId']; value=await self.terminal(run_id)
        self.assertEqual(value['status'],'completed'); self.assertTrue(value['cleanupConfirmed'])
        report=await self.client.get(f'/api/research/validation/reports/{run_id}')
        self.assertTrue(report.json()['testFixture']); self.assertEqual(len(report.json()['resultSha256']),64)
        history=(await self.client.get('/api/research/validation/runs')).json()
        self.assertEqual(len(history['runs']),1); self.assertNotIn('report',history['runs'][0])
        restored=ValidationService(root=self.root,data_root=self.service.data_root,env={})
        self.assertEqual(restored.get(run_id)['report'],value['report'])
        self.assertFalse(restored.tasks)
        await restored.close()

    async def test_checksum_mismatch_refuses_data_and_persisted_report(self):
        self.register()
        (self.service.data_root/'rows.jsonl').write_text('changed',encoding='utf-8')
        response=await self.client.post('/api/research/validation/audit',json={'datasetId':'fixture-dataset'})
        self.assertEqual(response.status_code,409)
        result=self.service.submit('audit',{'datasetId':'historical-reference-inventory'})
        await self.terminal(result['runId'])
        (self.service.data_root/'runs'/result['runId']/'result.json').write_text('{}',encoding='utf-8')
        response=await self.client.get(f"/api/research/validation/reports/{result['runId']}")
        self.assertEqual(response.status_code,503)

    async def test_cancel_is_idempotent_and_does_not_release_slot_before_cleanup(self):
        started,cleaning,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
        calls=[]
        async def runner(payload):
            calls.append(payload); started.set(); await asyncio.sleep(100)
        async def stop(process):
            cleaning.set(); await release.wait()
        self.service.runner=runner; self.service._stop=stop
        first=self.service.submit('audit',{'datasetId':'historical-reference-inventory'})['runId']
        await started.wait()
        cancellation=asyncio.create_task(self.service.cancel(first))
        await cleaning.wait()
        second=self.service.submit('audit',{'datasetId':'historical-reference-inventory'})['runId']
        await asyncio.sleep(.02)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.service.get(first)['status'],'running')
        release.set(); await cancellation
        self.assertTrue(self.service.get(first)['cleanupConfirmed'])
        await self.service.cancel(first)
        await self.service.cancel(second)
        self.assertEqual(self.service.get(first)['status'],'cancelled')

    async def test_immediate_pending_cancel_starts_no_worker(self):
        run=self.service.submit('audit',{'datasetId':'historical-reference-inventory'})['runId']
        value=await self.service.cancel(run)
        self.assertEqual(value['status'],'cancelled')
        self.assertTrue(value['cleanupConfirmed'])
        self.assertEqual((await self.service.cancel(run))['status'],'cancelled')

    async def test_concurrent_cancel_waits_same_cleanup_without_false_quarantine(self):
        started,cleaning,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
        async def runner(_): started.set(); await asyncio.sleep(100)
        async def stop(_): cleaning.set(); await release.wait()
        self.service.runner=runner; self.service._stop=stop
        run=self.service.submit('audit',{'datasetId':'historical-reference-inventory'})['runId']
        await started.wait()
        first=asyncio.create_task(self.service.cancel(run)); await cleaning.wait()
        second=asyncio.create_task(self.service.cancel(run)); await asyncio.sleep(.01)
        self.assertFalse(first.done()); self.assertFalse(second.done())
        release.set(); await asyncio.gather(first,second)
        self.assertTrue(self.service.get(run)['cleanupConfirmed'])
        self.assertEqual(self.service.get(run)['status'],'cancelled')
        self.assertFalse(self.service.quarantine)

    async def test_worker_failure_does_not_echo_sensitive_output_or_raw_fields(self):
        async def leak(_):
            return {'labels':{'private-experiment':1},'smiles':'fixture'}
        self.service.runner=leak
        run=self.service.submit('audit',{'datasetId':'historical-reference-inventory'})['runId']
        value=await self.terminal(run)
        self.assertEqual(value['status'],'failed'); self.assertIsNone(value['report'])
        self.assertNotIn('private-experiment',json.dumps(value))
        async def fail(_): raise RuntimeError('C:/private/private-experiment.csv')
        self.service.runner=fail
        run=self.service.submit('audit',{'datasetId':'historical-reference-inventory'})['runId']
        value=await self.terminal(run)
        self.assertNotIn('private-experiment',json.dumps(value)); self.assertTrue(value['cleanupConfirmed'])

    async def test_close_immediately_seals_pending_and_allows_clean_restart(self):
        runs=[self.service.submit('audit',{'datasetId':'historical-reference-inventory'})['runId'] for _ in range(3)]
        await self.service.close()
        for run in runs:
            self.assertEqual(self.service.get(run)['status'],'cancelled')
            self.assertTrue(self.service.get(run)['cleanupConfirmed'])
        restored=ValidationService(root=self.root,data_root=self.service.data_root,env={},runner=self.service.runner)
        restored._check_previous_workers()
        self.assertFalse(restored.quarantine)
        await restored.close()

    async def test_timeout_and_quarantine_are_explicit(self):
        async def delayed(_): await asyncio.sleep(100)
        self.service.runner=delayed; self.service.timeout=.01
        run=self.service.submit('audit',{'datasetId':'historical-reference-inventory'})['runId']
        value=await self.terminal(run)
        self.assertEqual(value['error']['code'],'VALIDATION_TIMEOUT')
        atomic_json(self.service.data_root/'quarantine.json',{'active':True})
        response=await self.client.post('/api/research/validation/audit',json={'datasetId':'historical-reference-inventory'})
        self.assertEqual(response.status_code,503)

    async def test_restart_unknown_worker_blocks_reuse_without_rewriting_history(self):
        run='a'*32
        original={'runId':run,'status':'running','createdAt':'fixture','report':None,'resultSha256':None}
        path=self.service.data_root/'runs'/run/'record.json'; atomic_json(path,original)
        with self.assertRaises(ValidationError):
            self.service.submit('audit',{'datasetId':'historical-reference-inventory'})
        self.assertEqual(json.loads(path.read_text()),original)
        self.assertEqual(self.service.get(run)['status'],'interrupted')

    async def test_trusted_paths_and_public_aggregation_fail_closed(self):
        for name in ('../labels.json','C:/private/labels','https://x.example/file','a\\file.json'):
            with self.assertRaises(ValidationError): trusted_file(self.root,name)
        for value in ({'rows':[]},{'labels':{}},{'reason':'C:/private/label-file.json'},
                      {'rawRows':[{'compoundId':'fixture','label':1}]},
                      {'accessToken':'fixture-secret','raw_rows':[]},
                      {'reason':'/srv/private/experiment.csv'}, {'filePath':'/srv/private/x.csv'},
                      {'reason':r'\\server\private\data.csv'}):
            with self.assertRaises(ValueError): public_aggregate(value)
        self.assertEqual(public_aggregate({'sourceUrl':'https://www.ebi.ac.uk/chembl/',
                                          'source':'backend/validation_worker.py'})['source'],
                         'backend/validation_worker.py')
        self.assertEqual((await self.client.get('/api/research/validation/runs/not-a-run')).status_code,404)


if __name__=='__main__': unittest.main()
