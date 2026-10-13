"""Offline, source-pinned experimental retrieval, never a confidence estimator.

Only local research display of at most five observations is supported. The
index, source molecules and labels are not redistributed with the application.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import threading

from rdkit import Chem, DataStructs, rdBase
from rdkit.Chem import Draw, rdFingerprintGenerator

from backend.chemistry import CHEMISTRY_VERSION, canonicalize_smiles
from backend.training_data import DATASETS, ENDPOINTS, LICENSE_NOTE, sha256_file
from backend.candidate_data import SOURCES, TOX21_ASSAYS, TOX21_ENDPOINTS
from backend.v3_data import V2_MANIFEST_SHA256, V2_SPLIT_SHA256, EXPOSED_POOL_SHA256, _required_hash

ROOT = Path(__file__).resolve().parents[1]
INDEX_SCHEMA = 'aidd.local-experimental-evidence.v1'
SEARCH_VERSION = 'aidd.evidence-morgan-search.v1'
LABELS_SHA = '75e5f2c8766efe24cf6cbca57ce1e72bc9028705303b29fff8ad6016c6b0bd6b'
TOX21_SHA = '6546b23e114788a0e5c2ca41122b83b88e6a0ba1cedcf760c03db005d831059c'
FP_SPEC = {'radius': 2, 'bits': 2048, 'includeChirality': True}
LIMITATIONS = [
    'Tanimoto is structural similarity, not prediction confidence or experimental certainty.',
    'A nearby compound is not an experiment on the query molecule; exact identity also does not establish human risk.',
    'Original hERG thresholds are heterogeneous/unspecified; Karim uses <10 micromolar. Definitions cannot be equated.',
    'Previously exposed labels are explanation-only observations, never a new blind test or tuning/calibration input.',
    'Underlying source redistribution and commercial rights remain unresolved; local research display only.',
]


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')


def _source_rows(data_dir, candidate_dir, model_dir, *, rebuild=False):
    """Fixed bytes, original task meaning and frozen membership are mandatory."""
    data_dir, candidate_dir, model_dir = map(Path, (data_dir, candidate_dir, model_dir))
    paths = [(model_dir / 'manifest.json', V2_MANIFEST_SHA256), (model_dir / 'split.json', V2_SPLIT_SHA256),
             (data_dir / 'processed/toxicity.csv', LABELS_SHA), (candidate_dir / 'processed/tox21.csv', TOX21_SHA),
             (candidate_dir / 'processed/herg_external_pool.csv', EXPOSED_POOL_SHA256)]
    paths += [(data_dir / 'raw' / (spec['endpoint'] + '.csv'), spec['sha256']) for spec in DATASETS]
    paths += [(candidate_dir / 'raw' / spec['fileName'], spec['sha256']) for spec in SOURCES]
    for path, digest in paths:
        _required_hash(path, digest)
    if rebuild:
        # Reuse the original raw-label/CT_TOX reconstruction and candidate audit.
        from backend.v3_data import _verified_inputs
        from backend.multitask_data import _verified_tox21_rows
        _, _, _, candidate, _ = _verified_inputs(data_dir, model_dir, candidate_dir, ROOT / 'artifacts/external-herg-evaluation.json')
        _verified_tox21_rows(candidate_dir, candidate['tox21Rows'])
    split = _read(model_dir / 'split.json')
    roles = {(row['smiles'], row['endpoint']): name for name, rows in split['splits'].items() for row in rows}
    records = []
    for row in _csv(data_dir / 'processed/toxicity.csv'):
        role = roles.get((row['smiles'], row['endpoint']))
        if role is None:
            raise ValueError('Experimental row is absent from the pinned frozen partition')
        records.append({'smiles': row['smiles'], 'endpoint': row['endpoint'], 'label': int(row['label']),
                        'sourceKey': row['endpoint'], 'source_id': str(row['source_id']), 'source_row': str(row['source_row']),
                        'dataRole': 'frozen-train-observation' if role == 'train' else 'previously-exposed-' + role + '-observation'})
    for filename, key in [('tox21.csv', 'tox21'), ('herg_external_pool.csv', 'herg_karim')]:
        for row in _csv(candidate_dir / 'processed' / filename):
            records.append({'smiles': row['smiles'], 'endpoint': row['endpoint'], 'label': int(row['label']),
                            'sourceKey': key, 'source_id': str(row['source_id']), 'source_row': str(row['source_row']),
                            'dataRole': 'audited-assay-candidate-observation' if key == 'tox21' else 'previously-exposed-retrospective-karim-observation'})
    records.sort(key=lambda row: (row['smiles'], row['endpoint'], row['sourceKey'], row['source_row']))
    return records, paths


def prepare_index(output, data_dir=ROOT / 'data', candidate_dir=ROOT / 'data/candidates', model_dir=ROOT / 'models/aidd-toxnet'):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('Evidence index exists; select a fresh directory, do not overwrite history')
    records, paths = _source_rows(data_dir, candidate_dir, model_dir, rebuild=True)
    output.mkdir(parents=True)
    payload = b''.join(_bytes(row) for row in records)
    (output / 'observations.jsonl').write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {'schema': INDEX_SCHEMA, 'datasetVersion': 'aidd-evidence-' + digest[:16],
                'records': {'file': 'observations.jsonl', 'sha256': digest, 'count': len(records)},
                'uniqueStructures': len({row['smiles'] for row in records}), 'chemistryVersion': CHEMISTRY_VERSION,
                'rdkitVersion': rdBase.rdkitVersion, 'fingerprint': FP_SPEC,
                'sourceRoots': {'data': str(Path(data_dir).resolve()), 'candidates': str(Path(candidate_dir).resolve()), 'model': str(Path(model_dir).resolve())},
                'sources': [{'path': str(path.resolve()), 'sha256': checksum} for path, checksum in paths],
                'permission': 'local-research-only; source commercial/redistribution rights unresolved',
                'rawLabelsRebuiltAndVerified': True, 'optimizationUse': False, 'newBlindTest': False, 'licenseNote': LICENSE_NOTE}
    (output / 'manifest.json').write_bytes(_bytes(manifest))
    return manifest


def _source_definition(key, endpoint):
    if key in ENDPOINTS:
        spec = next(spec for spec in DATASETS if spec['endpoint'] == key)
        return {'database': spec['name'], 'sourceSha256': spec['sha256'], 'datasetVersion': 'aidd-toxicity-75e5f2c8766efe24',
                'positiveDefinition': spec['positiveClass'], 'documentationUrl': spec['documentationUrl'], 'reference': spec['reference'],
                'sourceAssay': endpoint, 'threshold': 'heterogeneous/unspecified' if key == 'herg' else 'source binary label'}
    spec = next(spec for spec in SOURCES if spec['id'] == key)
    assay = TOX21_ASSAYS[TOX21_ENDPOINTS.index(endpoint)] if key == 'tox21' else 'hERG blocker <10 micromolar'
    return {'database': spec['name'], 'sourceSha256': spec['sha256'], 'datasetVersion': 'aidd-candidates-333b4f7d03532e15',
            'positiveDefinition': f'{assay}: active=1, inactive=0; absent is unknown' if key == 'tox21' else spec['labelMeaning'],
            'documentationUrl': spec['documentationUrl'], 'reference': None, 'sourceAssay': assay,
            'threshold': '<10 micromolar' if key == 'herg_karim' else 'assay-specific'}


def _image(smiles):
    out = io.BytesIO()
    Draw.MolToImage(Chem.MolFromSmiles(smiles), size=(320, 180)).save(out, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(out.getvalue()).decode('ascii')


class EvidenceSearch:
    def __init__(self, index_dir):
        self.index_dir = Path(index_dir).resolve()
        self._lock, self._loaded, self._signature = threading.RLock(), None, None

    def _load(self):
        manifest = _read(self.index_dir / 'manifest.json')
        if (manifest.get('schema') != INDEX_SCHEMA or manifest.get('chemistryVersion') != CHEMISTRY_VERSION
                or manifest.get('rdkitVersion') != rdBase.rdkitVersion or manifest.get('fingerprint') != FP_SPEC
                or manifest.get('rawLabelsRebuiltAndVerified') is not True or manifest.get('optimizationUse') is not False):
            raise ValueError('Unsupported or unaudited local evidence index')
        record = manifest.get('records', {})
        if record.get('file') != 'observations.jsonl':
            raise ValueError('Unsupported evidence file')
        path = self.index_dir / record['file']
        _required_hash(path, record['sha256'])
        if manifest.get('datasetVersion') != 'aidd-evidence-' + record['sha256'][:16]:
            raise ValueError('Evidence version does not match record bytes')
        roots = manifest['sourceRoots']
        expected, sources = _source_rows(roots['data'], roots['candidates'], roots['model'])
        ledger = [{'path': str(p.resolve()), 'sha256': h} for p, h in sources]
        if manifest.get('sources') != ledger:
            raise ValueError('Evidence source ledger is not the fixed audited snapshot')
        signature = (record['sha256'], tuple((str(p), h) for p, h in sources))
        if self._loaded is not None and signature == self._signature:
            return self._loaded
        with path.open(encoding='utf-8') as handle:
            actual = [json.loads(line) for line in handle if line.strip()]
        if actual != expected or record.get('count') != len(actual):
            raise ValueError('Evidence labels/provenance differ from pinned experimental source observations')
        smiles = sorted({row['smiles'] for row in actual})
        if manifest.get('uniqueStructures') != len(smiles):
            raise ValueError('Evidence structure count mismatch')
        generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048, includeChirality=True)
        fingerprints = [generator.GetFingerprint(Chem.MolFromSmiles(canonicalize_smiles(value))) for value in smiles]
        by_structure = {}
        for row in actual:
            by_structure.setdefault(row['smiles'], []).append(row)
        self._loaded = manifest, smiles, fingerprints, by_structure, generator
        self._signature = signature
        return self._loaded

    def search(self, smiles, endpoint=None, limit=3, min_similarity=0.3):
        try:
            canonical = canonicalize_smiles(smiles)
        except ValueError:
            return {'status': 'invalid-input', 'results': [], 'errorCode': 'INVALID_SMILES', 'limitations': LIMITATIONS}
        if type(limit) is not int or not 1 <= limit <= 5 or type(min_similarity) not in (float, int) or not 0 <= min_similarity <= 1:
            raise ValueError('Evidence search limit 1..5 and similarity 0..1 required')
        if endpoint is not None and endpoint not in (*ENDPOINTS, *TOX21_ENDPOINTS):
            raise ValueError('Unknown experimental endpoint')
        try:
            with self._lock:
                manifest, structures, fingerprints, records, generator = self._load()
                similarities = DataStructs.BulkTanimotoSimilarity(generator.GetFingerprint(Chem.MolFromSmiles(canonical)), fingerprints)
                order = sorted(range(len(structures)), key=lambda i: (-similarities[i], structures[i]))
                results = []
                for i in order:
                    if similarities[i] < min_similarity:
                        break
                    for row in records[structures[i]]:
                        if endpoint is not None and row['endpoint'] != endpoint:
                            continue
                        same = row['sourceKey'] in ENDPOINTS or row['sourceKey'] == 'tox21'
                        results.append({**{key: value for key, value in row.items() if key != 'smiles'},
                                        **_source_definition(row['sourceKey'], row['endpoint']),
                                        'canonicalSmiles': row['smiles'], 'imageDataUrl': _image(row['smiles']),
                                        'similarity': float(similarities[i]), 'exactMatch': row['smiles'] == canonical,
                                        'use': 'explanation-only', 'probabilityInterpretation': 'not-a-prediction',
                                        'definitionConsistency': {'status': 'same-source-task-contract' if same else 'not-equivalent-threshold',
                                                                  'sameEndpointNameAloneIsSufficient': False,
                                                                  'note': 'Karim <10 μM cannot be equated to original heterogeneous hERG thresholds.' if not same else 'Same source label definition; not proof of same assay conditions or source independence.'}})
                        if len(results) >= limit:
                            break
                    if len(results) >= limit:
                        break
                return {'status': 'available' if results else 'insufficient-evidence', 'results': results,
                        'provenance': {'searchVersion': SEARCH_VERSION, 'indexVersion': manifest['datasetVersion'],
                                       'indexSha256': manifest['records']['sha256'], 'fingerprint': FP_SPEC,
                                       'sourceRights': manifest['permission'], 'endpointFilter': endpoint, 'minSimilarity': min_similarity},
                        'limitations': LIMITATIONS}
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            return {'status': 'insufficient-evidence', 'results': [], 'errorCode': 'EVIDENCE_INDEX_UNAVAILABLE',
                    'message': '没有可通过来源/哈希核验的本地实验索引；请先显式离线准备。', 'limitations': LIMITATIONS}
