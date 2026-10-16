"""Synthetic software fixtures only; no scientific sample or real activity labels.

All requests are intercepted by httpx.MockTransport. The test suite does not
download data, call predictors or manufacture an eligible independent cohort.
"""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from scripts import survey_chembl_expanded_labelblind as survey
from test.test_survey_chembl_labelblind import metadata_rows


class FakeExpandedAPI:
    def __init__(self, count=401, mutate=None):
        self.calls, self.mutate, self.count = [], mutate, count
        self.rows = metadata_rows()
        first = self.rows['activity'][0]
        self.rows['activity'] = [dict(first, activity_id=index + 1, record_id=index + 1,
                                      molecule_chembl_id=f'CHEMBL{1000 + index}') for index in range(count)]
        self.private_dir = None

    def __call__(self, request):
        self.calls.append(request)
        if str(request.url).startswith(survey.RELEASE):
            return httpx.Response(200, content=b'Synthetic license fixture; no real rights approval.')
        kind = request.url.path.rsplit('/', 1)[-1].removesuffix('.json')
        if kind == 'status':
            payload = {'chembl_db_version': survey.VERSION, 'status': 'UP'}
        else:
            if kind == 'activity' and self.private_dir:
                assert (self.private_dir / 'plan.json').exists(), 'Plan must precede first activity fetch'
                assert survey.strict_json((self.private_dir / 'plan.json').read_bytes()) == survey.frozen_plan()
            offset = int(request.url.params['offset'])
            rows = copy.deepcopy(self.rows[kind])
            if kind in ('assay', 'document'):
                field = kind + '_chembl_id'
                identifiers = set(request.url.params[field + '__in'].split(','))
                rows = [row for row in rows if row[field] in identifiers]
            total = len(rows)
            rows = rows[offset:offset + survey.PAGE_SIZE]
            payload = {survey.PLURALS[kind]: rows,
                       'page_meta': {'limit': survey.PAGE_SIZE, 'offset': offset, 'total_count': total,
                                     'next': 'untrusted-remote-next' if offset + survey.PAGE_SIZE < total else None,
                                     'previous': None if offset == 0 else 'untrusted-remote-previous'}}
        if self.mutate:
            self.mutate(kind, payload, request)
        return httpx.Response(200, json=payload)


class ExpandedChEMBLTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def collect(self, api=None, name='snapshot'):
        api = api or FakeExpandedAPI()
        api.private_dir = self.root / name
        with httpx.Client(transport=httpx.MockTransport(api), follow_redirects=False) as client:
            result = survey.collect(api.private_dir, client)
        return *result, api

    def load(self, name='snapshot', pin=None):
        directory = self.root / name
        pin = pin or survey.sha((directory / 'receipt.json').read_bytes())
        return survey.load_snapshot(directory, pin)

    def test_plan_frozen_before_activity_and_pagination_reproduces_exact_rows(self):
        bundle, receipt, api = self.collect()
        self.assertEqual(len(bundle['activities']), 401)
        self.assertTrue(receipt['wholeTargetSurvey'])
        self.assertEqual(bundle['schema'], survey.SCHEMA)
        self.assertEqual(survey.sha((self.root / 'snapshot/plan.json').read_bytes()), receipt['planSha256'])
        activity_requests = [request for request in api.calls if request.url.path.endswith('/activity.json')]
        self.assertEqual([int(request.url.params['offset']) for request in activity_requests], [0, 200, 400])
        for request in activity_requests:
            self.assertEqual(request.url.params['order_by'], 'activity_id')
            self.assertEqual(request.url.params['only'], ','.join(survey.FIELDS['activity']))
            self.assertEqual(set(request.url.params), {'only', 'limit', 'offset', 'target_chembl_id', 'order_by'})
        self.assertEqual(self.load(), (bundle, receipt))
        self.assertFalse(receipt['labelsRead'])
        self.assertFalse(receipt['outcomeQualifiersRead'])
        self.assertFalse(receipt['qualityFlagsRead'])

    def test_max2000_caps_larger_target_without_following_remote_links(self):
        bundle, receipt, api = self.collect(FakeExpandedAPI(count=2201))
        requests = [request for request in api.calls if request.url.path.endswith('/activity.json')]
        self.assertEqual(len(requests), 10)
        self.assertEqual(len(bundle['activities']), 2000)
        self.assertFalse(receipt['wholeTargetSurvey'])
        self.assertEqual(receipt['availableTargetActivityMetadataCount'], 2201)
        self.assertTrue(all(request.url.host == 'www.ebi.ac.uk' for request in requests))
        self.assertEqual(self.load()[0], bundle)

    def test_empty_target_and_exact_page_boundary_are_not_extra_fetched(self):
        for count in (0, 200, 400):
            with self.subTest(count=count):
                bundle, _, api = self.collect(FakeExpandedAPI(count=count), name=f'count-{count}')
                self.assertEqual(len(bundle['activities']), count)
                self.assertEqual(sum(request.url.path.endswith('/activity.json') for request in api.calls), max(1, count // 200))
                self.assertEqual(self.load(f'count-{count}')[0], bundle)

    def test_projection_violation_stops_before_rawbody_or_bundle_persisted(self):
        for key in ('standard_value', 'value', 'standard_relation', 'relation', 'pchembl_value', 'activity_comment', 'data_validity_comment', 'potential_duplicate'):
            def mutate(kind, payload, request, extra=key):
                if kind == 'activity':
                    payload['activities'][0][extra] = 'software-forbidden-sentinel'
            directory = self.root / key
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.collect(FakeExpandedAPI(mutate=mutate), name=key)
            self.assertFalse((directory / 'activity-0.json').exists())
            self.assertFalse((directory / 'bundle.json').exists())
            self.assertFalse((directory / 'receipt.json').exists())

    def test_count_drift_offset_mismatch_incomplete_page_and_duplicate_ids_rejected(self):
        for fault in ('count', 'offset', 'short-page', 'duplicate', 'reverse'):
            def mutate(kind, payload, request, wanted=fault):
                if kind != 'activity' or request.url.params['offset'] != '200':
                    return
                if wanted == 'count':
                    payload['page_meta']['total_count'] += 1
                elif wanted == 'offset':
                    payload['page_meta']['offset'] = 0
                elif wanted == 'short-page':
                    payload['activities'].pop()
                elif wanted == 'duplicate':
                    payload['activities'][0]['activity_id'] = 1
                else:
                    payload['activities'].reverse()
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                self.collect(FakeExpandedAPI(mutate=mutate), name=fault)
            self.assertFalse((self.root / fault / 'receipt.json').exists())

    def test_source_join_must_match_original_activity_members(self):
        for fault in ('missing', 'wrong', 'duplicate', 'paginated'):
            def mutate(kind, payload, request, wanted=fault):
                if kind != 'assay':
                    return
                if wanted == 'missing':
                    payload['assays'].clear(); payload['page_meta']['total_count'] = 0
                elif wanted == 'wrong':
                    payload['assays'][0]['assay_chembl_id'] = 'CHEMBL999'
                elif wanted == 'duplicate':
                    payload['assays'].append(copy.deepcopy(payload['assays'][0])); payload['page_meta']['total_count'] = 2
                else:
                    payload['page_meta']['next'] = 'untrusted-next'
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                self.collect(FakeExpandedAPI(mutate=mutate), name='join-' + fault)

    def test_source_ids_are_joined_in_fixed_sorted_batches_of40(self):
        api = FakeExpandedAPI(count=81)
        original_assay = api.rows['assay'][0]
        original_document = api.rows['document'][0]
        api.rows['assay'] = []
        api.rows['document'] = []
        for index, activity in enumerate(api.rows['activity']):
            assay_id, document_id = f'CHEMBL{5000 + index}', f'CHEMBL{6000 + index}'
            activity.update(assay_chembl_id=assay_id, document_chembl_id=document_id)
            api.rows['assay'].append(dict(original_assay, assay_chembl_id=assay_id, document_chembl_id=document_id))
            api.rows['document'].append(dict(original_document, document_chembl_id=document_id))
        bundle, _, api = self.collect(api)
        for kind in ('assay', 'document'):
            batches = [request.url.params[kind + '_chembl_id__in'].split(',') for request in api.calls
                       if request.url.path.endswith('/' + kind + '.json')]
            self.assertEqual([len(batch) for batch in batches], [40, 40, 1])
            self.assertEqual(len(bundle[survey.PLURALS[kind]]), 81)
        self.assertEqual(self.load()[0], bundle)

    def test_metadata_query_cannot_select_outcomes_or_change_target(self):
        valid = {'only': ','.join(survey.FIELDS['activity']), 'limit': 200, 'offset': 0,
                 'target_chembl_id': survey.TARGET, 'order_by': 'activity_id'}
        for key, value in (('order_by', 'standard_value'), ('target_chembl_id', 'CHEMBL241'),
                           ('offset', 1), ('offset', 2000), ('standard_relation', '='), ('only', 'activity_id,standard_value')):
            query = dict(valid, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                survey.validate_query('activity', query)

    def test_version_mismatch_has_no_activity_fetch(self):
        def mutate(kind, payload, request):
            if kind == 'status':
                payload['chembl_db_version'] = 'ChEMBL_38'
        api = FakeExpandedAPI(mutate=mutate)
        with self.assertRaises(ValueError):
            self.collect(api)
        self.assertEqual(len(api.calls), 1)

    def test_redirect_fixedhosts_and_budgets_fail_closed(self):
        api = FakeExpandedAPI()
        with httpx.Client(transport=httpx.MockTransport(api)) as client:
            reader = survey.BoundedExpandedReader(client, self.root)
            with self.assertRaises(ValueError):
                reader.read('unsafe.json', 'https://example.com/activity.json')
            reader.requests = survey.MAX_REQUESTS
            with self.assertRaises(ValueError):
                reader.read('status.json', survey.API + 'status.json')
        self.assertEqual(api.calls, [])
        for limit in ('MAX_RESPONSE_BYTES', 'MAX_TOTAL_BYTES'):
            directory = self.root / limit; directory.mkdir()
            with patch.object(survey, limit, 1), httpx.Client(transport=httpx.MockTransport(FakeExpandedAPI())) as client:
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    survey.BoundedExpandedReader(client, directory).read('status.json', survey.API + 'status.json')
            self.assertFalse((directory / 'status.json').exists())
        calls = []
        def redirect(request):
            calls.append(request)
            return httpx.Response(302, headers={'location': 'https://example.com/labels'})
        with httpx.Client(transport=httpx.MockTransport(redirect)) as client:
            with self.assertRaises((ValueError, httpx.HTTPError)):
                survey.collect(self.root / 'redirect', client)
        self.assertEqual(len(calls), 1)

    def test_receipt_pin_plan_raw_response_and_bundle_must_all_match(self):
        self.collect()
        directory = self.root / 'snapshot'
        originals = {item.name: item.read_bytes() for item in directory.iterdir()}
        pin = survey.sha(originals['receipt.json'])
        for filename in ('plan.json', 'bundle.json', 'activity-1.json', 'receipt.json'):
            with self.subTest(filename=filename):
                (directory / filename).write_bytes(originals[filename] + b' ')
                with self.assertRaises(ValueError):
                    self.load(pin=pin)
                (directory / filename).write_bytes(originals[filename])
        with self.assertRaises(ValueError):
            self.load(pin='0' * 64)

    def test_reanchored_receipt_cannot_mask_wrong_offsets_or_missing_source_rows(self):
        self.collect()
        directory = self.root / 'snapshot'
        original = survey.strict_json((directory / 'receipt.json').read_bytes())
        for fault in ('offset', 'missing-page', 'traversal', 'reordered-pages'):
            receipt = copy.deepcopy(original)
            responses = receipt['responses']
            activity = [item for item in responses if item['resource'].startswith('activity-')]
            if fault == 'offset':
                activity[1]['params']['offset'] = 0
            elif fault == 'missing-page':
                responses.remove(activity[1])
            elif fault == 'traversal':
                activity[1]['resource'] = '../activity-1.json'
            else:
                first, second = responses.index(activity[0]), responses.index(activity[1])
                responses[first], responses[second] = responses[second], responses[first]
            (directory / 'receipt.json').write_bytes(survey.encoded(receipt))
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                self.load()
        (directory / 'receipt.json').write_bytes(survey.encoded(original))
        bundle = survey.strict_json((directory / 'bundle.json').read_bytes())
        bundle['activities'].pop()
        raw = survey.encoded(bundle)
        (directory / 'bundle.json').write_bytes(raw)
        original['bundleSha256'] = survey.sha(raw)
        original['retrievedRecordCount'] = len(bundle['activities'])
        (directory / 'receipt.json').write_bytes(survey.encoded(original))
        with self.assertRaises(ValueError):
            self.load()

    def test_existing_snapshot_is_never_overwritten(self):
        self.collect()
        directory = self.root / 'snapshot'
        before = {item.name: item.read_bytes() for item in directory.iterdir()}
        with self.assertRaises(FileExistsError):
            self.collect()
        self.assertEqual(before, {item.name: item.read_bytes() for item in directory.iterdir()})


if __name__ == '__main__':
    unittest.main()
