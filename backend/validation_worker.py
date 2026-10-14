"""One CPU-only process, immutable registered inputs, aggregate output only."""
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime,timezone


def run_job(payload):
    from backend.validation_service import (SCHEMA,LIMITATIONS,Registration,load_json,
                                             trusted_file,public_aggregate,file_sha)
    from backend.independent_validation_data import build_historical_references,audit_dataset

    if os.environ.get('CUDA_VISIBLE_DEVICES') != '':
        raise ValueError('Worker must be isolated from GPUs')
    root,data_root=Path(payload['workspaceRoot']),Path(payload['dataRoot'])
    request,kind=payload['request'],payload['kind']
    if kind not in ('audit','evaluate'): raise ValueError('Unsupported purpose')
    if kind == 'audit':
        references=build_historical_references(root)
        if request['datasetId'] == 'historical-reference-inventory':
            audit={'status':'not-independently-qualified','qualified':False,
                   'reason':'历史训练、开发和已曝光评估仅作为禁入参考，不是新测试数据。',
                   'historicalReferences':references['receipt'],'heldoutLabelsRead':False}
        else:
            reg=Registration.model_validate(payload['registration'])
            metadata=load_json(trusted_file(data_root,reg.metadataFile,expected=reg.metadataSha256))
            protocol=load_json(trusted_file(data_root,reg.protocolFile,expected=reg.protocolSha256))
            source=trusted_file(data_root,reg.rowsFile,expected=reg.rowsSha256)
            with source.open(encoding='utf-8') as handle:
                rows=[]
                for line in handle:
                    if line.strip(): rows.append(json.loads(line))
                    if len(rows)>50000: raise ValueError('Audit member budget exceeded')
            result=audit_dataset(rows,metadata,references,protocol)
            audit=result['publicAudit']
        report={'schema':SCHEMA,'formalAvailability':False,'datasetAudit':audit,
                'independenceStatus':audit.get('status','not-independently-qualified'),
                'fairComparison':{'status':'not-evaluated','endpoints':[]},
                'calibration':{'status':'not-fitted','reason':'没有获准且隔离的校准集。'},
                'applicability':{'status':'not-validated','reason':'无独立域内/域外性能证据。'},
                'negativeTransfer':{'status':'not-evaluated'},'limitations':LIMITATIONS,
                'execution':{'device':'cpu','labelsRead':False,'heldoutLabelsRead':False}}
    else:
        from scripts.evaluate_independent_models import run_evaluation
        from backend.independent_validation_data import load_sealed_dataset
        reg=Registration.model_validate(payload['registration'])
        seal=trusted_file(data_root,reg.sealFile,expected=reg.sealSha256)
        from scripts.register_independent_dataset import validate_registration_binding
        validate_registration_binding(data_root,reg)
        result=run_evaluation(seal,reg.sealSha256,payload['modelSpecs'],seed=42,device='cpu')
        dataset=load_sealed_dataset(seal,reg.sealSha256,purpose='evaluate',allow_labels=False)
        manifest=dataset['manifest']
        report={'schema':SCHEMA,'formalAvailability':False,'datasetAudit':{
                    'status':'qualified-sealed-evaluation','qualified':True,
                    'datasetId':request['datasetId'],'datasetVersion':manifest.get('datasetVersion'),
                    'manifestSha256':reg.sealSha256,'protocolSha256':manifest['frozenProtocolSha256'],
                    'exposure':result['exposure'],'sourcePopulationIndependent':'operator-reviewed-evidence'},
                'independenceStatus':result['exposure']['status'],'fairComparison':result,
                'calibration':{'status':'not-fitted','reason':'本次只评价原始分数，没有独立校准工件。'},
                'applicability':{'status':'not-validated','reason':'覆盖启发式尚未通过独立域内/域外验证。'},
                'limitations':LIMITATIONS,'execution':{'device':'cpu','seed':42,'labelReadAfterPrediction':True}}
    files=['backend/validation_worker.py','scripts/evaluate_independent_models.py',
           'scripts/register_independent_dataset.py','backend/independent_validation_data.py',
           'backend/dataset_overlap_audit.py','backend/independent_validation_metrics.py','backend/validation_predictors.py']
    report['evaluationSource']={'version':'aidd.independent-evaluation.v1','seed':42,'executedAt':datetime.now(timezone.utc).isoformat(),
                                'sourceSha256':{name:file_sha(root/name) for name in files if (root/name).is_file()}}
    return public_aggregate(report)


def main():
    raw=sys.stdin.buffer.read(65537)
    if len(raw)>65536: raise ValueError('Worker request too large')
    try:
        report=run_job(json.loads(raw))
        print(json.dumps({'status':'ok','report':report},ensure_ascii=False,allow_nan=False))
    except Exception:
        # Do not echo raw structures, labels, private paths or provider errors.
        print(json.dumps({'status':'rejected','errorCode':'VALIDATION_WORKER_REJECTED'}))
        raise SystemExit(1)


if __name__ == '__main__': main()
