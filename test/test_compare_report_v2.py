"""CPU-only report contract tests; fixture scores are not scientific results."""
import copy
import json
import unittest

from backend.chemistry import analyze_chemistry
from backend.compare_question import interpret_question
from backend.compare_reporting import (
    LIMITATIONS, REPORT_SCHEMA, REPORT_SCHEMA_V2, build_snapshot, digest,
    report_messages, report_output_format, report_policy, validate_report, validate_report_v2,
)
from backend.compare_schemas import MODEL_IDS
from test.compare_fixture import fixture_prediction, fixture_report


def compact_report(snapshot):
    return {'schema': REPORT_SCHEMA_V2, 'snapshotSha256': snapshot['snapshotSha256'],
            'sectionOrder': [s['id'] for s in snapshot['sections']],
            'claimOrder': {s['id']: list(s['claimIds']) for s in snapshot['sections']},
            'commentary': [{'sectionId': 'limits', 'text': '尚无充分证据支持因果机制。'}],
            'uncertainty': '仅是研究原型。', 'refusal': None}


class CompactReportTests(unittest.TestCase):
    def test_duplicate_json_keys_rejected_by_independent_validator(self):
        raw = json.dumps(compact_report(self.snapshot))
        duplicate = raw.replace('{', '{"snapshotSha256":"wrong",', 1)
        with self.assertRaises(ValueError):
            validate_report_v2(duplicate, self.snapshot)
        nested = raw.replace('"sectionId": "limits"', '"sectionId":"prediction","sectionId":"limits"')
        with self.assertRaises(ValueError):
            validate_report_v2(nested, self.snapshot)

    def setUp(self):
        chemistry = analyze_chemistry('OCC')
        structure = {key: chemistry[key] for key in ('canonicalSmiles', 'structureId')}
        models = {key: {'status': 'unavailable', 'data': None} for key in MODEL_IDS}
        models[MODEL_IDS[0]] = {'status': 'completed', 'data': {'prediction': fixture_prediction(structure)}}
        self.snapshot = build_snapshot(structure, chemistry, models)

    def test_reconstructs_every_fact_source_identity_and_version_without_generated_values(self):
        raw = compact_report(self.snapshot)
        raw['claimOrder']['prediction'].reverse()
        result = validate_report(raw, self.snapshot, report_contract='v2', report_mode='fast')
        self.assertEqual(result['reportSchema'], REPORT_SCHEMA_V2)
        self.assertEqual(result['reportMode'], 'fast')
        self.assertEqual(result['verifiedFacts'], self.snapshot['facts'])
        self.assertEqual(result['verifiedSources'], self.snapshot['sources'])
        self.assertEqual(result['structure'], self.snapshot['structure'])
        self.assertEqual(result['chemistryVersion'], self.snapshot['chemistryVersion'])
        self.assertFalse(result['factReconstruction']['generatedScientificFieldsAccepted'])
        self.assertEqual(result['limitations'], LIMITATIONS)
        expected = '\n'.join(next(f['text'] for f in self.snapshot['facts'] if f['id'] == key)
                             for key in raw['claimOrder']['prediction'])
        self.assertEqual(result['sections'][0]['text'], expected)
        result['verifiedFacts'][0]['value']['score'] = .99
        self.assertNotEqual(result['verifiedFacts'][0]['value'], self.snapshot['facts'][0]['value'])

    def test_generated_scores_sources_versions_and_identities_have_no_writable_fields(self):
        for field, value in [('score', .99), ('scores', {'herg': .99}), ('sourceIds', ['invented']),
                             ('sources', []), ('modelVersion', 'altered'), ('structureId', 'bad'),
                             ('structure', {}), ('factChecks', []), ('tools', []), ('reportMode', 'deep')]:
            with self.subTest(field=field):
                raw = compact_report(self.snapshot)
                raw[field] = value
                with self.assertRaises(ValueError):
                    validate_report(raw, self.snapshot)
        raw = compact_report(self.snapshot)
        raw['commentary'][0]['score'] = .99
        with self.assertRaises(ValueError):
            validate_report(raw, self.snapshot)

    def test_hash_binds_source_versions_structure_and_all_prediction_values(self):
        for mutation in ('score', 'structure', 'weight', 'modelVersion', 'rulesVersion', 'chemistry', 'limitations'):
            with self.subTest(mutation=mutation):
                snapshot = copy.deepcopy(self.snapshot)
                if mutation == 'score': snapshot['facts'][0]['value']['score'] = .99
                elif mutation == 'structure': snapshot['structure']['canonicalSmiles'] = 'CCN'
                elif mutation == 'weight': snapshot['sources'][0]['weightsSha256'] = '0' * 64
                elif mutation == 'modelVersion': snapshot['sources'][0]['modelVersion'] = 'changed'
                elif mutation == 'rulesVersion': snapshot['sources'][-1]['rulesVersion'] = 'changed'
                elif mutation == 'chemistry': snapshot['chemistryVersion'] = 'changed'
                else: snapshot['limitations'].pop()
                with self.assertRaises(ValueError):
                    validate_report(compact_report(self.snapshot), snapshot)

    def test_malformed_frozen_snapshot_internal_links_are_rejected_even_if_rehashed(self):
        for mutation in ('duplicateFact', 'sourceLink', 'structureHash', 'sectionMembership', 'missingVersion', 'limits'):
            with self.subTest(mutation=mutation):
                snapshot = copy.deepcopy(self.snapshot)
                if mutation == 'duplicateFact': snapshot['facts'].append(copy.deepcopy(snapshot['facts'][0]))
                elif mutation == 'sourceLink': snapshot['facts'][0]['sourceIds'] = ['missing']
                elif mutation == 'structureHash': snapshot['structure']['structureId'] = '0' * 64
                elif mutation == 'sectionMembership': snapshot['facts'][0]['sectionId'] = 'limits'
                elif mutation == 'missingVersion': snapshot['sources'][0]['modelVersion'] = None
                else: snapshot['limitations'].pop()
                snapshot.pop('snapshotSha256')
                snapshot['snapshotSha256'] = digest(snapshot)
                with self.assertRaises(ValueError):
                    validate_report(compact_report(snapshot), snapshot)

    def test_complete_membership_no_duplicates_invented_sections_or_cross_section_moves(self):
        for mutation in ('omit', 'duplicate', 'invent', 'cross', 'section', 'sectionDuplicate'):
            with self.subTest(mutation=mutation):
                raw = compact_report(self.snapshot)
                if mutation == 'omit': raw['claimOrder']['prediction'].pop()
                elif mutation == 'duplicate': raw['claimOrder']['prediction'].append(raw['claimOrder']['prediction'][0])
                elif mutation == 'invent': raw['claimOrder']['prediction'][0] = 'model:fictional'
                elif mutation == 'cross': raw['claimOrder']['limits'].append(raw['claimOrder']['prediction'].pop())
                elif mutation == 'section': raw['claimOrder']['invented'] = []
                else: raw['sectionOrder'][0] = 'limits'
                with self.assertRaises(ValueError): validate_report(raw, self.snapshot)

    def test_generated_prose_is_always_unverified_including_uncertainty_and_refusal(self):
        raw = compact_report(self.snapshot)
        raw['commentary'][0]['text'] = '一种假设机制，仅用于测试，不是实验。'
        raw['refusal'] = '没有实验支持，拒绝给出人体风险。'
        result = validate_report(raw, self.snapshot)
        self.assertEqual({a['type'] for a in result['unverifiedAdditions']}, {'commentary', 'uncertainty', 'refusal'})
        self.assertTrue(all(a['verification'] == 'unverified' for a in result['unverifiedAdditions']))
        self.assertFalse(result['sourceVerification']['newScientificFactsVerified'])
        self.assertTrue(all(s['commentaryVerification'] == 'unverified' for s in result['sections']))

    def test_fast_bounds_checked_after_parsing_not_just_prompt_advice(self):
        for mutation in ('paragraph', 'total', 'count', 'uncertainty', 'refusal', 'blank', 'control'):
            with self.subTest(mutation=mutation):
                raw = compact_report(self.snapshot)
                if mutation == 'paragraph': raw['commentary'][0]['text'] = '研' * 241
                elif mutation == 'total': raw['commentary'] = [{'sectionId': 'limits', 'text': '研' * 201}] * 3
                elif mutation == 'count': raw['commentary'] *= 5
                elif mutation == 'uncertainty': raw['uncertainty'] = '研' * 181
                elif mutation == 'refusal': raw['refusal'] = '研' * 181
                elif mutation == 'blank': raw['commentary'][0]['text'] = '  '
                else: raw['commentary'][0]['text'] = '研究\x00'
                with self.assertRaises(ValueError): validate_report(raw, self.snapshot, report_mode='fast')

    def test_deep_is_separate_bounded_prose_budget_but_same_complete_facts(self):
        raw = compact_report(self.snapshot)
        raw['commentary'][0]['text'] = '研究' * 200
        with self.assertRaises(ValueError): validate_report(raw, self.snapshot, report_mode='fast')
        deep = validate_report(raw, self.snapshot, report_mode='deep')
        self.assertEqual(deep['verifiedFacts'], self.snapshot['facts'])
        self.assertEqual(deep['limitations'], LIMITATIONS)
        raw['commentary'][0]['text'] = '研' * 801
        with self.assertRaises(ValueError): validate_report(raw, self.snapshot, report_mode='deep')

    def test_original_v1_signature_output_and_history_remain_supported(self):
        raw = fixture_report(self.snapshot)
        raw['commentary'][0]['text'] = '研' * 1500
        result = validate_report(raw, self.snapshot)
        self.assertEqual(result['reportSchema'], REPORT_SCHEMA)
        self.assertNotIn('reportMode', result)
        with self.assertRaises(ValueError): validate_report(raw, self.snapshot, report_contract='v2')
        with self.assertRaises(ValueError): validate_report(compact_report(self.snapshot), self.snapshot, report_contract='v1')
        # New fast option applies the same prose bound to a v1 comparison arm.
        with self.assertRaises(ValueError): validate_report(raw, self.snapshot, report_mode='fast')

    def test_strict_raw_json_unsupported_modes_contracts_and_no_nonfinite_values(self):
        self.assertEqual(validate_report(json.dumps(compact_report(self.snapshot)), self.snapshot)['reportSchema'], REPORT_SCHEMA_V2)
        for raw in ('[]', '{"schema":NaN}', '{"schema":"unknown"}', 'x' * (2 * 1024 * 1024 + 1)):
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(ValueError): validate_report(raw, self.snapshot)
        for mode in ('auto', None, 1):
            with self.assertRaises(ValueError): report_messages(self.snapshot, report_mode=mode)
        with self.assertRaises(ValueError): report_messages(self.snapshot, report_contract='main')
        with self.assertRaises(ValueError): validate_report_v2(fixture_report(self.snapshot), self.snapshot)

    def test_v2_prompt_omits_duplicate_facts_but_not_any_fact_or_scientific_limit(self):
        v1 = report_messages(self.snapshot)
        fast = report_messages(self.snapshot, report_contract='v2', report_mode='fast')
        deep = report_messages(self.snapshot, report_contract='v2', report_mode='deep')
        payload = json.loads(fast[1]['content'])
        self.assertEqual(payload['snapshot']['snapshotSha256'], self.snapshot['snapshotSha256'])
        self.assertEqual(payload['snapshot']['limitations'], LIMITATIONS)
        self.assertEqual(len(payload['snapshot']['facts']), len(self.snapshot['facts']))
        self.assertNotIn('text', payload['snapshot']['facts'][0])
        self.assertNotIn('sections', payload['snapshot'])
        self.assertEqual(payload['snapshot']['sectionTitles'], {s['id']: s['title'] for s in self.snapshot['sections']})
        self.assertEqual(payload['promptVersion'], 'aidd.compare.independent-report.v2.1')
        self.assertIsInstance(payload['responseTemplate']['claimOrder'], dict)
        self.assertEqual(payload['responseTemplate']['commentary'], [])
        self.assertEqual(validate_report(payload['responseTemplate'], self.snapshot, report_mode='fast')['verifiedFacts'], self.snapshot['facts'])
        self.assertNotIn('responseSchema', payload)
        self.assertNotEqual(fast, deep)
        # This checks bytes only; no token-count or runtime claim is made.
        self.assertLess(len(json.dumps(fast)), len(json.dumps(v1)))
        policy = report_policy('fast')
        policy['commentaryItems'] = 100
        self.assertEqual(report_policy('fast')['commentaryItems'], 4)

    def test_snapshot_does_not_share_mutable_global_limits(self):
        self.snapshot['limitations'].append('local change')
        self.assertNotIn('local change', LIMITATIONS)

    def test_v2_generation_schema_has_strict_shape_exact_dynamic_members_and_hash(self):
        schema = report_output_format(self.snapshot, report_contract='v2', report_mode='fast')
        self.assertFalse(schema['additionalProperties'])
        expected_fields = {'schema', 'snapshotSha256', 'sectionOrder', 'claimOrder', 'commentary', 'uncertainty', 'refusal'}
        self.assertEqual(set(schema['properties']), expected_fields)
        self.assertEqual(set(schema['required']), expected_fields)
        properties = schema['properties']
        self.assertEqual(properties['schema']['const'], REPORT_SCHEMA_V2)
        self.assertEqual(properties['snapshotSha256']['const'], self.snapshot['snapshotSha256'])
        self.assertEqual(set(properties['sectionOrder']['items']['enum']), {s['id'] for s in self.snapshot['sections']})
        self.assertEqual(properties['sectionOrder']['minItems'], 4)
        self.assertEqual(properties['sectionOrder']['maxItems'], 4)
        self.assertEqual(properties['claimOrder']['type'], 'object')
        self.assertFalse(properties['claimOrder']['additionalProperties'])
        for section in self.snapshot['sections']:
            dynamic = properties['claimOrder']['properties'][section['id']]
            self.assertEqual(dynamic['items']['enum'], section['claimIds'])
            self.assertEqual(dynamic['minItems'], len(section['claimIds']))
            self.assertEqual(dynamic['maxItems'], len(section['claimIds']))
        self.assertNotIn('description', schema)

    def test_generation_schema_applies_mode_bounds_but_keeps_postvalidator_strict(self):
        for mode in ('fast', 'deep'):
            schema = report_output_format(self.snapshot, report_contract='v2', report_mode=mode)
            policy = report_policy(mode)
            self.assertEqual(schema['properties']['commentary']['maxItems'], policy['commentaryItems'])
            self.assertEqual(schema['$defs']['CompactCommentary']['properties']['text']['maxLength'], policy['commentaryCharacters'])
            self.assertFalse(schema['$defs']['CompactCommentary']['additionalProperties'])
            for name in ('uncertainty', 'refusal'):
                options = schema['properties'][name]['anyOf']
                self.assertEqual(next(item for item in options if item['type'] == 'string')['maxLength'], policy[name + 'Characters'])
        raw = compact_report(self.snapshot)
        raw['claimOrder'] = list(raw['claimOrder'].values())
        with self.assertRaises(ValueError): validate_report(raw, self.snapshot)
        raw = compact_report(self.snapshot)
        raw['commentary'] *= 12
        with self.assertRaises(ValueError): validate_report(raw, self.snapshot)
        # Schema output is independent and cannot mutate future formats.
        schema['properties']['claimOrder']['properties']['prediction']['items']['enum'].append('invented')
        self.assertNotIn('invented', str(report_output_format(self.snapshot, report_contract='v2')))

    def test_v1_generation_format_remains_json_fast_keeps_all_factchecks(self):
        for mode in ('fast', 'deep'):
            self.assertEqual(report_output_format(self.snapshot, report_contract='v1', report_mode=mode), 'json')
        fast = report_messages(self.snapshot, report_contract='v1', report_mode='fast')
        self.assertIn('factChecks必须逐条完整', fast[0]['content'])
        self.assertNotIn('避免逐项重复事实', fast[0]['content'])
        self.assertEqual(json.loads(fast[1]['content'])['snapshot']['facts'], self.snapshot['facts'])
        for contract, mode in [('unsupported', 'deep'), ('v2', 'unknown')]:
            with self.assertRaises(ValueError): report_output_format(self.snapshot, report_contract=contract, report_mode=mode)

    def test_dynamic_generation_schema_does_not_mix_snapshots_or_hide_integrity_failure(self):
        original = report_output_format(self.snapshot, report_contract='v2')
        changed = copy.deepcopy(self.snapshot)
        changed['facts'][0]['value']['score'] = .99
        with self.assertRaises(ValueError): report_output_format(changed, report_contract='v2')
        self.assertEqual(report_output_format(self.snapshot, report_contract='v2'), original)


class ChineseQuestionTests(unittest.TestCase):
    def test_chinese_and_english_endpoint_terms_have_deterministic_source_topics(self):
        for question, topic in [('心脏毒性如何', 'herg'), ('肝毒性', 'dili'), ('肝损伤', 'dili'),
                                ('致突变性', 'ames'), ('临床毒性', 'clintox'), ('HERG', 'herg'),
                                ('DILI', 'dili'), ('Ames', 'ames'), ('ClinTox', 'clintox'),
                                ('通路活性', 'tox21'), ('分析hERG通道', 'herg'), ('DILI分析', 'dili'),
                                ('Tox21通路', 'tox21')]:
            with self.subTest(question=question):
                result = interpret_question(question)
                self.assertEqual(result['scope'], topic)
                self.assertEqual(result['status'], 'recognized')
                self.assertFalse(result['taskAutomaticallySelected'])

    def test_bbb_requires_explicit_reviewed_task_and_never_defines_a_toxicity_label(self):
        pending = interpret_question('能穿过血脑屏障吗？')
        self.assertTrue(pending['requiresSelection'])
        self.assertEqual(pending['activeTopics'], [])
        supported = interpret_question('BBB 血脑屏障', specialist_task='tdc-bbb-martins')
        self.assertEqual(supported['activeTopics'], ['bbb'])
        self.assertFalse(supported['requiresSelection'])
        wrong = interpret_question('血脑屏障', specialist_task='tdc-herg')
        self.assertTrue(wrong['requiresSelection'])
        self.assertTrue(any('不等于' in text for text in supported['warnings']))

    def test_ambiguous_unsupported_multiple_and_disabled_pathway_are_explicit(self):
        unknown = interpret_question('这个分子是否安全')
        self.assertEqual(unknown['status'], 'requires-selection')
        self.assertEqual(unknown['topics'], [])
        unsupported = interpret_question('肾毒性、致癌和安全剂量')
        self.assertEqual(set(unsupported['unsupportedTopics']), {'nephrotoxicity', 'carcinogenicity', 'human-safe-dose'})
        self.assertTrue(unsupported['requiresSelection'])
        multiple = interpret_question('心脏毒性与肝毒性')
        self.assertEqual(multiple['scope'], 'multiple-topics')
        self.assertEqual(multiple['topics'], ['herg', 'dili'])
        self.assertTrue(interpret_question('通路活性', include_tox21=False)['requiresSelection'])

    def test_no_raw_user_question_or_private_path_is_forwarded(self):
        secret = '肝毒性 C:/Users/private-key/secret.txt 自由原始文字'
        result = interpret_question(secret)
        self.assertNotIn('private-key', str(result))
        self.assertNotIn('自由原始文字', str(result))
        self.assertEqual(interpret_question('')['status'], 'empty')
        self.assertEqual(interpret_question('othergword')['topics'], [])
        with self.assertRaises(ValueError): interpret_question('x' * 1001)


if __name__ == '__main__':
    unittest.main()
