"""Label-contract-bound research metrics; never relabel exposed data as blind.

The caller must first verify a frozen, rights-reviewed data seal. These numeric
helpers do not authorize a dataset merely because a dict says ``qualified``.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import re

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score

METRICS = ('auroc', 'averagePrecision', 'brier', 'ece10Bins')
ROLES = ('independent-evaluation', 'development-retrospective', 'calibration-diagnostic')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def point_metrics(labels, scores):
    y, p = np.asarray(labels, dtype=float), np.asarray(scores, dtype=float)
    if y.ndim != 1 or y.shape != p.shape or not np.isin(y, (0, 1)).all() or not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
        raise ValueError('Observed labels and finite bounded scores must align')
    result = {'count': len(y), 'positives': int(y.sum()), 'negatives': int(len(y) - y.sum()),
              'positiveRate': float(y.mean()) if len(y) else None,
              **{key: None for key in METRICS}, 'status': 'no-data'}
    if not len(y):
        return result
    result.update(status='computed' if 0 < y.sum() < len(y) else 'single-class', brier=float(np.mean((p-y)**2)))
    bins = np.minimum((p*10).astype(int), 9)
    result['ece10Bins'] = float(sum(np.sum(bins == b)/len(y)*abs(float(p[bins == b].mean()-y[bins == b].mean())) for b in range(10) if (bins == b).any()))
    if result['status'] == 'computed':
        result.update(auroc=float(roc_auc_score(y, p)), averagePrecision=float(average_precision_score(y, p)))
    return result


def cluster_bootstrap(labels, scores, groups, *, other_scores=None, replicates=500, seed=42,
                      minimum_per_class=10, minimum_groups=10):
    """Resample scaffold groups, identically for a paired score difference."""
    y, p = np.asarray(labels, dtype=float), np.asarray(scores, dtype=float)
    point_metrics(y, p)
    if len(groups) != len(y) or any(not isinstance(g, str) or not g for g in groups):
        raise ValueError('Every observed molecule needs an audited scaffold group')
    if type(replicates) is not int or not 10 <= replicates <= 5000 or type(seed) is not int or seed < 0:
        raise ValueError('Invalid fixed bootstrap budget or seed')
    if (type(minimum_per_class) is not int or type(minimum_groups) is not int
            or min(minimum_per_class, minimum_groups) < 2):
        raise ValueError('Bootstrap support guards must be at least two')
    q = None if other_scores is None else np.asarray(other_scores, dtype=float)
    if q is not None:
        point_metrics(y, q)
    lookup = {group: np.flatnonzero(np.asarray(groups) == group) for group in sorted(set(groups))}
    output = {'method': 'paired-scaffold-cluster-bootstrap' if q is not None else 'scaffold-cluster-bootstrap',
              'seed': seed, 'requestedReplicates': replicates, 'clusterCount': len(lookup), 'confidenceLevel': .95,
              'status': 'insufficient-support', 'minimumPerClass': minimum_per_class, 'minimumGroups': minimum_groups,
              'intervals': {key: {'lower': None, 'upper': None, 'validReplicates': 0} for key in METRICS},
              'limitations': 'Exploratory cluster percentile intervals; seeds are not independent molecular observations.'}
    if min(int(y.sum()), len(y)-int(y.sum())) < minimum_per_class or len(lookup) < minimum_groups:
        return output
    rng, keys = np.random.default_rng(seed), list(lookup)
    values = {key: [] for key in METRICS}
    for _ in range(replicates):
        selected = np.concatenate([lookup[keys[i]] for i in rng.integers(len(keys), size=len(keys))])
        first = point_metrics(y[selected], p[selected])
        second = point_metrics(y[selected], q[selected]) if q is not None else None
        for metric in METRICS:
            if first[metric] is not None and (second is None or second[metric] is not None):
                values[metric].append(first[metric] if second is None else second[metric]-first[metric])
    complete = True
    for metric, observed in values.items():
        interval = output['intervals'][metric]
        interval['validReplicates'] = len(observed)
        if len(observed) >= math.ceil(.8*replicates):
            interval.update(lower=float(np.quantile(observed, .025)), upper=float(np.quantile(observed, .975)))
        else:
            complete = False
    output['status'] = 'computed-exploratory' if complete else 'insufficient-valid-bootstrap-replicates'
    return output


def _protocol(protocol):
    if not isinstance(protocol, dict) or protocol.get('qualified') is not True or protocol.get('role') not in ROLES:
        raise ValueError('A qualified sealed evaluation role is required; no implicit test authorization')
    checksum = protocol.get('protocolSha256', protocol.get('frozenProtocolSha256'))
    if not valid_sha(checksum):
        raise ValueError('Frozen protocol SHA256 is required')
    tasks = protocol.get('tasks')
    hashes = protocol.get('taskContractHashes')
    if not isinstance(tasks, list) or not tasks or len(set(tasks)) != len(tasks) or not isinstance(hashes, dict) or any(not valid_sha(hashes.get(t)) for t in tasks):
        raise ValueError('Explicit reviewed per-task label contract hashes are required')
    return tasks, checksum


def _rows(rows, tasks):
    identifiers, labels, groups, sources = [], {}, [], []
    for row in rows:
        identifier = row.get('structureId')
        if not valid_sha(identifier) or identifier in labels:
            raise ValueError('Unique audited structure IDs are required')
        group = row.get('scaffoldGroup', row.get('groupId'))
        source = row.get('sourceId', row.get('sourceDataset'))
        if not isinstance(group, str) or not group or not isinstance(source, str) or not source:
            raise ValueError('Audited scaffold groups and experimental source strata are required')
        observed = row.get('labels')
        if not isinstance(observed, dict) or set(observed)-set(tasks) or any(type(v) is not int or v not in (0, 1) for v in observed.values()):
            raise ValueError('Missing labels must be omitted; observed labels must match explicit task contracts')
        identifiers.append(identifier)
        labels[identifier] = observed
        groups.append(group)
        sources.append(source)
    if not identifiers:
        raise ValueError('No qualified real experimental evaluation rows')
    return identifiers, labels, groups, sources


def _aligned(rows, prediction, protocol):
    tasks, _ = _protocol(protocol)
    identifiers, labels, groups, sources = _rows(rows, tasks)
    binding = prediction.get('modelBinding') or {}
    if binding.get('artifactVerified') is not True or not valid_sha(binding.get('weightsSha256')) or not valid_sha(binding.get('manifestSha256')):
        raise ValueError('Real verified model binding is required')
    if prediction.get('structureIds') != identifiers:
        raise ValueError('Prediction structure order/membership mismatch; rows may not be silently dropped or reordered')
    actual_tasks = prediction.get('taskIds')
    if not isinstance(actual_tasks, list) or len(actual_tasks) != len(set(actual_tasks)):
        raise ValueError('Invalid prediction task order')
    values = prediction.get('scores')
    if not isinstance(values, list) or len(values) != len(rows) or any(not isinstance(v, list) or len(v) != len(actual_tasks) for v in values):
        raise ValueError('Prediction score dimensions do not match all requested members')
    for task in tasks:
        if task in actual_tasks and (binding.get('taskContractVersion') != protocol.get('taskContractVersion') or (binding.get('taskContractHashes') or {}).get(task) != protocol['taskContractHashes'][task]):
            raise ValueError('Source/positive-class/threshold task contract mismatch; endpoint names alone do not establish comparability')
    return tasks, identifiers, labels, groups, sources, actual_tasks, values


def evaluate_predictions(rows, predictions, protocol, *, bootstrap_replicates=500, seed=42):
    tasks, checksum = _protocol(protocol)
    tasks, identifiers, labels, groups, sources, actual, values = _aligned(rows, predictions, protocol)
    output = {'schema': 'aidd.independent-model-evaluation.v1', 'role': protocol['role'], 'protocolSha256': checksum,
              'independentTest': protocol['role'] == 'independent-evaluation', 'formalAvailability': False,
              'memberCount': len(rows), 'memberOrderSha256': digest(identifiers), 'modelBinding': predictions['modelBinding'],
              'seed': seed, 'endpoints': {}, 'sourceStrata': {}, 'status': 'completed',
              'limitations': ['Label probability is not human clinical risk.', 'No model or calibration selection may use these evaluation labels.']}
    complete_rows = sum(all(task in actual and type(values[i][actual.index(task)]) in (float, int)
        and math.isfinite(values[i][actual.index(task)]) and 0 <= values[i][actual.index(task)] <= 1 for task in tasks) for i in range(len(rows)))
    output['coverage'] = {'requestedStructures': len(rows), 'fullyScoredStructures': complete_rows,
                          'uncoveredStructures': len(rows)-complete_rows, 'allRequestedMembersPreserved': True}
    if complete_rows != len(rows):
        output['status'] = 'partial-coverage'
    for stratum in [None, *sorted(set(sources))]:
        endpoint_results = {}
        for task in tasks:
            selected = [i for i, identifier in enumerate(identifiers) if task in labels[identifier] and (stratum is None or sources[i] == stratum)]
            y = [labels[identifiers[i]][task] for i in selected]
            column = actual.index(task) if task in actual else None
            p = [values[i][column] if column is not None else None for i in selected]
            covered = [type(value) in (float, int) and math.isfinite(value) and 0 <= value <= 1 for value in p]
            denominator = sum(stratum is None or source == stratum for source in sources)
            if not all(covered):
                result = {'count': len(y), 'positives': sum(y), 'negatives': len(y)-sum(y), 'coveredCount': sum(covered),
                          'positiveRate': sum(y)/len(y) if y else None,
                          'status': 'coverage-incomplete', **{metric: None for metric in METRICS}, 'confidenceIntervals': None}
                output['status'] = 'partial-coverage'
            else:
                result = point_metrics(y, p)
                result['coveredCount'] = len(y)
                result['confidenceIntervals'] = cluster_bootstrap(y, p, [groups[i] for i in selected], replicates=bootstrap_replicates, seed=seed,
                    minimum_per_class=protocol.get('minimumBootstrapPerClass', 10), minimum_groups=protocol.get('minimumBootstrapGroups', 10))
                if (protocol['role'] == 'independent-evaluation' and result['status'] == 'computed'
                        and min(result['positives'], result['negatives']) < protocol.get('minimumBootstrapPerClass', 10)):
                    # New independent evidence needs the predeclared ranking
                    # support. Retrospective point estimates remain untouched.
                    result.update(auroc=None, averagePrecision=None, status='insufficient-class-support')
            result['smallSampleWarning'] = min(result['positives'], result['negatives']) < protocol.get('minimumBootstrapPerClass', 10)
            result.update(missingLabelCount=denominator-len(y), coverageDenominator=len(y),
                coverageFraction=sum(covered)/len(y) if y else None,
                observedMemberOrderSha256=digest([identifiers[i] for i in selected]),
                coveredMemberOrderSha256=digest([identifiers[i] for i, ok in zip(selected, covered) if ok]))
            endpoint_results[task] = result
        if stratum is None:
            output['endpoints'] = endpoint_results
        else:
            output['sourceStrata'][stratum] = endpoint_results
    return output


def evaluate_paired_models(rows, predictions_by_model, protocol, *, bootstrap_replicates=500, seed=42):
    tasks, checksum = _protocol(protocol)
    if not isinstance(predictions_by_model, dict) or len(predictions_by_model) < 2:
        raise ValueError('At least two predeclared actual models are required for comparison')
    evaluations = {name: evaluate_predictions(rows, prediction, protocol, bootstrap_replicates=bootstrap_replicates, seed=seed)
                   for name, prediction in predictions_by_model.items()}
    deltas = []
    for first, second in itertools.combinations(predictions_by_model, 2):
        aligned = _aligned(rows, predictions_by_model[first], protocol)
        _, identifiers, labels, groups, sources, first_tasks, p = aligned
        second_tasks, q = predictions_by_model[second]['taskIds'], predictions_by_model[second]['scores']
        per_task = {}
        for task in tasks:
            a, b = evaluations[first]['endpoints'][task], evaluations[second]['endpoints'][task]
            valid = a.get('coveredCount') == a['count'] and b.get('coveredCount') == b['count'] and task in first_tasks and task in second_tasks
            selected = [i for i, identifier in enumerate(identifiers) if task in labels[identifier]]
            common = [i for i in selected if task in first_tasks and task in second_tasks
                and all(type(score) in (float, int) and math.isfinite(score) and 0 <= score <= 1
                        for score in (p[i][first_tasks.index(task)], q[i][second_tasks.index(task)]))]
            per_task[task] = {'status': ('no-observed-labels' if not selected else 'paired') if valid else 'coverage-incomplete', 'count': len(selected),
                              'positiveRate': sum(labels[identifiers[i]][task] for i in selected)/len(selected) if selected else None,
                              'missingLabelCount': len(rows)-len(selected), 'coverageDenominator': len(selected),
                              'coverageFraction': len(common)/len(selected) if selected else None,
                              'commonEvaluableMemberCount': len(common),
                              'commonEvaluableMemberOrderSha256': digest([identifiers[i] for i in common]),
                              'deltaSecondMinusFirst': {metric: b[metric]-a[metric] if valid and a[metric] is not None and b[metric] is not None else None for metric in METRICS},
                              'confidenceIntervals': cluster_bootstrap([labels[identifiers[i]][task] for i in selected], [p[i][first_tasks.index(task)] for i in selected],
                                [groups[i] for i in selected], other_scores=[q[i][second_tasks.index(task)] for i in selected], replicates=bootstrap_replicates, seed=seed,
                                minimum_per_class=protocol.get('minimumBootstrapPerClass', 10), minimum_groups=protocol.get('minimumBootstrapGroups', 10)) if valid else None}
        deltas.append({'firstModel': first, 'secondModel': second, 'direction': 'second-minus-first; AUROC/AP higher and Brier/ECE lower are favorable', 'endpoints': per_task})
    return {'schema': 'aidd.independent-paired-comparison.v1', 'role': protocol['role'], 'protocolSha256': checksum,
            'formalAvailability': False, 'models': evaluations, 'pairedDifferences': deltas,
            'selectionPermitted': False, 'labelsAndMembersMatched': True, 'seed': seed,
            'scientificLimit': 'No clinical validation or independent seed-level significance claim.'}
