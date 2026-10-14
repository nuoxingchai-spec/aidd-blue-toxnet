"""Offline frozen-model evaluation; labels are opened only after predictions."""
from __future__ import annotations

import argparse
import importlib.metadata
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.independent_validation_metrics import digest, evaluate_paired_models, valid_sha
from backend.validation_predictors import FrozenResearchPredictor
from backend.dataset_overlap_audit import evaluator_source_sha256

MODELS = ('A4', 'B16', 'C_Morgan')


def register_exposure(seal_path, *, manifest_sha256, protocol_sha256, model_bindings, seed,
                      evaluator_sha256, evaluation_settings):
    """Immutable, conservative exposure registration before any label reading.

    Same frozen protocol can be reproduced. Different bindings/seed/protocol
    cannot regain fresh-blind status by changing the output file name.
    This local ledger is a research safeguard, not tamper-resistant attestation.
    """
    seal = Path(seal_path)
    directory = seal if seal.is_dir() else seal.parent
    if (directory.is_symlink() or not directory.is_dir() or not valid_sha(manifest_sha256)
            or not valid_sha(protocol_sha256) or not valid_sha(evaluator_sha256)):
        raise ValueError('A verified existing sealed dataset directory is required')
    ledger = directory/'evaluation-exposure.json'
    lock = directory/'.evaluation-exposure.lock'
    if ledger.is_symlink() or lock.is_symlink():
        raise ValueError('Exposure ledger links are not allowed')
    fingerprint = digest({'manifestSha256': manifest_sha256, 'protocolSha256': protocol_sha256,
                          'modelBindings': model_bindings, 'seed': seed, 'evaluationSettings': evaluation_settings,
                          'evaluatorSourceSha256': evaluator_sha256})
    descriptor = None
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        if ledger.exists():
            previous = json.loads(ledger.read_text(encoding='utf-8'))
            if previous.get('schema') != 'aidd.independent-exposure-ledger.v1' or previous.get('fingerprint') != fingerprint:
                raise ValueError('Dataset was registered under different models/protocol/seed; fresh blind evaluation is prohibited')
            return {'status': 'previously-evaluated-same-frozen-protocol', 'freshBlindTestClaimed': False,
                    'ledgerSha256': digest(previous), 'firstRegistrationAt': previous['firstRegistrationAt']}
        entry = {'schema': 'aidd.independent-exposure-ledger.v1', 'fingerprint': fingerprint,
                 'manifestSha256': manifest_sha256, 'protocolSha256': protocol_sha256, 'seed': seed,
                 'modelBindingsSha256': digest(model_bindings), 'firstRegistrationAt': datetime.now(timezone.utc).isoformat(),
                 'evaluatorSourceSha256': evaluator_sha256, 'evaluationSettings': evaluation_settings,
                 'meaning': 'conservatively registered before model execution and label access; interrupted attempts remain registered'}
        with ledger.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(entry, sort_keys=True, indent=2, allow_nan=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        return {'status': 'first-registered-frozen-protocol', 'freshBlindTestClaimed': False,
                'ledgerSha256': digest(entry), 'firstRegistrationAt': entry['firstRegistrationAt'],
                'limitation': 'Independent status additionally depends on the complete historical exposure audit, not this registration alone.'}
    except FileExistsError:
        raise ValueError('Another evaluator owns this sealed dataset exposure ledger') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
            lock.unlink()


def _match_bindings(expected, actual):
    if not isinstance(expected, list) or len(expected) != len(MODELS) or {entry.get('modelId') for entry in expected} != set(MODELS):
        raise ValueError('The sealed protocol must freeze exactly A4, B16 and C_Morgan')
    for entry in expected:
        binding = actual[entry['modelId']]
        for key in ('weightsSha256', 'manifestSha256', 'taskContractVersion'):
            if entry.get(key) != binding.get(key):
                raise ValueError('Actual model differs from its predeclared sealed binding')
        if entry.get('version', entry.get('modelVersion')) != binding.get('modelVersion'):
            raise ValueError('Actual model version differs from the frozen protocol')
        contracts = entry.get('taskContractHashes')
        if (not isinstance(contracts, dict) or not contracts
                or any(binding['taskContractHashes'].get(task) != value for task, value in contracts.items())):
            raise ValueError('Actual source/label semantics differ from the frozen protocol')


def run_evaluation(seal_path, expected_sha256, model_specs, *, seed=42, device='cpu',
                   bootstrap_replicates=500, batch_size=8, predictor_factory=FrozenResearchPredictor):
    from backend.independent_validation_data import load_sealed_dataset, frozen_evaluation_settings
    started = time.perf_counter()
    if set(model_specs) != set(MODELS) or type(seed) is not int or seed != 42:
        raise ValueError('This acceptance protocol freezes A4/B16/C_Morgan and seed 42')
    unopened = load_sealed_dataset(seal_path, expected_sha256, purpose='evaluate', allow_labels=False)
    manifest = unopened['manifest']
    if manifest.get('qualified') is not True or manifest.get('status') != 'sealed-independent-evaluation':
        raise ValueError('No qualified sealed independent experimental dataset; metrics remain not evaluated')
    frozen = unopened['protocol']
    settings = frozen_evaluation_settings()
    evaluator_sha = evaluator_source_sha256()
    if (frozen.get('evaluationSettings') != settings or settings['bootstrapReplicates'] != bootstrap_replicates
            or frozen.get('evaluatorSourceSha256') != evaluator_sha):
        raise ValueError('Current evaluator source or statistical settings differ from the frozen protocol; model/label access denied')
    if type(batch_size) is not int or not 1 <= batch_size <= 256 or device not in ('cpu', 'cuda'):
        raise ValueError('Invalid bounded batch/device execution settings')
    predictors = {name: predictor_factory(**spec, device=device) for name, spec in model_specs.items()}
    bindings = {name: predictor.binding() for name, predictor in predictors.items()}
    _match_bindings(manifest['modelBindings'], bindings)
    exposure = register_exposure(seal_path, manifest_sha256=expected_sha256,
        protocol_sha256=manifest['frozenProtocolSha256'], model_bindings=bindings, seed=seed,
        evaluator_sha256=evaluator_sha,
        evaluation_settings={**settings, 'batchSize': batch_size, 'device': device})
    smiles = [row['canonicalSmiles'] for row in unopened['rows']]
    predictions = {name: predictors[name].predict_batch(smiles, batch_size=batch_size) for name in MODELS}
    prediction_sha = {name: digest(predictions[name]) for name in MODELS}
    # No caller-provided labels or paths enter an HTTP request. Labels are read
    # in this offline worker only after all frozen predictor outputs exist.
    opened = load_sealed_dataset(seal_path, expected_sha256, purpose='evaluate', allow_labels=True)
    if [row['structureId'] for row in opened['rows']] != [row['structureId'] for row in unopened['rows']]:
        raise ValueError('Sealed membership changed between prediction and label access')
    protocol = {'qualified': True, 'role': 'independent-evaluation', 'protocolSha256': manifest['frozenProtocolSha256'],
                'tasks': list(manifest['taskContractHashes']), 'taskContractVersion': manifest['taskContractVersion'],
                'taskContractHashes': manifest['taskContractHashes'],
                'minimumBootstrapPerClass': settings['minimumBootstrapPerClass'],
                'minimumBootstrapGroups': settings['minimumBootstrapGroups']}
    result = evaluate_paired_models(opened['rows'], predictions, protocol, bootstrap_replicates=bootstrap_replicates, seed=seed)
    completed = all(model['status'] == 'completed' for model in result['models'].values())
    versions = {'python': sys.version.split()[0]}
    for package in ('numpy', 'scipy', 'scikit-learn', 'rdkit', 'torch', 'transformers', 'tokenizers', 'safetensors'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    result.update(datasetManifestSha256=expected_sha256, predictionBatchSha256=prediction_sha,
                  exposure=exposure, labelReadAfterPrediction=True,
                  status='completed-research-evaluation' if completed else 'completed-with-coverage-failures',
                  evaluatorSourceSha256=evaluator_sha, evaluationSettings=settings,
                  evaluationProtocolId=frozen.get('protocolId'), evaluationProtocolVersion=frozen.get('protocolVersion'),
                  evaluatedAtUtc=datetime.now(timezone.utc).isoformat(), elapsedSeconds=time.perf_counter()-started,
                  execution={'device': device, 'batchSize': batch_size, 'dependencies': versions},
                  rawLabelsOrStructuresIncluded=False, formalAvailability=False)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sealed-dataset', required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--models-root', required=True)
    parser.add_argument('--encoder-dir', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    parser.add_argument('--bootstrap-replicates', type=int, default=500)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise FileExistsError('Use a new aggregate output; previous scientific results are immutable')
    root = Path(args.models_root)
    specs = {name: {'model_dir': root/f'{name}-seed-42', 'encoder_dir': args.encoder_dir if name != 'C_Morgan' else None} for name in MODELS}
    result = run_evaluation(args.sealed_dataset, args.manifest_sha256, specs, device=args.device, bootstrap_replicates=args.bootstrap_replicates)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    print(json.dumps({'status': result['status'], 'exposure': result['exposure']['status'], 'formalAvailability': False}))


if __name__ == '__main__':
    main()
