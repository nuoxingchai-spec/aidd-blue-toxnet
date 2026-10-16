"""Synthetic metadata workbooks only; no experimental results or network."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from scripts import audit_toxcast_metadata as audit


def workbook(rows, fields, shared=False):
    namespace = audit.NS['x']
    tag = lambda name: '{' + namespace + '}' + name
    root = ET.Element(tag('worksheet'))
    data = ET.SubElement(root, tag('sheetData'))
    strings = []
    # Leave column B unused to verify that sparse XML cells retain alignment.
    columns = ['A'] + [chr(ord('C') + index) for index in range(len(fields) - 1)]
    for number, values in enumerate([dict(zip(fields, fields)), *rows], start=1):
        row = ET.SubElement(data, tag('row'), r=str(number))
        for column, field in zip(columns, fields):
            if field not in values:
                continue
            value = str(values[field])
            cell = ET.SubElement(row, tag('c'), r=column + str(number), t='s' if shared else 'inlineStr')
            if shared:
                strings.append(value)
                ET.SubElement(cell, tag('v')).text = str(len(strings) - 1)
            else:
                ET.SubElement(ET.SubElement(cell, tag('is')), tag('t')).text = value
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as archive:
        archive.writestr('xl/worksheets/sheet1.xml', ET.tostring(root))
        if shared:
            table = ET.Element(tag('sst'))
            for value in strings:
                ET.SubElement(ET.SubElement(table, tag('si')), tag('t')).text = value
            archive.writestr('xl/sharedStrings.xml', ET.tostring(table))
    return raw.getvalue()


def fixture_files():
    annotations = []
    for endpoint, name, design, source in (
        ('1', 'Software_hERG', 'binding reporter', 'NVS'),
        ('2', 'Software_channel', 'binding reporter', 'ERF'),
        ('3', 'Software_hErG_reporter', 'functional reporter', 'TOX21'),
        ('4', 'Software_other', 'functional reporter', 'OTHER'),
    ):
        annotations.append({'aeid': endpoint, 'assay_name': name, 'organism': 'human',
                            'ncbi_taxon_id': '9606', 'assay_design_type': design,
                            'assay_function_type': 'binding', 'normalized_data_type': 'percent_activity',
                            'assay_source_name': source})
    mappings = [{'aeid': '2', 'official_symbol': 'KCNH2', 'ncbi_taxon_id': '9606',
                 'assay_component_endpoint_name.x': 'software-channel',
                 'assay_component_endpoint_name.y': 'software-channel'}]
    return {name: workbook(rows, sorted(fields), shared=index == 1)
            for index, (name, rows, fields) in enumerate(zip(audit.PINS, (annotations, mappings), audit.FIELDS))}


class ToxCastMetadataAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.files = fixture_files()
        for name, raw in self.files.items():
            (self.directory / name).write_bytes(raw)
        pins = {name: hashlib.sha256(raw).hexdigest() for name, raw in self.files.items()}
        self.patch = patch.object(audit, 'PINS', pins)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_synthetic_metadata_matching_categories_and_null_structure_counts(self):
        result = audit.audit_metadata(self.directory)
        self.assertEqual(result['endpointMetadataCount'], 3)
        self.assertEqual(result['assayDesignEndpointCounts'], {'binding reporter': 2, 'functional reporter': 1})
        self.assertEqual(result['normalizedDataTypeEndpointCounts'], {'percent_activity': 3})
        self.assertEqual(result['assaySpeciesEndpointCounts'], {'human': 3})
        self.assertEqual(result['annotationConflictCount'], 1)
        self.assertEqual(result['tox21SourceEndpointCount'], 1)
        self.assertEqual(result['targetMappingWithoutAnnotationCount'], 0)
        for key in ('candidateStructureCount', 'structurallyPassedCount', 'independentlyQualifiedStructureCount'):
            self.assertIsNone(result[key])
        self.assertFalse(result['labelsRead'])
        self.assertFalse(result['canEnterStage7C'])
        self.assertFalse(result['independentAccuracyEvaluated'])
        self.assertEqual(result['rightsStatus'], 'unresolved')
        self.assertNotIn('Software_', json.dumps(result))
        self.assertNotIn(str(self.directory), json.dumps(result))

    def test_sha256_failure_prevents_parsing_even_after_first_valid_file(self):
        name = list(self.files)[1]
        (self.directory / name).write_bytes(b'Not the pinned metadata workbook')
        with patch.object(audit, 'metadata_rows') as parser, self.assertRaises(ValueError):
            audit.audit_metadata(self.directory)
        parser.assert_not_called()

    def test_required_metadata_columns_cannot_be_silently_missing(self):
        raw = workbook([{'aeid': '1'}], ['aeid'])
        with self.assertRaises(ValueError):
            audit.metadata_rows(raw, audit.FIELDS[0])

    def test_duplicate_endpoint_identifiers_are_rejected(self):
        name = next(iter(self.files))
        rows = audit.metadata_rows(self.files[name], audit.FIELDS[0])
        raw = workbook([*rows, rows[0]], sorted(audit.FIELDS[0]))
        (self.directory / name).write_bytes(raw)
        with patch.dict(audit.PINS, {name: hashlib.sha256(raw).hexdigest()}), self.assertRaises(ValueError):
            audit.audit_metadata(self.directory)

    def test_json_export_is_aggregate_only_and_never_overwritten(self):
        output = self.directory / 'aggregate.json'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(audit.main(['--private-dir', str(self.directory), '--output', str(output)]), 0)
        original = output.read_bytes()
        self.assertEqual(json.loads(original)['endpointMetadataCount'], 3)
        with patch.object(audit, 'audit_metadata') as reader, contextlib.redirect_stderr(io.StringIO()), \
             self.assertRaises(SystemExit):
            audit.main(['--private-dir', str(self.directory), '--output', str(output)])
        reader.assert_not_called()
        self.assertEqual(output.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
