"""Run a real, serial, loopback-only 2x2 report benchmark against Compare.

No provider keys, downloads, synthetic predictions, or DeepSeek requests.
Inputs stay in memory and the local Compare run only; the aggregate output
whitelists structure hashes, model revisions, validation, and real telemetry.
The service keeps its own normal minimal SQLite history; this script never
deletes history or changes Ollama residency / quarantine policy.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import statistics
import sys
import tempfile
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from backend.compare_gpu_metrics import NvidiaSmiSampler

ARMS = (
    {'id': 'v1-deep', 'reportContract': 'v1', 'reportMode': 'deep'},
    {'id': 'v2-deep', 'reportContract': 'v2', 'reportMode': 'deep'},
    {'id': 'v1-fast', 'reportContract': 'v1', 'reportMode': 'fast'},
    {'id': 'v2-fast', 'reportContract': 'v2', 'reportMode': 'fast'},
)
TERMINAL = {'completed', 'partial', 'failed', 'cancelled'}
MODEL_IDS = ('toxnet-v3-b16', 'txgemma-2b-predict', 'ollama-qwen-local', 'deepseek-api')
METRIC_KEYS = ('durationMs', 'queueWaitMs', 'wallSeconds', 'promptTokens', 'completionTokens',
               'totalTokens', 'tokensPerSecond', 'generationSeconds', 'totalDurationSeconds',
               'loadSeconds', 'promptEvaluationSeconds', 'predictionSeconds', 'loadAndExecutionSeconds')


class BenchmarkError(RuntimeError):
    """Only fixed, non-sensitive codes are shown to the operator."""


def validate_base_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.username or parsed.password or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment):
        raise ValueError('Compare benchmark requires a plain HTTP loopback origin')
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('Invalid Compare port') from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('Invalid Compare port')
    return value.rstrip('/')


def request_for_arm(smiles, arm, *, model=None, task='tdc-bbb-martins'):
    if arm not in ARMS:
        raise ValueError('Unsupported benchmark arm')
    return {'smiles': smiles, 'question': '', 'mode': 'compare',
            'primaryPredictor': 'toxnet-v3-b16', 'specialistPredictor': 'txgemma-2b-predict',
            'specialistTask': task, 'primaryReporter': 'ollama-qwen-local', 'comparisonReporter': None,
            'includeTox21': True, 'includeEvidence': False, 'includeAttribution': False,
            'explainEndpoint': None, 'ollamaModel': model, 'allowCloudTransfer': False,
            'persistSensitiveContent': False, 'reportContract': arm['reportContract'], 'reportMode': arm['reportMode']}


def _finite(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _identifier(value):
    return value if (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', value)
                     and not re.match(r'^[A-Za-z]:/', value) and '://' not in value) else None


def _hash(value):
    return value if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) else None


def _error_code(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', value) else None


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.isoformat() if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def project_board_memory(value):
    """Stable telemetry whitelist, including safe unavailable sampler results."""
    if not isinstance(value, dict) or value.get('schema') != 'aidd.compare.board-memory-sampling.v1':
        return None
    output = {'schema': 'aidd.compare.board-memory-sampling.v1',
              'status': value.get('status') if value.get('status') in ('sampled', 'unavailable') else 'unavailable',
              'scope': 'entire board; includes desktop and external applications; not model-attributed',
              'startedAt': _timestamp(value.get('startedAt')), 'endedAt': _timestamp(value.get('endedAt')),
              'exactPeakBytes': None, 'modelAttributedPeakBytes': None,
              'sampledMaximumLimit': 'short peaks may be missed; interval includes query overhead',
              **{key: _finite(value.get(key)) for key in ('requestedIntervalSeconds', 'sampleCount', 'attemptCount')},
              **{key: value.get(key) if type(value.get(key)) is bool else None for key in ('measured', 'memoryMeasured', 'sampleCapReached')},
              'gpus': [{key: _finite(row.get(key)) for key in ('index', 'sampledMaximumUsedMiB', 'firstObservedUsedMiB', 'lastObservedUsedMiB', 'totalMiB', 'sampleCount')}
                       for row in (value.get('gpus') or [])[:16] if isinstance(row, dict)],
              'errorCodes': [code for item in (value.get('errorCodes') or [])[:20] if (code := _error_code(item))]}
    process = value.get('otherProcessContext') or {}
    output['otherProcessContext'] = {'maximumObservedComputeProcessCount': _finite(process.get('maximumObservedComputeProcessCount')),
                                     'ownership': 'not-attributed; may include benchmark and external compute processes',
                                     'graphicsProcessesCovered': False, 'processNamesOrPathsRecorded': False}
    return output


def project_runtime(value):
    value = value or {}
    commit = value.get('gitCommit')
    return {'gitCommit': commit if isinstance(commit, str) and re.fullmatch('[a-f0-9]{40}', commit) else None,
            'sourceSha256': _hash(value.get('sourceSha256')),
            'sourceFilesDifferFromCommit': value.get('sourceFilesDifferFromCommit') if type(value.get('sourceFilesDifferFromCommit')) is bool else None,
            'capturedAt': _timestamp(value.get('capturedAt'))}


def validate_runtime(response, expected_commit=None):
    runtime = project_runtime(response.get('runtimeProvenance'))
    if runtime['sourceFilesDifferFromCommit'] is not False or runtime['gitCommit'] is None or runtime['sourceSha256'] is None:
        raise BenchmarkError('COMPARE_RUNTIME_SOURCE_NOT_AUDITED')
    if expected_commit is not None and runtime['gitCommit'] != expected_commit:
        raise BenchmarkError('COMPARE_RUNTIME_COMMIT_MISMATCH')
    if response.get('acceptingRuns') is not True:
        raise BenchmarkError('COMPARE_SERVICE_NOT_ACCEPTING_RUNS')
    if response.get('inFlightRuns'):
        raise BenchmarkError('COMPARE_EXISTING_ACTIVE_RUN')
    if (response.get('resourceState') or {}).get('blocked'):
        raise BenchmarkError('COMPARE_RESOURCE_QUARANTINED')
    return runtime


def summarize_run(record, *, arm, repeat, board_memory=None):
    """Deliberately never serializes SMILES, questions, reports, paths or rows."""
    report = (record.get('reports') or {}).get('ollama-qwen-local') or {}
    report_sources = report.get('sourceVerification') or {}
    source_match = (report_sources.get('existingFields') == 'exact-match'
                    and report_sources.get('scoresAndSourcesChanged') is False)
    # Accepted reports have zero field mismatches. Rejected reports do not
    # reveal a reliable number of source mistakes; leave that count null.
    source_errors = 0 if source_match else None
    summary = {'runId': _identifier(record.get('runId')), 'arm': arm['id'], 'repeat': repeat,
               'reportContract': arm['reportContract'], 'reportMode': arm['reportMode'],
               'requestedArmConfirmed': (record.get('request') or {}).get('reportContract') == arm['reportContract']
                                        and (record.get('request') or {}).get('reportMode') == arm['reportMode'],
               'status': record.get('status') if record.get('status') in TERMINAL else 'invalid',
               'structureId': _hash((record.get('structure') or {}).get('structureId')),
               'snapshotSha256': _hash((record.get('snapshot') or {}).get('snapshotSha256') or report.get('snapshotSha256')),
               'runtimeProvenance': project_runtime(record.get('runtimeProvenance')),
               'durationMs': _finite(record.get('durationMs')), 'models': {},
               'report': {'reportSchema': _identifier(report.get('reportSchema')),
                          'validation': report.get('status') if report.get('status') in ('verified-structured-report',) else None,
                          'verifiedFactCount': len(report['verifiedFactIds']) if isinstance(report.get('verifiedFactIds'), list) else None,
                          'sourceCount': len((record.get('snapshot') or {})['sources']) if isinstance((record.get('snapshot') or {}).get('sources'), list) else None,
                          'requiredLimitationCount': len(report['limitations']) if isinstance(report.get('limitations'), list) else None,
                          'sourceErrorCount': source_errors,
                          'unverifiedAdditionCount': len(report['unverifiedAdditions']) if isinstance(report.get('unverifiedAdditions'), list) else None,
                          'unverifiedAdditionMeaning': 'unverified commentary items; not an automated count of false scientific claims',
                          'newScientificFactsVerified': False,
                          'unsupportedScientificClaimCount': None},
               'cloudRequestAttempted': (record.get('cloudTransfer') or {}).get('requestAttempted') is True,
               'cloudExecutionRecorded': (record.get('cloudTransfer') or {}).get('executed') is True,
               'boardMemory': project_board_memory(board_memory),
               'ttftSeconds': None, 'ttftStatus': 'not-measured; non-streaming response',
               'loadCondition': 'unload-between-generations; first model residency uncontrolled; no intentional warm-resident measurements',
               'operatingSystemFileCacheState': 'not-controlled; cold model residency does not mean cold file cache'}
    for model_id in MODEL_IDS:
        item = (record.get('models') or {}).get(model_id) or {}
        provenance, metrics = item.get('provenance') or {}, item.get('metrics') or {}
        prediction = (item.get('data') or {}).get('prediction') or {}
        cleanup = metrics.get('cleanupState') or {}
        summary['models'][model_id] = {
            'status': item.get('status') if item.get('status') in ('completed', 'partial', 'failed', 'unavailable', 'unsupported', 'cancelled', 'skipped') else 'invalid',
            'modelId': _identifier(provenance.get('modelId') or provenance.get('modelVersion')),
            'revision': _identifier(provenance.get('revision')),
            'weightsSha256': _hash(provenance.get('weightsSha256')),
            'sourceRevision': _identifier(provenance.get('sourceRevision')),
            'ggufSha256': _hash(provenance.get('ggufSha256')),
            'generationOptions': {key: _finite((provenance.get('generationOptions') or {}).get(key)) for key in ('num_ctx', 'num_predict')},
            'metrics': {key: _finite(metrics.get(key)) for key in METRIC_KEYS},
            'reportValidation': metrics.get('reportValidation') if metrics.get('reportValidation') in ('verified', 'verifying', 'fact-check-failed') else None,
            'cleanupConfirmed': cleanup.get('confirmed') if type(cleanup.get('confirmed')) is bool else None,
            'cleanupState': cleanup.get('state') if cleanup.get('state') in ('idle', 'released', 'quarantined', 'cleaning', 'running') else None,
            'errorCode': _error_code((item.get('error') or {}).get('code')),
            'outputTaskCount': len(prediction['endpoints']) if isinstance(prediction.get('endpoints'), list) else None,
            'pytorchMemory': {key: _finite((metrics.get('gpu') or {}).get(key)) for key in ('peakAllocatedBytes', 'peakReservedBytes')},
            'pytorchMemoryScope': 'isolated process allocator; not board or Ollama memory',
            'boardMemory': project_board_memory(metrics.get('boardMemory')),
        }
    return summary


def distribution(values):
    values = sorted(value for value in values if _finite(value) is not None)
    if not values:
        return {'n': 0, 'median': None, 'p95': None, 'p95Method': 'linear interpolation', 'statisticalLimit': 'not-measured'}
    p95 = None
    if len(values) >= 2:
        position = (len(values) - 1) * .95
        lo = math.floor(position)
        hi = math.ceil(position)
        p95 = values[lo] + (values[hi] - values[lo]) * (position - lo)
    return {'n': len(values), 'median': statistics.median(values), 'p95': p95,
            'p95Method': 'linear interpolation; no P95 estimate from a single observation',
            'statisticalLimit': 'exploratory small sample; not a stable population P95' if len(values) < 20 else 'finite local sample; not a deployment SLA'}


def _comparability_key(row):
    return (row.get('structureId'), row.get('snapshotSha256'), tuple(
        (model_id, row['models'][model_id].get('modelId'), row['models'][model_id].get('revision'),
         row['models'][model_id].get('weightsSha256'), row['models'][model_id].get('ggufSha256'))
        for model_id in MODEL_IDS[:3]),
        (row['runtimeProvenance']['gitCommit'], row['runtimeProvenance']['sourceSha256']))


def aggregate(rows):
    """No improvement claim is produced if snapshots or model digests differ."""
    groups = {}
    for row in rows:
        groups.setdefault((row['structureId'], row['repeat']), []).append(row)
    comparisons = []
    model_identities = {_comparability_key(row)[2] for row in rows}
    runtime_identities = {_comparability_key(row)[3] for row in rows}
    global_model_stability = len(model_identities) == 1 and len(runtime_identities) == 1
    for (structure, repeat), members in groups.items():
        arms = {row['arm']: row for row in members}
        valid = (global_model_stability and len(members) == 4 and len(arms) == 4 and structure is not None
                 and all(row.get('snapshotSha256') is not None for row in members)
                 and len({_comparability_key(row) for row in members}) == 1
                 and all(row['models']['ollama-qwen-local']['revision'] is not None
                         and row['models']['toxnet-v3-b16']['weightsSha256'] is not None
                         and row['models']['txgemma-2b-predict']['revision'] is not None
                         and row['runtimeProvenance']['gitCommit'] is not None
                         and row['runtimeProvenance']['sourceSha256'] is not None
                         and row['runtimeProvenance']['sourceFilesDifferFromCommit'] is False for row in members)
                 and all(row['report']['validation'] == 'verified-structured-report'
                         and row['report']['reportSchema'] == 'aidd.compare.report.' + row['reportContract']
                         and row['requestedArmConfirmed']
                         and row['report']['sourceErrorCount'] == 0
                         and not row['cloudRequestAttempted'] and not row['cloudExecutionRecorded']
                         and all(row['models'][model_id]['status'] == 'completed'
                                 and row['models'][model_id]['cleanupConfirmed'] is True for model_id in MODEL_IDS[:3]) for row in members))
        comparisons.append({'structureId': structure, 'repeat': repeat, 'comparable': valid,
                            'basis': 'same exact fact snapshot, structure ID and three model identities; verified reports and confirmed cleanup',
                            'reason': None if valid else 'incomplete, different snapshot/revision, failed report, cloud attempt or unconfirmed cleanup',
                            'armRunIds': {key: row['runId'] for key, row in arms.items()}})
    statistics_by_arm = []
    eligible_ids = {run_id for group in comparisons if group['comparable'] for run_id in group['armRunIds'].values()}
    for arm in ARMS:
        members = [row for row in rows if row['arm'] == arm['id']]
        verified = [row for row in members if row['runId'] in eligible_ids]
        statistics_by_arm.append({'arm': arm['id'], 'attemptedCount': len(members),
                                  'verifiedCount': sum(row['report']['validation'] == 'verified-structured-report' for row in members),
                                  'verifiedFraction': sum(row['report']['validation'] == 'verified-structured-report' for row in members) / len(members) if members else None,
                                  'comparableCount': len(verified),
                                  'runDurationMs': distribution([row['durationMs'] for row in verified]),
                                  **{key: distribution([row['models']['ollama-qwen-local']['metrics'][key] for row in verified])
                                     for key in ('durationMs', 'generationSeconds', 'promptTokens', 'completionTokens', 'tokensPerSecond')}})
    return {'pairedComparability': comparisons, 'sameModelIdentityAcrossAllRuns': global_model_stability,
            'arms': statistics_by_arm,
            'analysisLimit': 'engineering telemetry only, no toxicity accuracy ranking; small samples are exploratory; fixed arm order may have time/order effects'}


def _json_request(client, method, path, *, payload=None):
    response = client.request(method, path, json=payload)
    if response.status_code >= 400:
        raise BenchmarkError(f'COMPARE_HTTP_{response.status_code}')
    if len(response.content) > 16 * 1024 * 1024:
        raise BenchmarkError('COMPARE_RESPONSE_TOO_LARGE')
    try:
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except ValueError:
        raise BenchmarkError('COMPARE_INVALID_JSON') from None


def _save(path, output):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.benchmark-', suffix='.json', delete=False) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    temporary.replace(path)


def _recover_owned_interruption(client, output, run_id, *, arm, repeat, board_memory=None):
    """Preserve terminal telemetry after cancelling only this script's run.

    Hard process termination cannot reach this handler; activeRunId remains
    in the previous atomic checkpoint for explicit operator recovery.
    """
    try:
        record = _json_request(client, 'DELETE', f'/api/research/compare/runs/{run_id}')
        if record.get('status') not in TERMINAL:
            record = _json_request(client, 'GET', f'/api/research/compare/runs/{run_id}')
        if record.get('runId') != run_id or record.get('status') not in TERMINAL:
            raise BenchmarkError('COMPARE_INTERRUPTION_TERMINAL_STATE_UNCONFIRMED')
        if not any(row['runId'] == run_id for row in output['runs']):
            recovered = summarize_run(record, arm=arm, repeat=repeat, board_memory=board_memory)
            recovered['interruptedDuringArm'] = True
            output['runs'].append(recovered)
        output['interruptionCleanup'] = {'runId': run_id, 'terminalStatus': record['status'],
                                          'terminalRecordObserved': True,
                                          'policy': 'inspect individual model cleanup confirmations; terminal status alone does not prove GPU release'}
        output.pop('activeRunId', None)
    except (BenchmarkError, httpx.HTTPError):
        output['cleanupAfterInterruption'] = 'unconfirmed; inspect service quarantine before any new run'


def run_benchmark(args):
    base_url = validate_base_url(args.base_url)
    if not 1 <= len(args.smiles) <= 10 or not 1 <= args.repeat <= 10:
        raise ValueError('Use 1..10 inputs and 1..10 repetitions')
    planned = len(args.smiles) * args.repeat * len(ARMS)
    if planned > args.max_runs or not 4 <= args.max_runs <= 120:
        raise ValueError('Planned complete 2x2 groups exceed --max-runs (4..120)')
    if not 10 <= args.timeout <= 1800 or not .2 <= args.poll_seconds <= 10:
        raise ValueError('Unsupported timeout or polling interval')
    output_path = Path(args.output)
    if args.source_revision is not None and re.fullmatch('[a-f0-9]{40}', args.source_revision) is None:
        raise ValueError('--source-revision must be a full 40-character commit SHA')
    if output_path.exists():
        raise ValueError('Output already exists; choose a new receipt path to preserve history')
    output = {'schema': 'aidd.compare.report-benchmark.v1',
              'generatedAt': datetime.now(timezone.utc).isoformat(), 'formalAvailability': False,
              'status': 'running', 'arms': list(ARMS), 'plannedRunCount': planned, 'runs': [],
              'inputPolicy': 'only canonical structure IDs persisted; no SMILES, raw reports, questions, data rows, keys or local paths',
              'executionPolicy': {'serial': True, 'cloudTransferAllowed': False, 'modelDownloads': False,
                                  'residency': 'existing adapter keep_alive=0; first residency and OS file cache uncontrolled; no warm-resident test arm',
                                  'warmRunsMeasured': False, 'ttftMeasured': False,
                                  'boardMemory': 'full-run window; external process usage included; sampled maximum only',
                                  'sourceRevision': args.source_revision if re.fullmatch('[a-f0-9]{40}', args.source_revision or '') else None},
              'summary': aggregate([])}
    current = None
    # No inherited proxy, remote redirects, credentials, or cloud requests.
    with httpx.Client(base_url=base_url, timeout=15, trust_env=False, follow_redirects=False,
                      headers={'Origin': base_url, 'Content-Type': 'application/json'}) as client:
        runtime = validate_runtime(_json_request(client, 'GET', '/api/research/compare/runtime'), args.source_revision)
        output['runtimeProvenance'] = runtime
        existing = _json_request(client, 'GET', '/api/research/compare/runs?limit=100')
        if any(row.get('status') in ('pending', 'running') for row in existing.get('runs', [])):
            raise BenchmarkError('COMPARE_EXISTING_ACTIVE_RUN')
        status = _json_request(client, 'GET', '/api/research/models')
        if (status.get('resourcePolicy') or {}).get('blocked'):
            raise BenchmarkError('COMPARE_RESOURCE_QUARANTINED')
        available = {row['id']: row for row in status.get('models', [])}
        if not all(available.get(key, {}).get('available') is True for key in MODEL_IDS[:3]):
            raise BenchmarkError('COMPARE_THREE_LOCAL_MODELS_REQUIRED')
        _save(output_path, output)
        try:
            for repeat in range(1, args.repeat + 1):
                for smiles in args.smiles:
                    for arm in ARMS:
                        board_memory = None
                        sampler = NvidiaSmiSampler(interval_seconds=args.gpu_sample_seconds).start() if args.gpu_sample else None
                        try:
                            submitted = _json_request(client, 'POST', '/api/research/compare/runs', payload=request_for_arm(smiles, arm, model=args.ollama_model))
                            candidate = submitted.get('runId')
                            if not isinstance(candidate, str) or not re.fullmatch('[a-f0-9]{32}', candidate):
                                raise BenchmarkError('COMPARE_INVALID_RUN_ID')
                            current = candidate
                            # A hard-terminated CLI cannot run Python cleanup.
                            # Persist the owned run ID immediately so manual
                            # recovery can cancel exactly this request by API.
                            output['activeRunId'] = current
                            _save(output_path, output)
                            deadline = time.monotonic() + args.timeout
                            while True:
                                record = _json_request(client, 'GET', f'/api/research/compare/runs/{current}')
                                if record.get('status') in TERMINAL:
                                    break
                                if time.monotonic() >= deadline:
                                    _json_request(client, 'DELETE', f'/api/research/compare/runs/{current}')
                                    record = _json_request(client, 'GET', f'/api/research/compare/runs/{current}')
                                    break
                                time.sleep(args.poll_seconds)
                        finally:
                            board_memory = sampler.stop() if sampler else None
                        output['runs'].append(summarize_run(record, arm=arm, repeat=repeat, board_memory=board_memory))
                        if output['runs'][-1]['runtimeProvenance'] != runtime:
                            raise BenchmarkError('COMPARE_RUNNING_SERVICE_CHANGED_STOPPED')
                        current = None
                        output.pop('activeRunId', None)
                        output['summary'] = aggregate(output['runs'])
                        _save(output_path, output)
                        print(f"{arm['id']} repeat={repeat} status={output['runs'][-1]['status']} completed={len(output['runs'])}/{planned}", flush=True)
                        if output['runs'][-1]['cloudRequestAttempted'] or output['runs'][-1]['cloudExecutionRecorded']:
                            raise BenchmarkError('UNEXPECTED_CLOUD_ATTEMPT_STOPPED')
                        if any(output['runs'][-1]['models'][key]['cleanupConfirmed'] is False for key in MODEL_IDS[:3]):
                            raise BenchmarkError('LOCAL_CLEANUP_UNCONFIRMED_STOPPED')
            output['status'] = 'completed'
        except BaseException:
            output['status'] = 'interrupted'
            if current:
                _recover_owned_interruption(client, output, current, arm=arm, repeat=repeat, board_memory=board_memory)
            raise
        finally:
            output['summary'] = aggregate(output['runs'])
            _save(output_path, output)
    return output


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--base-url', default='http://127.0.0.1:4185')
    result.add_argument('--smiles', action='append', required=True, help='Repeat this flag for a second real legal input; inputs are never written to the receipt')
    result.add_argument('--repeat', type=int, default=1)
    result.add_argument('--max-runs', type=int, default=12)
    result.add_argument('--ollama-model', default=None, help='Must already be installed; never pulled')
    result.add_argument('--timeout', type=int, default=900)
    result.add_argument('--poll-seconds', type=float, default=1)
    result.add_argument('--gpu-sample', action='store_true')
    result.add_argument('--gpu-sample-seconds', type=float, default=.25)
    result.add_argument('--source-revision', default=None, help='Optional audited running service commit SHA; not inferred from the client checkout')
    result.add_argument('--output', required=True, help='New aggregate receipt; refuses to overwrite existing history')
    return result


def main():
    try:
        receipt = run_benchmark(parser().parse_args())
        print(f"Real attempts recorded: {len(receipt['runs'])}; inspect pairedComparability before comparing telemetry.")
        return 0
    except (BenchmarkError, ValueError) as error:
        print(f'Benchmark stopped: {error}', file=sys.stderr)
        return 2
    except httpx.HTTPError:
        print('Benchmark stopped: LOOPBACK_HTTP_UNAVAILABLE (no provider fallback)', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Benchmark cancelled; receipt retains completed attempts. Verify cleanup before retrying.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
