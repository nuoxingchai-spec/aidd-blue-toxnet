"""Stage 7B prequalification of projected ChEMBL metadata; never read labels.

This is deliberately not an independent-evaluation approval path. It accepts
only the bounded label-blind projection, uses the existing historical identity
policy, and returns aggregates. Source/rights/task evidence remains unapproved.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from rdkit import rdBase

from backend.chemistry import CHEMISTRY_VERSION, canonicalize_smiles
from backend.dataset_overlap_audit import (
    historical_forbidden_index, lineage_overlap, object_sha256,
    structural_overlap, validate_references,
)
from backend.independent_validation_data import _group_rows
from backend.v3_data import IDENTITY_POLICY, IDENTITY_VERSION


class _Projection(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


ChemblId = Annotated[StrictStr, Field(pattern=r'^CHEMBL[0-9]+$', max_length=40)]
Text = Annotated[StrictStr, Field(min_length=1, max_length=4000)]
SmallText = Annotated[StrictStr, Field(min_length=1, max_length=200)]
PositiveId = Annotated[StrictInt, Field(ge=0)]


class TargetProjection(_Projection):
    target_chembl_id: Literal['CHEMBL240']
    pref_name: SmallText
    organism: Literal['Homo sapiens']
    target_type: Literal['SINGLE PROTEIN']


class ActivityProjection(_Projection):
    activity_id: PositiveId
    molecule_chembl_id: ChemblId
    canonical_smiles: Annotated[StrictStr, Field(max_length=4096)] | None
    assay_chembl_id: ChemblId
    document_chembl_id: ChemblId | None
    src_id: PositiveId
    record_id: PositiveId
    standard_type: SmallText | None
    standard_units: SmallText | None
    type: SmallText | None
    units: SmallText | None


class AssayProjection(_Projection):
    assay_chembl_id: ChemblId
    assay_type: SmallText | None
    assay_organism: SmallText | None
    assay_tax_id: PositiveId | None
    assay_cell_type: SmallText | None
    assay_tissue: SmallText | None
    confidence_score: Annotated[StrictInt, Field(ge=0, le=9)] | None
    relationship_type: SmallText | None
    document_chembl_id: ChemblId | None
    src_id: PositiveId | None


class DocumentProjection(_Projection):
    document_chembl_id: ChemblId
    doi: SmallText | None
    pubmed_id: SmallText | PositiveId | None
    title: Text | None
    journal: SmallText | None
    year: Annotated[StrictInt, Field(ge=1800, le=2200)] | None
    src_id: PositiveId | None


class SurveyBundle(_Projection):
    schema_version: Literal['aidd.stage7.chembl-labelblind.v1'] = Field(alias='schema')
    version: Literal['ChEMBL_37']
    target: TargetProjection
    activities: list[ActivityProjection] = Field(max_length=200000)
    assays: list[AssayProjection] = Field(max_length=200000)
    documents: list[DocumentProjection] = Field(max_length=200000)


# Unknown endpoint/unit text is not copied into the public artifact. These are
# metadata categories, not conversion rules or approval of task equivalence.
_ENDPOINTS = {'IC50', 'Ki', 'Kd', 'EC50', 'AC50', 'GI50', 'LC50', 'ED50',
              'EC90', 'IC90', 'Inhibition', 'Activity', 'Potency', 'Ratio'}
_UNITS = {'nM', 'uM', 'µM', 'μM', 'mM', 'M', '%', 'pM', 'fM',
          'ug.mL-1', 'mg.mL-1', 'ng.mL-1', 'dimensionless'}
_GLOBAL_BLOCKERS = (
    'task-equivalence-not-reviewed',
    'local-research-rights-not-reviewed',
    'source-population-independence-not-reviewed',
    'historical-publication-assay-cohort-mapping-incomplete',
    'historical-herg-literature-source-family-not-disjoint',
)


def _bucket(value, allowed):
    return value if value in allowed else ('unspecified' if value is None else 'other-unreviewed')


def _index(rows, key):
    result = {}
    for row in rows:
        if row[key] in result:
            raise ValueError('Duplicate metadata identifiers make source joins ambiguous')
        result[row[key]] = row
    return result


def _metadata_reasons(row, assays, documents):
    reasons = set()
    assay = assays.get(row['assay_chembl_id'])
    document = documents.get(row['document_chembl_id'])
    if assay is None:
        reasons.add('missing-assay-mapping')
    else:
        if assay['document_chembl_id'] != row['document_chembl_id']:
            reasons.add('activity-assay-document-mismatch')
        if assay['src_id'] is None or assay['src_id'] != row['src_id']:
            reasons.add('activity-assay-source-mismatch')
        if assay['document_chembl_id'] is None or assay['document_chembl_id'] not in documents:
            reasons.add('missing-assay-document-mapping')
        if assay['assay_organism'] != 'Homo sapiens' or assay['assay_tax_id'] != 9606:
            reasons.add('human-assay-species-not-established')
        if assay['confidence_score'] != 9 or assay['relationship_type'] != 'D':
            reasons.add('direct-target-mapping-not-established')
    if document is None:
        reasons.add('missing-activity-document-mapping')
    else:
        if document['src_id'] is None or document['src_id'] != row['src_id']:
            reasons.add('activity-document-source-mismatch')
        if not document['doi'] and not document['pubmed_id']:
            reasons.add('missing-original-publication-identifier')
    if row['standard_type'] is None or row['standard_units'] is None:
        reasons.add('missing-endpoint-or-unit')
    return reasons


def audit_survey(bundle, references):
    """Return a public aggregate; never qualify, seal, predict or inspect labels.

    Repeated molecular identities are all quarantined rather than selecting an
    experiment using its unobserved result. Distinct canonical structures are
    grouped before any exclusion so salt/tautomer bridges cannot evade history.
    Reason counts are non-additive; ``exclusiveRecordExclusions`` partitions all
    input records using the documented precedence.
    """
    survey = SurveyBundle.model_validate(bundle).model_dump(by_alias=True)
    validate_references(references)
    forbidden, reference_counts = historical_forbidden_index(references)
    activities = survey['activities']
    assays = _index(survey['assays'], 'assay_chembl_id')
    documents = _index(survey['documents'], 'document_chembl_id')
    activity_ids = Counter(row['activity_id'] for row in activities)
    if any(count > 1 for count in activity_ids.values()):
        raise ValueError('Duplicate activity identifiers: survey pagination is ambiguous')
    molecule_ids = Counter(row['molecule_chembl_id'] for row in activities)
    canonical_rows = defaultdict(list)
    valid_rows, invalid_reasons, metadata_reasons = {}, {}, {}
    endpoint_units = Counter()
    species = Counter()
    assay_types = Counter()
    reasons = Counter()
    source_publications = set()
    for document in documents.values():
        if document['doi']:
            source_publications.add(document['doi'])
        if document['pubmed_id']:
            source_publications.add('pubmed:' + str(document['pubmed_id']))
    source_overlaps = lineage_overlap({
        'sourceId': 'chembl-37-herg-labelblind-survey',
        'lineage': {'families': ['herg-literature-compilations'],
                    'publicationIds': sorted(source_publications),
                    'cohortIds': [], 'assayIds': list(assays)},
    }, references)
    for position, row in enumerate(activities):
        endpoint_units[(_bucket(row['standard_type'], _ENDPOINTS), _bucket(row['standard_units'], _UNITS))] += 1
        assay = assays.get(row['assay_chembl_id'])
        organism = None if assay is None else assay['assay_organism']
        species['human' if organism == 'Homo sapiens' else ('unspecified' if not organism else 'non-human')] += 1
        assay_types[_bucket(None if assay is None else assay['assay_type'], {'B', 'F', 'A', 'T', 'P', 'U'})] += 1
        metadata_reasons[position] = _metadata_reasons(row, assays, documents)
        reasons.update(metadata_reasons[position])
        if row['canonical_smiles'] is None or not row['canonical_smiles'].strip():
            invalid_reasons[position] = 'missing-structure'
            continue
        try:
            canonical = canonicalize_smiles(row['canonical_smiles'])
            identity, overlaps = structural_overlap(canonical, forbidden)
        except (ValueError, RuntimeError, TypeError):
            invalid_reasons[position] = 'invalid-or-unsupported-structure'
            continue
        canonical_rows[canonical].append(position)
        valid_rows[position] = {'canonicalSmiles': canonical, 'auditIdentity': identity, 'overlaps': overlaps}
    # All distinct valid structures enter the component analysis, including
    # duplicate-containing identities, before any row is discarded.
    structures = [dict(valid_rows[positions[0]]) for positions in canonical_rows.values()]
    _group_rows(structures)
    contaminated = {row['groupId'] for row in structures if row['overlaps']}
    direct = {row['canonicalSmiles'] for row in structures if row['overlaps']}
    connected = {row['canonicalSmiles'] for row in structures if not row['overlaps'] and row['groupId'] in contaminated}
    duplicate_canonical = {canonical for canonical, positions in canonical_rows.items() if len(positions) > 1}
    duplicate_molecule = {key for key, count in molecule_ids.items() if count > 1}
    overlap_levels, overlap_roles = Counter(), Counter()
    for row in structures:
        overlap_levels.update({item.rsplit(':', 1)[1] for item in row['overlaps']})
        overlap_roles.update({item.rsplit(':', 1)[0] for item in row['overlaps']})
    exclusive = Counter()
    screen_passed = set()
    for position, row in enumerate(activities):
        row_reasons = set(_GLOBAL_BLOCKERS)
        if not references['completeReferences']:
            row_reasons.add('incomplete-historical-references')
        if position in invalid_reasons:
            exclusion = invalid_reasons[position]
            row_reasons.add(exclusion)
        else:
            canonical = valid_rows[position]['canonicalSmiles']
            if canonical in direct:
                row_reasons.add('historical-structure-overlap')
            if canonical in connected:
                row_reasons.add('historical-overlap-connected-component')
            if canonical in duplicate_canonical:
                row_reasons.add('duplicate-canonical-structure')
            if row['molecule_chembl_id'] in duplicate_molecule:
                row_reasons.add('duplicate-molecule-identifier')
            exclusion = next((reason for reason in (
                'historical-structure-overlap', 'historical-overlap-connected-component',
                'duplicate-canonical-structure', 'duplicate-molecule-identifier') if reason in row_reasons), None)
            if exclusion is None:
                if references['completeReferences']:
                    screen_passed.add(canonical)
                exclusion = ('unresolved-record-metadata' if metadata_reasons[position]
                             else 'unresolved-task-rights-and-source-review')
        reasons.update(row_reasons)
        exclusive[exclusion] += 1
    blockers = list(_GLOBAL_BLOCKERS)
    if not references['completeReferences']:
        blockers.append('incomplete-historical-references')
    if not activities:
        blockers.append('no-real-candidate-records')
    result = {
        'schema': 'aidd.stage7.candidate-audit.v1', 'stage': 'Stage7B',
        'status': 'not-independently-qualified', 'qualified': False, 'canEnterStage7C': False,
        'sourceVersion': survey['version'], 'surveySha256': object_sha256(survey),
        'referencesSha256': references['receiptSha256'], 'completeReferences': references['completeReferences'],
        'inputRecordCount': len(activities), 'uniqueMoleculeIdCount': len(molecule_ids),
        'assayMetadataCount': len(assays), 'documentMetadataCount': len(documents),
        'validStructureRecordCount': len(valid_rows),
        'missingStructureRecordCount': sum(reason == 'missing-structure' for reason in invalid_reasons.values()),
        'invalidStructureRecordCount': sum(reason == 'invalid-or-unsupported-structure' for reason in invalid_reasons.values()),
        'distinctCanonicalStructureCount': len(structures),
        'duplicateCanonicalGroupCount': len(duplicate_canonical),
        'duplicateCanonicalRecordCount': sum(len(canonical_rows[key]) for key in duplicate_canonical),
        'duplicateMoleculeIdCount': len(duplicate_molecule),
        'duplicateMoleculeRecordCount': sum(molecule_ids[key] for key in duplicate_molecule),
        'directHistoricalOverlapDistinctStructureCount': len(direct),
        'connectedOverlapDistinctStructureCount': len(connected),
        'structurallyDisjointDistinctStructureCount': len(structures) - len(direct | connected),
        'structureAndDuplicateScreenPassedDistinctCount': len(screen_passed),
        'independentlyQualifiedStructureCount': 0,
        'historicalOverlapDistinctStructuresByIdentity': dict(sorted(overlap_levels.items())),
        'historicalOverlapDistinctStructuresByRole': dict(sorted(overlap_roles.items())),
        'exclusiveRecordExclusions': dict(sorted(exclusive.items())),
        'nonAdditiveRecordReasonCounts': dict(sorted(reasons.items())),
        'endpointUnitRecordGroups': [{'endpoint': endpoint, 'unit': unit, 'recordCount': count}
                                    for (endpoint, unit), count in sorted(endpoint_units.items())],
        'assaySpeciesRecordCounts': dict(sorted(species.items())),
        'assayTypeRecordCounts': dict(sorted(assay_types.items())),
        'historicalReferenceCounts': reference_counts, 'sourceOverlap': source_overlaps,
        'exactPublicationAssayCohortIndependence': 'unresolved-incomplete-historical-row-provenance',
        'sourceFamilyPolicy': 'conservative-herg-literature-family-exclusion-pending-provenance',
        'blockingReasons': sorted(blockers),
        'labelsRead': False, 'labelsIncluded': False, 'positiveCount': None, 'negativeCount': None,
        'independentAccuracyEvaluated': False, 'sealingPerformed': False,
        'oldTestLabelsUsedForSelection': False,
        'chemistryVersion': CHEMISTRY_VERSION, 'identityVersion': IDENTITY_VERSION,
        'identityPolicy': IDENTITY_POLICY, 'rdkitVersion': rdBase.rdkitVersion,
        'pretrainingStructureExposure': 'unknown',
        'limitations': [
            'Metadata survey only; no endpoint/unit conversion or binary positive definition is inferred.',
            'The human target organism does not establish the species or conditions of each assay.',
            'Structural disjointness does not establish experiment/source-population independence.',
            'All acyclic structures share one conservative identity group.',
            'Reason counts overlap; exclusive record exclusions partition the input records.',
            'A bounded survey cannot establish eligibility or absence of eligible data in the full release.',
        ],
    }
    return result
