"""Validated chemistry and explicitly uncalibrated demonstration rules.

RDKit supplies parsing, aromaticity, canonical identity, descriptors and drawing.
The additive endpoint scores below are local demonstration rules. They are not
model predictions, calibrated probabilities, or verified TDC dataset labels.
"""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
from math import isfinite
from typing import Any

from rdkit import Chem, rdBase
from rdkit.Chem import Crippen, Descriptors, Lipinski, rdDepictor, rdMolDescriptors
from rdkit.Chem.Draw import rdMolDraw2D

CHEMISTRY_VERSION = "rdkit-chemistry-1.0.0"
RULES_VERSION = "local-demo-rules-2.0.0"
MAX_SMILES_LENGTH = 4096
MAX_HEAVY_ATOMS = 200
MAX_ATOMS = 400

_REFERENCE_FIXTURES = (
    ("CC(=O)NC1=CC=C(O)C=C1", "acetaminophen", "对乙酰氨基酚", "DILI 示例", (0.19, 0.78, 0.09, 0.48)),
    ("CC(C)(C)C1=CC=C(C=C1)C(O)CCCN1CCC(CC1)C(C2=CC=CC=C2)(C3=CC=CC=C3)O", "terfenadine", "特非那定", "hERG 示例", (0.94, 0.42, 0.07, 0.82)),
    ("C1C(=O)NC(=O)N1N=CC2=CC=C(O2)[N+](=O)[O-]", "nitrofurantoin", "呋喃妥因", "硝基杂环示例", (0.16, 0.61, 0.89, 0.72)),
    ("CC(=O)OC1=CC=CC=C1C(=O)O", "aspirin", "阿司匹林", "对照示例", (0.13, 0.22, 0.07, 0.24)),
    ("CCO", "ethanol", "乙醇", "小分子对照示例", (0.04, 0.16, 0.02, 0.18)),
    ("C1=CC=C2C(=C1)C(=O)C3=CC=CC=C3C2=O", "anthraquinone", "蒽醌", "醌结构示例", (0.34, 0.57, 0.48, 0.63)),
)

_ENDPOINTS = (
    {"id": "herg", "short": "hERG", "label": "hERG 抑制", "description": "心脏钾通道相关结构信号"},
    {"id": "dili", "short": "DILI", "label": "药物性肝损伤", "description": "肝损伤相关结构信号"},
    {"id": "ames", "short": "Ames", "label": "Ames 致突变", "description": "致突变相关结构信号"},
    {"id": "clintox", "short": "ClinTox", "label": "临床毒性", "description": "综合结构警示负担"},
)

# SMARTS match chemical subgraphs, so atom order, ring labels, and Kekule versus
# aromatic input notation do not determine whether an alert is reported.
_ALERT_DEFINITIONS = (
    {"id": "nitro", "label": "硝基 / 硝基芳烃", "severity": "high", "detail": "硝基结构提示潜在代谢活化与致突变信号，需要实验确认。", "smarts": ("[N+;X3](=[O;X1])[O-;X1]",), "weights": {"herg": 0.03, "dili": 0.08, "ames": 0.27, "clintox": 0.17}},
    {"id": "aniline", "label": "苯胺 / 芳香胺", "severity": "high", "detail": "芳香碳连接非酰胺胺氮；不包含芳香环内氮或酰胺氮。", "smarts": ("[c]-[N;X3,X4;!$(N-[C,S,P]=[O,S,N]);!$(N=[O,S,N,C])]",), "weights": {"herg": 0.03, "dili": 0.13, "ames": 0.18, "clintox": 0.14}},
    {"id": "hydrazine", "label": "肼 / 联氨结构", "severity": "high", "detail": "非芳香氮之间的单键提示肼或肼衍生物结构。", "smarts": ("[N]-[N]",), "weights": {"herg": 0.02, "dili": 0.17, "ames": 0.23, "clintox": 0.18}},
    {"id": "epoxide", "label": "环氧乙烷 / 环氧化物", "severity": "high", "detail": "含氧三元环提示亲电反应性，需要结合暴露与实验判断。", "smarts": ("[O;X2;r3]1-[C;r3]-[C;r3]-1",), "weights": {"herg": 0.02, "dili": 0.13, "ames": 0.24, "clintox": 0.18}},
    {"id": "michael", "label": "Michael 受体", "severity": "medium", "detail": "检测与羰基、腈或硝基共轭的碳碳双键，提示潜在亲电受体。", "smarts": ("[C]=[C]-[C](=[O])", "[C]=[C]-[C]#[N]", "[C]=[C]-[N+](=[O])[O-]"), "weights": {"herg": 0.04, "dili": 0.08, "ames": 0.13, "clintox": 0.11}},
    {"id": "quinone", "label": "醌 / 氧化还原循环片段", "severity": "high", "detail": "共轭六元环二酮提示醌结构；包含邻醌、对醌及相应稠环结构。", "smarts": ("[C;R]1(=O)-[#6;R]=,:[#6;R]-[C;R](=O)-[#6;R]=,:[#6;R]-1", "[C;R]1(=O)-[C;R](=O)-[#6;R]=,:[#6;R]-[#6;R]=,:[#6;R]-1"), "weights": {"herg": 0.07, "dili": 0.16, "ames": 0.19, "clintox": 0.17}},
    {"id": "aromatic_halide", "label": "芳香卤素", "severity": "medium", "detail": "芳香碳连接卤素；该片段本身不证明毒性。", "smarts": ("[c]-[F,Cl,Br,I]",), "weights": {"herg": 0.08, "dili": 0.04, "ames": 0.03, "clintox": 0.04}},
    {"id": "acyl_halide", "label": "酰卤", "severity": "high", "detail": "羰基碳连接卤素提示高反应性酰卤结构。", "smarts": ("[C](=[O])-[F,Cl,Br,I]",), "weights": {"herg": 0.03, "dili": 0.12, "ames": 0.19, "clintox": 0.15}},
    {"id": "thiol", "label": "游离巯基 / 硫负离子", "severity": "medium", "detail": "检测碳连接的游离巯基及其硫负离子形式，不包含硫醚。", "smarts": ("[S;H1;+0]-[#6]", "[S-;X1]-[#6]"), "weights": {"herg": 0.02, "dili": 0.07, "ames": 0.04, "clintox": 0.08}},
    {"id": "sulfonate", "label": "磺酸 / 磺酸酯 / 硫酸酯", "severity": "medium", "detail": "检测 S(=O)₂–O 结构；不将普通砜或磺酰胺视为磺酸酯。", "smarts": ("[S;X4](=[O])(=[O])-[O;X2]", "[S;X4](=[O])(=[O])-[O-;X1]"), "weights": {"herg": 0.02, "dili": 0.08, "ames": 0.05, "clintox": 0.09}},
)


def _compile_smarts(smarts: str) -> Chem.Mol:
    pattern = Chem.MolFromSmarts(smarts)
    if pattern is None:
        raise RuntimeError(f"Invalid internal SMARTS: {smarts}")
    return pattern


_ALERT_PATTERNS = {
    definition["id"]: tuple(_compile_smarts(pattern) for pattern in definition["smarts"])
    for definition in _ALERT_DEFINITIONS
}
_BASIC_AMINE = _compile_smarts("[N;X3,X4;!$(N-[C,S,P]=[O,S,N]);!$(N=*);!$(N#*)]")


def _parse(smiles: str) -> Chem.Mol:
    parameters = Chem.SmilesParserParams()
    parameters.parseName = False
    parameters.allowCXSMILES = False
    # Parse and sanitize together: RDKit rejects dangling bonds, invalid ring
    # closures, non-ring aromatic atoms, and forbidden valences.
    with rdBase.BlockLogs():
        molecule = Chem.MolFromSmiles(smiles, parameters)
    if molecule is None or molecule.GetNumAtoms() == 0:
        raise ValueError("SMILES 无效：无法解析或违反化学价态。")
    if any(atom.GetAtomicNum() == 0 for atom in molecule.GetAtoms()):
        raise ValueError("不支持未定义元素或通配符原子。")
    if molecule.GetNumHeavyAtoms() > MAX_HEAVY_ATOMS or molecule.GetNumAtoms() > MAX_ATOMS:
        raise ValueError(f"分子过大：最多支持 {MAX_HEAVY_ATOMS} 个重原子。")
    # Atom map numbers are external annotation, not chemical identity.
    for atom in molecule.GetAtoms():
        atom.SetAtomMapNum(0)
    return molecule


def _canonical(molecule: Chem.Mol) -> str:
    return Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)


def _build_reference_library() -> dict[str, dict[str, Any]]:
    references = {}
    for smiles, compound_id, name, note, scores in _REFERENCE_FIXTURES:
        molecule = _parse(smiles)
        references[_canonical(molecule)] = {
            "id": compound_id,
            "name": name,
            "note": f"{note}；固定演示数据，未核验真实标签来源。",
            "scores": {endpoint["id"]: score for endpoint, score in zip(_ENDPOINTS, scores)},
        }
    return references


_REFERENCES = _build_reference_library()


def _descriptors(molecule: Chem.Mol) -> dict[str, Any]:
    counts = Counter(atom.GetSymbol() for atom in molecule.GetAtoms())
    counts["H"] += sum(atom.GetTotalNumHs(includeNeighbors=False) for atom in molecule.GetAtoms() if atom.GetAtomicNum() != 1)
    if not counts["H"]:
        counts.pop("H", None)
    amine_indices = {
        match[0]
        for match in molecule.GetSubstructMatches(_BASIC_AMINE)
        if molecule.GetAtomWithIdx(match[0]).GetFormalCharge() >= 0
        and any(neighbor.GetAtomicNum() == 6 for neighbor in molecule.GetAtomWithIdx(match[0]).GetNeighbors())
    }
    return {
        "molecularWeight": round(Descriptors.MolWt(molecule), 2),
        "logP": round(Crippen.MolLogP(molecule), 2),
        "tpsa": round(rdMolDescriptors.CalcTPSA(molecule, includeSandP=True), 2),
        "hbd": Lipinski.NumHDonors(molecule),
        "hba": Lipinski.NumHAcceptors(molecule),
        "rings": rdMolDescriptors.CalcNumRings(molecule),
        "aromaticAtoms": sum(atom.GetIsAromatic() for atom in molecule.GetAtoms()),
        "rotatableBonds": rdMolDescriptors.CalcNumRotatableBonds(molecule, strict=True),
        "heavyAtoms": molecule.GetNumHeavyAtoms(),
        "basicAmines": len(amine_indices),
        "elementCounts": dict(sorted(counts.items())),
        "formula": rdMolDescriptors.CalcMolFormula(molecule),
    }


def _alerts(molecule: Chem.Mol) -> list[dict[str, Any]]:
    alerts = []
    for definition in _ALERT_DEFINITIONS:
        atom_indices = {
            index
            for pattern in _ALERT_PATTERNS[definition["id"]]
            for match in molecule.GetSubstructMatches(pattern, uniquify=True)
            for index in match
        }
        if atom_indices:
            alerts.append({
                "id": definition["id"],
                "label": definition["label"],
                "severity": definition["severity"],
                "detail": definition["detail"],
                "matchedAtomIndices": sorted(atom_indices),
                "weights": dict(definition["weights"]),
            })
    return sorted(alerts, key=lambda item: (item["severity"] != "high", item["id"]))


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return min(upper, max(lower, value))


def _feature_signals(descriptors: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"id": "lipophilicity", "label": "高亲脂性", "value": _clamp((descriptors["logP"] - 3) / 3), "detail": f"RDKit cLogP {descriptors['logP']}", "weights": {"herg": 0.18, "dili": 0.10, "ames": 0.02, "clintox": 0.10}},
        {"id": "basic_amine", "label": "非酰胺胺位点", "value": _clamp(descriptors["basicAmines"] / 2), "detail": f"{descriptors['basicAmines']} 个胺候选位点（不等同于 pKa）", "weights": {"herg": 0.24, "dili": 0.04, "ames": 0.02, "clintox": 0.10}},
        {"id": "aromatic_load", "label": "芳香负载", "value": _clamp((descriptors["aromaticAtoms"] - 8) / 16), "detail": f"{descriptors['aromaticAtoms']} 个芳香原子", "weights": {"herg": 0.08, "dili": 0.11, "ames": 0.08, "clintox": 0.10}},
        {"id": "molecular_size", "label": "高分子量", "value": _clamp((descriptors["molecularWeight"] - 350) / 450), "detail": f"RDKit 分子量 {descriptors['molecularWeight']} Da", "weights": {"herg": 0.04, "dili": 0.10, "ames": 0.03, "clintox": 0.08}},
        {"id": "polarity", "label": "低极性 / 低 TPSA", "value": _clamp((75 - descriptors["tpsa"]) / 75), "detail": f"RDKit TPSA {descriptors['tpsa']}", "weights": {"herg": 0.06, "dili": 0.04, "ames": 0.03, "clintox": 0.05}},
    ]


def _score_endpoints(descriptors: dict[str, Any], alerts: list[dict[str, Any]], reference: dict[str, Any] | None) -> list[dict[str, Any]]:
    bases = {"herg": 0.07, "dili": 0.09, "ames": 0.045, "clintox": 0.06}
    signals = _feature_signals(descriptors)
    results = []
    for endpoint in _ENDPOINTS:
        endpoint_id = endpoint["id"]
        score = bases[endpoint_id]
        drivers = [{"id": "baseline", "label": "演示规则基线", "detail": "预设规则常数，未经数据拟合。", "contribution": bases[endpoint_id]}]
        for signal in signals:
            contribution = signal["weights"][endpoint_id] * signal["value"]
            if contribution > 0:
                score += contribution
                drivers.append({"id": signal["id"], "label": signal["label"], "detail": signal["detail"], "contribution": round(contribution, 6)})
        for alert in alerts:
            contribution = alert["weights"][endpoint_id]
            if contribution > 0:
                score += contribution
                drivers.append({"id": alert["id"], "label": alert["label"], "detail": alert["detail"], "contribution": contribution})
        if endpoint_id == "clintox":
            ordered_scores = sorted((item["ruleScore"] for item in results), reverse=True)
            proxy = sum(value * weight for value, weight in zip(ordered_scores, (0.5, 0.3, 0.2)))
            contribution = max(0.0, proxy - 0.45) * 0.28
            if contribution > 0:
                score += contribution
                drivers.append({"id": "endpoint_burden", "label": "其他端点规则负担", "detail": "前三个端点规则分数的加权代理，未经校准。", "contribution": round(contribution, 6)})
        bounded_score = _clamp(score, 0.01, 0.98)
        if bounded_score != score:
            drivers.append({"id": "score_clamp", "label": "规则分数边界调整", "detail": "规则范围限定为 0.01–0.98。", "contribution": round(bounded_score - score, 6)})
        drivers.sort(key=lambda item: abs(item["contribution"]), reverse=True)
        results.append({
            **endpoint,
            "source": "本地结构规则（未训练、未校准）",
            "ruleScore": round(bounded_score, 3),
            "referenceScore": reference["scores"][endpoint_id] if reference else None,
            "scoreSource": "rule",
            "explanationSource": "structure-rules",
            "ruleDrivers": drivers,
        })
    return results


def _risk_tier(score: float) -> dict[str, str]:
    if score < 0.33:
        return {"id": "low", "label": "低规则信号", "action": "结合实验资料复核", "color": "blue"}
    if score < 0.58:
        return {"id": "moderate", "label": "中等规则信号", "action": "核查结构警示与实验资料", "color": "amber"}
    if score < 0.78:
        return {"id": "high", "label": "高规则信号", "action": "优先核查结构警示与实验资料", "color": "orange"}
    return {"id": "critical", "label": "重点关注规则信号", "action": "优先核查结构警示与实验资料", "color": "red"}


def _draw_and_structure(molecule: Chem.Mol) -> tuple[str, dict[str, Any]]:
    drawing_molecule = Chem.Mol(molecule)
    rdDepictor.Compute2DCoords(drawing_molecule)
    drawer = rdMolDraw2D.MolDraw2DSVG(700, 360)
    drawer.drawOptions().padding = 0.10
    drawer.drawOptions().bondLineWidth = 1.6
    drawer.drawOptions().minFontSize = 12
    drawer.drawOptions().maxFontSize = 28
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, drawing_molecule)
    drawer.FinishDrawing()
    conformer = drawing_molecule.GetConformer()
    structure = {
        "atoms": [{"index": atom.GetIdx(), "symbol": atom.GetSymbol(), "aromatic": atom.GetIsAromatic(), "formalCharge": atom.GetFormalCharge(), "isotope": atom.GetIsotope(), "hydrogens": atom.GetTotalNumHs(includeNeighbors=True), "x": round(conformer.GetAtomPosition(atom.GetIdx()).x, 4), "y": round(conformer.GetAtomPosition(atom.GetIdx()).y, 4)} for atom in drawing_molecule.GetAtoms()],
        "bonds": [{"index": bond.GetIdx(), "from": bond.GetBeginAtomIdx(), "to": bond.GetEndAtomIdx(), "order": bond.GetBondTypeAsDouble(), "aromatic": bond.GetIsAromatic(), "inRing": bond.IsInRing(), "type": str(bond.GetBondType()), "stereo": str(bond.GetStereo())} for bond in drawing_molecule.GetBonds()],
    }
    return drawer.GetDrawingText(), structure


def _validated_input(smiles: str) -> str:
    """Shared strict lexical validation for analysis and dataset identity."""
    if not isinstance(smiles, str):
        raise ValueError("SMILES 必须是文本。")
    original = smiles.strip()
    if not original:
        raise ValueError("请输入 SMILES。")
    if len(original) > MAX_SMILES_LENGTH:
        raise ValueError(f"SMILES 过长：最多支持 {MAX_SMILES_LENGTH} 个字符。")
    if any(character.isspace() for character in original):
        raise ValueError("SMILES 内部不能包含空白或附加名称。")
    return original


def canonicalize_smiles(smiles: str) -> str:
    """Return validated canonical isomeric SMILES with atom maps removed.

    Uses the same input, sanitization, and size restrictions as analysis; salts,
    isotopes and stereochemistry are preserved, without tautomer normalization.
    """
    return _canonical(_parse(_validated_input(smiles)))


def analyze_chemistry(smiles: str) -> dict[str, Any]:
    """Analyze one complete SMILES; reject invalid or oversized inputs.

    Descriptor and alert atom indices refer to the molecule parsed from
    canonicalSmiles, making indices stable across equivalent input traversals.
    Salts/fragments, isotopes and stereochemistry are preserved; no tautomer or
    protonation standardization is performed.
    """
    original = _validated_input(smiles)
    canonical = canonicalize_smiles(original)
    molecule = _parse(canonical)
    descriptors = _descriptors(molecule)
    alerts = _alerts(molecule)
    reference = _REFERENCES.get(canonical)
    endpoints = _score_endpoints(descriptors, alerts, reference)
    scores = [endpoint["ruleScore"] for endpoint in endpoints]
    overall = round(_clamp(0.62 * max(scores) + 0.28 * sum(scores) / len(scores) + 0.10 * _clamp(len(alerts) / 4)), 3)
    svg, structure = _draw_and_structure(molecule)
    if not all(isfinite(descriptors[key]) for key in ("molecularWeight", "logP", "tpsa")):
        raise ValueError("无法计算该分子的有限数值描述符。")
    return {
        "input": original,
        "canonicalSmiles": canonical,
        "structureId": sha256(canonical.encode("utf-8")).hexdigest(),
        "known": {key: reference[key] for key in ("id", "name", "note")} if reference else None,
        "descriptors": descriptors,
        "alerts": alerts,
        "endpoints": endpoints,
        "ruleOverall": overall,
        "tier": _risk_tier(overall),
        "rules": {"version": RULES_VERSION, "method": "additive-structure-rules", "calibrated": False, "scoreMeaning": "heuristic-score-not-probability", "thresholds": [0.33, 0.58, 0.78], "thresholdMeaning": "demonstration-rule-score-tiers"},
        "provenance": {"chemistryVersion": CHEMISTRY_VERSION, "rulesVersion": RULES_VERSION, "rdkitVersion": rdBase.rdkitVersion, "descriptors": "RDKit MolWt / Crippen MolLogP / TPSA (including S,P) / Lipinski", "identity": "canonical isomeric SMILES; atom maps removed; salts, isotopes and stereo retained", "referenceScores": {"kind": "demonstration-fixture", "source": None, "verified": False, "usedForScoring": False}},
        "moleculeSvg": svg,
        "structure": structure,
    }
