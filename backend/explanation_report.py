"""Source-bounded Chinese research reports with an optional closed-claim planner.

External language models may order verified claims, not author new scientific
facts, scores, experiments or citations. Experimental rows never leave localhost.
"""
from __future__ import annotations

import copy
import hashlib
import json
import socket
import urllib.error
import urllib.request

from backend.model_client import ModelConfig

REPORT_VERSION = 'aidd.source-bounded-research-report.v1'
PLAN_SCHEMA = 'aidd.llm.report-plan.v1'
PROMPT_VERSION = 'aidd.closed-claim-organization.v1'
TITLES = {'prediction': '模型计算结果', 'calibration': '校准状态', 'applicability': '适用域与结构相似度',
          'attribution': '原子与片段归因', 'rules': '人工结构规则', 'experiments': '真实实验观察',
          'mechanism': '机制证据与不确定性', 'limits': '研究原型的使用边界'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def build_report(prediction, attribution, evidence, alerts, *, endpoint):
    claims, sources = [], []
    model = prediction['provenance']
    sources.append({'id': 'model', 'kind': 'model-computation', 'modelVersion': model['modelVersion'],
                    'weightsSha256': model['weightsSha256'], 'artifactSha256': model.get('artifactSha256', model.get('manifestSha256'))})

    def add(claim_id, section, text, kind, source_ids, confidence, limitations):
        claims.append({'id': claim_id, 'sectionId': section, 'text': text, 'sourceKind': kind,
                       'sourceIds': source_ids, 'confidence': confidence, 'limitations': limitations})

    for item in prediction['endpoints']:
        add('model:' + item['id'], 'prediction', f"{item['id']} 模型输出为 {item['score']:.6f}；这是实际加载模型的来源标签预测，不是人体风险概率。",
            'model-computation', ['model'], '计算值可复现；临床可靠性未验证', ['Model predictions are not experimental observations.'])
    sources.append({'id': 'calibration', 'kind': 'model-metadata', 'data': copy.deepcopy(prediction['calibration'])})
    add('calibration:status', 'calibration', f"校准状态：{prediction['calibration'].get('status', 'unknown')}。内部校准或内部误差测量不等于独立校准认证。",
        'model-metadata', ['calibration'], '仅按工件记录；无独立认证', ['No new calibration was fitted for this report.'])
    sources.append({'id': 'domain', 'kind': 'model-metadata', 'data': copy.deepcopy(prediction['applicability'])})
    add('domain:status', 'applicability', f"适用域状态：{prediction['applicability'].get('status', 'unknown')}。最大 Tanimoto 是结构相似度，不能解释为预测置信度。",
        'model-metadata', ['domain'], '未验证', ['Similarity does not establish exposure, dose, mechanism or clinical applicability.'])
    if model.get('modelVersion', '').startswith('aidd-toxnet-v2-'):
        add('domain:fingerprint-contract', 'applicability', '历史 v2 适用域指纹不包含手性；本轮实验检索使用包含手性的指纹。两者的相似度统计与阈值应分别解读。',
            'model-metadata', ['domain'], '方法差异已明确；未验证', ['The historical applicability calculation is preserved; retrieval does not recalibrate it.'])
    sources.append({'id': 'attribution', 'kind': 'neural-attribution', 'status': attribution['status'],
                    'method': attribution.get('method'), 'provenance': copy.deepcopy(attribution.get('provenance', {}))})
    if attribution['status'] in ('available', 'computed-with-warning'):
        add('attribution:method', 'attribution', f"{endpoint} 的归因采用 {attribution['method']}；状态为 {attribution['status']}。正负贡献仅解释该模型相对指定基线或扰动的变化，不等于毒性因果机制。",
            'neural-attribution', ['model', 'attribution'], '模型相关性解释；检查数值诊断', attribution.get('limitations', []))
        groups = attribution.get('fragments', [])[:3]
        for i, group in enumerate(groups):
            if 'contribution' in group:
                text = f"{group.get('label', '片段')} 的神经归因汇总为 {group['contribution']:+.6f}（logit 单位）。SMARTS 仅用于分组命名，重叠分组不能相加。"
            else:
                text = f"删除原子 {group.get('removedAtomIndices', [])} 的合法变体重算响应差为 {group.get('delta', 0):+.6f}；这不是加性原子贡献。"
            add(f'attribution:group:{i}', 'attribution', text, 'neural-attribution', ['attribution'], '计算响应；非因果证据', ['Baseline/perturbation may be outside training distribution.'])
    else:
        add('attribution:missing', 'attribution', '真实模型归因 unavailable；不使用规则、注意力或演示值代替神经归因。',
            'insufficient-evidence', ['attribution'], '证据不足', attribution.get('limitations', []))
    sources.append({'id': 'rules', 'kind': 'human-rule', 'rulesVersion': alerts['rulesVersion'], 'method': 'SMARTS structural matching'})
    if alerts['alerts']:
        for item in alerts['alerts']:
            add('rule:' + item['id'], 'rules', f"SMARTS 规则检出“{item['label']}”：{item['detail']}。这只是人工规则警示，不是神经归因或本分子的实验机制验证。",
                'human-rule', ['rules'], '启发式结构警示', ['A structural motif does not prove toxicity or establish a mechanism.'])
    else:
        add('rules:none', 'rules', '当前 SMARTS 规则未命中。未命中警示不能证明无毒或安全。', 'human-rule', ['rules'], '启发式覆盖有限', ['Rules are incomplete.'])
    for i, row in enumerate(evidence.get('results', [])):
        source_id = f'experiment:{i}'
        sources.append({'id': source_id, 'kind': 'experimental-observation',
                        **{k: copy.deepcopy(row.get(k)) for k in ('database', 'sourceSha256', 'datasetVersion', 'endpoint', 'positiveDefinition', 'source_id', 'source_row', 'sourceAssay', 'reference', 'documentationUrl', 'dataRole', 'definitionConsistency')}})
        add(source_id, 'experiments', f"{row['database']} 的 {row['endpoint']} 记录标签为 {row['label']}，Tanimoto={row['similarity']:.4f}，精确结构匹配={row['exactMatch']}。正类定义：{row['positiveDefinition']}。这是该实验来源的观察，不是模型对当前分子的置信度。",
            'experimental-observation', [source_id], '来源标签已核验；异质实验条件仍有限制',
            [row['definitionConsistency']['note'], 'A similar compound observation is not an observation on the query unless identity matches.'])
    if not evidence.get('results'):
        add('experiments:missing', 'experiments', '没有可通过审计的相似实验记录：证据不足，不编造相似样本、实验数值或参考文献。',
            'insufficient-evidence', [], '证据不足', evidence.get('limitations', []))
    # Assay labels and structural alerts cannot validate a molecular mechanism.
    add('mechanism:uncertain', 'mechanism', '本报告收集的是模型响应、结构警示及来源标签；尚无充分证据确认该分子的具体毒性机制。结构归因、通路活性和自然语言整理不等于科学机制验证，机制结论不确定。',
        'mechanism-hypothesis', [], '不确定；尚无充分证据', ['No mechanistic experiment or verified molecule-specific mechanistic literature was supplied.'])
    add('limits:research', 'limits', '蓝研 ToxNet 属于研究原型。本报告不构成临床诊断、监管建议或人体毒性发生概率；不根据解释报告更新权重、校准或历史评估。',
        'limitation', ['model'], '研究用途', ['Independent experiments, calibration and applicability validation are required.'])
    sections = []
    for section_id, title in TITLES.items():
        members = [claim for claim in claims if claim['sectionId'] == section_id]
        kinds = sorted({claim['sourceKind'] for claim in members})
        sections.append({'id': section_id, 'title': title, 'text': '\n'.join(c['text'] for c in members),
                         'claimIds': [c['id'] for c in members], 'sourceKind': kinds[0] if len(kinds) == 1 else 'mixed-explicit-sources',
                         'sourceIds': sorted({s for c in members for s in c['sourceIds']}),
                         'confidence': '；'.join(dict.fromkeys(c['confidence'] for c in members)),
                         'limitations': list(dict.fromkeys(limit for c in members for limit in c['limitations']))})
    return {'status': 'available', 'reportVersion': REPORT_VERSION, 'sections': sections, 'claims': claims, 'sources': sources,
            'mechanismAssessment': {'status': 'insufficient-evidence', 'verifiedMechanisticClaims': [],
                'contextSourceIds': [s['id'] for s in sources if s['kind'] == 'experimental-observation'],
                'contextMeaning': 'Dataset references and assay label definitions are context, not molecule-specific mechanistic proof.',
                'structuralRuleSourceIds': ['rules'], 'conclusion': '不确定；尚无充分证据。'},
            'evidenceBundleSha256': digest({'claims': claims, 'sources': sources}),
            'language': {'status': 'not-requested', 'mode': 'local-source-bounded-Chinese-rendering'},
            'limitations': ['Attribution and language organization are not mechanistic validation.', 'No clinical diagnosis or human-risk probability.']}


def plan_body(config, report):
    # Deliberately omit all retrieval molecules, individual labels, citations,
    # source row identities and original training data from external requests.
    skeleton = [{'id': section['id'], 'title': section['title'],
                 'claims': [{'id': claim['id'], 'sourceKind': claim['sourceKind'], 'confidence': claim['confidence']}
                            for claim in report['claims'] if claim['sectionId'] == section['id']]}
                for section in report['sections']]
    prompt = ('Organize a Chinese research report using ONLY supplied opaque section and claim IDs. Return one JSON object '
              'with exactly schema="aidd.llm.report-plan.v1", sectionOrder (all supplied section IDs once), '
              'and claimOrder (mapping each section ID to all its own claim IDs once). Do not add text, claims, numbers, '
              'citations or scores. Facts are rendered locally. Preserve limitations and all source categories.')
    return {'model': config.model_name, 'messages': [{'role': 'system', 'content': prompt},
            {'role': 'user', 'content': json.dumps({'promptVersion': PROMPT_VERSION, 'sections': skeleton}, ensure_ascii=False)}],
            **({'response_format': {'type': 'json_object'}} if config.json_mode else {}),
            **({'temperature': config.temperature} if config.temperature is not None else {})}


def apply_plan(report, plan):
    if not isinstance(plan, dict) or set(plan) != {'schema', 'sectionOrder', 'claimOrder'} or plan['schema'] != PLAN_SCHEMA:
        raise ValueError('Language model attempted unsupported/free scientific content')
    original = {s['id']: s for s in report['sections']}
    order, claim_order = plan['sectionOrder'], plan['claimOrder']
    if (not isinstance(order, list) or len(order) != len(original) or set(order) != set(original)
            or not isinstance(claim_order, dict) or set(claim_order) != set(original)):
        raise ValueError('Report plan omits or invents sections')
    by_id = {c['id']: c for c in report['claims']}
    sections = []
    for name in order:
        ids = claim_order[name]
        if not isinstance(ids, list) or len(ids) != len(original[name]['claimIds']) or set(ids) != set(original[name]['claimIds']):
            raise ValueError('Report plan omits/invents/reassigns claims or references')
        sections.append({**original[name], 'claimIds': ids, 'text': '\n'.join(by_id[c]['text'] for c in ids)})
    return sections


def organize_with_language(report, client, provider):
    output = copy.deepcopy(report)
    unavailable = {'status': 'unavailable', 'provider': provider, 'mode': 'closed-claim-ordering',
                   'message': '语言模型整理不可用；本机事实报告仍保留。', 'sentExperimentalRows': False,
                   'sentMolecularStructures': False, 'generatedScientificFactsAccepted': False}
    config = getattr(client, 'config', None)
    if (not isinstance(config, ModelConfig) or config.api_format != 'openai' or config.configuration_error()
            or len(config.model_name) > 200 or len(config.model_version) > 200):
        output['language'] = {**unavailable, 'errorCode': 'LANGUAGE_MODEL_NOT_CONFIGURED'}
        return output
    request = urllib.request.Request(config.api_url, data=json.dumps(plan_body(config, report), ensure_ascii=False).encode('utf-8'),
              headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                       **({'Authorization': 'Bearer ' + config.api_key} if config.api_key else {})}, method='POST')
    try:
        with client.opener.open(request, timeout=config.timeout_seconds) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError('Response too large')
        outer = json.loads(data)
        plan = json.loads(outer['choices'][0]['message']['content'])
        output['sections'] = apply_plan(report, plan)
        returned = outer.get('model')
        output['language'] = {'status': 'organized', 'provider': provider, 'mode': 'closed-claim-ordering',
                              'modelName': config.model_name, 'configuredModelVersion': config.model_version,
                              'returnedModel': returned if isinstance(returned, str) and len(returned) <= 200 and not (config.api_key and config.api_key in returned) else None,
                              'promptVersion': PROMPT_VERSION, 'contractVersion': PLAN_SCHEMA,
                              'versionVerification': 'configured/returned identifiers only; immutable provider snapshot not independently verified',
                              'hardware': 'provider-managed API; no local language-model weights loaded',
                              'licenseStatus': 'provider account terms apply; local weight license/redistribution not asserted',
                              'sentExperimentalRows': False, 'sentMolecularStructures': False, 'generatedScientificFactsAccepted': False}
    except (socket.timeout, TimeoutError):
        output['language'] = {**unavailable, 'errorCode': 'LANGUAGE_MODEL_TIMEOUT'}
    except urllib.error.HTTPError:
        output['language'] = {**unavailable, 'errorCode': 'LANGUAGE_MODEL_HTTP_ERROR'}
    except (ValueError, TypeError, KeyError, IndexError, OSError, urllib.error.URLError):
        output['language'] = {**unavailable, 'errorCode': 'LANGUAGE_MODEL_INVALID_OR_FAILED_RESPONSE'}
    return output
