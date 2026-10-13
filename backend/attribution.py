"""Auditable neural attribution, separate from rule scores and model predictions.

v2: joint atom-embedding/Morgan Integrated Gradients of a calibrated logit.
v3: actual model reruns after legal terminal/BRICS fragment deletion.
Neither method establishes chemical causality, mechanisms, or clinical risk.
No historical model source, weight, calibration, or parameter gradient is changed.
"""
from __future__ import annotations

import base64
import hashlib
import math
from pathlib import Path

from rdkit import Chem, DataStructs, rdBase
from rdkit.Chem import BRICS, rdDepictor, rdFingerprintGenerator
from rdkit.Chem.Draw import rdMolDraw2D

from backend.chemistry import canonicalize_smiles
from backend.model_client import ENDPOINT_IDS

ATTRIBUTION_VERSION = "aidd.neural-attribution.v1"
MAX_EXPLANATION_ATOMS = 128
MAX_IG_STEPS = 128
MAX_FRAGMENT_RERUNS = 8
IG_ABSOLUTE_TOLERANCE = 0.03
IG_RELATIVE_TOLERANCE = 0.03
IG_CONVERGENCE_TOLERANCE = 0.05
GROUP_PATTERNS = (
    ("carbonyl", "羰基", "[CX3]=[OX1]"),
    ("amide", "酰胺", "[NX3]-[CX3](=[OX1])"),
    ("hydroxyl", "羟基", "[OX2H]-[#6]"),
    ("amine", "非酰胺胺", "[NX3;!$(N-C=O)]"),
    ("nitro", "硝基", "[N+](=O)[O-]"),
    ("halogen", "碳连接卤素", "[#6]-[F,Cl,Br,I]"),
)


class AttributionUnavailable(ValueError):
    def __init__(self, code, message, *, diagnostics=None):
        super().__init__(message)
        self.code, self.message, self.diagnostics = code, message, diagnostics


def source_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def integrated_gradients(function, inputs, baselines, *, steps):
    """Gauss-Legendre path integral; endpoints are evaluated separately.

    autograd.grad requests only path inputs. It never accumulates parameter
    gradients. Double-precision accumulation reduces cancellation error; the
    actual network/input arithmetic retains its original dtype.
    """
    import numpy as np
    import torch
    if type(steps) is not int or steps < 1 or len(inputs) != len(baselines) or not inputs:
        raise ValueError("Invalid Integrated Gradients input/baseline/steps")
    if any(x.shape != baseline.shape or x.dtype != baseline.dtype for x, baseline in zip(inputs, baselines)):
        raise ValueError("Integrated Gradients baseline shape/dtype mismatch")
    nodes, weights = np.polynomial.legendre.leggauss(steps)
    accumulators = [torch.zeros_like(value, dtype=torch.float64) for value in inputs]
    with torch.inference_mode(False), torch.enable_grad():
        for node, weight in zip(nodes, weights):
            alpha = float((node + 1) / 2)
            points = tuple((baseline + alpha * (value - baseline)).detach().requires_grad_(True)
                           for value, baseline in zip(inputs, baselines))
            target = function(*points)
            if target.numel() != 1 or not bool(torch.isfinite(target).all()):
                raise ValueError("Attribution target must be a finite scalar")
            gradients = torch.autograd.grad(target.reshape(()), points, create_graph=False, retain_graph=False, allow_unused=True)
            for index, gradient in enumerate(gradients):
                if gradient is not None:
                    if not bool(torch.isfinite(gradient).all()):
                        raise ValueError("Attribution gradient is non-finite")
                    accumulators[index] += gradient.detach().double() * float(weight / 2)
        with torch.no_grad():
            actual = float(function(*inputs).reshape(()))
            reference = float(function(*baselines).reshape(()))
    contributions = tuple((value - baseline).double() * accumulated for value, baseline, accumulated in zip(inputs, baselines, accumulators))
    total = sum(float(value.sum()) for value in contributions)
    return contributions, {"steps": steps, "quadrature": "Gauss-Legendre", "targetValue": actual,
                           "baselineValue": reference, "targetDifference": actual - reference,
                           "contributionSum": total, "completenessResidual": actual - reference - total}


def embedded_forward(model, features, atom_values, fingerprint):
    """Exact v2 forward after atom embedding lookup; graph/CLS remain fixed."""
    import torch
    value = torch.cat((model.cls.expand(atom_values.shape[0], -1, -1), atom_values), dim=1)
    bias = (model.distance_bias(features["distances"]) + model.bond_bias(features["bonds"])).permute(0, 3, 1, 2)
    bias = bias.masked_fill(~features["mask"][:, None, None, :], float("-inf"))
    for block in model.blocks:
        value = block(value, bias)
    graph = model.graph_norm(value[:, 0])
    return model.head(torch.cat((graph, model.fingerprint_encoder(fingerprint)), dim=-1))


def morgan_to_atoms(molecule, bit_contributions, *, fingerprint_size=2048, expected_fingerprint=None):
    """Distribute each actual active-bit contribution across recorded environments.

    Repeated/collided environments share bit credit equally; each environment's
    credit is shared equally among its atoms. This is a transparent mapping
    convention, not an independently identified causal atomic contribution.
    """
    import numpy as np
    generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=fingerprint_size, includeChirality=True)
    additional = rdFingerprintGenerator.AdditionalOutput()
    additional.AllocateBitInfoMap()
    fingerprint = generator.GetFingerprint(molecule, additionalOutput=additional)
    bits = np.zeros(fingerprint_size, dtype=np.float32)
    DataStructs.ConvertToNumpyArray(fingerprint, bits)
    if expected_fingerprint is not None and not np.array_equal(bits, np.asarray(expected_fingerprint)):
        raise ValueError("Attribution Morgan fingerprint differs from the actual model input")
    values = np.asarray(bit_contributions, dtype=np.float64)
    if values.shape != (fingerprint_size,) or not np.isfinite(values).all():
        raise ValueError("Invalid Morgan bit attribution dimension/value")
    atoms = np.zeros(molecule.GetNumAtoms(), dtype=np.float64)
    records = []
    for bit, occurrences in sorted(additional.GetBitInfoMap().items()):
        contribution = float(values[bit])
        environments = []
        for center, radius in occurrences:
            indices = {center}
            for bond_index in Chem.FindAtomEnvironmentOfRadiusN(molecule, radius, center):
                bond = molecule.GetBondWithIdx(bond_index)
                indices.update((bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()))
            for index in indices:
                atoms[index] += contribution / len(occurrences) / len(indices)
            environments.append({"center": center, "radius": radius, "atomIndices": sorted(indices)})
        records.append({"bit": bit, "contribution": contribution, "environments": environments,
                        "repeatedOrCollidedEnvironment": len(occurrences) > 1})
    if any(abs(float(values[index])) > 1e-8 for index in range(fingerprint_size) if not bits[index]):
        raise ValueError("Inactive Morgan bit has unexplained nonzero attribution")
    if not math.isclose(float(atoms.sum()), float(values.sum()), rel_tol=1e-8, abs_tol=1e-8):
        raise ValueError("Morgan atom mapping does not conserve bit contributions")
    return atoms, records


def group_contributions(molecule, atom_scores):
    groups = []
    for group_id, label, smarts in GROUP_PATTERNS:
        pattern = Chem.MolFromSmarts(smarts)
        for match in molecule.GetSubstructMatches(pattern):
            indices = sorted(set(match))
            groups.append({"id": group_id, "label": label, "atomIndices": indices,
                           "contribution": sum(float(atom_scores[index]) for index in indices),
                           "source": "sum-of-neural-atom-attributions; SMARTS only names the group"})
    for ring in molecule.GetRingInfo().AtomRings():
        if all(molecule.GetAtomWithIdx(index).GetIsAromatic() for index in ring):
            groups.append({"id": "aromatic-ring", "label": "芳香环", "atomIndices": sorted(ring),
                           "contribution": sum(float(atom_scores[index]) for index in ring),
                           "source": "sum-of-neural-atom-attributions"})
    return sorted(groups, key=lambda value: abs(value["contribution"]), reverse=True)


def heatmap_png(molecule, atom_scores, *, width=720, height=440):
    maximum = max((abs(float(value)) for value in atom_scores), default=0) or 1
    colors = {}
    for index, value in enumerate(atom_scores):
        strength = min(1, abs(float(value)) / maximum)
        target = (0.93, 0.28, 0.22) if value > 0 else (0.13, 0.46, 0.87)
        colors[index] = tuple(1 - strength * (1 - channel) for channel in target)
    drawing_molecule = Chem.Mol(molecule)
    rdDepictor.Compute2DCoords(drawing_molecule)
    drawer = rdMolDraw2D.MolDraw2DCairo(width, height)
    drawer.drawOptions().addAtomIndices = True
    drawer.DrawMolecule(drawing_molecule, highlightAtoms=list(colors), highlightAtomColors=colors,
                        highlightAtomRadii={index: 0.32 for index in colors})
    drawer.FinishDrawing()
    return "data:image/png;base64," + base64.b64encode(drawer.GetDrawingText()).decode("ascii")


def legal_fragment_deletions(canonical, *, maximum=MAX_FRAGMENT_RERUNS):
    molecule = Chem.MolFromSmiles(canonical)
    proposals = []
    # Terminal single atoms are local deletion probes, never chemical mechanisms.
    proposals.extend(("terminal-atom", (atom.GetIdx(),)) for atom in molecule.GetAtoms() if atom.GetDegree() == 1)
    for (left, right), _labels in BRICS.FindBRICSBonds(molecule):
        bond = molecule.GetBondBetweenAtoms(left, right)
        cut = Chem.FragmentOnBonds(molecule, [bond.GetIdx()], addDummies=False)
        parts = [part for part in Chem.GetMolFrags(cut) if left in part or right in part]
        if len(parts) == 2:
            proposals.extend(("brics-terminal-fragment", tuple(part)) for part in sorted(parts, key=len))
    seen, result = set(), []
    for method, indices in proposals:
        if not indices or len(indices) >= molecule.GetNumAtoms():
            continue
        edited = Chem.RWMol(molecule)
        for index in sorted(indices, reverse=True):
            edited.RemoveAtom(index)
        variant = edited.GetMol()
        try:
            with rdBase.BlockLogs():
                Chem.SanitizeMol(variant)
            variant_smiles = canonicalize_smiles(Chem.MolToSmiles(variant, canonical=True, isomericSmiles=True))
        except (ValueError, RuntimeError):
            continue
        if variant_smiles == canonical or variant_smiles in seen:
            continue
        seen.add(variant_smiles)
        result.append({"deletionMethod": method, "removedAtomIndices": sorted(indices), "perturbedSmiles": variant_smiles})
        if len(result) >= maximum:
            break
    return result


class AttributionEngine:
    def __init__(self, local_client, research_client=None):
        self.local_client, self.research_client = local_client, research_client

    def explain(self, smiles, endpoint="herg", model="v2", steps=32):
        try:
            canonical = canonicalize_smiles(smiles)
            molecule = Chem.MolFromSmiles(canonical)
            if molecule.GetNumAtoms() > MAX_EXPLANATION_ATOMS:
                raise AttributionUnavailable("ATTRIBUTION_BUDGET_EXCEEDED", f"解释最多支持 {MAX_EXPLANATION_ATOMS} 个原子；原模型预测不受此解释预算影响。")
            if type(steps) is not int or not 8 <= steps <= MAX_IG_STEPS:
                raise AttributionUnavailable("ATTRIBUTION_INVALID_SETTINGS", f"IG steps 必须为 8..{MAX_IG_STEPS} 的整数。")
            if model == "v2":
                return self._v2(canonical, molecule, endpoint, steps)
            if model in ("v3", "multitask"):
                return self._fragment(canonical, molecule, endpoint)
            raise AttributionUnavailable("ATTRIBUTION_MODEL_UNSUPPORTED", "不支持的解释模型；不能为外部 API 输出伪造局部归因。")
        except AttributionUnavailable as error:
            return {"status": "unavailable", "method": None, "endpoint": endpoint, "atoms": [], "fragments": [],
                    "imageDataUrl": None, "errorCode": error.code, "message": error.message,
                    "diagnostics": error.diagnostics, "provenance": {"attributionVersion": ATTRIBUTION_VERSION},
                    "limitations": ["No rule or simulated attribution fallback."]}
        except Exception as error:
            return {"status": "unavailable", "method": None, "endpoint": endpoint, "atoms": [], "fragments": [],
                    "imageDataUrl": None, "errorCode": "INVALID_SMILES" if isinstance(error, ValueError) and model not in ("v2", "v3", "multitask") else "ATTRIBUTION_UNAVAILABLE",
                    "message": "无法计算真实归因；请核验分子、模型工件、依赖和计算预算。",
                    "provenance": {"attributionVersion": ATTRIBUTION_VERSION}, "limitations": ["No synthetic output or mechanism claim."]}

    def _v2(self, canonical, molecule, endpoint, steps):
        if endpoint not in ENDPOINT_IDS:
            raise AttributionUnavailable("ATTRIBUTION_ENDPOINT_UNSUPPORTED", "v2 仅支持原四端点归因。")
        if self.local_client is None:
            raise AttributionUnavailable("ATTRIBUTION_MODEL_UNAVAILABLE", "真实 v2 模型不可用。")
        with self.local_client._lock:
            loaded = self.local_client._ensure_loaded()
            torch, network, actual_model = loaded["torch"], loaded["network"], loaded["model"]
            manifest = loaded["manifest"]
            expected_code = manifest.get("training", {}).get("sourceHashes", {}).get("modelCode")
            if expected_code != hashlib.sha256(Path(network.__file__).read_bytes()).hexdigest():
                raise AttributionUnavailable("ATTRIBUTION_MODEL_CODE_MISMATCH", "模型源码与已训练工件记录不符。")
            if actual_model.training or manifest["architecture"].get("id") != "aidd.graph-transformer.v2":
                raise AttributionUnavailable("ATTRIBUTION_MODEL_UNSUPPORTED", "归因仅支持已核验且处于 eval 的 v2 图模型。")
            structure_id = hashlib.sha256(canonical.encode()).hexdigest()
            before = self.local_client.predict(canonical, structure_id)
            if before["provenance"].get("weightsSha256") != manifest["artifacts"]["weights"]["sha256"] or before["provenance"].get("artifactSha256") != loaded["manifestSha256"]:
                raise AttributionUnavailable("ATTRIBUTION_MODEL_CHANGED", "归因开始时真实模型快照已变化。")
            index = ENDPOINT_IDS.index(endpoint)
            try:
                with torch.inference_mode(False), torch.enable_grad():
                    features = network.featurize_smiles(canonical, manifest["architecture"])
                    atom_values = (sum(embedding(features["atoms"][..., column]) for column, embedding in enumerate(actual_model.atom_embeddings))
                                   / math.sqrt(len(actual_model.atom_embeddings)))[:, 1:].detach()
                    fingerprint = features["fingerprint"].detach()
                    with torch.no_grad():
                        original = actual_model(**features)
                        manual = embedded_forward(actual_model, features, atom_values, fingerprint)
                    difference = float((original - manual).abs().max())
                    if difference > 1e-6:
                        raise AttributionUnavailable("ATTRIBUTION_FORWARD_MISMATCH", "嵌入归因路径与真实模型 forward 不一致。")
                    temperature, bias = loaded["temperatures"][index], loaded["biases"][index]
                    target = lambda atoms, fp: embedded_forward(actual_model, features, atoms, fp)[0, index] / temperature + bias
                    inputs = atom_values, fingerprint
                    baselines = tuple(torch.zeros_like(value) for value in inputs)
                    coarse, coarse_info = integrated_gradients(target, inputs, baselines, steps=steps)
                    fine_steps = min(MAX_IG_STEPS, steps * 2)
                    fine, diagnostics = integrated_gradients(target, inputs, baselines, steps=fine_steps)
                    change = sum(float((left - right).abs().sum()) for left, right in zip(coarse, fine))
                    scale = sum(float(value.abs().sum()) for value in fine)
                    relative_change = change / max(scale, 1e-10)
                    tolerance = max(IG_ABSOLUTE_TOLERANCE, IG_RELATIVE_TOLERANCE * abs(diagnostics["targetDifference"]))
                    diagnostics.update(requestedSteps=steps, comparedSteps=[steps, fine_steps],
                                       convergenceL1RelativeChange=relative_change, convergenceTested=fine_steps > steps,
                                       completenessTolerance=tolerance, completenessPassed=abs(diagnostics["completenessResidual"]) <= tolerance,
                                       convergencePassed=fine_steps > steps and relative_change <= IG_CONVERGENCE_TOLERANCE,
                                       originalForwardMaximumAbsoluteDifference=difference)
                    graph_scores = fine[0].sum(-1).detach().cpu().numpy().reshape(-1)
                    bit_scores = fine[1].detach().cpu().numpy().reshape(-1)
                    fp_scores, bit_records = morgan_to_atoms(molecule, bit_scores, fingerprint_size=fingerprint.shape[1],
                                                           expected_fingerprint=fingerprint.detach().cpu().numpy().reshape(-1))
                    atom_scores = graph_scores + fp_scores
            except torch.cuda.OutOfMemoryError:
                raise AttributionUnavailable("ATTRIBUTION_OOM", "归因内存不足；减少步数或分子大小，不会生成替代解释。") from None
            after = self.local_client.predict(canonical, structure_id)
            if before["endpoints"] != after["endpoints"] or any(before["provenance"].get(key) != after["provenance"].get(key) for key in ("weightsSha256", "artifactSha256", "modelVersion")):
                raise AttributionUnavailable("ATTRIBUTION_PREDICTION_CHANGED", "归因后预测发生变化，拒绝发布解释。")
            atoms = [{"index": atom.GetIdx(), "element": atom.GetSymbol(), "contribution": float(atom_scores[atom.GetIdx()]),
                      "graphContribution": float(graph_scores[atom.GetIdx()]), "morganContribution": float(fp_scores[atom.GetIdx()])}
                     for atom in molecule.GetAtoms()]
            return {"status": "available" if diagnostics["completenessPassed"] and diagnostics["convergencePassed"] else "computed-with-warning",
                    "method": "joint-embedding-morgan-integrated-gradients", "endpoint": endpoint, "canonicalSmiles": canonical,
                    "structureId": structure_id, "score": before["endpoints"][index]["score"], "unit": "calibrated logit contribution; not probability change",
                    "atoms": atoms, "fragments": group_contributions(molecule, atom_scores), "morganBits": bit_records,
                    "imageDataUrl": heatmap_png(molecule, atom_scores), "diagnostics": diagnostics,
                    "provenance": {"attributionVersion": ATTRIBUTION_VERSION, "attributionSourceSha256": source_sha256(),
                                   "modelName": manifest["modelName"], "modelVersion": manifest["modelVersion"],
                                   "artifactSha256": loaded["manifestSha256"], "weightsSha256": manifest["artifacts"]["weights"]["sha256"],
                                   "endpoint": endpoint, "calibrationTemperature": temperature, "calibrationBias": bias,
                                   "baseline": "zero atom embeddings and zero Morgan; CLS, graph distances, bonds and mask held fixed",
                                   "torchVersion": str(torch.__version__), "rdkitVersion": rdBase.rdkitVersion,
                                   "parameterGradientsAccumulated": False, "predictionBeforeAfterIdentical": True},
                    "limitations": ["Integrated Gradients explains this model relative to an artificial baseline; it is not a causal chemical intervention.",
                                    "Graph geometry/CLS are fixed and not attributed; calibration bias cancels in the difference.",
                                    "Morgan repeated/collided environments share bit credit equally; atom allocation is a mapping convention.",
                                    "Functional groups sum these signed neural contributions and may overlap; their sums are not additive across groups.",
                                    "Red raises and blue lowers the selected endpoint logit; colors do not prove mechanisms or clinical safety.",
                                    "Inspect completeness and step convergence; computed-with-warning is not a numerically converged explanation."]}

    def _fragment(self, canonical, molecule, endpoint):
        client = self.research_client
        if client is None:
            raise AttributionUnavailable("ATTRIBUTION_MODEL_UNAVAILABLE", "未配置真实研究模型，不能计算片段删除解释。")
        with client._lock:
            original = client.predict_all(canonical)
            scores = {row["id"]: row["score"] for row in original["endpoints"]}
            if endpoint not in scores:
                raise AttributionUnavailable("ATTRIBUTION_ENDPOINT_UNSUPPORTED", "研究模型未实际训练该端点。")
            fragments, skipped = [], []
            for proposal in legal_fragment_deletions(canonical):
                try:
                    variant = client.predict_all(proposal["perturbedSmiles"])
                    if any(original["provenance"].get(key) != variant["provenance"].get(key) for key in ("weightsSha256", "manifestSha256", "modelVersion")):
                        raise AttributionUnavailable("ATTRIBUTION_MODEL_CHANGED", "片段重算模型快照变化。")
                    variant_scores = {row["id"]: row["score"] for row in variant["endpoints"]}
                    if endpoint not in variant_scores:
                        raise ValueError("Endpoint unavailable")
                    fragments.append({**proposal, "originalScore": scores[endpoint], "perturbedScore": variant_scores[endpoint],
                                      "delta": scores[endpoint] - variant_scores[endpoint], "unit": "original minus perturbed model score"})
                except AttributionUnavailable:
                    # A changed model snapshot invalidates the entire comparison.
                    raise
                except Exception:
                    skipped.append({**proposal, "status": "unsupported-perturbation"})
            if not fragments:
                raise AttributionUnavailable("ATTRIBUTION_NO_LEGAL_PERTURBATIONS", "没有可通过化学及真实模型输入校验的删除片段。")
            after = client.predict_all(canonical)
            if original["endpoints"] != after["endpoints"] or any(original["provenance"].get(key) != after["provenance"].get(key) for key in ("weightsSha256", "manifestSha256", "modelVersion")):
                raise AttributionUnavailable("ATTRIBUTION_PREDICTION_CHANGED", "片段解释后原分子预测发生变化。")
            fragments.sort(key=lambda item: abs(item["delta"]), reverse=True)
            highlight = [0.0] * molecule.GetNumAtoms()
            for atom_index in fragments[0]["removedAtomIndices"]:
                highlight[atom_index] = fragments[0]["delta"]
            return {"status": "available", "method": "legal-fragment-deletion-model-rerun", "endpoint": endpoint,
                    "canonicalSmiles": canonical, "score": scores[endpoint], "atoms": [], "fragments": fragments,
                    "imageDataUrl": heatmap_png(molecule, highlight), "skippedPerturbations": skipped,
                    "provenance": {**original["provenance"], "attributionVersion": ATTRIBUTION_VERSION,
                                   "attributionSourceSha256": source_sha256(), "maximumPerturbations": MAX_FRAGMENT_RERUNS,
                                   "predictionBeforeAfterIdentical": True, "imageMeaning": "largest-absolute-delta removed fragment highlight; not per-atom attribution"},
                    "limitations": ["Deletion changes the molecule; valid sanitized fragments can still be outside the training domain.",
                                    "Delta is a finite model perturbation response, not an additive atom attribution or causal mechanism.",
                                    "Fragments can overlap; their deltas must not be summed into a total risk.",
                                    "The original input and original endpoint score are preserved; no attention-as-causality or SMARTS score substitution."]}
