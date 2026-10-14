"""Bounded asynchronous compare runs. Models are independent, never averaged."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from backend.chemistry import analyze_chemistry
from backend.compare_reporting import (CLOUD_CATEGORIES, LIMITATIONS, build_snapshot, encoded,
                                       report_messages, validate_b16, validate_report, validate_specialist)
from backend.compare_resources import CompareResourceManager, ResourceUnavailable
from backend.compare_logging import configure_diagnostics, diagnostic
from backend.compare_persistence import CompareRunStore
from backend.compare_schemas import CONTRACT_VERSION, MODEL_IDS, ComparePresetRequest, CompareSettings
from backend.compare_runtime import STARTUP_RUNTIME

TERMINAL = {'completed', 'partial', 'failed', 'cancelled'}


def now():
    return datetime.now(timezone.utc).isoformat()


def bounded_int(env, name, default, low, high):
    try:
        value = int(env.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


class CompareRunError(Exception):
    def __init__(self, code, message, status_code=422):
        self.code, self.public_message, self.status_code = code, message, status_code


class CompareOrchestrator:
    def __init__(self, registry, *, env=None, resources=None, presets_path=None, persistence_path=None, store=None):
        self.registry = registry
        self.adapters = registry.adapters
        self.resources = resources or CompareResourceManager()
        self.runtime_provenance = copy.deepcopy(STARTUP_RUNTIME)
        env = env or {}
        self.sample_gpu = str(env.get('TOXNET_COMPARE_GPU_SAMPLING', 'false')).lower() == 'true'
        self.stage_seconds = bounded_int(env, 'TOXNET_COMPARE_STAGE_TIMEOUT_SECONDS', 240, 1, 600)
        self.run_seconds = bounded_int(env, 'TOXNET_COMPARE_RUN_TIMEOUT_SECONDS', 900, 5, 1800)
        self.ttl = bounded_int(env, 'TOXNET_COMPARE_TTL_SECONDS', 1800, 1, 86400)
        self.maximum_records = bounded_int(env, 'TOXNET_COMPARE_MAX_RUNS', 32, 1, 256)
        self.max_running = bounded_int(env, 'TOXNET_COMPARE_MAX_RUNNING', 1, 1, 4)
        self.semaphore = asyncio.Semaphore(self.max_running)
        self.runs, self.tasks, self.expiry = {}, {}, {}
        self.store = store or CompareRunStore(persistence_path, recovery_ttl_seconds=self.ttl)
        self.cancel_tasks = {}
        self.started, self.closing, self._close_task = False, False, None
        self.single_worker = bounded_int(env, 'WEB_CONCURRENCY', 1, 1, 32) == 1
        self.presets_path = Path(presets_path).resolve() if presets_path else None
        self.presets = []
        self._load_presets()

    async def start(self):
        if self.started:
            return
        if not self.single_worker:
            raise CompareRunError('COMPARE_SINGLE_WORKER_REQUIRED', 'Compare 当前仅支持本机单 worker；不得使用多 worker GPU 调度。', 503)
        configure_diagnostics()
        self.store.start()
        # An interrupted local execution can leave an independent Ollama
        # daemon or orphan worker alive. Restart is not evidence of release.
        if self.store.has_interrupted_local_execution():
            self.store.set_quarantine('INTERRUPTED_LOCAL_EXECUTION')
        recovered = self.store.recover_interrupted()
        self.store.prune()
        blocked = self.store.get_quarantine()
        if blocked:
            self.resources.blocked_reason = blocked
        self.started = True
        diagnostic(stage='service-started', error_code='INTERRUPTED_RUNS_RECOVERED' if recovered else None)

    def _persist(self, record, *, strict=True):
        try:
            if self.resources.blocked_reason:
                # Never overwrite an interrupted local record with a terminal
                # one until the durable quarantine marker has been saved.
                self.store.set_quarantine('LOCAL_MODEL_CLEANUP_UNCONFIRMED')
            remaining = max(0, self.expiry[record['runId']] - time.monotonic()) if record['status'] in TERMINAL and record['runId'] in self.expiry else self.ttl
            self.store.upsert(record, time.time() + remaining)
        except Exception:
            record['persistence'] = {'status': 'failed', 'errorCode': 'COMPARE_PERSISTENCE_UNAVAILABLE'}
            diagnostic(request_id=record.get('requestId'), run_id=record['runId'], stage='persistence-failed', error_code='COMPARE_PERSISTENCE_UNAVAILABLE')
            if strict:
                raise CompareRunError('COMPARE_PERSISTENCE_UNAVAILABLE', '实验记录保存失败；停止后续调用，请检查本机数据库。', 503) from None

    def _log(self, record, stage, model_id=None):
        item = record['models'].get(model_id, {})
        adapter = self.adapters.get(model_id)
        cleanup = getattr(adapter, 'cleanup_status', lambda: None)()
        diagnostic(request_id=record.get('requestId'), run_id=record['runId'], model_id=model_id, stage=stage,
                   error_code=(item.get('error') or record.get('error') or {}).get('code'),
                   duration_ms=(item.get('metrics') or {}).get('durationMs', record.get('durationMs')),
                   cleanup_state=cleanup, provider_request_attempted=record['cloudTransfer']['requestAttempted'])

    def _load_presets(self):
        if self.presets_path and self.presets_path.is_file():
            try:
                if self.presets_path.stat().st_size > 128 * 1024:
                    return
                value = json.loads(self.presets_path.read_text(encoding='utf-8'))
                if not isinstance(value, list) or len(value) > 20:
                    return
                validated = []
                for row in value:
                    if not isinstance(row, dict) or set(row) - {'id', 'name', 'settings', 'createdAt', 'updatedAt'} or not {'id', 'name', 'settings'} <= set(row) or not isinstance(row['id'], str):
                        return
                    item = ComparePresetRequest.model_validate({'name': row['name'], 'settings': row['settings']})
                    if any(v['id'] == row['id'] or v['name'].casefold() == item.name.casefold() for v in validated):
                        return
                    validated.append({'id': row['id'], **item.model_dump(), 'createdAt': row.get('createdAt', now()), 'updatedAt': row.get('updatedAt', now())})
                self.presets = validated
            except (ValueError, OSError, TypeError):
                self.presets = []

    def sweep(self):
        clock = time.monotonic()
        for identifier in list(self.runs):
            if self.runs[identifier]['status'] in TERMINAL and self.expiry.get(identifier, float('inf')) <= clock:
                self.runs.pop(identifier, None)
                self.tasks.pop(identifier, None)
                self.expiry.pop(identifier, None)
                self.cancel_tasks.pop(identifier, None)
                self.store.delete(identifier)
        self.store.prune()

    async def describe(self):
        async with self.resources.slot('availability-check'):
            try:
                models = await asyncio.wait_for(self.registry.describe(), timeout=self.stage_seconds)
            finally:
                # Status probes can load a real worker too. Own one drain task
                # and finish all adapters even if one cleanup raises/cancels.
                from backend.compare_resources import wait_tracked_cleanup
                await wait_tracked_cleanup(asyncio.create_task(self._drain_adapters()))
        return {'contractVersion': CONTRACT_VERSION, 'models': models,
                'runtimeProvenance': copy.deepcopy(self.runtime_provenance),
                'specialistTasks': self._task_list(), 'resourcePolicy': self.resources.public(),
                'runPolicy': {'maxRunning': self.max_running, 'maxRecords': self.maximum_records,
                              'stageTimeoutSeconds': self.stage_seconds, 'runTimeoutSeconds': self.run_seconds,
                              'terminalRecordTtlSeconds': self.ttl, 'persistence': 'local-sqlite',
                              'defaultContentPolicy': 'minimal-summary; full content requires explicit per-run local consent',
                              'singleWorkerRequired': True, 'automaticResume': False}}

    def _quarantine(self, reason='LOCAL_MODEL_CLEANUP_UNCONFIRMED'):
        self.resources.blocked_reason = reason
        try:
            self.store.set_quarantine(reason)
        except Exception:
            diagnostic(stage='quarantine-persistence-failed', error_code='COMPARE_PERSISTENCE_UNAVAILABLE')

    async def _drain_adapters(self, *, close=False):
        uncertain = bool(self.resources.blocked_reason)
        for adapter in self.adapters.values():
            operation = getattr(adapter, 'close' if close else 'wait_cleanup', None)
            if operation:
                try:
                    await operation()
                except (Exception, asyncio.CancelledError):
                    uncertain = True
            if getattr(adapter, 'cleanup_safe', True) is False:
                uncertain = True
        if uncertain:
            self._quarantine()
        return not uncertain

    @staticmethod
    def _task_list():
        from backend.compare_tasks import task_registry
        return task_registry()

    def _validate_task(self, task):
        if task is not None and task not in {item['id'] for item in self._task_list()}:
            raise CompareRunError('UNKNOWN_SPECIALIST_TASK', '专项任务不在已审核注册中心。')

    def submit(self, payload, request_id=None):
        if self.closing:
            raise CompareRunError('COMPARE_SERVICE_STOPPING', '服务正在关闭，不能启动新的模型。', 503)
        self._validate_task(payload.specialistTask)
        self.sweep()
        if len(self.runs) >= self.maximum_records:
            raise CompareRunError('COMPARE_CAPACITY_EXCEEDED', '比较运行队列已满，请等待记录过期。', 429)
        chemistry = analyze_chemistry(payload.smiles)
        identifier = uuid4().hex
        record = {'contractVersion': CONTRACT_VERSION, 'runId': identifier, 'status': 'pending',
                  'runtimeProvenance': copy.deepcopy(self.runtime_provenance),
                  'createdAt': now(), 'updatedAt': now(), 'requestId': request_id, 'request': payload.model_dump(),
                  'contentSaved': payload.persistSensitiveContent, 'restored': False,
                  'recovery': {'contentAvailable': True, 'executionResumed': False},
                  'structure': {key: chemistry[key] for key in ('canonicalSmiles', 'structureId')},
                  'models': {model: {'status': 'pending', 'data': None, 'metrics': {}, 'provenance': {}, 'error': None}
                             for model in MODEL_IDS}, 'snapshot': None, 'reports': {}, 'comparison': None,
                  'limitations': LIMITATIONS, 'cloudTransfer': {'allowed': payload.allowCloudTransfer,
                      'executed': False, 'requestAttempted': False, 'informationCategories': CLOUD_CATEGORIES if payload.allowCloudTransfer else [],
                      'rawExperimentalRows': False, 'privatePaths': False, 'freeQuestion': False}}
        self._persist(record)
        self.runs[identifier] = record
        self.tasks[identifier] = asyncio.create_task(self._run(record, chemistry))
        self._log(record, 'run-submitted')
        return {'contractVersion': CONTRACT_VERSION, 'runId': identifier, 'status': 'pending',
                'pollUrl': f'/api/research/compare/runs/{identifier}'}

    def get(self, identifier):
        self.sweep()
        if identifier not in self.runs:
            saved = self.store.get(identifier)
            if saved is None:
                raise CompareRunError('COMPARE_RUN_NOT_FOUND', '运行不存在或已过期。', 404)
            saved['restored'] = True
            return saved
        result = copy.deepcopy(self.runs[identifier])
        result['resourceState'] = self.resources.public()
        return result

    def list_runs(self, limit=20, offset=0, q=''):
        self.sweep()
        saved = self.store.list(limit=limit, offset=offset, q=q)
        return {'contractVersion': CONTRACT_VERSION, 'runs': saved['items'], 'total': saved['total'],
                'limit': limit, 'offset': offset, 'hasMore': offset + len(saved['items']) < saved['total']}

    def runtime_status(self):
        return {'contractVersion': CONTRACT_VERSION, 'runtimeProvenance': copy.deepcopy(self.runtime_provenance),
                'resourceState': self.resources.public(), 'persistence': self.store.diagnostics(),
                'acceptingRuns': self.started and not self.closing,
                'inFlightRuns': sum(not task.done() for task in self.tasks.values())}

    async def cancel(self, identifier):
        record = self.get(identifier)
        if record['status'] in TERMINAL:
            return record
        if identifier not in self.tasks:
            return record
        if identifier not in self.cancel_tasks:
            async def stop_once():
                task = self.tasks[identifier]
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if self.runs[identifier]['status'] not in TERMINAL:
                    self._cancel_record(self.runs[identifier])
            self.cancel_tasks[identifier] = asyncio.create_task(stop_once())
        # HTTP client cancellation never cancels the tracked stop operation.
        await asyncio.shield(self.cancel_tasks[identifier])
        return self.get(identifier)

    def _cancel_record(self, record):
        record['status'], record['updatedAt'] = 'cancelled', now()
        for item in record['models'].values():
            if item['status'] in ('pending', 'running'):
                item['status'] = 'cancelled'
        self.expiry[record['runId']] = time.monotonic() + self.ttl
        self._persist(record, strict=False)
        self._log(record, 'run-cancelled')

    async def _execute(self, record, model_id, context, *, reporter=False):
        item, adapter = record['models'][model_id], self.adapters[model_id]
        item['status'], record['updatedAt'] = 'running', now()
        self._persist(record)
        self._log(record, 'model-started', model_id)
        started = time.perf_counter()
        execution_started = False
        sampler = None
        try:
            async def operation():
                nonlocal execution_started, sampler
                execution_started = True
                if self.sample_gpu and model_id != 'deepseek-api':
                    from backend.compare_gpu_metrics import NvidiaSmiSampler
                    sampler = NvidiaSmiSampler(interval_seconds=0.25).start()
                result = await adapter.execute(copy.deepcopy(context))
                if not isinstance(result, dict) or not isinstance(result.get('data'), dict) or result.get('status') not in ('completed', 'partial'):
                    raise ValueError('Adapter returned an invalid typed envelope')
                return result
            if model_id == 'deepseek-api':
                result = await asyncio.wait_for(operation(), self.stage_seconds)
                queue = {}
            else:
                async with self.resources.slot(model_id, adapter) as queue:
                    result = await asyncio.wait_for(operation(), self.stage_seconds)
            data = result['data']
            # Preserve measured engineering evidence even if scientific fields
            # fail deterministic validation; untrusted report text stays null.
            item['provenance'] = copy.deepcopy(result.get('provenance') or {})
            item['metrics'] = {**copy.deepcopy(result.get('metrics') or {}), **queue}
            if model_id == 'toxnet-v3-b16':
                validate_b16(data.get('prediction'), record['structure'])
            if model_id == 'txgemma-2b-predict':
                validate_specialist(data, record['structure'], record['request']['specialistTask'], result.get('provenance') or {})
            if reporter:
                raw = data.get('rawReport', data.get('report', data))
                item['metrics']['reportValidation'] = 'verifying'
                verified = validate_report(raw, context['snapshot'],
                    report_contract=record['request'].get('reportContract', 'v1'),
                    report_mode=record['request'].get('reportMode', 'deep'))
                record['reports'][model_id] = verified
                data = {'report': verified}
                item['metrics']['reportValidation'] = 'verified'
                item['metrics']['sourceErrorCount'] = 0
                item['metrics']['unverifiedAdditionCount'] = len(verified['unverifiedAdditions'])
            item.update(status='partial' if result.get('status') == 'partial' else 'completed', data=copy.deepcopy(data),
                        provenance=copy.deepcopy(result.get('provenance') or {}), metrics={**item['metrics'], **queue})
        except asyncio.CancelledError as error:
            item['metrics'].update(copy.deepcopy(getattr(error, 'metrics', {}) or {}))
            item['provenance'].update(copy.deepcopy(getattr(error, 'provenance', {}) or {}))
            partial = getattr(adapter, 'last_partial', None)
            if execution_started and model_id == 'toxnet-v3-b16' and isinstance(partial, dict):
                try:
                    prediction = partial.get('prediction', (partial.get('data') or {}).get('prediction'))
                    validate_b16(prediction, record['structure'])
                    item.update(status='partial', data={'prediction': copy.deepcopy(prediction)},
                                provenance=copy.deepcopy(partial.get('provenance') or {}),
                                error={'code': 'OPTIONAL_STAGE_CANCELLED', 'message': '已完成真实预测保留；后续阶段已取消。'})
                    item['metrics'].update(copy.deepcopy(partial.get('metrics') or {}))
                except ValueError:
                    item['status'] = 'cancelled'
            else:
                item['status'] = 'cancelled'
            raise
        except (asyncio.TimeoutError, TimeoutError):
            item.update(status='failed', error={'code': 'COMPARE_MODEL_TIMEOUT', 'message': '模型调用超过规定时间。'})
            partial = getattr(adapter, 'last_partial', None)
            if execution_started and model_id == 'toxnet-v3-b16' and isinstance(partial, dict):
                try:
                    prediction = (partial.get('data') or {}).get('prediction')
                    validate_b16(prediction, record['structure'])
                    item.update(status='partial', data={'prediction': copy.deepcopy(prediction)},
                                provenance=copy.deepcopy(partial.get('provenance') or {}),
                                error={'code': 'OPTIONAL_STAGE_TIMEOUT', 'message': '已完成真实预测保留；后续阶段超时。'})
                    item['metrics'].update(copy.deepcopy(partial.get('metrics') or {}))
                except ValueError:
                    pass
        except Exception as error:
            item['metrics'].update(copy.deepcopy(getattr(error, 'metrics', {}) or {}))
            item['provenance'].update(copy.deepcopy(getattr(error, 'provenance', {}) or {}))
            code = getattr(error, 'code', 'COMPARE_INVALID_OR_FAILED_OUTPUT')
            message = getattr(error, 'public_message', '模型输出或事实核验失败；该结果不可用。')
            if reporter and item['metrics'].get('reportValidation') == 'verifying':
                item['metrics']['reportValidation'] = 'fact-check-failed'
                item['metrics']['sourceErrorCount'] = None
                item['metrics']['unverifiedAdditionCount'] = None
                code = 'COMPARE_REPORT_FACT_CHECK_FAILED'
            status = 'unsupported' if 'UNSUPPORTED' in code else 'unavailable' if any(word in code for word in ('UNAVAILABLE', 'NOT_READY', 'NOT_CONFIGURED', 'UNCONFIGURED', 'MISSING', 'AUTHORIZATION', 'DISABLED', 'LICENSE_REQUIRED', 'NOT_INSTALLED', 'CONNECTION_ERROR')) else 'failed'
            if isinstance(error, ResourceUnavailable):
                code, message, status = 'COMPARE_RESOURCE_QUARANTINED', '本地资源已隔离；停止 Compare 后按安全恢复流程验证清理，普通重启不会解除隔离。', 'unavailable'
            item.update(status=status, error={'code': code, 'message': message})
        finally:
            if sampler is not None:
                # No GPU ownership in the sampler. The bounded driver-query
                # join completes after adapter cleanup; it does not release a
                # resource slot early or create an untracked cancellation task.
                item['metrics']['boardMemory'] = sampler.stop()
            if model_id == 'deepseek-api':
                attempted_for = getattr(adapter, 'attempted_for', None)
                record['cloudTransfer']['requestAttempted'] = bool(attempted_for(record['runId']) if attempted_for else item['provenance'].get('requestAttempted', False))
                record['cloudTransfer']['executed'] = item['status'] in ('completed', 'partial')
                item['metrics'].setdefault('httpRequestState', 'attempted-unconfirmed' if record['cloudTransfer']['requestAttempted'] else 'not-attempted')
            item['metrics']['durationMs'] = round((time.perf_counter() - started) * 1000, 3)
            cleanup = getattr(adapter, 'cleanup_status', lambda: None)()
            if cleanup is not None:
                item['metrics']['cleanupState'] = cleanup
            if self.resources.blocked_reason:
                self._quarantine()
            record['updatedAt'] = now()
            self._persist(record)
            self._log(record, 'model-finished', model_id)

    async def _run(self, record, chemistry):
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.run_seconds):
                async with self.semaphore:
                    record['queueWaitMs'] = round((time.perf_counter() - started) * 1000, 3)
                    record['status'], record['updatedAt'] = 'running', now()
                    self._persist(record)
                    context = {**record['structure'], 'request': record['request']}
                    await self._execute(record, 'toxnet-v3-b16', context)
                    task = record['request']['specialistTask']
                    if task is None:
                        record['models']['txgemma-2b-predict']['status'] = 'skipped'
                    elif not next(t for t in self._task_list() if t['id'] == task).get('supported'):
                        record['models']['txgemma-2b-predict'].update(status='unsupported', error={'code': 'TXGEMMA_TASK_UNSUPPORTED', 'message': '未审核任务不执行预测。'})
                    else:
                        await self._execute(record, 'txgemma-2b-predict', context)
                    snapshot = build_snapshot(record['structure'], chemistry, record['models'],
                        include_tox21=record['request']['includeTox21'], question=record['request']['question'],
                        specialist_task=record['request']['specialistTask'])
                    frozen = encoded(snapshot)
                    record['snapshot'] = json.loads(frozen)
                    self._persist(record)
                    # Only the immutable common snapshot is provided, never a
                    # preceding generator's report or the free user question.
                    report_request = {key: value for key, value in record['request'].items() if key not in ('smiles', 'question')}
                    messages = report_messages(json.loads(frozen),
                        report_contract=record['request'].get('reportContract', 'v1'),
                        report_mode=record['request'].get('reportMode', 'deep'))
                    report_context = {'runId': record['runId'], 'snapshotSha256': snapshot['snapshotSha256'], 'snapshot': json.loads(frozen), 'messages': messages,
                                      'request': report_request, 'reporterModel': record['request']['ollamaModel']}
                    await self._execute(record, 'ollama-qwen-local', report_context, reporter=True)
                    if not record['request']['comparisonReporter'] or not record['request']['allowCloudTransfer']:
                        record['models']['deepseek-api'].update(status='skipped', error={'code': 'CLOUD_NOT_AUTHORIZED', 'message': '未授权云端外传，没有调用 DeepSeek。'})
                    else:
                        await self._execute(record, 'deepseek-api', report_context, reporter=True)
                    record['comparison'] = self._comparison(record)
                    states = [item['status'] for item in record['models'].values()]
                    succeeded = any(state in ('completed', 'partial') for state in states)
                    record['status'] = 'failed' if not succeeded else 'partial' if any(state in ('partial', 'failed', 'unavailable', 'unsupported') for state in states) else 'completed'
        except asyncio.CancelledError:
            self._cancel_record(record)
            raise
        except (asyncio.TimeoutError, TimeoutError):
            record['status'] = 'partial' if any(item['status'] in ('completed', 'partial') for item in record['models'].values()) else 'failed'
            record['error'] = {'code': 'COMPARE_RUN_TIMEOUT', 'message': '整个比较运行已超时，已完成结果保留。'}
            for item in record['models'].values():
                if item['status'] in ('pending', 'running'):
                    item['status'] = 'cancelled'
        except Exception as error:
            record['status'] = 'partial' if any(item['status'] in ('completed', 'partial') for item in record['models'].values()) else 'failed'
            record['error'] = {'code': getattr(error, 'code', 'COMPARE_RUN_FAILED'), 'message': getattr(error, 'public_message', '比较运行失败，已完成结果保留。')}
            for item in record['models'].values():
                if item['status'] in ('pending', 'running'):
                    item['status'] = 'unavailable'
        finally:
            record['durationMs'] = round((time.perf_counter() - started) * 1000, 3)
            record['updatedAt'] = now()
            self.expiry[record['runId']] = time.monotonic() + self.ttl
            self._persist(record, strict=False)
            self._log(record, 'run-finished')

    @staticmethod
    def _comparison(record):
        reports = record['reports']
        ids = list(reports)
        return {'method': 'independent-outputs-no-vote-no-average', 'scientificAccuracyRanked': False,
                'directPredictionComparison': False,
                'predictionComparisonReason': 'Reviewed TxGemma BBB task is a different endpoint; no same-label independent evaluation.',
                'sharedSnapshotSha256': record['snapshot']['snapshotSha256'],
                'independentReporterInputs': True, 'reportersCompleted': ids,
                'verifiedFactsShared': sorted(set.intersection(*(set(reports[i]['verifiedFactIds']) for i in ids))) if ids else [],
                'sectionOrdersEqual': [s['id'] for s in reports[ids[0]]['sections']] == [s['id'] for s in reports[ids[1]]['sections']] if len(ids) == 2 else None,
                'unverifiedProseCounts': {key: len(value['unverifiedAdditions']) for key, value in reports.items()}}

    def list_presets(self):
        return {'contractVersion': CONTRACT_VERSION, 'presets': copy.deepcopy(self.presets), 'cloudConsentPersisted': False}

    def save_preset(self, payload):
        self._validate_task(payload.settings.specialistTask)
        if any(row['name'].casefold() == payload.name.casefold() for row in self.presets):
            raise CompareRunError('PRESET_NAME_CONFLICT', '已存在同名配置，请修改名称或编辑原配置。', 409)
        if len(self.presets) >= 20:
            raise CompareRunError('PRESET_CAPACITY_EXCEEDED', '预设已达数量限制。', 429)
        row = {'id': uuid4().hex, **payload.model_dump(), 'createdAt': now(), 'updatedAt': now()}
        updated = [*self.presets, row]
        self._write_presets(updated)
        return copy.deepcopy(row)

    def update_preset(self, identifier, payload):
        self._validate_task(payload.settings.specialistTask)
        original = next((row for row in self.presets if row['id'] == identifier), None)
        if original is None:
            raise CompareRunError('PRESET_NOT_FOUND', '预设不存在。', 404)
        if any(row['id'] != identifier and row['name'].casefold() == payload.name.casefold() for row in self.presets):
            raise CompareRunError('PRESET_NAME_CONFLICT', '已存在同名配置，请修改名称。', 409)
        row = {'id': identifier, **payload.model_dump(), 'createdAt': original['createdAt'], 'updatedAt': now()}
        self._write_presets([row if item['id'] == identifier else item for item in self.presets])
        return copy.deepcopy(row)

    def delete_preset(self, identifier):
        if not any(row['id'] == identifier for row in self.presets):
            raise CompareRunError('PRESET_NOT_FOUND', '预设不存在。', 404)
        self._write_presets([row for row in self.presets if row['id'] != identifier])
        return {'id': identifier, 'deleted': True}

    def _write_presets(self, updated):
        if self.presets_path:
            self.presets_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.presets_path.parent, prefix='.compare-presets-', delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(encoded(updated))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.presets_path)
            except OSError:
                raise CompareRunError('PRESET_SAVE_FAILED', '无法保存本机研究预设。', 503) from None
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()
        self.presets = updated

    async def close(self):
        if self._close_task is None:
            self.closing = True
            async def close_once():
                try:
                    await asyncio.gather(*(self.cancel(identifier) for identifier, task in self.tasks.items() if not task.done()), return_exceptions=True)
                    await self._drain_adapters(close=True)
                    for record in self.runs.values():
                        self._persist(record, strict=False)
                finally:
                    self.store.close()
            self._close_task = asyncio.create_task(close_once())
        from backend.compare_resources import wait_tracked_cleanup
        await wait_tracked_cleanup(self._close_task)
