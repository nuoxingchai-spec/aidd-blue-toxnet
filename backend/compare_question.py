"""Deterministic research-topic recognition, never a task or label generator.

The original question is not copied into model prompts or cloud snapshots.
Recognising a broad Chinese toxicity term only selects an existing source-label
topic; it does not equate that endpoint with all clinical effects of that name.
"""
from __future__ import annotations

import re

QUESTION_CONTRACT = 'aidd.compare.question-scope.v1'

_TOPICS = (
    ('herg', (r'(?<![a-z0-9_])herg(?![a-z0-9_])', r'心脏毒', r'心脏安全', r'心肌毒', r'心律失常')),
    ('dili', (r'(?<![a-z0-9_])dili(?![a-z0-9_])', r'肝毒', r'肝损伤', r'肝脏安全')),
    ('ames', (r'(?<![a-z0-9_])ames(?![a-z0-9_])', r'致突变', r'诱变性')),
    ('clintox', (r'(?<![a-z0-9_])clintox(?![a-z0-9_])', r'临床毒', r'临床安全')),
    ('bbb', (r'(?<![a-z0-9_])bbb(?![a-z0-9_])', r'血脑屏障', r'脑血屏障', r'入脑', r'透脑')),
    ('tox21', (r'(?<![a-z0-9_])tox21(?![a-z0-9_])', r'通路活性', r'通路激活', r'十二.{0,2}通路', r'12.{0,2}通路')),
)
_UNSUPPORTED = (
    ('carcinogenicity', r'致癌|carcinogen'),
    ('reproductive-toxicity', r'生殖毒|致畸|reproductive|teratogen'),
    ('nephrotoxicity', r'肾毒|肾损伤|nephrotox'),
    ('human-safe-dose', r'安全剂量|人体剂量|临床剂量|safe\s+dose'),
)


def interpret_question(question: str, *, specialist_task=None, include_tox21=True):
    """Return bounded local topic IDs and warnings; never auto-select a task.

    Multiple explicit topics are supported together. Vague/unrecognised prose
    instead requests an explicit choice. In particular, BBB recognition alone
    cannot enable TxGemma: its reviewed task must already have been selected.
    """
    if not isinstance(question, str) or len(question) > 1000:
        raise ValueError('Question must be bounded text')
    text = question.strip().lower()
    topics = [topic for topic, patterns in _TOPICS if any(re.search(pattern, text) for pattern in patterns)]
    unsupported = [identifier for identifier, pattern in _UNSUPPORTED if re.search(pattern, text)]
    warnings = []
    active = list(topics)
    if 'herg' in topics:
        warnings.append('心脏毒性仅定位到现有 hERG 来源标签；hERG 不能代表全部心脏毒性。')
    if 'clintox' in topics:
        warnings.append('临床毒性仅定位到 ClinTox 来源标签，不是人体临床风险概率。')
    if 'bbb' in topics:
        if specialist_task != 'tdc-bbb-martins':
            active.remove('bbb')
            warnings.append('血脑屏障需要显式选择已审核的 BBB_Martins 专项任务；未自动启用。')
        warnings.append('BBB 穿透类别不等于 hERG、DILI、Ames 或 ClinTox 毒性标签。')
    if 'tox21' in topics:
        warnings.append('Tox21 是十二个独立实验通路任务，不能视作四毒性标签。')
        if not include_tox21:
            active.remove('tox21')
            warnings.append('当前未包含 Tox21 报告，请先显式开启通路展示。')
    if unsupported:
        warnings.append('问题包含未支持的研究任务；请明确选择已有端点，不会推测实验标签或生成替代预测。')
    if text and not topics and not unsupported:
        warnings.append('未识别到明确的受支持研究主题，请选择端点或已审核任务。')
    needs_selection = bool(unsupported or (text and not topics) or set(active) != set(topics))
    status = 'empty' if not text else 'requires-selection' if needs_selection else 'recognized'
    scope = active[0] if len(active) == 1 else 'multiple-topics' if active else 'whole-molecule'
    return {'contractVersion': QUESTION_CONTRACT, 'status': status, 'scope': scope,
            'topics': topics, 'activeTopics': active, 'unsupportedTopics': unsupported,
            'requiresSelection': needs_selection, 'warnings': warnings,
            'taskAutomaticallySelected': False, 'rawQuestionIncluded': False}
