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
from backend.compare_question import interpret_question

FACT_SCHEMA = 'aidd.compare.facts.v1'
REPORT_SCHEMA = 'aidd.compare.report.v1'
REPORT_SCHEMA_V2 = 'aidd.compare.report.v2'
PROMPT_VERSION = 'aidd.compare.independent-report.v1'
PROMPT_VERSION_V2 = 'aidd.compare.independent-report.v2'
REPORT_POLICIES = {
    'fast': {'commentaryItems': 4, 'commentaryCharacters': 240, 'totalCommentaryCharacters': 600,
             'uncertaintyCharacters': 180, 'refusalCharacters': 180},
    'deep': {'commentaryItems': 8, 'commentaryCharacters': 800, 'totalCommentaryCharacters': 4000,
             'uncertaintyCharacters': 600, 'refusalCharacters': 400},
}
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
    if provenance.get('runtime') == 'ollama-gguf':
        from backend.compare_ollama_txgemma import validate_gguf_provenance
        source_verified = (validate_gguf_provenance(provenance)
                           and provenance.get('taskContract') == expected['taskContractVersion']
                           and provenance.get('templateSha256') == expected['templateSha256'])
    elif provenance.get('runtime') in (None, 'local-transformers'):
        source_verified = provenance.get('revision') == TXGEMMA_REVISION
    else:
        source_verified = False
    if encoded(tasks[0]) != encoded(expected) or not source_verified or provenance.get('formalAvailability') is not False:
        raise ValueError('Specialist class, task semantics or revision were altered')
    return data


def build_snapshot(structure, chemistry, model_results, *, include_tox21=True, question='', specialist_task=None):
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
            k: copy.deepcopy((tx.get('provenance') or {}).get(k)) for k in ('modelId', 'revision', 'snapshotAuditSha256', 'taskContract', 'templateSha256',
                'runtime', 'sourceKind', 'sourceRepository', 'sourceRevision', 'ggufSha256', 'quantization',
                'upstreamRevisionStatus', 'localPackageVerification', 'licenseStatus')}})
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
    interpretation = interpret_question(question, specialist_task=specialist_task, include_tox21=include_tox21)
    snapshot = {'schema': FACT_SCHEMA, 'structure': {k: structure[k] for k in ('canonicalSmiles', 'structureId')},
                'chemistryVersion': CHEMISTRY_VERSION, 'questionScope': interpretation['scope'],
                'questionInterpretation': interpretation, 'facts': facts, 'sources': sources,
                'sections': [{'id': key, 'title': title, 'claimIds': [f['id'] for f in facts if f['sectionId'] == key]}
                             for key, title in SECTIONS.items()], 'limitations': copy.deepcopy(LIMITATIONS),
                'sentInformationCategories': list(CLOUD_CATEGORIES), 'experimentalRowsIncluded': False,
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


class CompactCommentary(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    sectionId: Literal['prediction', 'specialist', 'evidence', 'limits']
    text: StrictStr = Field(min_length=1, max_length=800)


class GeneratedReportV2(BaseModel):
    """Only ordering and unverified prose may come from the generator.

    Prediction values, chemical identity, source IDs and model versions have no
    writable fields. They are reconstructed from the trusted frozen snapshot.
    """
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal['aidd.compare.report.v2'] = Field(alias='schema')
    snapshotSha256: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')
    sectionOrder: list[StrictStr] = Field(min_length=4, max_length=4)
    claimOrder: dict[StrictStr, list[StrictStr]]
    commentary: list[CompactCommentary] = Field(default_factory=list, max_length=8)
    uncertainty: StrictStr | None = Field(default=None, min_length=1, max_length=600)
    refusal: StrictStr | None = Field(default=None, min_length=1, max_length=400)


def report_policy(report_mode):
    if report_mode not in REPORT_POLICIES:
        raise ValueError('Unsupported report mode; expected fast or deep')
    return copy.deepcopy(REPORT_POLICIES[report_mode])


def _contract_schema(report_contract):
    if report_contract == 'v1':
        return REPORT_SCHEMA
    if report_contract == 'v2':
        return REPORT_SCHEMA_V2
    raise ValueError('Unsupported report contract; expected v1 or v2')


def _mode_instructions(report_mode, *, compact=True):
    limits = report_policy(report_mode)
    purpose = ('快速报告：简述四端点、通路提示、专项类别、现有证据状态和主要局限；避免逐项重复事实。'
               if report_mode == 'fast' else
               '深度报告：解释端点差异、模型来源、校准与适用域、规则/归因、证据和专项可比性；'
               '研究假设和后续实验建议仅为未验证推断，不得声称机制成立。')
    result = (purpose + f"commentary最多{limits['commentaryItems']}段，每段不超过{limits['commentaryCharacters']}字，"
              f"合计不超过{limits['totalCommentaryCharacters']}字；可为空。")
    if compact:
        result += f"uncertainty最多{limits['uncertaintyCharacters']}字，refusal最多{limits['refusalCharacters']}字。"
    return result


def report_messages(snapshot, *, report_contract='v1', report_mode='deep'):
    schema_id = _contract_schema(report_contract)
    report_policy(report_mode)
    if report_contract == 'v2':
        # Omit duplicate human-readable fact text and the verbose JSON schema.
        # The original full snapshot hash still binds all facts, identity,
        # provenance, versions and limitations. Nothing is omitted at render.
        compact = {k: copy.deepcopy(snapshot[k]) for k in
                   ('snapshotSha256', 'structure', 'chemistryVersion', 'questionScope', 'sources', 'sections', 'limitations')}
        compact['facts'] = [{k: copy.deepcopy(f[k]) for k in ('id', 'sourceKind', 'value', 'sourceIds')}
                            for f in snapshot['facts']]
        if 'questionInterpretation' in snapshot:
            compact['questionInterpretation'] = copy.deepcopy(snapshot['questionInterpretation'])
        system = ('你是中文研究报告整理器。仅整理给定事实，全部事实由后端重建。只返回严格JSON，不要Markdown或工具调用。'
                  f'schema必须为{schema_id}；原样复制snapshotSha256。'
                  'sectionOrder包含每个章节ID各一次；claimOrder按各章claimIds列出全部事实各一次，不能跨章。'
                  '只允许字段schema,snapshotSha256,sectionOrder,claimOrder,commentary,uncertainty,refusal。'
                  'commentary为[{"sectionId":"limits","text":"尚无充分证据"}]形式；uncertainty和refusal为字符串或null。'
                  '严禁输出factChecks、score、sourceIds、模型版本、结构或任何新字段；不得重算分数或类别。'
                  '解释均为unverified，不能新增实验值、文献、临床风险或确认的毒性机制。'
                  '缺少证据时明确“不确定”或“尚无充分证据”；保留全部事实ID和科研限制。'
                  + _mode_instructions(report_mode))
        return [{'role': 'system', 'content': system}, {'role': 'user', 'content': encoded({
            'promptVersion': PROMPT_VERSION_V2, 'reportMode': report_mode, 'snapshot': compact})}]
    schema = GeneratedReport.model_json_schema()
    system = ('你是中文研究报告整理器。仅使用提供的已核验事实；不得改写分数、来源、类别或发明实验与文献。'
              '返回一个严格 JSON 对象，schema=aidd.compare.report.v1。复制 snapshotSha256。'
              'sectionOrder包含所有章节各一次；claimOrder保持每个事实属于原章节且各一次。'
              'factChecks逐条精确复制每个事实的id到claimId、value和sourceIds。commentary可为空；新增文字只会标为unverified，不能代替事实。'
              '科学机制不确定；不要输出临床风险概率、工具调用或Markdown。')
    if report_mode == 'fast':
        system += _mode_instructions(report_mode, compact=False)
    return [{'role': 'system', 'content': system}, {'role': 'user', 'content': encoded({
        'promptVersion': PROMPT_VERSION, 'snapshot': snapshot, 'responseSchema': schema})}]


def _parse_report(raw):
    if isinstance(raw, str):
        if len(raw.encode()) > 2 * 1024 * 1024:
            raise ValueError('Report too large')
        def unique_keys(pairs):
            output = {}
            for key, value in pairs:
                if key in output:
                    raise ValueError('Duplicate report JSON key')
                output[key] = value
            return output
        raw = json.loads(raw, object_pairs_hook=unique_keys,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    if not isinstance(raw, dict):
        raise ValueError('Report must be a JSON object')
    return raw


def validate_report(raw, snapshot, *, report_contract=None, report_mode='deep'):
    """Dispatch by the recorded schema, or require the requested contract.

    The original two-argument call remains valid for historical v1 records.
    Callers processing new runs must pass the selected mode and contract.
    """
    raw = _parse_report(raw)
    report_policy(report_mode)
    schema_id = raw.get('schema')
    if report_contract is not None and schema_id != _contract_schema(report_contract):
        raise ValueError('Generated report differs from the requested contract')
    if schema_id == REPORT_SCHEMA_V2:
        return validate_report_v2(raw, snapshot, report_mode=report_mode)
    if schema_id != REPORT_SCHEMA:
        raise ValueError('Unsupported report schema')
    result = _validate_report_v1(raw, snapshot)
    if report_mode == 'fast':
        _validate_prose(GeneratedReport.model_validate(raw), report_mode)
        result['reportMode'] = report_mode
    return result


def _validate_report_v1(raw, snapshot):
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


def _validate_prose(report, report_mode):
    policy = report_policy(report_mode)
    if len(report.commentary) > policy['commentaryItems']:
        raise ValueError('Report exceeds the selected commentary item budget')
    if sum(len(c.text) for c in report.commentary) > policy['totalCommentaryCharacters']:
        raise ValueError('Report exceeds the selected commentary total budget')
    for item in report.commentary:
        if len(item.text) > policy['commentaryCharacters']:
            raise ValueError('Report exceeds the selected commentary paragraph budget')
    texts = [c.text for c in report.commentary]
    for name in ('uncertainty', 'refusal'):
        value = getattr(report, name, None)
        if value is not None:
            if len(value) > policy[name + 'Characters']:
                raise ValueError('Report exceeds the selected ' + name + ' budget')
            texts.append(value)
    if any(not text.strip() or any(ord(c) < 32 and c not in '\n\t' for c in text) for text in texts):
        raise ValueError('Report prose contains blank or invalid control text')


def _verify_snapshot_v2(snapshot, reported_hash):
    """Check the frozen envelope and internal identity/section/source links.

    This is a consistency check on the server's trusted snapshot, not a claim
    that a self-reported hash authenticates arbitrary user-supplied evidence.
    The snapshot is never accepted as an API request field.
    """
    import re
    from backend.chemistry import canonicalize_smiles

    original = copy.deepcopy(snapshot)
    checksum = original.pop('snapshotSha256', None)
    if (not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-f]{64}', checksum)
            or digest(original) != checksum or reported_hash != checksum):
        raise ValueError('Report has wrong or mutated fact snapshot')
    if snapshot.get('schema') != FACT_SCHEMA or not isinstance(snapshot.get('chemistryVersion'), str):
        raise ValueError('Unknown fact snapshot or missing chemistry version')
    structure = snapshot.get('structure')
    if not isinstance(structure, dict) or set(structure) != {'canonicalSmiles', 'structureId'}:
        raise ValueError('Snapshot chemical identity is incomplete')
    canonical = canonicalize_smiles(structure['canonicalSmiles'])
    if (canonical != structure['canonicalSmiles']
            or hashlib.sha256(canonical.encode()).hexdigest() != structure['structureId']):
        raise ValueError('Snapshot chemical identity is inconsistent')
    source_rows = snapshot.get('sources')
    fact_rows, section_rows = snapshot.get('facts'), snapshot.get('sections')
    if not all(isinstance(rows, list) for rows in (source_rows, fact_rows, section_rows)):
        raise ValueError('Snapshot facts, sources and sections must be lists')
    if not all(isinstance(row, dict) and isinstance(row.get('id'), str) and row['id'] for row in source_rows + fact_rows + section_rows):
        raise ValueError('Snapshot contains invalid identifiers')
    sources = {s['id']: s for s in source_rows}
    facts = {f['id']: f for f in fact_rows}
    sections = {s['id']: s for s in section_rows}
    if (len(sources) != len(source_rows) or len(facts) != len(fact_rows)
            or len(sections) != len(section_rows) or set(sections) != set(SECTIONS)):
        raise ValueError('Snapshot has duplicate or unknown identities or sections')
    for source in sources.values():
        if source['id'] == 'b16':
            if (not isinstance(source.get('weightsSha256'), str)
                    or not re.fullmatch(r'[0-9a-f]{64}', source['weightsSha256'])
                    or not source.get('modelVersion') or not source.get('taskContractVersion')):
                raise ValueError('Snapshot B16 weights, model or task version is missing')
        if source['id'] == 'rules' and not source.get('rulesVersion'):
            raise ValueError('Snapshot rule version is missing')
    for fact in facts.values():
        refs = fact.get('sourceIds')
        if (fact.get('sectionId') not in sections or not isinstance(fact.get('text'), str)
                or 'value' not in fact or not isinstance(refs, list)
                or not all(isinstance(ref, str) and ref in sources for ref in refs)
                or len(set(refs)) != len(refs)):
            raise ValueError('Snapshot fact has invalid source or section membership')
    for identifier, section in sections.items():
        expected = {f['id'] for f in facts.values() if f['sectionId'] == identifier}
        ids = section.get('claimIds')
        if (not isinstance(ids, list) or not all(isinstance(key, str) for key in ids)
                or len(ids) != len(expected) or set(ids) != expected):
            raise ValueError('Snapshot section does not contain its exact facts')
    if snapshot.get('limitations') != LIMITATIONS:
        raise ValueError('Snapshot omits or alters required scientific limitations')
    return checksum, facts, sections


def validate_report_v2(raw, snapshot, *, report_mode='deep'):
    """Reconstruct scientific fields exclusively from the verified snapshot."""
    report = GeneratedReportV2.model_validate(_parse_report(raw))
    _validate_prose(report, report_mode)
    checksum, facts, sections = _verify_snapshot_v2(snapshot, report.snapshotSha256)
    if (len(report.sectionOrder) != len(sections) or set(report.sectionOrder) != set(sections)
            or set(report.claimOrder) != set(sections)):
        raise ValueError('Report omits, duplicates or invents sections')
    additions = [{'sectionId': item.sectionId, 'text': item.text, 'verification': 'unverified', 'type': 'commentary'}
                 for item in report.commentary]
    for name in ('uncertainty', 'refusal'):
        text = getattr(report, name)
        if text is not None:
            additions.append({'sectionId': 'limits', 'text': text, 'verification': 'unverified', 'type': name})
    rendered = []
    for identifier in report.sectionOrder:
        ids = report.claimOrder[identifier]
        if len(ids) != len(sections[identifier]['claimIds']) or set(ids) != set(sections[identifier]['claimIds']):
            raise ValueError('Report moved, omitted or invented a conclusion')
        rendered.append({'id': identifier, 'title': sections[identifier]['title'], 'claimIds': list(ids),
                         'text': '\n'.join(facts[key]['text'] for key in ids),
                         'commentary': [item['text'] for item in additions if item['sectionId'] == identifier],
                         'commentaryVerification': 'unverified'})
    return {'status': 'verified-structured-report', 'reportSchema': REPORT_SCHEMA_V2,
            'reportMode': report_mode, 'snapshotSha256': checksum, 'sections': rendered,
            'verifiedFactIds': list(facts), 'verifiedFacts': copy.deepcopy(snapshot['facts']),
            'verifiedSources': copy.deepcopy(snapshot['sources']), 'structure': copy.deepcopy(snapshot['structure']),
            'chemistryVersion': snapshot['chemistryVersion'], 'unverifiedAdditions': additions,
            'uncertainty': report.uncertainty, 'refusal': report.refusal,
            'sourceVerification': {'existingFields': 'exact-match', 'scoresAndSourcesChanged': False,
                                   'newScientificFactsVerified': False, 'method': 'server-snapshot-reconstruction',
                                   'identityAndVersionsBoundBySnapshotSha256': True},
            'factReconstruction': {'status': 'complete', 'factCount': len(facts),
                                   'sourceCount': len(snapshot['sources']), 'generatedScientificFieldsAccepted': False},
            'limitations': copy.deepcopy(snapshot['limitations'])}
