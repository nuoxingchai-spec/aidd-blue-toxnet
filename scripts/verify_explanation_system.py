"""Actual-weight/source verification; no history writes or new efficacy claims."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import torch
torch.set_num_threads(6)
from fastapi.testclient import TestClient
from backend.attribution import AttributionEngine
from backend.local_model import LocalModelClient
from backend.multitask_model import MultitaskModelClient
from backend.evidence_search import EvidenceSearch
from backend.main import create_app


def checksum(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def verify():
    local=LocalModelClient(env={}); engine=AttributionEngine(local)
    loaded=local._ensure_loaded(); before={k:v.clone() for k,v in loaded['model'].state_dict().items()}
    cases=[]
    for smiles, endpoint in [('CCO','herg'),('CCO','dili'),('CCO','ames'),('CCO','clintox'),('CC(=O)Nc1ccc(O)cc1','dili')]:
        started=time.monotonic(); result=engine.explain(smiles,endpoint=endpoint)
        assert result['status'] in ('available','computed-with-warning'), result
        assert result['provenance']['weightsSha256']==loaded['manifest']['artifacts']['weights']['sha256']
        assert result['provenance']['predictionBeforeAfterIdentical']
        assert result['diagnostics']['originalForwardMaximumAbsoluteDifference'] <= 1e-6
        assert result['imageDataUrl'].startswith('data:image/png;base64,')
        assert len(result['atoms']) > 0
        cases.append({'structureId':result['structureId'],'endpoint':endpoint,'method':result['method'],
                      'status':result['status'],'atomCount':len(result['atoms']),'diagnostics':result['diagnostics'],
                      'durationSeconds':round(time.monotonic()-started,3)})
    assert all(torch.equal(before[k],v) for k,v in loaded['model'].state_dict().items())
    assert all(p.grad is None for p in loaded['model'].parameters())
    import csv
    with (ROOT/'data/processed/toxicity.csv').open(encoding='utf-8',newline='') as handle:
        known=next(row for row in csv.DictReader(handle) if row['endpoint']=='herg')['smiles']
    evidence=EvidenceSearch(ROOT/'data/evidence-index'); retrieved=evidence.search(known,endpoint='herg')
    assert retrieved['status']=='available' and 1 <= len(retrieved['results']) <= 3
    assert all('sourceSha256' in row and row['use']=='explanation-only' for row in retrieved['results'])
    assert retrieved['results'][0]['exactMatch'] and retrieved['results'][0]['similarity']==1.0
    cli=TestClient(create_app(registry={'local':local},default_provider='local',local_client=local,env={},
                              config_path=ROOT/'artifacts/explanation-no-config.env'))
    original=cli.post('/api/analyze',json={'smiles':'CCO','provider':'local'}).json()
    explained=cli.post('/api/research/explain',json={'smiles':'CCO','includeLanguage':True,
                         'expectedWeightsSha256':local.get_model_info()['weightsSha256'],'expectedStructureId':original['structureId']})
    assert explained.status_code==200, explained.text[:300]
    packet=explained.json()
    assert packet['report']['language']['status']=='unavailable'
    assert {r['id']:r['score'] for r in packet['modelPrediction']['endpoints']}=={r['id']:r['score'] for r in original['endpoints']}
    assert 'referenceScore' not in json.dumps(packet) and packet['provenance']['humanRiskProbability'] is False
    assert cli.post('/api/research/explain',json={'smiles':'CCO','expectedWeightsSha256':'a'*64}).status_code==409
    assert cli.post('/api/research/explain',json={'smiles':'CC='}).status_code==422
    assert cli.post('/api/research/explain',json={'smiles':'CCO','model':'v3'}).status_code==503
    model=ROOT/'artifacts/v3-multitask/baselines-20261013/B16-seed-42'
    research=MultitaskModelClient(model,encoder_dir=ROOT/'models/pretrained/molformer',allow_research=True)
    perturb=AttributionEngine(local,research).explain('CCO',endpoint='tox21_nr_ar',model='v3')
    assert perturb['status']=='available',perturb
    assert perturb['provenance']['weightsSha256']==research.get_model_info()['weightsSha256']
    assert perturb['atoms']==[] and perturb['provenance']['predictionBeforeAfterIdentical']
    history=json.loads((ROOT/'artifacts/v3-multitask/v2-preservation.json').read_text())
    protected=[{'file':r['file'],'sha256':checksum(ROOT/r['file'])} for r in history['files']]
    assert all(p['sha256']==r['afterSha256'] for p,r in zip(protected,history['files']))
    index=json.loads((ROOT/'data/evidence-index/manifest.json').read_text())
    return {'schema':'aidd.explanation-system-verification.v1','status':'passed','modelVersion':loaded['manifest']['modelVersion'],
            'weightsSha256':loaded['manifest']['artifacts']['weights']['sha256'],'attributionSourceSha256':checksum(ROOT/'backend/attribution.py'),
            'realV2Attributions':cases,'parametersBeforeAfterIdentical':True,'parameterGradientsUnmodified':True,
            'actualAPI':{'predictionScoresUnchanged':True,'languageUnavailableLocalReportPreserved':True,'modelVersion409':True,'invalidInput422':True,'unconfiguredV3_503':True},
            'v3Attribution':{'status':perturb['status'],'method':perturb['method'],'weightsSha256':perturb['provenance']['weightsSha256'],
                             'legalPerturbationCount':len(perturb['fragments']),'originalPredictionUnchanged':True},
            'localIndex':{'version':index['datasetVersion'],'records':index['records']['count'],'uniqueStructures':index['uniqueStructures'],
                          'sha256':index['records']['sha256'],'retrievalCheckedAgainstSourceLedger':True},
            'protectedV2Files':protected,'newModelTrainingPerformed':False,'scientificMechanismValidated':False,
            'clinicalUseCertified':False,'externalPaidLanguageModelInvoked':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--output',type=Path,required=True); a=p.parse_args()
    if a.output.exists(): p.error('Choose a new verification output; preserve history')
    result=verify(); a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({'status':result['status'],'v2Cases':len(result['realV2Attributions']),'v3Status':result['v3Attribution']['status'],'indexRecords':result['localIndex']['records']}))
