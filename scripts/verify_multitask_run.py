"""Independently verify real multitask run membership, metrics and inference.

Reads only this experiment's permitted development labels and the protected
files' byte hashes. No fitting, calibration, model selection or history edits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import torch
from sklearn.metrics import average_precision_score, roc_auc_score

from backend.multitask_data import load_multitask_data
from backend.multitask_model import MultitaskModelClient


def digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def observed_metrics(rows):
    y = np.asarray([row['label'] for row in rows], dtype=np.float64)
    p = np.asarray([row['score'] for row in rows], dtype=np.float64)
    count, positives = len(y), int(y.sum())
    values = {'count': count, 'positives': positives, 'negatives': count - positives,
              'auroc': None, 'averagePrecision': None, 'brier': None, 'ece10Bins': None}
    if count:
        values['brier'] = float(np.mean((p - y) ** 2))
        buckets = np.minimum((p * 10).astype(int), 9)
        values['ece10Bins'] = float(sum(np.mean(buckets == b) * abs(np.mean(p[buckets == b]) - np.mean(y[buckets == b]))
                                       for b in range(10) if np.any(buckets == b)))
        if 0 < positives < count:
            values['auroc'] = float(roc_auc_score(y, p))
            values['averagePrecision'] = float(average_precision_score(y, p))
    return values


def verify_run(experiment, data_dir, snapshot):
    experiment, data_dir = Path(experiment).resolve(), Path(data_dir).resolve()
    summary = json.loads((experiment / 'experiment.json').read_text(encoding='utf-8'))
    data, samples = load_multitask_data(data_dir)
    if (summary.get('status') != 'completed' or summary.get('datasetVersion') != data['datasetVersion']
            or summary.get('splitSha256') != data['splitSha256']
            or summary.get('dataManifestSha256') != digest(data_dir / 'manifest.json')):
        raise ValueError('Experiment completion/data identity mismatch')
    runs, seeds = summary.get('runs', []), summary.get('seeds', [])
    models = {run['model'] for run in runs}
    if (len(seeds) < 3 or len(set(seeds)) != len(seeds) or not {'A4', 'B16', 'C_Morgan'}.issubset(models)
            or len(runs) != len(models) * len(seeds)
            or {(run['model'], run['seed']) for run in runs} != {(model, seed) for model in models for seed in seeds}):
        raise ValueError('Missing/duplicated common baseline or fixed seed runs')
    validation = [sample for sample in samples if sample['split'] == 'validation']
    original_tasks = ('herg', 'dili', 'ames', 'clintox')
    member_rows = [{'smiles': sample['smiles'], 'split': 'validation',
                    'labels': {task: sample['labels'][task] for task in original_tasks if task in sample['labels']}}
                   for sample in validation if any(task in sample['labels'] for task in original_tasks)]
    membership_hash = hashlib.sha256(json.dumps(member_rows, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    if summary.get('fourTaskValidationMembershipSha256') != membership_hash:
        raise ValueError('Common four-task validation membership hash is invalid')
    verified = []
    for run in runs:
        directory = (experiment / run['directory']).resolve()
        if not directory.is_relative_to(experiment) or digest(directory / 'manifest.json') != run['manifestSha256']:
            raise ValueError('Run directory or manifest checksum mismatch')
        client = MultitaskModelClient(directory, encoder_dir=snapshot, allow_research=True)
        info = client.get_model_info()
        if not info.get('configured'):
            raise ValueError(f"Real artifact rejected: {run['directory']}: {info.get('errorCode')}")
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        actual_evaluation = info['evaluation']
        if (actual_evaluation.get('model') != run['model'] or actual_evaluation.get('seed') != run['seed']
                or manifest['training'].get('seed') != run['seed']
                or actual_evaluation.get('fourTaskValidationMembershipSha256') != membership_hash
                or manifest['training'].get('fourTaskValidationMembershipSha256') != membership_hash
                or run.get('fourTaskValidationMembershipSha256') != membership_hash):
            raise ValueError('Actual artifact model/seed or comparison membership differs from summary')
        for source_path, checksum in manifest['training']['sourceHashes'].items():
            target = (ROOT / source_path).resolve()
            if not target.is_relative_to(ROOT) or digest(target) != checksum:
                raise ValueError('Training source bytes differ from the recorded version')
        if manifest['dataset']['datasetVersion'] != data['datasetVersion'] or manifest['dataset']['splitSha256'] != data['splitSha256']:
            raise ValueError('Head and development split mismatch')
        trained = [task for task in manifest['tasks'] if manifest['training']['endpointStatus'][task] == 'trained']
        expected = {(sample['smiles'], task): label for sample in validation
                    for task, label in sample['labels'].items() if task in trained}
        all_observations = {(sample['smiles'], task): label for sample in validation
                            for task, label in sample['labels'].items() if task in manifest['tasks']}
        record_path = directory / manifest['artifacts']['validationPredictions']['file']
        records = json.loads(record_path.read_text(encoding='utf-8'))
        observed, recorded_cells = {}, set()
        by_task = {task: [] for task in trained}
        for row in records:
            key = row['smiles'], row['endpoint']
            if key in recorded_cells or key not in all_observations or row['label'] != all_observations[key] or row.get('split') != 'validation':
                raise ValueError('Observed predictions do not match exact permitted validation membership/labels')
            recorded_cells.add(key)
            if key[1] not in trained:
                if row.get('score') is not None or row.get('predictionStatus') != manifest['training']['endpointStatus'][key[1]]:
                    raise ValueError('Disabled/untrained task must not expose a learned prediction')
                continue
            score = row['score']
            if row.get('predictionStatus') != 'trained' or type(score) not in (float, int) or not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError('Nonfinite/out-of-range observed prediction')
            observed[key] = score
            by_task[key[1]].append(row)
        if set(observed) != set(expected) or recorded_cells != set(all_observations):
            raise ValueError('Missing validation prediction for an observed trained task')
        for task, rows in by_task.items():
            measured = observed_metrics(rows)
            for field, actual in measured.items():
                for recorded_metrics in (run['evaluation']['endpoints'][task], actual_evaluation['endpoints'][task]):
                    recorded = recorded_metrics[field]
                    if (actual is None) != (recorded is None) or actual is not None and abs(actual - recorded) > 1e-12:
                        raise ValueError(f'Recorded metric differs from exact observed predictions: {task}/{field}')
        for task in set(manifest['tasks']) - set(trained):
            labels = [label for (smiles, endpoint), label in all_observations.items() if endpoint == task]
            expected_counts = {'count': len(labels), 'positives': sum(labels), 'negatives': len(labels) - sum(labels)}
            for recorded_metrics in (run['evaluation']['endpoints'][task], actual_evaluation['endpoints'][task]):
                if any(recorded_metrics[field] != value for field, value in expected_counts.items()) or any(
                        recorded_metrics[field] is not None for field in ('auroc', 'averagePrecision', 'brier', 'ece10Bins')):
                    raise ValueError('Disabled task counts or unavailable metrics do not match observed cells')
        candidates = [sample for sample in validation if any(task in sample['labels'] for task in trained)]
        probe_errors = []
        for sample in candidates[:2]:
            result = client.predict_all(sample['smiles'])
            scores = {row['id']: row['score'] for row in result['endpoints']}
            for task in trained:
                key = sample['smiles'], task
                if key in observed:
                    probe_errors.append(abs(scores[task] - observed[key]))
        if not probe_errors or max(probe_errors) > 3e-5:
            raise ValueError('Loaded inference differs from recorded batch validation predictions')
        verified.append({'model': run['model'], 'seed': run['seed'], 'manifestSha256': run['manifestSha256'],
                         'weightsSha256': manifest['artifacts']['weights']['sha256'],
                         'parameterCount': manifest['parameterCount'], 'trainedTasks': len(trained),
                         'observedValidationPredictions': len(observed), 'recordedValidationCells': len(records), 'metricRecomputation': 'passed',
                         'loadedInferenceMaximumAbsoluteDifference': max(probe_errors)})
    protected = json.loads((ROOT / 'artifacts/v3/v2-preservation.json').read_text(encoding='utf-8'))
    preserved = []
    for entry in protected['files']:
        actual = digest(ROOT / entry['file'])
        if actual != entry['afterSha256']:
            raise ValueError(f"Protected v2/history artifact changed: {entry['file']}")
        preserved.append({'file': entry['file'], 'beforeSha256': entry['afterSha256'], 'afterSha256': actual})
    return {'schema': 'aidd.toxnet.multitask-run-verification.v1', 'status': 'verified',
            'verificationCodeSha256': digest(Path(__file__)),
            'experimentSha256': digest(experiment / 'experiment.json'), 'datasetVersion': data['datasetVersion'],
            'splitSha256': data['splitSha256'], 'runs': verified, 'seeds': seeds,
            'independentTest': False, 'legacyHeldoutAndKarimLabelsUsed': False,
            'protectedV2Files': preserved, 'purpose': 'Membership, metric and inference checks; no new performance selection'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=Path, default=ROOT / 'artifacts/v3-multitask/baselines-20261013')
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data/v3-multitask-audited')
    parser.add_argument('--snapshot', type=Path, default=ROOT / 'models/pretrained/molformer')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Verification output exists; choose a new path and preserve history')
    torch.set_num_threads(6)
    try:
        result = verify_run(args.experiment, args.data_dir, args.snapshot)
    except (ValueError, OSError, KeyError, RuntimeError) as error:
        parser.exit(1, f'Verification failed: {error}\n')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'runs': len(result['runs']), 'experimentSha256': result['experimentSha256']}))


if __name__ == '__main__':
    main()
