"""Independent explanation orchestration; historical predictions stay immutable."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from backend.chemistry import analyze_chemistry, canonicalize_smiles, RULES_VERSION
from backend.model_client import ENDPOINT_IDS, ModelAPIError
from backend.attribution import AttributionEngine
from backend.evidence_search import EvidenceSearch
from backend.explanation_report import build_report, organize_with_language

CONTRACT_VERSION = 'aidd.research.explanation.api.v1'
ROOT = Path(__file__).resolve().parents[1]


class ExplanationService:
    def __init__(self, local_client, configuration, *, research_client=None, evidence=None, attribution=None):
        self.local, self.configuration, self.research = local_client, configuration, research_client
        values = configuration.startup_values
        self.evidence = evidence or EvidenceSearch(values.get('TOXNET_EVIDENCE_INDEX_DIR', ROOT / 'data/evidence-index'))
        self.attribution = attribution or AttributionEngine(local_client, research_client)

    def explain(self, smiles, *, endpoint='herg', model='v2', include_language=False, language_provider=None,
                steps=32, expected_weights=None, expected_structure=None):
        canonical = canonicalize_smiles(smiles)
        identity = hashlib.sha256(canonical.encode()).hexdigest()
        if expected_structure is not None and expected_structure != identity:
            raise ModelAPIError('EXPLANATION_STRUCTURE_CHANGED', '分子结构与原报告不一致，请重新筛选。', 409)
        client = self.local if model == 'v2' else self.research
        if client is None:
            raise ModelAPIError('EXPLANATION_MODEL_UNAVAILABLE', '研究模型未配置或未完成真实训练。', 503)
        with client._lock:
            prediction = client.predict(canonical, identity) if model == 'v2' else client.predict_all(canonical)
            if model == 'v2' and endpoint not in ENDPOINT_IDS or endpoint not in {p['id'] for p in prediction['endpoints']}:
                raise ModelAPIError('EXPLANATION_ENDPOINT_UNAVAILABLE', '所选模型未实际训练该端点。', 422)
            provenance = prediction['provenance']
            if expected_weights and expected_weights != provenance.get('weightsSha256'):
                raise ModelAPIError('EXPLANATION_MODEL_CHANGED', '实际模型权重与原报告不一致，请重新筛选。', 409)
            attribution = self.attribution.explain(canonical, endpoint=endpoint, model=model, steps=steps)
            if attribution['status'] in ('available', 'computed-with-warning') and any(
                    attribution['provenance'].get(key) != provenance.get(key)
                    for key in ('weightsSha256', 'modelVersion', 'artifactSha256', 'manifestSha256')):
                attribution = {'status': 'unavailable', 'method': None, 'atoms': [], 'fragments': [], 'imageDataUrl': None,
                               'errorCode': 'ATTRIBUTION_MODEL_CHANGED', 'provenance': {}, 'limitations': ['Model snapshot differs; no attribution published.']}
        evidence = self.evidence.search(canonical, endpoint=endpoint, limit=3)
        chemistry = analyze_chemistry(canonical)
        alerts = {'status': 'available', 'alerts': chemistry['alerts'], 'rulesVersion': RULES_VERSION,
                  'sourceKind': 'human-rule', 'notNeuralAttribution': True}
        report = build_report(prediction, attribution, evidence, alerts, endpoint=endpoint)
        if include_language:
            with self.configuration.lock:
                provider_client = self.configuration.registry.get(language_provider)
            report = organize_with_language(report, provider_client, language_provider)
        return {'contractVersion': CONTRACT_VERSION, 'status': 'available' if attribution['status'] == 'available' and evidence['status'] == 'available' else 'partial',
                'canonicalSmiles': canonical, 'structureId': identity, 'generatedAt': datetime.now(timezone.utc).isoformat(),
                'model': model, 'endpoint': endpoint, 'modelPrediction': prediction, 'attribution': attribution,
                'evidence': evidence, 'structuralAlerts': alerts, 'report': report,
                'provenance': {'explanationContractVersion': CONTRACT_VERSION, 'modelVersion': provenance.get('modelVersion'),
                               'weightsSha256': provenance.get('weightsSha256'), 'reportVersion': report['reportVersion'],
                               'evidenceBundleSha256': report['evidenceBundleSha256'], 'researchOnly': True,
                               'clinicalDiagnosis': False, 'humanRiskProbability': False},
                'limitations': ['Research prototype; not clinical diagnosis or human-risk probability.',
                                'Attribution is not a causal mechanism; language organization is not scientific validation.',
                                'Experimental database structures/individual labels are not transmitted to language providers.']}
