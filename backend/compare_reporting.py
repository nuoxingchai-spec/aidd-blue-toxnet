"""Immutable common facts, exact report checks and explicitly unverified prose.

Report generators never supply prediction scores. Both receive independently
decoded copies of exactly the same minimal snapshot. Experimental rows, local
paths, provider keys and the free-form user question are excluded.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from backend.chemistry import CHEMISTRY_VERSION, RULES_VERSION
from backend.model_client import ENDPOINT_IDS
from backend.multitask_model import ALL_TASKS

FACT_SCHEMA = 'aidd.compare.facts.v1'
REPORT_SCHEMA = 'aidd.compare.report.v1'
PROMPT_VERSION = 'aidd.compare.independent-report.v1'
SECTIONS = {'prediction': '模型计算结果', 'specialist': '专项任务与可比性',
            'evidence': '规则、归因与实验证据摘要', 'limits': '科学限制与下一步'}
LIMITATIONS = [
    '研究原型，不构成临床诊断、监管建议或人体风险概率。',
    'B16 未独立校准，适用域未验证；旧 test 和 hERG_Karim 已曝光，不用于本轮调参。',
    'ClinTox 少样本且 B16 可能负迁移；没有新独立标签时不发布准确率排名。',
    'Tox21 通路活性不等于四毒性标签；不同模型不投票、不求平均。',
    '结构相似度不等于置信度，归因不等于因果机制，机制不确定、尚无充分证据。',
    '语言模型整理不等于科学验证；新增自由文字未经科学核验。',
]
CLOUD_CATEGORIES = ['canonical-query-structure', 'verified-model-scores', 'task-semantics',
                    'rules-and-attribution-summary', 'aggregate-evidence-status', 'research-limitations']


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def validate_b16(prediction, structure):
    if not isinstance(prediction, dict) or prediction.get('structureId') != structure['structureId'] or prediction.get('canonicalSmiles') != structure['canonicalSmiles']:
        raise ValueError('B16 returned a different chemical identity')
    rows = prediction.get('endpoints')
    if not isinstance(rows, list) or len(rows) != 16 or {r.get('id') for r in rows if isinstance(r, dict)} != set(ALL_TASKS):
        raise ValueError('B16 must return the exact sixteen independently trained tasks')
    for row in rows:
        if type(row.get('score')) not in (float, int) or not math.isfinite(row['score']) or not 0 <= row['score'] <= 1:
            raise ValueError('B16 score is not a finite bounded model output')
    provenance = prediction.get('provenance', {})
    if provenance.get('formalAvailability') is not False or not isinstance(provenance.get('weightsSha256'), str) or len(provenance['weightsSha256']) != 64:
        raise ValueError('B16 completion/research provenance missing')
    return prediction


def validate_specialist(data, structure, task_id, provenance):
    from backend.compare_tasks import parse_task_output
    from backend.compare_txgemma import TXGEMMA_REVISION
    if not isinstance(data, dict) or data.get('structureId') != structure['structureId']:
        raise ValueError('Specialist returned a different structure')
    tasks = data.get('tasks')
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
        raise ValueError('Specialist must return exactly the selected reviewed task')
    expected = parse_task_output(task_id, tasks[0].get('class', ''))
    if encoded(tasks[0]) != encoded(expected) or provenance.get('revision') != TXGEMMA_REVISION or provenance.get('formalAvailability') is not False:
        raise ValueError('Specialist class, task semantics or revision were altered')
    return data


def build_snapshot(structure, chemistry, model_results, *, include_tox21=True, question=''):
    facts, sources = [], []
    def add(identifier, section, kind, text, value, source_ids):
        facts.append({'id': identifier, 'sectionId': section, 'sourceKind': kind, 'text': text,
                      'value': copy.deepcopy(value), 'sourceIds': list(source_ids)})
    b16 = model_results['toxnet-v3-b16']
    prediction = (b16.get('data') or {}).get('prediction')
    if prediction is not None:
        validate_b16(prediction, structure)
        p = prediction['provenance']
        sources.append({'id': 'b16', 'kind': 'model-computation', 'modelVersion': p.get('modelVersion'),
                        'weightsSha256': p['weightsSha256'], 'taskContractVersion': p.get('taskContractVersion')})
        for row in prediction['endpoints']:
            if row['id'] not in ENDPOINT_IDS and not include_tox21:
                continue
            value = {'endpoint': row['id'], 'score': row['score'], 'scoreKind': 'uncalibrated_score',
                     'taskType': 'toxicity-source-label' if row['id'] in ENDPOINT_IDS else 'tox21-assay-activity'}
            add('model:' + row['id'], 'prediction', 'model-computation',
                f"{row['id']} 实际 B16 输出 {row['score']:.6f}；未校准来源标签分数，不是人体毒性概率。", value, ['b16'])
        add('b16:calibration', 'prediction', 'model-metadata', 'B16 未拟合独立校准；适用域未验证。',
            {'calibration': 'not-fitted', 'applicability': 'not-validated'}, ['b16'])
    else:
        add('b16:missing', 'prediction', 'insufficient-evidence', 'B16 真实预测不可用，没有替代分数。',
            {'status': b16['status']}, [])
    tx = model_results['txgemma-2b-predict']
    txdata = tx.get('data')
    # Only source-contracted class outputs are facts; they never become a score.
    if txdata and tx['status'] in ('completed', 'partial'):
        sources.append({'id': 'txgemma', 'kind': 'therapeutic-task-model', **{
            k: copy.deepcopy((tx.get('provenance') or {}).get(k)) for k in ('modelId', 'revision', 'snapshotAuditSha256', 'taskContract', 'templateSha256')}})
        safe = {k: copy.deepcopy(txdata['tasks'][0][k]) for k in ('taskId', 'tdcTask', 'class', 'classLabel', 'positive', 'probability', 'outputType', 'directComparable', 'taskContractVersion', 'templateSha256')}
        add('txgemma:class', 'specialist', 'model-computation',
            'TxGemma 专项任务按官方模板解析类别；类别不是概率，不默认与四毒性端点可比。', safe, ['txgemma'])
    else:
        add('txgemma:missing', 'specialist', 'insufficient-evidence',
            'TxGemma 未运行或任务未审核；不构造专项预测。', {'status': tx['status']}, [])
    sources.append({'id': 'rules', 'kind': 'human-rule', 'rulesVersion': RULES_VERSION})
    alerts = chemistry.get('alerts', [])
    add('rules:count', 'evidence', 'human-rule', f'当前 SMARTS 规则命中 {len(alerts)} 项，仅为人工警示，非神经归因或实验机制。',
        {'matchedAlertIds': [item['id'] for item in alerts], 'count': len(alerts)}, ['rules'])
    data = b16.get('data') or {}
    attribution = data.get('attribution') or {'status': 'not-requested'}
    add('attribution:status', 'evidence', 'neural-attribution' if attribution.get('status') in ('available', 'computed-with-warning') else 'insufficient-evidence',
        f"真实模型归因状态：{attribution.get('status', 'unavailable')}；不是因果机制。",
        {'status': attribution.get('status', 'unavailable'), 'method': attribution.get('method')}, ['b16'] if prediction else [])
    evidence = data.get('evidence') or {'status': 'not-requested'}
    # No experimental molecule, individual label, citation, row ID or path is
    # included in the report snapshot, even for a local language generator.
    add('evidence:summary', 'evidence', 'evidence-audit-summary',
        f"本机实验检索状态：{evidence.get('status', 'insufficient-evidence')}；相似度不是置信度。",
        {'status': evidence.get('status', 'insufficient-evidence'), 'auditedRecordCount': len(evidence.get('results', []))}, [])
    for index, text in enumerate(LIMITATIONS):
        add(f'limit:{index}', 'limits', 'limitation', text, {'limitation': text}, [])
    # Raw user prose is never forwarded. Only a locally derived topic label is
    # shared, identically, with both independent report generators.
    topic = next((task for task in ENDPOINT_IDS if task.lower() in question.lower()), 'whole-molecule')
    snapshot = {'schema': FACT_SCHEMA, 'structure': {k: structure[k] for k in ('canonicalSmiles', 'structureId')},
                'chemistryVersion': CHEMISTRY_VERSION, 'questionScope': topic, 'facts': facts, 'sources': sources,
                'sections': [{'id': key, 'title': title, 'claimIds': [f['id'] for f in facts if f['sectionId'] == key]}
                             for key, title in SECTIONS.items()], 'limitations': LIMITATIONS,
                'sentInformationCategories': CLOUD_CATEGORIES, 'experimentalRowsIncluded': False,
                'privatePathsIncluded': False, 'freeQuestionIncluded': False}
    snapshot['snapshotSha256'] = digest(snapshot)
    return snapshot


class FactCheck(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    claimId: StrictStr
    value: dict
    sourceIds: list[StrictStr]


class Commentary(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    sectionId: Literal['prediction', 'specialist', 'evidence', 'limits']
    text: StrictStr = Field(min_length=1, max_length=2000)


class GeneratedReport(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal['aidd.compare.report.v1'] = Field(alias='schema')
    snapshotSha256: StrictStr
    sectionOrder: list[StrictStr]
    claimOrder: dict[StrictStr, list[StrictStr]]
    factChecks: list[FactCheck]
    commentary: list[Commentary] = Field(default_factory=list, max_length=8)


def report_messages(snapshot):
    schema = GeneratedReport.model_json_schema()
    system = ('你是中文研究报告整理器。仅使用提供的已核验事实；不得改写分数、来源、类别或发明实验与文献。'
              '返回一个严格 JSON 对象，schema=aidd.compare.report.v1。复制 snapshotSha256。'
              'sectionOrder包含所有章节各一次；claimOrder保持每个事实属于原章节且各一次。'
              'factChecks逐条精确复制每个事实的id到claimId、value和sourceIds。commentary可为空；新增文字只会标为unverified，不能代替事实。'
              '科学机制不确定；不要输出临床风险概率、工具调用或Markdown。')
    return [{'role': 'system', 'content': system}, {'role': 'user', 'content': encoded({
        'promptVersion': PROMPT_VERSION, 'snapshot': snapshot, 'responseSchema': schema})}]


def validate_report(raw, snapshot):
    if isinstance(raw, str):
        if len(raw.encode()) > 2 * 1024 * 1024:
            raise ValueError('Report too large')
        raw = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    report = GeneratedReport.model_validate(raw)
    original = copy.deepcopy(snapshot)
    checksum = original.pop('snapshotSha256')
    if digest(original) != checksum or report.snapshotSha256 != checksum:
        raise ValueError('Report has wrong or mutated fact snapshot')
    sections = {s['id']: s for s in snapshot['sections']}
    facts = {f['id']: f for f in snapshot['facts']}
    if len(report.sectionOrder) != len(sections) or set(report.sectionOrder) != set(sections) or set(report.claimOrder) != set(sections):
        raise ValueError('Report omits, duplicates or invents sections')
    checks = {c.claimId: c for c in report.factChecks}
    if len(checks) != len(report.factChecks) or set(checks) != set(facts):
        raise ValueError('Report omits, duplicates or invents facts')
    for identifier, fact in facts.items():
        checked = checks[identifier]
        # Canonical JSON comparison also rejects bool-as-number substitution.
        if encoded(checked.value) != encoded(fact['value']) or checked.sourceIds != fact['sourceIds']:
            raise ValueError('Report changed score, data or source identifiers')
    rendered = []
    for identifier in report.sectionOrder:
        ids = report.claimOrder[identifier]
        if len(ids) != len(sections[identifier]['claimIds']) or set(ids) != set(sections[identifier]['claimIds']):
            raise ValueError('Report moved, omitted or invented a conclusion')
        rendered.append({'id': identifier, 'title': sections[identifier]['title'], 'claimIds': ids,
                         'text': '\n'.join(facts[key]['text'] for key in ids),
                         'commentary': [c.text for c in report.commentary if c.sectionId == identifier],
                         'commentaryVerification': 'unverified'})
    return {'status': 'verified-structured-report', 'reportSchema': REPORT_SCHEMA, 'snapshotSha256': checksum,
            'sections': rendered, 'verifiedFactIds': list(facts),
            'unverifiedAdditions': [{'sectionId': c.sectionId, 'text': c.text, 'verification': 'unverified'} for c in report.commentary],
            'sourceVerification': {'existingFields': 'exact-match', 'scoresAndSourcesChanged': False,
                                   'newScientificFactsVerified': False}, 'limitations': LIMITATIONS}
