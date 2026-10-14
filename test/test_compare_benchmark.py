"""Deterministic CPU fixtures; none of these are real benchmark results."""
from __future__ import annotations

from argparse import Namespace
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from scripts import benchmark_compare_reports as benchmark


def fixture_runtime():
    return {'schema': 'aidd.compare.runtime-source.v1', 'gitCommit': 'a' * 40,
            'sourceSha256': 'a' * 64, 'sourceFilesDifferFromCommit': False,
            'capturedAt': '2026-10-14T02:00:00+00:00'}


def fixture_record(arm, index=1):
    models = {key: {'status': 'completed', 'data': {},
                    'provenance': {'modelId': f'fixture-model-{key}', 'revision': 'b' * 64,
                                   'weightsSha256': 'c' * 64},
                    'metrics': {'promptTokens': 100, 'completionTokens': 50, 'generationSeconds': 2,
                                'tokensPerSecond': 25, 'durationMs': 4000,
                                'cleanupState': {'confirmed': True, 'state': 'released'}}}
              for key in benchmark.MODEL_IDS}
    models['deepseek-api'] = {'status': 'skipped', 'metrics': {}, 'provenance': {}}
    return {'runId': f'{index:032x}', 'status': 'completed', 'durationMs': 6000,
            'runtimeProvenance': fixture_runtime(),
            'request': {'smiles': 'DO-NOT-PUBLISH-SMILES', 'question': 'DO-NOT-PUBLISH-QUESTION',
                        'reportContract': arm['reportContract'], 'reportMode': arm['reportMode']},
            'structure': {'structureId': 'd' * 64, 'canonicalSmiles': 'DO-NOT-PUBLISH-SMILES'},
            'snapshot': {'snapshotSha256': 'e' * 64, 'privatePath': 'DO-NOT-PUBLISH-PATH'},
            'models': models, 'cloudTransfer': {'requestAttempted': False, 'executed': False},
            'reports': {'ollama-qwen-local': {'status': 'verified-structured-report',
                        'reportSchema': 'aidd.compare.report.' + arm['reportContract'],
                        'sourceVerification': {'existingFields': 'exact-match', 'scoresAndSourcesChanged': False},
                        'unverifiedAdditions': [{'text': 'DO-NOT-PUBLISH-REPORT'}]}}}


class BenchmarkTests(unittest.TestCase):
    def summaries(self):
        return [benchmark.summarize_run(fixture_record(arm, index), arm=arm, repeat=1)
                for index, arm in enumerate(benchmark.ARMS, 1)]

    def test_four_arms_are_independent_2x2_and_no_cloud(self):
        pairs = {(arm['reportContract'], arm['reportMode']) for arm in benchmark.ARMS}
        self.assertEqual(pairs, {('v1', 'deep'), ('v2', 'deep'), ('v1', 'fast'), ('v2', 'fast')})
        for arm in benchmark.ARMS:
            payload = benchmark.request_for_arm('CCO', arm)
            self.assertFalse(payload['allowCloudTransfer'])
            self.assertFalse(payload['persistSensitiveContent'])
            self.assertIsNone(payload['comparisonReporter'])
        with self.assertRaises(ValueError):
            benchmark.request_for_arm('CCO', {'id': 'invented'})

    def test_loopback_origin_only_and_credentials_rejected(self):
        for accepted in ('http://127.0.0.1:4185', 'http://localhost:4185/', 'http://[::1]:4185'):
            self.assertEqual(benchmark.validate_base_url(accepted), accepted.rstrip('/'))
        for rejected in ('https://example.com', 'http://127.0.0.1.evil.test', 'http://u:p@localhost:4185',
                         'http://localhost:4185/path', 'http://localhost:4185?secret=1', 'file:///etc/passwd'):
            with self.subTest(url=rejected), self.assertRaises(ValueError):
                benchmark.validate_base_url(rejected)

    def test_projection_excludes_all_input_text_and_reports(self):
        row = self.summaries()[0]
        output = json.dumps(row)
        self.assertNotIn('DO-NOT-PUBLISH', output)
        self.assertIsNone(row['ttftSeconds'])
        self.assertEqual(row['report']['unverifiedAdditionCount'], 1)
        self.assertIsNone(row['report']['unsupportedScientificClaimCount'])
        self.assertEqual(row['report']['sourceErrorCount'], 0)

    def test_bad_metrics_remain_null_not_zero(self):
        arm = benchmark.ARMS[0]
        record = fixture_record(arm)
        record['models']['ollama-qwen-local']['metrics'].update(promptTokens=-1, completionTokens=True,
                                                               durationMs=float('nan'), tokensPerSecond=float('inf'))
        row = benchmark.summarize_run(record, arm=arm, repeat=1)
        for key in ('promptTokens', 'completionTokens', 'durationMs', 'tokensPerSecond'):
            self.assertIsNone(row['models']['ollama-qwen-local']['metrics'][key])

    def test_same_snapshot_revision_and_success_required(self):
        result = benchmark.aggregate(self.summaries())
        self.assertTrue(result['pairedComparability'][0]['comparable'])
        self.assertEqual(result['arms'][0]['comparableCount'], 1)
        self.assertEqual(result['arms'][0]['completionTokens']['median'], 50)
        self.assertIsNone(result['arms'][0]['completionTokens']['p95'])

    def test_mutation_and_failed_cleanup_exclude_comparison(self):
        mutations = (
            lambda row: row.update(snapshotSha256='f' * 64),
            lambda row: row['models']['ollama-qwen-local'].update(revision='f' * 64),
            lambda row: row['models']['toxnet-v3-b16'].update(weightsSha256=None),
            lambda row: row['models']['txgemma-2b-predict'].update(cleanupConfirmed=False),
            lambda row: row['report'].update(validation=None, sourceErrorCount=None),
            lambda row: row['report'].update(reportSchema='aidd.compare.report.v1'),
            lambda row: row.update(requestedArmConfirmed=False),
            lambda row: row.update(cloudRequestAttempted=True),
        )
        for mutation in mutations:
            rows = self.summaries()
            mutation(rows[1])
            with self.subTest(mutation=mutation):
                result = benchmark.aggregate(rows)
                self.assertFalse(result['pairedComparability'][0]['comparable'])
                self.assertEqual(result['arms'][0]['comparableCount'], 0)

    def test_model_change_between_repeats_cannot_mix_summary(self):
        rows = self.summaries()
        repeat_two = copy.deepcopy(rows)
        for row in repeat_two:
            row['repeat'] = 2
            row['runId'] = '2' + row['runId'][1:]
            row['models']['ollama-qwen-local']['revision'] = 'f' * 64
        result = benchmark.aggregate(rows + repeat_two)
        self.assertFalse(result['sameModelIdentityAcrossAllRuns'])
        self.assertTrue(all(not row['comparable'] for row in result['pairedComparability']))

    def test_failed_report_keeps_real_tokens_and_unknown_source_error_count(self):
        arm = benchmark.ARMS[0]
        record = fixture_record(arm)
        record['reports'] = {}
        record['models']['ollama-qwen-local'].update(status='failed', error={'code': 'COMPARE_REPORT_FACT_CHECK_FAILED'})
        row = benchmark.summarize_run(record, arm=arm, repeat=1)
        self.assertEqual(row['models']['ollama-qwen-local']['metrics']['completionTokens'], 50)
        self.assertIsNone(row['report']['sourceErrorCount'])

    def test_small_sample_statistics_document_p95_limit(self):
        self.assertIsNone(benchmark.distribution([])['median'])
        self.assertIsNone(benchmark.distribution([10])['p95'])
        stats = benchmark.distribution([10, 20, None, float('nan'), False])
        self.assertEqual(stats['n'], 2)
        self.assertEqual(stats['median'], 15)
        self.assertAlmostEqual(stats['p95'], 19.5)
        self.assertIn('small sample', stats['statisticalLimit'])

    def test_fake_model_metadata_cannot_publish_windows_path(self):
        arm = benchmark.ARMS[0]
        record = fixture_record(arm)
        record['models']['ollama-qwen-local']['provenance']['modelId'] = 'C:/Users/private/model'
        output = benchmark.summarize_run(record, arm=arm, repeat=1)
        self.assertIsNone(output['models']['ollama-qwen-local']['modelId'])

    def test_existing_receipt_and_oversized_run_plan_refused_before_http(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'receipt.json'
            output.write_text('original', encoding='utf-8')
            args = Namespace(base_url='http://127.0.0.1:4185', smiles=['CCO'], repeat=1, max_runs=4,
                             timeout=60, poll_seconds=.2, output=str(output), source_revision=None)
            with self.assertRaisesRegex(ValueError, 'already exists'):
                benchmark.run_benchmark(args)
            self.assertEqual(output.read_text(encoding='utf-8'), 'original')
            args.repeat, args.max_runs = 2, 4
            with self.assertRaisesRegex(ValueError, 'exceed'):
                benchmark.run_benchmark(args)

    def test_serial_http_flow_no_cloud_and_only_aggregate_written(self):
        arm_by_pair = {(arm['reportContract'], arm['reportMode']): arm for arm in benchmark.ARMS}
        state = {'active': None, 'records': {}, 'postCount': 0, 'requests': []}
        def handler(request):
            state['requests'].append((request.method, request.url.path))
            if request.url.path == '/api/research/compare/runtime':
                return httpx.Response(200, json={'runtimeProvenance': fixture_runtime(), 'inFlightRuns': 0,
                                                'acceptingRuns': True, 'resourceState': {'blocked': False}})
            if request.url.path == '/api/research/models':
                return httpx.Response(200, json={'models': [{'id': key, 'available': True} for key in benchmark.MODEL_IDS[:3]],
                                                'resourcePolicy': {'blocked': False}})
            if request.url.path == '/api/research/compare/runs' and request.method == 'GET':
                return httpx.Response(200, json={'runs': []})
            if request.method == 'POST':
                self.assertIsNone(state['active'])
                payload = json.loads(request.content)
                self.assertFalse(payload['allowCloudTransfer'])
                self.assertIsNone(payload['comparisonReporter'])
                state['postCount'] += 1
                arm = arm_by_pair[payload['reportContract'], payload['reportMode']]
                record = fixture_record(arm, state['postCount'])
                state['active'] = record['runId']
                state['records'][record['runId']] = record
                return httpx.Response(202, json={'runId': record['runId']})
            if request.method == 'GET':
                identifier = request.url.path.rsplit('/', 1)[-1]
                self.assertEqual(state['active'], identifier)
                # The POST-owned ID must be atomically durable before the
                # first poll, even if the CLI is killed during that poll.
                checkpoint = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(checkpoint['activeRunId'], identifier)
                self.assertEqual(checkpoint['status'], 'running')
                state['active'] = None
                return httpx.Response(200, json=state['records'][identifier])
            self.fail('Unexpected mocked benchmark request')
        real_client = httpx.Client
        with tempfile.TemporaryDirectory() as directory, patch.object(benchmark.httpx, 'Client',
                side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)):
            path = Path(directory) / 'fixture-only-not-real-results.json'
            args = Namespace(base_url='http://127.0.0.1:4185', smiles=['CCO'], repeat=1, max_runs=4,
                             timeout=60, poll_seconds=.2, output=str(path), gpu_sample=False,
                             ollama_model=None, source_revision='a' * 40)
            result = benchmark.run_benchmark(args)
            saved = path.read_text(encoding='utf-8')
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(state['postCount'], 4)
            self.assertNotIn('DO-NOT-PUBLISH', saved)
            self.assertNotIn('"smiles"', saved.lower())
            self.assertNotIn('activeRunId', json.loads(saved))
            self.assertTrue(result['summary']['pairedComparability'][0]['comparable'])

    def test_graceful_interruption_keeps_cancelled_usage_and_clears_active_checkpoint(self):
        arm = benchmark.ARMS[0]
        record = fixture_record(arm)
        identifier = record['runId']
        record['status'] = 'cancelled'
        record['reports'] = {}
        record['models']['ollama-qwen-local']['status'] = 'cancelled'
        calls = []
        def handler(request):
            calls.append((request.method, request.url.path))
            return httpx.Response(200, json=record)
        output = {'status': 'interrupted', 'activeRunId': identifier, 'runs': []}
        with httpx.Client(base_url='http://127.0.0.1:4185', transport=httpx.MockTransport(handler)) as client:
            benchmark._recover_owned_interruption(client, output, identifier, arm=arm, repeat=1)
        self.assertEqual(calls, [('DELETE', '/api/research/compare/runs/' + identifier)])
        self.assertNotIn('activeRunId', output)
        self.assertTrue(output['interruptionCleanup']['terminalRecordObserved'])
        self.assertEqual(len(output['runs']), 1)
        self.assertEqual(output['runs'][0]['status'], 'cancelled')
        self.assertTrue(output['runs'][0]['interruptedDuringArm'])
        self.assertEqual(output['runs'][0]['models']['ollama-qwen-local']['metrics']['completionTokens'], 50)
        self.assertNotIn('DO-NOT-PUBLISH', json.dumps(output))

    def test_unconfirmed_interruption_retains_owned_id_for_manual_recovery(self):
        arm = benchmark.ARMS[0]
        identifier = fixture_record(arm)['runId']
        def handler(request):
            raise httpx.ReadTimeout('fixture only', request=request)
        output = {'status': 'interrupted', 'activeRunId': identifier, 'runs': []}
        with httpx.Client(base_url='http://127.0.0.1:4185', transport=httpx.MockTransport(handler)) as client:
            benchmark._recover_owned_interruption(client, output, identifier, arm=arm, repeat=1)
        self.assertEqual(output['activeRunId'], identifier)
        self.assertEqual(output['runs'], [])
        self.assertIn('unconfirmed', output['cleanupAfterInterruption'])

    def test_interruption_recovery_never_overwrites_already_recorded_measurements(self):
        arm = benchmark.ARMS[0]
        original = benchmark.summarize_run(fixture_record(arm), arm=arm, repeat=1)
        identifier = original['runId']
        changed = fixture_record(arm)
        changed['durationMs'] = 10000
        output = {'status': 'interrupted', 'activeRunId': identifier, 'runs': [copy.deepcopy(original)]}
        with httpx.Client(base_url='http://127.0.0.1:4185', transport=httpx.MockTransport(lambda _: httpx.Response(200, json=changed))) as client:
            benchmark._recover_owned_interruption(client, output, identifier, arm=arm, repeat=1)
        self.assertEqual(output['runs'], [original])

    def test_runtime_mismatch_dirty_source_and_active_run_rejected(self):
        response = {'runtimeProvenance': fixture_runtime(), 'inFlightRuns': 0,
                    'acceptingRuns': True, 'resourceState': {'blocked': False}}
        self.assertEqual(benchmark.validate_runtime(response, 'a' * 40)['gitCommit'], 'a' * 40)
        with self.assertRaisesRegex(benchmark.BenchmarkError, 'COMMIT_MISMATCH'):
            benchmark.validate_runtime(response, 'b' * 40)
        for key, value, code in (('sourceFilesDifferFromCommit', True, 'SOURCE_NOT_AUDITED'),
                                 ('sourceSha256', None, 'SOURCE_NOT_AUDITED')):
            dirty = copy.deepcopy(response)
            dirty['runtimeProvenance'][key] = value
            with self.assertRaisesRegex(benchmark.BenchmarkError, code):
                benchmark.validate_runtime(dirty)
        response['inFlightRuns'] = 1
        with self.assertRaisesRegex(benchmark.BenchmarkError, 'ACTIVE_RUN'):
            benchmark.validate_runtime(response)

    def test_stage_gpu_projection_keeps_metrics_without_paths_or_pids(self):
        arm = benchmark.ARMS[0]
        record = fixture_record(arm)
        record['models']['ollama-qwen-local']['metrics']['boardMemory'] = {
            'schema': 'aidd.compare.board-memory-sampling.v1', 'status': 'sampled', 'measured': True,
            'sampleCount': 3, 'attemptCount': 3, 'requestedIntervalSeconds': .25,
            'startedAt': '2026-10-14T02:00:00+00:00', 'endedAt': '2026-10-14T02:00:01+00:00',
            'gpus': [{'index': 0, 'sampledMaximumUsedMiB': 6000, 'sampleCount': 3, 'secretPath': 'DO-NOT-PUBLISH-PATH'}],
            'otherProcessContext': {'maximumObservedComputeProcessCount': 2, 'processes': ['DO-NOT-PUBLISH-PID']},
            'errorCodes': [], 'localPath': 'DO-NOT-PUBLISH-PATH'}
        row = benchmark.summarize_run(record, arm=arm, repeat=1)
        board = row['models']['ollama-qwen-local']['boardMemory']
        self.assertEqual(board['gpus'][0]['sampledMaximumUsedMiB'], 6000)
        self.assertIsNone(board['exactPeakBytes'])
        self.assertNotIn('DO-NOT-PUBLISH', json.dumps(row))


if __name__ == '__main__':
    unittest.main()
