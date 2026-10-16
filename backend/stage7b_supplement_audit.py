"""Additive Stage 7B checks. No results, label mapping, model or evaluation IO.

The Stage 7 pilot and fixed historical reference contract remain unchanged.
Expanded structure metadata reuse that audit and additionally exclude the full
SHA-pinned historical Karim/Tox21 sources, projecting structural columns only.
"""
from __future__ import annotations

from collections import Counter
import csv
from pathlib import Path
from backend.candidate_data import SOURCES
from backend.chemistry import canonicalize_smiles
from backend.dataset_overlap_audit import (
    file_sha256, historical_forbidden_index, object_sha256, structural_overlap,
)
from backend.independent_validation_data import _group_rows, frozen_evaluation_settings
from backend.stage7_audit import SurveyBundle, audit_survey
from backend.v3_data import build_forbidden_index, leakage_identity

EXPANDED_SCHEMA = 'aidd.stage7.chembl-expanded-labelblind.v1'
HISTORY_SPECS = {
    'historical-full-herg-karim': {'relative': 'data/candidates/raw/herg_karim.tsv',
        'sha256': next(item['sha256'] for item in SOURCES if item['id'] == 'herg_karim'),
        'delimiter': '\t', 'column': 'Drug'},
    'historical-full-tox21': {'relative': 'data/candidates/raw/tox21.csv',
        'sha256': next(item['sha256'] for item in SOURCES if item['id'] == 'tox21'),
        'delimiter': ',', 'column': 'X'},
}


def full_source_structure_references(root):
    """Hash source files, then inspect only Drug/X; never inspect a Y/task cell.

    Invalid/unsupported historical structures remain a documented completeness
    limitation. They are not replaced, repaired using labels, or silently lost.
    Returned structure lists are private, only receipt is suitable for export.
    """
    structures, receipts, blockers = {}, {}, []
    for role, specification in HISTORY_SPECS.items():
        path = Path(root) / specification['relative']
        entry = {'expectedSourceSha256': specification['sha256'], 'labelsRead': False,
                 'inputRows': None, 'validStructureRows': None,
                 'uniqueCanonicalStructures': None, 'unsupportedOrMissingRows': None}
        receipts[role] = entry
        if not path.is_file() or path.is_symlink() or file_sha256(path) != specification['sha256']:
            entry['status'] = 'missing-or-mismatched-pinned-source'
            blockers.append(role + ':missing-or-mismatched-pinned-source')
            continue
        counts, canonicals = Counter(), set()
        try:
            with path.open(encoding='utf-8-sig', newline='') as handle:
                rows = csv.reader(handle, delimiter=specification['delimiter'])
                header = next(rows)
                if len(header) != len(set(header)) or specification['column'] not in header:
                    raise ValueError('Historical structure projection column is ambiguous')
                column = header.index(specification['column'])
                for row in rows:
                    counts['input'] += 1
                    try:
                        if len(row) != len(header):
                            raise ValueError('Malformed historical record')
                        canonical = canonicalize_smiles(row[column])
                        leakage_identity(canonical)
                    except (ValueError, TypeError, RuntimeError):
                        counts['unsupported'] += 1
                        continue
                    counts['valid'] += 1
                    canonicals.add(canonical)
        except (ValueError, StopIteration, csv.Error, UnicodeError):
            entry['status'] = 'invalid-structure-projection'
            blockers.append(role + ':invalid-structure-projection')
            continue
        structures[role] = sorted(canonicals)
        entry.update({'status': 'verified-structure-projection', 'inputRows': counts['input'],
                      'validStructureRows': counts['valid'], 'uniqueCanonicalStructures': len(canonicals),
                      'unsupportedOrMissingRows': counts['unsupported'],
                      'structureMembersSha256': object_sha256(structures[role])})
        if counts['unsupported']:
            blockers.append(role + ':unresolved-historical-structures')
    receipt = {'schema': 'aidd.stage7b.full-source-history.v1', 'sources': receipts,
               'complete': not blockers and set(structures) == set(HISTORY_SPECS),
               'blockingReasons': sorted(blockers), 'labelsRead': False,
               'historicalLabelsUsedForSelection': False}
    receipt['receiptSha256'] = object_sha256(receipt)
    return {'structures': structures, 'receipt': receipt}


def _combined_screen(activities, forbidden, complete):
    """Same identity/group/duplicate policy, with additive historical members."""
    canonical_positions, projected, invalid = {}, {}, Counter()
    molecule_counts = Counter(row['molecule_chembl_id'] for row in activities)
    for position, row in enumerate(activities):
        try:
            canonical = canonicalize_smiles(row['canonical_smiles'])
            identity, overlaps = structural_overlap(canonical, forbidden)
        except (ValueError, RuntimeError, TypeError):
            invalid['invalid-or-missing-structure'] += 1
            continue
        canonical_positions.setdefault(canonical, []).append(position)
        projected.setdefault(canonical, {'canonicalSmiles': canonical,
                                         'auditIdentity': identity, 'overlaps': overlaps})
    structures = list(projected.values())
    _group_rows(structures)
    polluted = {row['groupId'] for row in structures if row['overlaps']}
    direct = {row['canonicalSmiles'] for row in structures if row['overlaps']}
    connected = {row['canonicalSmiles'] for row in structures if not row['overlaps'] and row['groupId'] in polluted}
    passed, exclusive, levels, roles = set(), Counter(invalid), Counter(), Counter()
    for item in structures:
        canonical = item['canonicalSmiles']
        positions = canonical_positions[canonical]
        levels.update({value.rsplit(':', 1)[1] for value in item['overlaps']})
        roles.update({value.rsplit(':', 1)[0] for value in item['overlaps']})
        if canonical in direct:
            reason = 'historical-structure-overlap'
        elif canonical in connected:
            reason = 'historical-overlap-connected-component'
        elif len(positions) > 1 or any(molecule_counts[activities[p]['molecule_chembl_id']] > 1 for p in positions):
            reason = 'duplicate-structure-or-molecule'
        else:
            passed.add(canonical)
            reason = 'pending-task-source-rights-custodian-review'
        exclusive[reason] += len(positions)
    return {'historyComplete': complete, 'inputRecords': len(activities),
            'distinctCanonicalStructures': len(structures),
            'directHistoricalOverlapDistinctStructures': len(direct),
            'connectedOverlapDistinctStructures': len(connected),
            'observedDisjointDistinctStructures': len(structures) - len(direct | connected),
            'observedStructureAndDuplicatePasses': len(passed),
            'verifiedStructureAndDuplicatePasses': len(passed) if complete else None,
            'screenStatus': 'completed-structure-only' if complete else 'provisional-incomplete-history',
            'nonAdditiveHistoricalOverlapByIdentity': dict(sorted(levels.items())),
            'nonAdditiveHistoricalOverlapByRole': dict(sorted(roles.items())),
            'exclusiveRecordDisposition': dict(sorted(exclusive.items())),
            'sourceIndependent': False, 'independentlyQualifiedStructures': 0,
            'labelsRead': False, 'candidateResultFieldsUsed': False}


def audit_expanded(bundle, references, *, root):
    # Reuse the complete original strict nested schema, including every field
    # type. Only the top-level survey schema and bounded member cap differ.
    if not isinstance(bundle, dict) or bundle.get('schema') != EXPANDED_SCHEMA:
        raise ValueError('Unsupported expanded survey schema')
    if not isinstance(bundle.get('activities'), list) or len(bundle['activities']) > 2000:
        raise ValueError('Expanded acquisition exceeds the frozen member cap')
    legacy = {**bundle, 'schema': 'aidd.stage7.chembl-labelblind.v1'}
    validated = SurveyBundle.model_validate(legacy).model_dump(by_alias=True)
    base = audit_survey(validated, references)
    supplemental = full_source_structure_references(root)
    previous, _ = historical_forbidden_index(references)
    extra, _ = build_forbidden_index(supplemental['structures'])
    combined = {key: list(values) for key, values in previous.items()}
    for token, roles in extra.items():
        combined[token] = sorted(set(combined.get(token, [])) | set(roles))
    screen = _combined_screen(validated['activities'], combined,
                              references['completeReferences'] and supplemental['receipt']['complete'])
    result = {'schema': 'aidd.stage7b.expanded-candidate-audit.v1',
              'status': 'STAGE7C_BLOCKED', 'qualified': False, 'canEnterStage7C': False,
              'expandedBundleSchema': EXPANDED_SCHEMA, 'expandedBundleSha256': object_sha256(bundle),
              'originalAuditInputSha256': base['surveySha256'], 'baseFixedReferenceAudit': base,
              'additiveFullSourceHistory': supplemental['receipt'], 'combinedStructureScreen': screen,
              'originalSourceAndStructurePolicyChanged': False,
              'historicalSourceStructureCoverageExtended': True,
              'candidateOutcomeClassification': {'status': 'not-run-independent-custodian-required',
                  'exactIC50Records': None, 'rangeRecords': None, 'inequalityRecords': None,
                  'missingValueRecords': None, 'qualityAnomalyRecords': None,
                  'positiveCount': None, 'negativeCount': None},
              'labelsRead': False, 'modelsExecuted': False, 'performanceEvaluated': False,
              'formalAvailability': False, 'pretrainingStructureExposure': 'unknown',
              'blockingReasons': sorted(set(base['blockingReasons'] + supplemental['receipt']['blockingReasons']
                  + ['original-herg-label-contract-unresolved', 'independent-custodian-not-established',
                     'outcome-qualifier-and-quality-review-not-performed']))}
    result['auditSha256'] = object_sha256(result)
    return result


def eligibility_gate(chembl, epa, herg_contract):
    """This pre-review gate cannot grant label access or scientific readiness.

    Even a structural pass is not a reviewed independent CandidateMetadata /
    frozen IndependentProtocol. No self-declared provider result can enable
    evaluation, labels or a STAGE7C_CANDIDATE_READY status in this stage.
    """
    if (chembl.get('schema') != 'aidd.stage7b.expanded-candidate-audit.v1'
            or herg_contract.get('schema') != 'aidd.stage7b.herg-contract-evidence.v1'
            or epa.get('schema') != 'aidd.stage7b.epa-structure-audit.v1'
            or chembl.get('labelsRead') is not False or epa.get('candidateLabelsRead') is not False
            or herg_contract.get('labelsRead') is not False):
        raise ValueError('Gate inputs must be actual label-blind audit reports')
    if (chembl.get('modelsExecuted') is not False or chembl.get('performanceEvaluated') is not False
            or epa.get('modelsExecuted') is not False or epa.get('evaluationPerformed') is not False):
        raise ValueError('Candidate prediction/evaluation is forbidden at Stage 7B')
    reasons = sorted(set(chembl['blockingReasons'] + [
        'original-threshold-operator-unit-boundary-and-assay-contract-not-approved',
        'epa-tested-members-and-release-specific-structure-mapping-unverified',
        'source-population-publication-assay-batch-independence-not-reviewed',
        'specific-use-license-review-incomplete',
        'independent-custodian-label-support-attestation-missing',
        'frozen-approved-label-policy-and-evaluation-protocol-missing']))
    settings = frozen_evaluation_settings()
    return {'schema': 'aidd.stage7b.eligibility-gate.v1', 'status': 'STAGE7C_BLOCKED',
            'canEnterStage7C': False, 'formalAvailability': False,
            'chemblAuditSha256': object_sha256(chembl), 'epaAuditSha256': object_sha256(epa),
            'hergContractEvidenceSha256': object_sha256(herg_contract),
            'blockingReasons': reasons, 'developerLabelBlindnessMaintained': True,
            'independentCustodianSeparationVerified': False, 'labelsRead': False,
            'labelMappingPerformed': False, 'modelsExecuted': False,
            'performanceEvaluated': False, 'evaluationProtocolFrozen': False,
            'approvedLabelPolicyFrozen': False, 'auditAcquisitionPlanIsEvaluationProtocol': False,
            'supportRequirements': {'version': settings['version'],
                'minimumPerClass': settings['minimumBootstrapPerClass'],
                'minimumScaffoldClusters': settings['minimumBootstrapGroups'],
                'countsVisibleToDeveloper': False, 'supportAttestationStatus': 'missing-independent-custodian-attestation',
                'minimumSupportIsPowerAnalysis': False},
            'candidateClassCounts': None, 'scientificMetrics': None,
            'nextAction': 'Obtain approved original task evidence, release-bound source mapping, rights and independent custodianship; remain in Stage7B.'}
