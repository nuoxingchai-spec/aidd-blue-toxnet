"""Read-only, previously exposed development comparisons; no retraining."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from backend.independent_validation_metrics import METRICS, cluster_bootstrap, digest, point_metrics
from backend.v3_data import _components, leakage_identity

FOUR_TASKS = ('herg', 'dili', 'ames', 'clintox')


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _verified(directory, entry):
    path = (directory/entry['file']).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
        raise ValueError('Historical development artifact SHA256 or location mismatch')
    return path


def _predictions(directory, expected_manifest_sha256):
    if hashlib.sha256((directory/'manifest.json').read_bytes()).hexdigest() != expected_manifest_sha256:
        raise ValueError('Historical experiment manifest binding mismatch')
    manifest = _read(directory/'manifest.json')
    for entry in manifest['artifacts'].values():
        _verified(directory, entry)
    if manifest.get('status') != 'trained-completed' or manifest.get('formalAvailability') is not False:
        raise ValueError('A completed real research artifact is required')
    values = _read(_verified(directory, manifest['artifacts']['validationPredictions']))
    output = {task: {} for task in FOUR_TASKS}
    for row in values:
        if row.get('endpoint') not in output:
            continue
        if row.get('split') != 'validation' or row.get('predictionStatus') != 'trained':
            raise ValueError('Only recorded trained development validation predictions may be compared')
        task, smiles = row['endpoint'], row['smiles']
        if smiles in output[task] or type(row.get('label')) is not int or row['label'] not in (0, 1):
            raise ValueError('Duplicate or invalid historical observed labels')
        output[task][smiles] = (row['label'], row['score'])
    return manifest, output


def analyze_development_experiment(experiment_dir, *, bootstrap_replicates=500, seed=42, seeds=(42, 43, 44)):
    directory = Path(experiment_dir).resolve()
    experiment = _read(directory/'experiment.json')
    if not str(experiment.get('status', '')).startswith('completed') or experiment.get('newBlindTestClaimed') is not False:
        raise ValueError('Only explicitly exposed completed development experiments are accepted')
    if not seeds or len(seeds) != len(set(seeds)) or any(type(value) is not int for value in seeds):
        raise ValueError('Unique fixed model seeds are required; repetitions are not independent observations')
    keys = [(row['model'], row['seed']) for row in experiment['runs']]
    if len(keys) != len(set(keys)):
        raise ValueError('Duplicate historical model/seed bindings are forbidden')
    output = {'schema': 'aidd.negative-transfer-retrospective.v1', 'role': 'development-retrospective',
              'previouslyExposed': True, 'independentTest': False, 'formalAvailability': False,
              'datasetVersion': experiment['datasetVersion'], 'splitSha256': experiment['splitSha256'],
              'historicalExperimentSha256': hashlib.sha256((directory/'experiment.json').read_bytes()).hexdigest(),
              'seed': seed, 'runs': [], 'selectionPermitted': False,
              'limitations': ['Validation was used for checkpoint selection; these are not independent test estimates.',
                 'Paired seed runs share molecules and labels; seed count is not an independent sample size.',
                 'Negative AP differences are descriptive; auxiliary-task causality is not established.',
                 'ClinTox has very few positives; intervals may be unavailable and AP unstable.']}
    for model_seed in seeds:
        bindings = {(row['model'], row['seed']): row['manifestSha256'] for row in experiment['runs']}
        a, first = _predictions(directory/f'A4-seed-{model_seed}', bindings[('A4', model_seed)])
        b, second = _predictions(directory/f'B16-seed-{model_seed}', bindings[('B16', model_seed)])
        if (a['training']['fourTaskValidationMembershipSha256'] != b['training']['fourTaskValidationMembershipSha256']
                or a['dataset'] != b['dataset'] or any(a['taskContracts'][t] != b['taskContracts'][t] for t in FOUR_TASKS)):
            raise ValueError('Historical same-label membership or task-contract check failed')
        members = sorted(set().union(*(set(first[t]) for t in FOUR_TASKS)))
        components = _components([{'smiles': s, 'auditIdentity': leakage_identity(s)} for s in members])
        groups = {row['smiles']: digest(sorted(hashlib.sha256(x['smiles'].encode()).hexdigest() for x in component)) for component in components for row in component}
        result = {'seed': model_seed, 'A4WeightsSha256': a['artifacts']['weights']['sha256'], 'B16WeightsSha256': b['artifacts']['weights']['sha256'], 'endpoints': {},
                  'trainingObjectives': {name: {key: manifest['training'].get(key) for key in
                      ('classCounts', 'taskWeights', 'positiveWeights', 'positiveWeightPolicy', 'loss', 'selectionProtocol', 'bestEpoch', 'epochsExecuted')}
                      for name, manifest in (('A4', a), ('B16', b))},
                  'gradientInterference': {'status': 'not-measured', 'reason': 'Stored prediction artifacts do not contain shared-parameter gradients; no gradient mechanism inferred.'}}
        for task in FOUR_TASKS:
            if set(first[task]) != set(second[task]) or any(first[task][s][0] != second[task][s][0] for s in first[task]):
                raise ValueError('Development comparison coverage/labels differ; no silent intersection')
            ordered = sorted(first[task])
            y, p, q = ([first[task][s][0] for s in ordered], [first[task][s][1] for s in ordered], [second[task][s][1] for s in ordered])
            pa, pb = point_metrics(y, p), point_metrics(y, q)
            result['endpoints'][task] = {'count': len(y), 'positives': sum(y), 'negatives': len(y)-sum(y),
                 'positiveRate': sum(y)/len(y) if y else None, 'coverageFraction': 1.0 if y else None,
                 'sameObservedMemberOrderSha256': digest([hashlib.sha256(s.encode()).hexdigest() for s in ordered]), 'A4': pa, 'B16': pb,
                 'deltaB16MinusA4': {m: pb[m]-pa[m] if pb[m] is not None and pa[m] is not None else None for m in METRICS},
                 'confidenceIntervals': cluster_bootstrap(y, p, [groups[s] for s in ordered], other_scores=q,
                    replicates=bootstrap_replicates, seed=seed, minimum_per_class=10, minimum_groups=10)}
        output['runs'].append(result)
    output['descriptiveSeedMeanDelta'] = {task: {metric: float(np.mean(values)) if (values := [run['endpoints'][task]['deltaB16MinusA4'][metric] for run in output['runs'] if run['endpoints'][task]['deltaB16MinusA4'][metric] is not None]) else None for metric in METRICS} for task in FOUR_TASKS}
    output['seedVariability'] = {task: {name: {metric: {'count': len(values),
        'mean': float(np.mean(values)) if values else None,
        'standardDeviationAcrossSeeds': float(np.std(values, ddof=1)) if len(values) > 1 else None,
        'minimum': min(values) if values else None, 'maximum': max(values) if values else None}
        for metric in METRICS
        for values in [[run['endpoints'][task][name][metric] for run in output['runs'] if run['endpoints'][task][name][metric] is not None]]}
        for name in ('A4', 'B16')} for task in FOUR_TASKS}
    output['independentSeedSampleSize'] = None
    output['reportSha256'] = digest(output)
    return output


def gradient_cosine(main_gradient, auxiliary_gradient):
    """Optional input-only diagnostic; this function does not run backward/train."""
    first, second = (np.asarray(value, dtype=float).ravel() for value in (main_gradient, auxiliary_gradient))
    if first.shape != second.shape or not len(first) or not np.isfinite(first).all() or not np.isfinite(second).all():
        raise ValueError('Finite matching shared-parameter gradients are required')
    scale = float(np.linalg.norm(first)*np.linalg.norm(second))
    return {'status': 'computed-diagnostic' if scale else 'unavailable-zero-gradient',
            'cosine': float(np.clip(np.dot(first, second)/scale, -1, 1)) if scale else None,
            'mechanismOrTransferCausalityProven': False, 'trainingExecuted': False}


def controlled_ablation_config():
    return {'status': 'configuration-only-not-executed', 'retrainingAuthorized': False,
            'fourTaskWeights': 1.0, 'tox21TaskWeights': [0.0, .25], 'selectionRole': 'development-validation-only',
            'testUsedForSelection': False, 'requiredComparators': ['A4', 'B16', 'C_Morgan', 'B16_noTox21'],
            'seeds': [42, 43, 44], 'maximumEpochsPerMLP': 40,
            'plannedModelSeedRuns': 12, 'computeTimeEstimate': None,
            'authorizationRequiredBeforeTraining': True,
            'gradientDiagnosticRole': 'development-train-only; frozen test and calibration labels excluded',
            'mechanismConclusionsPermitted': False}
