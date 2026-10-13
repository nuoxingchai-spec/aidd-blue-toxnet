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
from backend.compare_schemas import CONTRACT_VERSION, MODEL_IDS, ComparePresetRequest, CompareSettings

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
    def __init__(self, registry, *, env=None, resources=None, presets_path=None):
        self.registry = registry
        self.adapters = registry.adapters
        self.resources = resources or CompareResourceManager()
        env = env or {}
        self.stage_seconds = bounded_int(env, 'TOXNET_COMPARE_STAGE_TIMEOUT_SECONDS', 240, 1, 600)
        self.run_seconds = bounded_int(env, 'TOXNET_COMPARE_RUN_TIMEOUT_SECONDS', 900, 5, 1800)
        self.ttl = bounded_int(env, 'TOXNET_COMPARE_TTL_SECONDS', 1800, 1, 86400)
        self.maximum_records = bounded_int(env, 'TOXNET_COMPARE_MAX_RUNS', 32, 1, 256)
        self.max_running = bounded_int(env, 'TOXNET_COMPARE_MAX_RUNNING', 1, 1, 4)
        self.semaphore = asyncio.Semaphore(self.max_running)
        self.runs, self.tasks, self.expiry = {}, {}, {}
        self.presets_path = Path(presets_path).resolve() if presets_path else None
        self.presets = []
        self._load_presets()

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
                    if not isinstance(row, dict) or set(row) != {'id', 'name', 'settings'} or not isinstance(row['id'], str):
                        return
                    item = ComparePresetRequest.model_validate({'name': row['name'], 'settings': row['settings']})
                    validated.append({'id': row['id'], **item.model_dump()})
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

    async def describe(self):
        async with self.resources.slot('availability-check'):
            models = await asyncio.wait_for(self.registry.describe(), timeout=self.stage_seconds)
        return {'contractVersion': CONTRACT_VERSION, 'models': models,
                'specialistTasks': self._task_list(), 'resourcePolicy': self.resources.public(),
                'runPolicy': {'maxRunning': self.max_running, 'maxRecords': self.maximum_records,
                              'stageTimeoutSeconds': self.stage_seconds, 'runTimeoutSeconds': self.run_seconds,
                              'terminalRecordTtlSeconds': self.ttl, 'persistence': 'process-memory-only'}}

    @staticmethod
    def _task_list():
        from backend.compare_tasks import task_registry
        return task_registry()

    def _validate_task(self, task):
        if task is not None and task not in {item['id'] for item in self._task_list()}:
            raise CompareRunError('UNKNOWN_SPECIALIST_TASK', '专项任务不在已审核注册中心。')

    def submit(self, payload):
        self._validate_task(payload.specialistTask)
        self.sweep()
        if len(self.runs) >= self.maximum_records:
            raise CompareRunError('COMPARE_CAPACITY_EXCEEDED', '比较运行队列已满，请等待记录过期。', 429)
        chemistry = analyze_chemistry(payload.smiles)
        identifier = uuid4().hex
        record = {'contractVersion': CONTRACT_VERSION, 'runId': identifier, 'status': 'pending',
                  'createdAt': now(), 'updatedAt': now(), 'request': payload.model_dump(),
                  'structure': {key: chemistry[key] for key in ('canonicalSmiles', 'structureId')},
                  'models': {model: {'status': 'pending', 'data': None, 'metrics': {}, 'provenance': {}, 'error': None}
                             for model in MODEL_IDS}, 'snapshot': None, 'reports': {}, 'comparison': None,
                  'limitations': LIMITATIONS, 'cloudTransfer': {'allowed': payload.allowCloudTransfer,
                      'executed': False, 'requestAttempted': False, 'informationCategories': CLOUD_CATEGORIES if payload.allowCloudTransfer else [],
                      'rawExperimentalRows': False, 'privatePaths': False, 'freeQuestion': False}}
        self.runs[identifier] = record
        self.tasks[identifier] = asyncio.create_task(self._run(record, chemistry))
        return {'contractVersion': CONTRACT_VERSION, 'runId': identifier, 'status': 'pending',
                'pollUrl': f'/api/research/compare/runs/{identifier}'}

    def get(self, identifier):
        self.sweep()
        if identifier not in self.runs:
            raise CompareRunError('COMPARE_RUN_NOT_FOUND', '运行不存在或已过期。', 404)
        return copy.deepcopy(self.runs[identifier])

    async def cancel(self, identifier):
        record = self.get(identifier)
        if record['status'] in TERMINAL:
            return record
        task = self.tasks[identifier]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # A pending task can be cancelled before its coroutine entered try.
        self._cancel_record(self.runs[identifier])
        return self.get(identifier)

    def _cancel_record(self, record):
        record['status'], record['updatedAt'] = 'cancelled', now()
        for item in record['models'].values():
            if item['status'] in ('pending', 'running'):
                item['status'] = 'cancelled'
        self.expiry[record['runId']] = time.monotonic() + self.ttl

    async def _execute(self, record, model_id, context, *, reporter=False):
        item, adapter = record['models'][model_id], self.adapters[model_id]
        item['status'], record['updatedAt'] = 'running', now()
        started = time.perf_counter()
        execution_started = False
        try:
            async def operation():
                nonlocal execution_started
                execution_started = True
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
                verified = validate_report(raw, context['snapshot'])
                record['reports'][model_id] = verified
                data = {'report': verified}
            item.update(status='partial' if result.get('status') == 'partial' else 'completed', data=copy.deepcopy(data),
                        provenance=copy.deepcopy(result.get('provenance') or {}), metrics={**copy.deepcopy(result.get('metrics') or {}), **queue})
        except asyncio.CancelledError:
            partial = getattr(adapter, 'last_partial', None)
            if execution_started and model_id == 'toxnet-v3-b16' and isinstance(partial, dict):
                try:
                    prediction = partial.get('prediction', (partial.get('data') or {}).get('prediction'))
                    validate_b16(prediction, record['structure'])
                    item.update(status='partial', data={'prediction': copy.deepcopy(prediction)},
                                provenance=copy.deepcopy(partial.get('provenance') or {}),
                                error={'code': 'OPTIONAL_STAGE_CANCELLED', 'message': '已完成真实预测保留；后续阶段已取消。'})
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
                except ValueError:
                    pass
        except Exception as error:
            code = getattr(error, 'code', 'COMPARE_INVALID_OR_FAILED_OUTPUT')
            message = getattr(error, 'public_message', '模型输出或事实核验失败；该结果不可用。')
            status = 'unsupported' if 'UNSUPPORTED' in code else 'unavailable' if any(word in code for word in ('UNAVAILABLE', 'NOT_READY', 'NOT_CONFIGURED', 'UNCONFIGURED', 'MISSING', 'AUTHORIZATION', 'DISABLED', 'LICENSE_REQUIRED', 'NOT_INSTALLED', 'CONNECTION_ERROR')) else 'failed'
            if isinstance(error, ResourceUnavailable):
                code, message, status = 'COMPARE_RESOURCE_QUARANTINED', '本地模型资源清理未确认，请检查服务后重启。', 'unavailable'
            item.update(status=status, error={'code': code, 'message': message})
        finally:
            if model_id == 'deepseek-api':
                attempted_for = getattr(adapter, 'attempted_for', None)
                record['cloudTransfer']['requestAttempted'] = bool(attempted_for(record['runId']) if attempted_for else item['provenance'].get('requestAttempted', False))
                record['cloudTransfer']['executed'] = item['status'] in ('completed', 'partial')
            item['metrics']['durationMs'] = round((time.perf_counter() - started) * 1000, 3)
            record['updatedAt'] = now()

    async def _run(self, record, chemistry):
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.run_seconds):
                async with self.semaphore:
                    record['status'], record['updatedAt'] = 'running', now()
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
                        include_tox21=record['request']['includeTox21'], question=record['request']['question'])
                    frozen = encoded(snapshot)
                    record['snapshot'] = json.loads(frozen)
                    # Only the immutable common snapshot is provided, never a
                    # preceding generator's report or the free user question.
                    report_request = {key: value for key, value in record['request'].items() if key not in ('smiles', 'question')}
                    report_context = {'runId': record['runId'], 'snapshotSha256': snapshot['snapshotSha256'], 'snapshot': json.loads(frozen), 'messages': report_messages(json.loads(frozen)),
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
        except Exception:
            record['status'] = 'partial' if any(item['status'] in ('completed', 'partial') for item in record['models'].values()) else 'failed'
            record['error'] = {'code': 'COMPARE_RUN_FAILED', 'message': '比较运行失败，已完成结果保留。'}
            for item in record['models'].values():
                if item['status'] in ('pending', 'running'):
                    item['status'] = 'unavailable'
        finally:
            record['durationMs'] = round((time.perf_counter() - started) * 1000, 3)
            record['updatedAt'] = now()
            self.expiry[record['runId']] = time.monotonic() + self.ttl

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
        if len(self.presets) >= 20:
            raise CompareRunError('PRESET_CAPACITY_EXCEEDED', '预设已达数量限制。', 429)
        row = {'id': uuid4().hex, **payload.model_dump()}
        updated = [*self.presets, row]
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
        return copy.deepcopy(row)

    async def close(self):
        active = [task for task in self.tasks.values() if not task.done()]
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
