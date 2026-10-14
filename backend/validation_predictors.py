"""Offline batch wrapper reusing the existing SHA-audited research loader."""
from __future__ import annotations

import hashlib
import json
import copy
from pathlib import Path

from backend.chemistry import canonicalize_smiles
from backend.independent_validation_metrics import digest
from backend.multitask_model import MultitaskModelClient


class FrozenResearchPredictor:
    def __init__(self, model_dir=None, *, encoder_dir=None, device='cpu', client=None):
        self.client = client or MultitaskModelClient(model_dir, encoder_dir=encoder_dir, device=device, allow_research=True)
        self.device = device

    def binding(self):
        loaded = self.client._ensure_loaded()
        m = loaded['manifest']
        refs = json.loads(loaded['paths']['trainingReferences'].read_text(encoding='utf-8'))
        if not isinstance(refs, list) or any(not isinstance(s, str) or canonicalize_smiles(s) != s for s in refs) or len(refs) != len(set(refs)):
            raise ValueError('Verified training references must be unique complete canonical structures')
        return {'schema': 'aidd.research-model-binding.v1', 'artifactVerified': True,
                'modelVersion': m['modelVersion'], 'manifestSha256': loaded['manifestSha256'],
                'weightsSha256': m['artifacts']['weights']['sha256'],
                'preprocessingSha256': m['artifacts']['preprocessing']['sha256'],
                'taskIds': m['tasks'], 'taskContractVersion': m['taskContractVersion'],
                'taskContractHashes': {t: digest(m['taskContracts'][t]) for t in m['tasks']},
                'datasetVersion': m['dataset']['datasetVersion'], 'splitSha256': m['dataset']['splitSha256'],
                'datasetManifestSha256': m['dataset'].get('manifestSha256'),
                'trainingSourceHashes': copy.deepcopy(m['training'].get('sourceHashes', {})),
                'trainingReferenceSha256': m['artifacts']['trainingReferences']['sha256'],
                'trainingStructureOrderSha256': digest(refs), 'trainingReferenceCount': len(refs),
                'encoder': copy.deepcopy(m.get('encoder')),
                'architecture': copy.deepcopy(m.get('architecture')),
                'parameterCount': loaded.get('parameterCount'),
                'frozenEncoderParameterCount': m.get('frozenEncoderParameterCount'),
                'formalAvailability': False, 'device': self.device}

    def training_references(self):
        self.binding()  # Includes original loader SHA verification, no labels.
        return json.loads(self.client._loaded['paths']['trainingReferences'].read_text(encoding='utf-8'))

    def predict_batch(self, smiles, *, batch_size=8):
        if type(batch_size) is not int or not 1 <= batch_size <= 256 or not isinstance(smiles, list):
            raise ValueError('An ordered input list and bounded batch size are required')
        binding = self.binding()
        loaded, tasks = self.client._loaded, binding['taskIds']
        scores, ids, coverage, valid = [[None]*len(tasks) for _ in smiles], [], [], []
        for i, value in enumerate(smiles):
            try:
                canonical = canonicalize_smiles(value)
                identifier = hashlib.sha256(canonical.encode()).hexdigest()
            except (ValueError, TypeError):
                ids.append(None)
                coverage.append({'status': 'unsupported', 'errorCode': 'INVALID_CHEMICAL_INPUT'})
                continue
            ids.append(identifier)
            try:
                if loaded['encoder'] is not None:
                    from backend.pretrained_encoder import validate_encoder_smiles
                    validate_encoder_smiles(canonical)
                valid.append((i, canonical))
                coverage.append({'status': 'pending', 'errorCode': None})
            except (ValueError, TypeError):
                coverage.append({'status': 'unsupported', 'errorCode': 'MODEL_INPUT_UNSUPPORTED'})
        import numpy as np
        for start in range(0, len(valid), batch_size):
            batch = valid[start:start+batch_size]
            try:
                canonical = [value for _, value in batch]
                raw = loaded['encoder'].encode(canonical, batch_size=batch_size) if loaded['encoder'] is not None else loaded['module'].morgan_features(canonical)
                features = ((raw-loaded['mean'])/loaded['scale']).astype(np.float32)
                values = loaded['module'].predict_head(loaded['head'], features, device=self.device, batch_size=batch_size)
                if values.shape != (len(batch), len(tasks)) or not np.isfinite(values).all() or ((values < 0) | (values > 1)).any():
                    raise ValueError('Invalid actual model output')
                for (row, _), vector in zip(batch, values):
                    scores[row] = [float(value) if loaded['manifest']['training']['endpointStatus'][task] == 'trained' else None for task, value in zip(tasks, vector)]
                    coverage[row] = {'status': 'completed', 'errorCode': None}
            except (RuntimeError, ValueError):
                for row, _ in batch:
                    coverage[row] = {'status': 'failed', 'errorCode': 'MODEL_BATCH_INFERENCE_FAILED'}
        return {'schema': 'aidd.validation.predictions.v1', 'structureIds': ids, 'taskIds': tasks,
                'scores': scores, 'coverage': coverage, 'modelBinding': binding,
                'status': 'completed' if all(row['status'] == 'completed' for row in coverage) else 'partial-coverage'}
