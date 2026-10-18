"""Synthetic identity/provider tests; no candidate labels, API or model calls."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from scripts import survey_pubchem_identities as survey


def payload(aid=588834, sids=None):
    return {'InformationList': {'Information': [{'AID': aid, 'SID': [7, 3] if sids is None else sids}]}}


class PubChemIdentityTests(unittest.TestCase):
    def run_provider(self, response, aids=None):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'new'
            calls = []
            def handle(request):
                calls.append(str(request.url))
                return response
            with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                result = survey.collect(out, aids or [588834], client)
            files = {p.name: p.read_bytes() for p in out.iterdir()}
            return result, files, calls

    def test_valid_projection_is_ordered_and_does_not_grant_population_or_qualification(self):
        value = survey.parse_identities(json.dumps(payload()).encode(), 588834)
        self.assertEqual(value['sids'], [3, 7])
        self.assertFalse(value['qualificationGranted'])
        self.assertFalse(value['canEnterStage7C'])
        self.assertFalse(value['rawTestedPopulationCompletenessProven'])

    def test_all_sid_endpoint_is_fixed_and_has_no_activity_selectors(self):
        self.assertEqual(survey.identity_url(588834),
            'https://pubchem.ncbi.nlm.nih.gov/rest/pug/assay/aid/588834/sids/JSON?sid_type=all')
        for value in (True, '588834', 1, 686):
            with self.subTest(value=value), self.assertRaises(ValueError):
                survey.identity_url(value)

    def test_result_fields_and_other_envelopes_rejected(self):
        for value in ({'PC_AssayContainer': []}, {'Waiting': {}}, payload() | {'hitcall': 1},
                      {'InformationList': {'Information': [{'AID': 588834, 'SID': [3], 'hitc': 1}]}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                survey.parse_identities(json.dumps(value).encode(), 588834)

    def test_wrong_aid_noninteger_negative_and_duplicate_identities_rejected(self):
        for value in (payload(aid=1511), payload(aid=True), payload(sids=[True]),
                      payload(sids=[0]), payload(sids=[-1]), payload(sids=['3']), payload(sids=[3, 3])):
            with self.subTest(value=value), self.assertRaises(ValueError):
                survey.parse_identities(json.dumps(value).encode(), 588834)

    def test_duplicate_json_keys_and_nonfinite_values_rejected(self):
        for raw in (b'{"InformationList":{},"InformationList":{}}', b'{"unexpected":NaN}', b'{"unexpected":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                survey.parse_identities(raw, 588834)

    def test_success_writes_only_validated_projection_and_plan_hash(self):
        response = httpx.Response(200, json=payload())
        result, files, calls = self.run_provider(response)
        row = result['responses'][0]
        self.assertEqual(row['associatedSidCount'], 2)
        self.assertEqual(row['status'], 'identity-projection-verified')
        self.assertEqual(set(files), {'plan.json', 'receipt.json', 'aid-588834-identities.json'})
        self.assertEqual(result['planSha256'], hashlib.sha256(files['plan.json']).hexdigest())
        self.assertEqual(row['projectionSha256'], hashlib.sha256(files['aid-588834-identities.json']).hexdigest())
        self.assertEqual(calls, [survey.identity_url(588834)])

    def test_unexpected_result_sentinel_never_persists(self):
        sentinel = 'SYNTHETIC_FORBIDDEN_OUTCOME_SENTINEL'
        response = httpx.Response(200, json=payload() | {'result': sentinel})
        result, files, _ = self.run_provider(response)
        self.assertIsNone(result['responses'][0]['associatedSidCount'])
        self.assertEqual(set(files), {'plan.json', 'receipt.json'})
        self.assertFalse(any(sentinel.encode() in raw for raw in files.values()))

    def test_errors_redirects_and_challenge_html_have_no_body_cache_or_fake_count(self):
        sentinel = b'SYNTHETIC_PRIVATE_ERROR_BODY'
        for status, content_type in ((403, 'application/json'), (302, 'application/json'), (200, 'text/html')):
            with self.subTest(status=status, content_type=content_type):
                result, files, _ = self.run_provider(httpx.Response(status, content=sentinel,
                    headers={'content-type': content_type}))
                row = result['responses'][0]
                self.assertIsNone(row['associatedSidCount'])
                self.assertEqual(row['responseBytesRead'], 0)
                self.assertIsNone(row['responseSha256'])
                self.assertFalse(any(sentinel in raw for raw in files.values()))

    def test_declared_and_streamed_size_limits_do_not_write_partial_identities(self):
        for response in (httpx.Response(200, json=payload(), headers={'content-length': '99999999'}),
                         httpx.Response(200, json=payload(), headers={'content-length': '1', 'content-encoding': 'identity'})):
            with self.subTest(response=response.status_code), patch.object(survey, 'MAX_BYTES', 5):
                result, files, _ = self.run_provider(response)
                self.assertIsNone(result['responses'][0]['associatedSidCount'])
                self.assertNotIn('aid-588834-identities.json', files)

    def test_false_content_length_rejected_without_raw_cache(self):
        result, files, _ = self.run_provider(httpx.Response(200, json=payload(), headers={'content-length': '1'}))
        self.assertIsNone(result['responses'][0]['associatedSidCount'])
        self.assertNotIn('aid-588834-identities.json', files)

    def test_existing_directory_and_duplicate_requests_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'new';out.mkdir();(out / 'historical.json').write_text('synthetic historical')
            with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload()))) as client:
                with self.assertRaises(OSError):
                    survey.collect(out, [588834], client)
                with self.assertRaises(ValueError):
                    survey.collect(Path(directory) / 'other', [588834, 588834], client)
            self.assertEqual((out / 'historical.json').read_text(), 'synthetic historical')

    def test_checkout_and_onedrive_identity_cache_rejected(self):
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload()))) as client:
            for out in (survey.ROOT / 'not-created', Path(tempfile.gettempdir()) / 'OneDrive' / 'not-created'):
                with self.subTest(out=out.name), self.assertRaises(ValueError):
                    survey.collect(out, [588834], client)


if __name__ == '__main__':
    unittest.main()
