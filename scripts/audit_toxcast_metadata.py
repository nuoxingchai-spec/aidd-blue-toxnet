"""Offline EPA invitrodb 4.3 assay-metadata review; no activities or labels.

Only two fixed, SHA256-pinned official metadata workbooks are accepted. Nothing
is downloaded, evaluated, sealed or qualified by this command.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

PINS = {
    'epa-assay-annotations-v43.xlsx': 'eecbdac47fe4b4c3db765d412d15526331c269d36c7d79dde938f59ea60461f9',
    'epa-assay-target-mappings-v43.xlsx': 'bf45c47dc383c16aea9c4cf92993f7034a3141a252aa02d9f7aa7216ac84c394',
}
FIELDS = (
    {'aeid', 'assay_name', 'organism', 'ncbi_taxon_id', 'assay_design_type',
     'assay_function_type', 'normalized_data_type', 'assay_source_name'},
    {'aeid', 'official_symbol', 'assay_component_endpoint_name.x',
     'assay_component_endpoint_name.y', 'ncbi_taxon_id'},
)
NS = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def metadata_rows(raw, required):
    """Project selected metadata columns from sheet1, respecting sparse cells."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
            strings = [''.join(t.text or '' for t in item.findall('.//x:t', NS)) for item in root]
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml')).find('x:sheetData', NS)
    if sheet is None or not len(sheet):
        raise ValueError('Missing metadata worksheet/header')

    def cell_value(cell):
        value = cell.find('x:v', NS)
        text = '' if value is None else value.text or ''
        if cell.get('t') == 's':
            return strings[int(text)]
        if cell.get('t') == 'inlineStr':
            return ''.join(t.text or '' for t in cell.findall('.//x:t', NS))
        return text

    def column(cell):
        match = re.fullmatch(r'([A-Z]+)[1-9][0-9]*', cell.get('r', ''))
        if not match:
            raise ValueError('Invalid worksheet cell reference')
        return match[1]

    headers = {column(cell): cell_value(cell) for cell in sheet[0]}
    if len(headers.values()) != len(set(headers.values())) or not required <= set(headers.values()):
        raise ValueError('Missing or duplicate metadata columns')
    projection = {key: value for key, value in headers.items() if value in required}
    rows = []
    for row in list(sheet)[1:]:
        selected = {projection[column(cell)]: cell_value(cell) for cell in row if column(cell) in projection}
        rows.append({key: selected.get(key, '').strip() for key in required})
    return rows


def audit_metadata(directory):
    # Verify both inputs before interpreting any workbook. Result matrices and
    # alternate releases cannot silently replace the audited metadata sources.
    files = {}
    for name, expected in PINS.items():
        raw = (Path(directory) / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('Official metadata SHA256 mismatch')
        files[name] = raw
    annotations, mappings = [metadata_rows(raw, fields) for raw, fields in zip(files.values(), FIELDS)]
    target_ids = {row['aeid'] for row in mappings if row['official_symbol'].casefold() == 'kcnh2'}
    indexed = {}
    for row in annotations:
        if not row['aeid'].isdigit() or row['aeid'] in indexed:
            raise ValueError('Missing or duplicate assay endpoint identifier')
        indexed[row['aeid']] = row
    selected = [row for row in indexed.values()
                if row['aeid'] in target_ids or re.search(r'herg|kcnh2', row['assay_name'], re.I)]
    category = lambda value, allowed: value if value in allowed else ('unspecified' if not value else 'other-unreviewed')
    counts = lambda key, allowed: dict(sorted(Counter(category(row[key], allowed) for row in selected).items()))
    return {
        'schema': 'aidd.stage7.toxcast-metadata-audit.v1', 'sourceRelease': 'EPA invitrodb4.3',
        'doi': '10.23645/epacomptox.6062623.v11', 'inputMetadataSha256': dict(PINS),
        'endpointMetadataCount': len(selected),
        'assayDesignEndpointCounts': counts('assay_design_type', {'binding reporter', 'functional reporter'}),
        'normalizedDataTypeEndpointCounts': counts('normalized_data_type', {'percent_activity'}),
        'assaySpeciesEndpointCounts': counts('organism', {'human'}),
        'annotationConflictCount': sum(row['assay_design_type'] == 'functional reporter'
                                       and row['assay_function_type'] == 'binding' for row in selected),
        'tox21SourceEndpointCount': sum(row['assay_source_name'].casefold() == 'tox21' for row in selected),
        'targetMappingWithoutAnnotationCount': len(target_ids - indexed.keys()),
        'candidateStructureCount': None, 'structurallyPassedCount': None,
        'structureAuditStatus': 'not-run-no-structures', 'rightsStatus': 'unresolved',
        'taskEquivalenceStatus': 'unresolved', 'independentlyQualifiedStructureCount': None,
        'canEnterStage7C': False, 'labelsRead': False, 'independentAccuracyEvaluated': False,
        'sealingPerformed': False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('Refusing to overwrite existing audit evidence')
    try:
        result = audit_metadata(args.private_dir)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('xb') as handle:
            handle.write((json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8'))
        print(json.dumps({'metadataAuditWritten': True, 'labelsRead': False, 'canEnterStage7C': False}))
        return 0
    except (ValueError, OSError, KeyError, IndexError, ET.ParseError, zipfile.BadZipFile):
        print('EPA metadata audit stopped: check pinned sources and metadata columns; no qualification granted.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
