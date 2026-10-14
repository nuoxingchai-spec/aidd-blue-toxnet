"""Separately bound, research-only calibration; never mutate model manifests."""
from __future__ import annotations

import math
import numpy as np

from backend.chemistry import canonicalize_smiles
from backend.independent_validation_metrics import digest, point_metrics, valid_sha
from backend.v3_data import identity_tokens, leakage_identity

METHODS = ('platt', 'temperature', 'isotonic')


def model_binding_sha256(binding):
    return digest({key: binding.get(key) for key in ('manifestSha256', 'weightsSha256', 'preprocessingSha256',
        'taskContractVersion', 'taskContractHashes', 'encoder', 'trainingReferenceSha256')})


def audit_role_isolation(identity_roles):
    """Require exact, parent, tautomer, scaffold AND provenance separation.

    The conservative historical audit groups all acyclic molecules together.
    That intentional restriction is preserved and can make calibration empty.
    """
    if not isinstance(identity_roles, dict) or set(identity_roles) != {'train', 'calibration', 'evaluation'}:
        raise ValueError('Separate train, calibration and evaluation identity/source records are required')
    seen_tokens, seen_sources, seen_lineage, counts, membership, source_membership = {}, {}, {}, {}, {}, {}
    for role, rows in identity_roles.items():
        if not rows:
            raise ValueError('Every declared calibration role requires audited membership')
        members, sources = [], []
        for row in rows:
            canonical = canonicalize_smiles(row.get('canonicalSmiles', row.get('smiles')))
            identifier = digest(canonical)  # Internal membership digest, not a SMILES substitute in prediction contracts.
            source, source_row = row.get('sourceId'), row.get('sourceRowId')
            if not isinstance(source, str) or not source or not isinstance(source_row, str) or not source_row:
                raise ValueError('Source and source-row provenance are required for all calibration roles')
            for token in identity_tokens(leakage_identity(canonical)):
                if seen_tokens.setdefault(token, role) != role:
                    raise ValueError('Cross-role chemical identity/parent/scaffold overlap blocks calibration')
            source_key = (source, source_row)
            if seen_sources.setdefault(source_key, role) != role:
                raise ValueError('Cross-role source-record overlap blocks calibration')
            for field in ('publicationIds', 'cohortIds', 'assayIds'):
                lineage = row.get(field, [])
                if not isinstance(lineage, list) or any(not isinstance(item, str) or not item.strip() for item in lineage):
                    raise ValueError('Declared source lineage must contain explicit identifiers')
                for item in lineage:
                    key = (field, item.strip().casefold())
                    if seen_lineage.setdefault(key, role) != role:
                        raise ValueError('Cross-role publication/cohort/assay lineage overlap blocks calibration')
            sources.append(source_key)
            members.append(identifier)
        if len(members) != len(set(members)):
            raise ValueError('Duplicate calibration role members are forbidden')
        counts[role], membership[role] = len(members), digest(members)
        source_membership[role] = digest(sources)
    return {'status': 'passed', 'counts': counts, 'membershipSha256': membership,
            'sourceMembershipSha256': source_membership,
            'policy': 'canonical, stereo/isotope-equivalent, parent, tautomer and scaffold separation plus source-row and declared lineage separation',
            'lineageCompleteness': 'only explicitly supplied lineage audited; no absent provenance inferred'}


def _logit(p):
    clipped = np.clip(p, 1e-6, 1-1e-6)
    return np.log(clipped/(1-clipped))


def _fit_input_digest(p, y, mask, tasks, member_hash):
    return digest({'scores': p.tolist(), 'observedLabels': [[int(y[i, j]) if mask[i, j] else None for j in range(len(tasks))] for i in range(len(p))],
                   'mask': mask.tolist(), 'tasks': list(tasks), 'calibrationMembershipSha256': member_hash})


def fit_calibrators(scores, labels, masks, *, tasks, method, model_binding, role_binding,
                    identity_roles, minimum_per_class=20):
    if method not in METHODS or role_binding.get('role') != 'calibration' or role_binding.get('predeclaredMethod') != method:
        raise ValueError('Calibration requires a frozen calibration-only role and predeclared method; no evaluation selection')
    if (role_binding.get('evaluationUsedForSelection') is not False or role_binding.get('labelReadAuthorized') is not True
            or not valid_sha(role_binding.get('protocolSha256')) or not valid_sha(role_binding.get('datasetManifestSha256'))):
        raise ValueError('Explicit label authorization and sealed protocol/data SHA256 are required')
    if (model_binding.get('artifactVerified') is not True
            or any(not valid_sha(model_binding.get(key)) for key in ('weightsSha256', 'manifestSha256', 'trainingReferenceSha256'))):
        raise ValueError('Verified actual model binding is required')
    if type(minimum_per_class) is not int or minimum_per_class < 2:
        raise ValueError('Calibration minimum support must be predeclared and at least two')
    audit = audit_role_isolation(identity_roles)
    if (role_binding.get('calibrationMembershipSha256') != audit['membershipSha256']['calibration']
            or role_binding.get('calibrationSourceMembershipSha256') != audit['sourceMembershipSha256']['calibration']
            or role_binding.get('taskContractHashes') != {task: model_binding.get('taskContractHashes', {}).get(task) for task in tasks}
            or role_binding.get('minimumPerClass') != minimum_per_class):
        raise ValueError('Calibration role must freeze exact member/source order, label semantics and minimum support')
    train_order = [canonicalize_smiles(row.get('canonicalSmiles', row.get('smiles'))) for row in identity_roles['train']]
    if digest(train_order) != model_binding.get('trainingStructureOrderSha256'):
        raise ValueError('Role train membership differs from the original verified model training references')
    p, y, mask = (np.asarray(value, dtype=float) for value in (scores, labels, masks))
    if (p.ndim != 2 or p.shape != y.shape or y.shape != mask.shape or p.shape != (len(identity_roles['calibration']), len(tasks))
            or not np.isfinite(p).all() or ((p < 0) | (p > 1)).any() or not np.isin(mask, (0, 1)).all()
            or not np.isin(y[mask == 1], (0, 1)).all() or len(set(tasks)) != len(tasks)
            or set(tasks)-set(model_binding.get('taskContractHashes', {}))):
        raise ValueError('Calibration arrays, observed masks and actual model task coverage must align')
    artifact = {'schema': 'aidd.research-calibration.v1', 'status': 'not-fitted', 'formalAvailability': False,
                'certified': False, 'method': method, 'fitRole': 'calibration', 'modelBindingSha256': model_binding_sha256(model_binding),
                'protocolSha256': role_binding['protocolSha256'], 'datasetManifestSha256': role_binding['datasetManifestSha256'],
                'taskIds': list(tasks), 'taskContractHashes': role_binding['taskContractHashes'],
                'roleAudit': audit, 'minimumPerClass': minimum_per_class, 'endpoints': {},
                'selectionPermitted': False, 'labelMeaning': 'source-contract label probability; not clinical or human risk'}
    artifact['fitInputSha256'] = _fit_input_digest(p, y, mask, tasks, audit['membershipSha256']['calibration'])
    for index, task in enumerate(tasks):
        selected = mask[:, index] == 1
        target, raw = y[selected, index], p[selected, index]
        npos = int(target.sum())
        support = max(minimum_per_class, 100) if method == 'isotonic' else minimum_per_class
        record = {'count': len(target), 'positives': npos, 'negatives': len(target)-npos,
                  'effectiveMinimumPerClass': support, 'minimumDistinctScores': 20 if method == 'isotonic' else None,
                  'status': 'insufficient-class-support', 'parameters': None}
        if min(npos, len(target)-npos) >= support:
            if method == 'platt':
                from sklearn.linear_model import LogisticRegression
                fitted = LogisticRegression(C=1.0, solver='lbfgs', max_iter=2000).fit(_logit(raw).reshape(-1, 1), target)
                if fitted.n_iter_[0] >= 2000:
                    raise ValueError('Platt optimization did not converge; calibration not published')
                parameters = {'coefficient': float(fitted.coef_[0, 0]), 'intercept': float(fitted.intercept_[0]), 'logitClip': 1e-6}
            elif method == 'temperature':
                from scipy.optimize import minimize_scalar
                logits = _logit(raw)
                def loss(log_temperature):
                    scaled = logits/math.exp(log_temperature)
                    return float(np.mean(np.logaddexp(0, scaled)-target*scaled))
                fitted = minimize_scalar(loss, bounds=(-4, 4), method='bounded')
                if not fitted.success or not math.isfinite(fitted.fun):
                    raise ValueError('Temperature optimization failed; calibration not published')
                parameters = {'temperature': math.exp(float(fitted.x)), 'logitClip': 1e-6, 'logTemperatureBounds': [-4, 4]}
            else:
                from sklearn.isotonic import IsotonicRegression
                if len(np.unique(raw)) < 20:
                    record['status'] = 'insufficient-score-diversity'
                    artifact['endpoints'][task] = record
                    continue
                fitted = IsotonicRegression(out_of_bounds='clip').fit(raw, target)
                parameters = {'xThresholds': fitted.X_thresholds_.tolist(), 'yThresholds': fitted.y_thresholds_.tolist()}
            record.update(status='fitted-research-only', parameters=parameters)
        artifact['endpoints'][task] = record
    fitted_count = sum(row['status'] == 'fitted-research-only' for row in artifact['endpoints'].values())
    artifact['status'] = 'fitted-research-only' if fitted_count == len(tasks) else 'partially-fitted' if fitted_count else 'not-fitted'
    artifact['artifactSha256'] = digest(artifact)
    return artifact


def apply_calibrators(scores, artifact, model_binding, *, tasks):
    if (artifact.get('schema') != 'aidd.research-calibration.v1' or artifact.get('certified') is not False
            or artifact.get('formalAvailability') is not False or artifact.get('method') not in METHODS
            or artifact.get('artifactSha256') != digest({k: v for k, v in artifact.items() if k != 'artifactSha256'})
            or artifact.get('modelBindingSha256') != model_binding_sha256(model_binding)
            or list(tasks) != artifact.get('taskIds')):
        raise ValueError('Calibration integrity or actual model binding mismatch')
    p = np.asarray(scores, dtype=float)
    if p.ndim != 2 or p.shape[1] != len(tasks) or not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
        raise ValueError('Invalid input scores; no implicit replacement')
    calibrated = [[None]*len(tasks) for _ in p]
    for column, task in enumerate(tasks):
        entry = artifact['endpoints'].get(task, {})
        if entry.get('status') != 'fitted-research-only':
            continue
        parameters = entry['parameters']
        if artifact['method'] == 'isotonic':
            values = np.interp(p[:, column], parameters['xThresholds'], parameters['yThresholds'])
        else:
            logits = _logit(p[:, column])
            scaled = logits/parameters['temperature'] if artifact['method'] == 'temperature' else logits*parameters['coefficient']+parameters['intercept']
            values = 1/(1+np.exp(-np.clip(scaled, -50, 50)))
        if not np.isfinite(values).all() or ((values < 0) | (values > 1)).any():
            raise ValueError('Calibration produced invalid scores')
        for row, value in enumerate(values):
            calibrated[row][column] = float(value)
    return {'rawScores': p.tolist(), 'calibratedLabelProbabilities': calibrated, 'taskIds': list(tasks),
            'clinicalRiskProbability': False, 'certified': False, 'calibrationSha256': artifact['artifactSha256']}


def _reliability_bins(y, p):
    buckets = np.minimum((p*10).astype(int), 9)
    return [{'lower': i/10, 'upper': (i+1)/10, 'count': int((buckets == i).sum()),
             'meanPredicted': float(p[buckets == i].mean()) if (buckets == i).any() else None,
             'observedPositiveRate': float(y[buckets == i].mean()) if (buckets == i).any() else None} for i in range(10)]


def calibration_diagnostics(raw, labels, masks, artifact, model_binding, *, tasks, role_binding):
    """Calibration-set resubstitution only, never heldout method selection."""
    if (role_binding.get('role') != 'calibration' or role_binding.get('evaluationUsedForSelection') is not False
            or role_binding.get('datasetManifestSha256') != artifact.get('datasetManifestSha256')
            or role_binding.get('protocolSha256') != artifact.get('protocolSha256')
            or role_binding.get('calibrationMembershipSha256') != artifact.get('roleAudit', {}).get('membershipSha256', {}).get('calibration')):
        raise ValueError('Diagnostics require the exact calibration-only source and member binding')
    applied = apply_calibrators(raw, artifact, model_binding, tasks=tasks)
    p, y, mask = (np.asarray(value, dtype=float) for value in (raw, labels, masks))
    if (p.shape != y.shape or y.shape != mask.shape or not np.isin(mask, (0, 1)).all()
            or not np.isin(y[mask == 1], (0, 1)).all()
            or _fit_input_digest(p, y, mask, tasks, role_binding['calibrationMembershipSha256']) != artifact.get('fitInputSha256')):
        raise ValueError('Calibration diagnostic input differs from the actual fitted source snapshot')
    result = {'schema': 'aidd.calibration-diagnostics.v1', 'role': 'calibration-resubstitution', 'independentTest': False,
              'methodSelectionPermitted': False, 'calibrationSha256': artifact['artifactSha256'], 'fitInputSha256': artifact['fitInputSha256'],
              'modelBindingSha256': artifact['modelBindingSha256'], 'datasetManifestSha256': artifact['datasetManifestSha256'],
              'protocolSha256': artifact['protocolSha256'], 'membershipSha256': role_binding['calibrationMembershipSha256'],
              'sourceMembershipSha256': artifact['roleAudit']['sourceMembershipSha256']['calibration'], 'endpoints': {},
              'warning': 'In-sample calibration diagnostics are optimistic; they do not establish external calibration or permit choosing methods on evaluation labels.'}
    for index, task in enumerate(tasks):
        observed = mask[:, index] == 1
        target, before = y[observed, index], p[observed, index]
        after_values = [applied['calibratedLabelProbabilities'][i][index] for i in np.flatnonzero(observed)]
        complete = all(value is not None for value in after_values)
        after = np.asarray(after_values, dtype=float) if complete else None
        result['endpoints'][task] = {'count': len(target), 'positives': int(target.sum()),
             'status': 'computed-resubstitution' if complete else 'calibrator-not-fitted',
             'before': {key: point_metrics(target, before)[key] for key in ('brier', 'ece10Bins')},
             'after': {key: point_metrics(target, after)[key] if complete else None for key in ('brier', 'ece10Bins')},
             'reliabilityBefore': _reliability_bins(target, before),
             'reliabilityAfter': _reliability_bins(target, after) if complete else None,
             'smallSampleWarning': len(target) < 200 or min(int(target.sum()), len(target)-int(target.sum())) < 20}
    result['diagnosticSha256'] = digest(result)
    return result
