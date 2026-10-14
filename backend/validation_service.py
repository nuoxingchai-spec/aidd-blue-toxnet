"""CPU-isolated scientific evaluation of operator-registered, sealed datasets.

No model/download/label read at application startup. No browser-supplied paths.
The Compare GPU scheduler and scientific validation workers never share state.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from backend.compare_persistence import _secure_path
from backend.compare_resources import wait_tracked_cleanup

SCHEMA = 'aidd.scientific-validation.v1'
ROOT = Path(__file__).resolve().parents[1]
MODELS = ('A4', 'B16', 'C_Morgan')
ID_PATTERN = r'^[a-z][a-z0-9-]{0,79}$'
LIMITATIONS = [
    '独立科学验收未完成；工程测试不证明毒性预测准确。',
    '旧 test、hERG_Karim 和开发 validation 已曝光，不能冒充新盲测。',
    'Tox21 是独立通路任务，BBB 分类不与四毒性标签直接比较。',
    'ClinTox 开发验证仅4个阳性，负迁移差值不等于已证实的架构失败。',
    '校准标签概率不是人体风险概率；Tanimoto相似度不是置信度。',
    '所有v3保持formalAvailability=false；预训练结构曝光未能核查。',
]


class ValidationError(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(code)
        self.code, self.public_message, self.status_code = code, message, status


class Registration(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: StrictStr = Field(pattern=ID_PATTERN)
    name: StrictStr = Field(min_length=1, max_length=160)
    version: StrictStr = Field(min_length=1, max_length=120)
    metadataFile: StrictStr
    rowsFile: StrictStr
    protocolFile: StrictStr
    metadataSha256: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    rowsSha256: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    protocolSha256: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    sealFile: StrictStr | None = None
    sealSha256: StrictStr | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


def file_sha(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def trusted_file(root, name, *, expected=None, limit=16 * 1024 * 1024):
    """Operator catalog references must be contained regular files, not links."""
    relative = Path(name)
    root = Path(root).resolve()
    target = root / relative
    if relative.is_absolute() or '..' in relative.parts or ':' in name or '\\' in name:
        raise ValidationError('VALIDATION_INVALID_PATH', '登记工件必须使用数据目录内的相对路径。')
    if any(part.is_symlink() for part in (target, *target.parents) if part != root.parent):
        raise ValidationError('VALIDATION_INVALID_PATH', '研究数据路径不允许符号链接。')
    target = target.resolve()
    if not target.is_relative_to(root) or not target.is_file() or target.stat().st_size > limit:
        raise ValidationError('VALIDATION_MISSING_DEPENDENCY', '登记工件缺失、越界或超过审计预算。')
    if expected is not None and file_sha(target) != expected:
        raise ValidationError('VALIDATION_CHECKSUM_MISMATCH', '登记工件SHA256校验失败。')
    return target


def load_json(path, *, limit=16 * 1024 * 1024):
    path = Path(path)
    if path.stat().st_size > limit:
        raise ValueError('JSON exceeds budget')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def atomic_json(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _secure_path(path.parent, directory=True)
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n'
    if exclusive:
        with path.open('x', encoding='utf-8') as target:
            target.write(data)
    else:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as target:
            target.write(data)
            temporary = Path(target.name)
        _secure_path(temporary)
        temporary.replace(path)
    _secure_path(path)


def utc():
    return datetime.now(timezone.utc).isoformat()


def public_aggregate(value):
    """Reject raw records/paths/credentials rather than publishing partial leaks."""
    banned = {'smiles', 'canonicalsmiles', 'labels', 'label', 'rows', 'rawrows', 'rawdata',
              'eligiblerows', 'quarantinedrows', 'recordjson', 'apikey', 'accesstoken',
              'token', 'secret', 'password', 'authorization', 'filepath', 'localpath',
              'modelfile', 'modeldir', 'encoderdir'}
    def walk(item):
        if isinstance(item, dict):
            if any(re.sub(r'[-_]', '', str(k).lower()) in banned for k in item):
                raise ValueError('Raw field in aggregate')
            for child in item.values(): walk(child)
        elif isinstance(item, list):
            for child in item: walk(child)
        elif isinstance(item, str) and (re.search(r'(?<![A-Za-z])[A-Za-z]:[\\/]|\\\\[A-Za-z0-9]|(?<![:/A-Za-z0-9])/[A-Za-z0-9_.-]+/', item)
                                      or re.search(r'(?:sk-[A-Za-z0-9]{24,}|hf_[A-Za-z0-9]{25,})', item)):
            raise ValueError('Private field in aggregate')
    walk(value)
    if len(json.dumps(value, allow_nan=False).encode()) > 8 * 1024 * 1024:
        raise ValueError('Aggregate exceeds response budget')
    return value


class ValidationService:
    def __init__(self, *, root=ROOT, data_root=None, env=None, runner=None):
        self.root, self.env = Path(root).resolve(), dict(os.environ if env is None else env)
        base = Path(self.env.get('LOCALAPPDATA') or (Path.home() / '.local/share'))
        self.data_root = Path(data_root or self.env.get('TOXNET_VALIDATION_ROOT') or
                              base / 'AIDD-Blue-ToxNet/scientific-validation').resolve()
        self.runner = runner  # explicit software-test injection only
        self.tasks, self.records = {}, {}
        self.semaphore = asyncio.Semaphore(1)
        self.closed, self.quarantine = False, False
        self.timeout = 600
        self._storage_checked = False

    def _check_previous_workers(self):
        if self._storage_checked: return
        for path in (self.data_root/'runs').glob('*/record.json'):
            try:
                record=load_json(path)
                if record.get('status') in ('pending','running'):
                    self.quarantine=True
            except Exception:
                self.quarantine=True
        if self.quarantine:
            atomic_json(self.data_root/'quarantine.json',{'active':True,'reason':'PREVIOUS_VALIDATION_WORKER_STATE_UNKNOWN','at':utc()})
        self._storage_checked=True

    def _registrations(self):
        path = self.data_root / 'registry.json'
        if not path.exists(): return {}
        try:
            value = load_json(path, limit=128 * 1024)
            if set(value) != {'schema', 'datasets'} or value['schema'] != 'aidd.validation.registry.v1' or len(value['datasets']) > 64:
                raise ValueError('Invalid catalog')
            rows = [Registration.model_validate(row) for row in value['datasets']]
            if len({r.id for r in rows}) != len(rows): raise ValueError('Duplicate dataset')
            if {r.id for r in rows}&{'historical-reference-inventory','fda-dilirank2','chembl-assay-candidate'}:
                raise ValueError('Reserved dataset IDs')
            return {r.id: r for r in rows}
        except Exception:
            raise ValidationError('VALIDATION_INVALID_REGISTRY', '服务器研究数据登记清单无效。', 503) from None

    def registration(self, identifier):
        result = self._registrations().get(identifier)
        if result is None:
            raise ValidationError('VALIDATION_DATASET_NOT_REGISTERED', '数据尚未由本机操作者登记；不接受浏览器文件路径。')
        for name, checksum in (('metadataFile', 'metadataSha256'), ('rowsFile', 'rowsSha256'), ('protocolFile', 'protocolSha256')):
            trusted_file(self.data_root, getattr(result, name), expected=getattr(result, checksum))
        return result

    def model_specs(self):
        base = self.root / 'artifacts/integration/multitask-cuda-20261013'
        return {model: {'model_dir': str(base / f'{model}-seed-42'),
                        'encoder_dir': str(self.root / 'models/pretrained/molformer') if model != 'C_Morgan' else None}
                for model in MODELS}

    def catalog(self):
        candidates = [
            {'id':'historical-reference-inventory', 'name':'历史训练、开发及已曝光来源禁入审计', 'version':'historical-v1',
             'qualification':'not-independently-qualified', 'rightsStatus':'source-rights-unresolved',
             'auditAvailable':True, 'evaluationAvailable':False, 'endpointContracts':[],
             'missingDependencies':['不是新的独立数据；只读取结构和来源元数据，不读旧test标签。']},
            {'id':'fda-dilirank2', 'name':'FDA DILIrank 2.0 / DILIst 候选', 'version':'audit-only',
             'qualification':'not-independently-qualified', 'rightsStatus':'requires-review',
             'auditAvailable':False, 'evaluationAvailable':False, 'endpointContracts':['dili'],
             'missingDependencies':['缺已审核结构映射、来源独立性和任务/权限合同。']},
            {'id':'chembl-assay-candidate', 'name':'ChEMBL 定义一致的新实验候选', 'version':'not-downloaded',
             'qualification':'not-independently-qualified', 'rightsStatus':'requires-assay-specific-review',
             'auditAvailable':False, 'evaluationAvailable':False, 'endpointContracts':['herg'],
             'missingDependencies':['尚无登记数据、固定版本、实验阈值与来源排除证据。']},
        ]
        try:
            for reg in self._registrations().values():
                protocol=load_json(trusted_file(self.data_root,reg.protocolFile,expected=reg.protocolSha256))
                candidates.append({'id':reg.id,'name':reg.name,'version':reg.version,
                                   'qualification':'pending-current-audit','rightsStatus':'see-registered-audit',
                                   'auditAvailable':True,'evaluationAvailable':bool(reg.sealFile and reg.sealSha256),
                                   'protocolId':protocol.get('protocolId'),
                                   'endpointContracts':[],'missingDependencies':[]})
            registry_error = None
        except (ValidationError,ValueError,KeyError) as error:
            registry_error = {'code':error.code if isinstance(error,ValidationError) else 'VALIDATION_INVALID_REGISTRY',
                              'message':'服务器登记的科研协议无效或SHA不匹配。'}
        models = []
        for name, spec in self.model_specs().items():
            path = Path(spec['model_dir']) / 'manifest.json'
            models.append({'id':name, 'available':path.is_file(), 'status':'artifact-present-not-execution-verified' if path.is_file() else 'missing-artifact',
                           'manifestSha256':file_sha(path) if path.is_file() else None,
                           'reason':'CPU离线复用原加载器；只有合格封存数据和固定协议才能评价。'})
        negative = {'status':'not-evaluated', 'reason':'开发集分析工件尚未准备；不能生成独立评价指标。'}
        source = self.root / 'artifacts/independent-validation/development-negative-transfer-20261015.json'
        if source.is_file():
            try: negative = public_aggregate(load_json(source))
            except Exception: negative = {'status':'invalid-artifact','reason':'开发分析聚合工件无效。'}
        return public_aggregate({'schema':SCHEMA,'formalAvailability':False,'status':'independent-scientific-acceptance-incomplete',
                'datasets':candidates,'models':models,'protocol':{'id':'independent-v1','status':'not-frozen-no-qualified-dataset','sha256':None},
                'negativeTransfer':negative,'licenseReview':[
                    {'component':'project-code','status':'no-root-license-declared'},
                    {'component':'TDC/FDA source labels','status':'underlying-rights-and-lineage-review-required'},
                    {'component':'MoLFormer','status':'Apache-2.0; fixed snapshot; dataset rights separate'},
                    {'component':'TxGemma GGUF','status':'third-party-quantized; upstream Google revision unknown; separate terms'}],
                'registryError':registry_error,'limitations':LIMITATIONS,'devicePolicy':'CPU-only isolated subprocess; no shared Compare GPU state'})

    def _submission_guard(self):
        self._check_previous_workers()
        if self.closed: raise ValidationError('VALIDATION_SHUTTING_DOWN','科研任务服务正在关闭。',503)
        if self.quarantine or (self.data_root / 'quarantine.json').exists():
            raise ValidationError('VALIDATION_CLEANUP_UNCONFIRMED','科研工作进程清理未确认；禁止新任务。',503)
        if sum(not t.done() for t in self.tasks.values()) >= 4:
            raise ValidationError('VALIDATION_QUEUE_FULL','科研CPU任务队列已满。',429)

    def _prepare_inputs(self,kind,request):
        identifier = request['datasetId']
        reg = None
        if identifier != 'historical-reference-inventory' or kind != 'audit':
            reg = self.registration(identifier)
        if kind == 'evaluate':
            if self.env.get('TOXNET_VALIDATION_ALLOW_LABELS','false').lower() != 'true':
                raise ValidationError('VALIDATION_LABEL_ACCESS_DISABLED','独立测试标签访问未由服务器操作者授权。',403)
            if not reg.sealFile or not reg.sealSha256:
                raise ValidationError('VALIDATION_DATASET_NOT_QUALIFIED','没有通过资格审计的封存数据，拒绝评价。')
            from backend.independent_validation_data import load_sealed_dataset
            try:
                path = trusted_file(self.data_root, reg.sealFile, expected=reg.sealSha256)
                bundle = load_sealed_dataset(path,reg.sealSha256,purpose='evaluate',allow_labels=False)
                from scripts.register_independent_dataset import validate_registration_binding
                validate_registration_binding(self.data_root,reg,seal_bundle=bundle)
                manifest = bundle['manifest']
                if manifest.get('role') != 'independent-evaluation' or not manifest.get('qualified'):
                    raise ValueError('Wrong sealed role')
                if request['protocolId'] != bundle['protocol'].get('protocolId'):
                    raise ValueError('Protocol differs')
            except (ValueError,KeyError):
                raise ValidationError('VALIDATION_DATASET_NOT_QUALIFIED','封存资格、数据SHA或固定协议不匹配。') from None
        return reg

    async def submit_async(self,kind,request):
        # Seal verification includes RDKit identity reconstruction. Keep it off
        # the API loop so Compare status/cancellation requests remain responsive.
        self._submission_guard()
        reg=await asyncio.to_thread(self._prepare_inputs,kind,request)
        return self.submit(kind,request,_prepared=(reg,))

    def submit(self,kind,request,*,_prepared=None):
        self._submission_guard()
        reg=self._prepare_inputs(kind,request) if _prepared is None else _prepared[0]
        identifier=request['datasetId']
        run_id = uuid4().hex
        record = {'schema':SCHEMA,'formalAvailability':False,'runId':run_id,'kind':kind,'datasetId':identifier,
                  'status':'pending','createdAt':utc(),'updatedAt':utc(),'device':'cpu','seed':42,
                  'report':None,'error':None,'resultSha256':None,'cleanupConfirmed':None}
        self.records[run_id] = record
        self._save(record)
        payload = {'kind':kind,'request':request,'registration':reg.model_dump() if reg else None,
                   'dataRoot':str(self.data_root),'workspaceRoot':str(self.root),'modelSpecs':self.model_specs()}
        self.tasks[run_id] = asyncio.create_task(self._execute(run_id,payload))
        return {'schema':SCHEMA,'runId':run_id,'status':'pending','pollUrl':f'/api/research/validation/runs/{run_id}'}

    def _save(self, record):
        atomic_json(self.data_root / 'runs' / record['runId'] / 'record.json',record)

    async def _stop(self, process):
        if process is None: return
        if process.returncode is None:
            try: process.terminate()
            except ProcessLookupError: pass
            try: await asyncio.wait_for(process.wait(),5)
            except asyncio.TimeoutError:
                try: process.kill()
                except ProcessLookupError: pass
                await asyncio.wait_for(process.wait(),5)
        await process.wait()

    async def _execute(self, run_id, payload):
        record, process, spawning = self.records[run_id], None, None
        owns_slot, final_status = False, 'failed'
        try:
            await self.semaphore.acquire()
            owns_slot=True
            if self.quarantine: raise ValidationError('VALIDATION_CLEANUP_UNCONFIRMED','工作进程已隔离。')
            record.update(status='running',updatedAt=utc()); self._save(record)
            if self.runner is not None:
                report = await asyncio.wait_for(self.runner(payload), self.timeout)
            else:
                env = {**os.environ,'CUDA_VISIBLE_DEVICES':'','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','OMP_NUM_THREADS':'2','MKL_NUM_THREADS':'2'}
                spawning = asyncio.create_task(asyncio.create_subprocess_exec(sys.executable,'-m','backend.validation_worker',cwd=self.root,
                    stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env))
                process = await asyncio.shield(spawning)
                async def read_bounded(stream, limit):
                    chunks,total=[],0
                    while chunk := await stream.read(65536):
                        total += len(chunk)
                        if total > limit: raise ValueError('Worker output budget')
                        chunks.append(chunk)
                    return b''.join(chunks)
                process.stdin.write(json.dumps(payload,allow_nan=False).encode()); await process.stdin.drain(); process.stdin.close()
                stdout,_ = await asyncio.wait_for(asyncio.gather(read_bounded(process.stdout,8*1024*1024),read_bounded(process.stderr,128*1024)),self.timeout)
                await asyncio.wait_for(process.wait(),10)
                result=json.loads(stdout)
                if process.returncode or result.get('status') != 'ok':
                    raise ValidationError('VALIDATION_WORKER_REJECTED','数据资格、工件或评测合同检查失败；未发布评价。')
                report=result['report']
            report=public_aggregate(report)
            result_path=self.data_root/'runs'/run_id/'result.json'
            atomic_json(result_path,report,exclusive=True)
            record.update(report=report,resultSha256=file_sha(result_path))
            final_status='completed'
        except asyncio.CancelledError:
            final_status='cancelled'
            record.update(error={'code':'VALIDATION_CANCELLED','message':'科研任务已取消，未发布未完成评价。'})
        except asyncio.TimeoutError:
            record.update(error={'code':'VALIDATION_TIMEOUT','message':'科研CPU任务超时，未发布未完成评价。'})
        except Exception as error:
            record.update(error={'code':error.code if isinstance(error,ValidationError) else 'VALIDATION_FAILED',
                                                'message':error.public_message if isinstance(error,ValidationError) else '科研任务失败；未发布模型分数或测试标签。'})
        finally:
            async def cleanup():
                owned=process
                if owned is None and spawning is not None:
                    owned=await asyncio.shield(spawning)
                await self._stop(owned)
            cleanup_task=asyncio.create_task(cleanup())
            try:
                await wait_tracked_cleanup(cleanup_task)
                record['cleanupConfirmed']=True
            except asyncio.CancelledError:
                if cleanup_task.done() and not cleanup_task.cancelled() and cleanup_task.exception() is None:
                    record['cleanupConfirmed']=True
                    final_status='cancelled'
                else:
                    self.quarantine=True
                    record['cleanupConfirmed']=False
                    atomic_json(self.data_root/'quarantine.json',{'active':True,'reason':'OWNED_VALIDATION_PROCESS_CLEANUP_UNCONFIRMED','at':utc()})
                    final_status='failed'
                    record['error']={'code':'VALIDATION_CLEANUP_UNCONFIRMED','message':'科研工作进程清理未确认，已持久隔离后续执行。'}
            except BaseException:
                self.quarantine=True
                record['cleanupConfirmed']=False
                atomic_json(self.data_root/'quarantine.json',{'active':True,'reason':'OWNED_VALIDATION_PROCESS_CLEANUP_UNCONFIRMED','at':utc()})
                final_status='failed'
                record['error']={'code':'VALIDATION_CLEANUP_UNCONFIRMED','message':'科研工作进程清理未确认，已持久隔离后续执行。'}
            record.update(status=final_status,updatedAt=utc())
            try:
                self._save(record)
            finally:
                if owns_slot: self.semaphore.release()

    def get(self, run_id):
        if not re.fullmatch(r'[a-f0-9]{32}',run_id):
            raise ValidationError('VALIDATION_RUN_NOT_FOUND','科研运行不存在。',404)
        path=self.data_root/'runs'/run_id/'record.json'
        if run_id not in self.records and not path.is_file():
            raise ValidationError('VALIDATION_RUN_NOT_FOUND','科研运行不存在。',404)
        try:
            record=public_aggregate(json.loads(json.dumps(self.records[run_id])) if run_id in self.records else
                                   load_json(trusted_file(self.data_root,f'runs/{run_id}/record.json')))
            if run_id not in self.records and record['status'] in ('pending','running'):
                record.update(status='interrupted',error={'code':'VALIDATION_INTERRUPTED','message':'上次服务已停止，不自动读取标签或继续模型。'})
            if record.get('resultSha256'):
                report_path=trusted_file(self.data_root,f'runs/{run_id}/result.json',expected=record['resultSha256'])
                record['report']=public_aggregate(load_json(report_path))
            return record
        except Exception:
            raise ValidationError('VALIDATION_INVALID_RECORD','科研运行工件校验失败。',503) from None

    def list_runs(self):
        paths=sorted((self.data_root/'runs').glob('*/record.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:100]
        rows=[]
        for path in paths:
            if re.fullmatch(r'[a-f0-9]{32}',path.parent.name):
                try:
                    row=self.get(path.parent.name)
                    rows.append({k:row.get(k) for k in ('runId','kind','datasetId','status','createdAt','updatedAt','cleanupConfirmed')})
                except ValidationError: continue
        return {'schema':SCHEMA,'runs':rows}

    async def cancel(self,run_id):
        self.get(run_id)
        task=self.tasks.get(run_id)
        if task is not None and not task.done():
            if task.cancelling()==0: task.cancel()
            try:
                await wait_tracked_cleanup(task)
            except asyncio.CancelledError:
                # A task cancelled before its coroutine starts owns no worker.
                if not (task.cancelled() and self.records[run_id]['status']=='pending'): raise
        # close() cancels every queued task before waiting. A later task may
        # already be done/cancelled here without ever entering _execute.
        if task is not None and task.cancelled() and self.records[run_id]['status']=='pending':
            self.records[run_id].update(status='cancelled',cleanupConfirmed=True,updatedAt=utc(),
                error={'code':'VALIDATION_CANCELLED','message':'排队任务已取消，没有启动模型或读取标签。'})
            self._save(self.records[run_id])
        return self.get(run_id)

    async def close(self):
        self.closed=True
        pending=[run_id for run_id,task in self.tasks.items() if not task.done()]
        # cancel() also seals pending tasks cancelled before _execute starts.
        # Leaving such records pending would falsely quarantine a clean restart.
        for run_id in pending:
            if self.tasks[run_id].cancelling()==0: self.tasks[run_id].cancel()
        for run_id in pending:
            await self.cancel(run_id)
