"""Opt-in real B16/v2 HTTP verification, with no cloud or TxGemma call.

Only aggregate receipts are exported, never query/experimental rows or keys.
Mock coverage lives in test.compare_fixture and is explicitly separate.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def sha(path):
    checksum = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', required=True)
    parser.add_argument('--encoder-dir', required=True)
    parser.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    parser.add_argument('--output', default='artifacts/compare/runtime-verification.json')
    args = parser.parse_args()
    from fastapi.testclient import TestClient
    from backend.compare_adapters import B16Adapter, DeepSeekReportAdapter, OllamaQwenAdapter
    from backend.compare_registry import CompareRegistry
    from backend.compare_txgemma import TxGemmaPredictAdapter
    from backend.compare_orchestrator import CompareOrchestrator
    from backend.local_model import LocalModelClient
    from backend.main import create_app

    protected = json.loads((ROOT / 'artifacts/v3/v2-preservation.json').read_text(encoding='utf-8'))['files']
    for row in protected:
        if sha(ROOT / row['file']) != row['afterSha256']:
            raise RuntimeError('Historical v2 artifact already differs: ' + row['file'])
    model_dir = Path(args.model_dir).resolve()
    frozen = {path: sha(path) for path in model_dir.iterdir() if path.is_file()}
    values = {'TOXNET_MULTITASK_ENABLE': 'true', 'TOXNET_MULTITASK_MODEL_DIR': str(model_dir),
              'TOXNET_MULTITASK_ENCODER_DIR': str(Path(args.encoder_dir).resolve()),
              'TOXNET_MULTITASK_DEVICE': args.device, 'TOXNET_COMPARE_PYTHON': sys.executable}
    registry = CompareRegistry({'toxnet-v3-b16': B16Adapter.from_env(values),
        'txgemma-2b-predict': TxGemmaPredictAdapter(env={}),
        'ollama-qwen-local': OllamaQwenAdapter(timeout_seconds=2),
        'deepseek-api': DeepSeekReportAdapter()})
    service = CompareOrchestrator(registry, env=values)
    local = LocalModelClient(model_dir=ROOT / 'models/aidd-toxnet', env={})
    receipt = {'schema': 'aidd.compare.real-runtime-verification.v1',
        'generatedAt': datetime.now(timezone.utc).isoformat(), 'realB16': True,
        'cloudCallAllowed': False, 'txgemmaExecuted': False, 'device': args.device, 'runs': []}
    with tempfile.TemporaryDirectory() as directory:
        application = create_app(registry={'local': local}, default_provider='local',
            config_path=Path(directory) / '.env', env={}, compare_service=service)
        with TestClient(application) as client:
            statuses = client.get('/api/research/models')
            assert statuses.status_code == 200, statuses.text
            receipt['modelStatuses'] = [{k: item.get(k) for k in ('id', 'available', 'configured', 'actualModelId', 'revision', 'errorCode', 'device', 'licenseStatus')}
                                       for item in statuses.json()['models']]
            assert receipt['modelStatuses'][0]['available'] is True
            predictions = []
            for smiles, include in [('CCO', True), ('OCC', False)]:
                accepted = client.post('/api/research/compare/runs', json={'smiles': smiles, 'includeTox21': include})
                assert accepted.status_code == 202
                limit = time.monotonic() + 300
                while time.monotonic() < limit:
                    result = client.get(accepted.json()['pollUrl']).json()
                    if result['status'] in ('completed', 'partial', 'failed', 'cancelled'):
                        break
                    time.sleep(.1)
                else:
                    client.delete(accepted.json()['pollUrl'])
                    raise RuntimeError('Real comparison exceeded verification timeout')
                b16 = result['models']['toxnet-v3-b16']
                assert b16['status'] == 'completed', b16.get('error')
                assert result['models']['deepseek-api']['status'] == 'skipped'
                assert not result['cloudTransfer']['requestAttempted']
                assert not result['cloudTransfer']['executed']
                prediction = b16['data']['prediction']
                assert len(prediction['endpoints']) == 16
                assert prediction['provenance']['formalAvailability'] is False
                predictions.append(prediction)
                receipt['runs'].append({'status': result['status'], 'b16Status': b16['status'],
                    'tox21InReport': include, 'taskCount': 16, 'modelVersion': prediction['provenance']['modelVersion'],
                    'weightsSha256': prediction['provenance']['weightsSha256'],
                    'manifestSha256': prediction['provenance']['manifestSha256'], 'formalAvailability': False,
                    'metrics': b16['metrics'], 'reportStatuses': {i: result['models'][i]['status'] for i in ('ollama-qwen-local', 'deepseek-api')}})
            assert predictions[0]['structureId'] == predictions[1]['structureId']
            assert predictions[0]['endpoints'] == predictions[1]['endpoints']
            v2 = [client.post('/api/analyze', json={'smiles': smiles, 'provider': 'local'}) for smiles in ('CCO', 'OCC')]
            assert all(r.status_code == 200 for r in v2)
            assert v2[0].json()['endpoints'] == v2[1].json()['endpoints']
            assert len(v2[0].json()['endpoints']) == 4
            receipt['v2Version'] = v2[0].json()['provenance']['model']['modelVersion']
            assert receipt['v2Version'] == 'aidd-toxnet-v2-5c22f6cc5dd3'
    assert all(sha(ROOT / row['file']) == row['afterSha256'] for row in protected)
    assert all(sha(path) == checksum for path, checksum in frozen.items())
    receipt.update(status='passed', equivalentInputsVerified=True, v2ProtectedFiles=len(protected),
                   v2ArtifactsUnchanged=True, b16ArtifactsUnchanged=True,
                   scientificAccuracyMeasured=False, queryRowsIncluded=False,
                   scope='real B16 HTTP inference and equivalent input; local Ollama attempted, no cloud or TxGemma generation')
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'passed', 'realB16': True, 'tasks': 16, 'v2ArtifactsUnchanged': True, 'b16ArtifactsUnchanged': True}))


if __name__ == '__main__':
    main()
