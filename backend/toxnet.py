"""AIDD's own graph-aware molecular Transformer, trained from random weights."""
from __future__ import annotations

import math
import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from rdkit import Chem, DataStructs
from rdkit.Chem import rdFingerprintGenerator
from backend.chemistry import canonicalize_smiles

MODEL_CODE_VERSION = "aidd.graph-transformer.v2"
DEFAULT_ARCHITECTURE = {
    "id": MODEL_CODE_VERSION, "name": "Graph-aware molecular Transformer",
    "hiddenSize": 192, "layers": 4, "heads": 6, "feedForward": 768,
    "fingerprintSize": 2048, "dropout": 0.15, "maxAtoms": 200,
    "input": "RDKit atom graph + shortest-distance/bond attention bias + Morgan fingerprint",
    "description": "从随机权重训练的分子专用 Transformer；非十亿级语言模型。",
}
ATOM_CARDINALITIES = (119, 9, 7, 6, 9, 2, 2, 9, 301)


def featurize_smiles(smiles: str, architecture: dict) -> dict[str, torch.Tensor]:
    canonical = canonicalize_smiles(smiles)
    mol = Chem.MolFromSmiles(canonical)
    count = mol.GetNumAtoms()
    if count > architecture.get("maxAtoms", 200):
        raise ValueError("分子超过本地模型的原子数上限。")
    atoms = [[0] * len(ATOM_CARDINALITIES)]
    for atom in mol.GetAtoms():
        if atom.GetIsotope() > 300:
            raise ValueError("本地模型仅支持质量数不超过 300 的同位素。")
        atoms.append([
            atom.GetAtomicNum(), max(0, min(8, atom.GetFormalCharge() + 4)),
            min(6, atom.GetDegree()), min(5, atom.GetTotalNumHs()),
            min(8, int(atom.GetHybridization())), int(atom.GetIsAromatic()),
            int(atom.IsInRing()), min(8, int(atom.GetChiralTag())), atom.GetIsotope(),
        ])
    distances = np.full((count + 1, count + 1), 10, dtype=np.int64)
    distances[1:, 1:] = np.minimum(Chem.GetDistanceMatrix(mol), 9).astype(np.int64)
    bonds = np.full((count + 1, count + 1), 5, dtype=np.int64)
    bonds[1:, 1:] = 0
    for bond in mol.GetBonds():
        kind = 4 if bond.GetIsAromatic() else {1.0: 1, 2.0: 2, 3.0: 3}.get(bond.GetBondTypeAsDouble(), 0)
        i, j = bond.GetBeginAtomIdx() + 1, bond.GetEndAtomIdx() + 1
        bonds[i, j] = bonds[j, i] = kind
    generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=architecture.get("fingerprintSize", 2048), includeChirality=True)
    fingerprint = np.zeros(architecture.get("fingerprintSize", 2048), dtype=np.float32)
    DataStructs.ConvertToNumpyArray(generator.GetFingerprint(mol), fingerprint)
    return {
        "atoms": torch.tensor(atoms, dtype=torch.long).unsqueeze(0),
        "distances": torch.from_numpy(distances).unsqueeze(0),
        "bonds": torch.from_numpy(bonds).unsqueeze(0),
        "mask": torch.ones(1, count + 1, dtype=torch.bool),
        "fingerprint": torch.from_numpy(fingerprint).unsqueeze(0),
    }


def collate_features(records: list[dict[str, torch.Tensor]]) -> dict[str, torch.Tensor]:
    batch, length = len(records), max(record["atoms"].shape[1] for record in records)
    out = {
        "atoms": torch.zeros(batch, length, len(ATOM_CARDINALITIES), dtype=torch.long),
        "distances": torch.full((batch, length, length), 11, dtype=torch.long),
        "bonds": torch.full((batch, length, length), 6, dtype=torch.long),
        "mask": torch.zeros(batch, length, dtype=torch.bool),
        "fingerprint": torch.cat([record["fingerprint"] for record in records]),
    }
    for index, record in enumerate(records):
        size = record["atoms"].shape[1]
        out["atoms"][index, :size] = record["atoms"][0]
        out["distances"][index, :size, :size] = record["distances"][0]
        out["bonds"][index, :size, :size] = record["bonds"][0]
        out["mask"][index, :size] = True
    return out


class GraphAttentionBlock(nn.Module):
    def __init__(self, width, heads, feed_forward, dropout):
        super().__init__()
        self.heads, self.width, self.dropout = heads, width, dropout
        self.norm1 = nn.LayerNorm(width)
        self.qkv = nn.Linear(width, width * 3)
        self.project = nn.Linear(width, width)
        self.norm2 = nn.LayerNorm(width)
        self.mlp = nn.Sequential(nn.Linear(width, feed_forward), nn.GELU(), nn.Dropout(dropout), nn.Linear(feed_forward, width))
        self.residual_dropout = nn.Dropout(dropout)

    def forward(self, value, bias):
        batch, length, width = value.shape
        qkv = self.qkv(self.norm1(value)).view(batch, length, 3, self.heads, width // self.heads).permute(2, 0, 3, 1, 4)
        attended = F.scaled_dot_product_attention(*qkv.unbind(0), attn_mask=bias, dropout_p=self.dropout if self.training else 0.0)
        value = value + self.residual_dropout(self.project(attended.transpose(1, 2).reshape(batch, length, width)))
        return value + self.residual_dropout(self.mlp(self.norm2(value)))


class MolecularTransformer(nn.Module):
    def __init__(self, architecture):
        super().__init__()
        self.architecture = architecture
        width, heads = architecture["hiddenSize"], architecture["heads"]
        if width % heads:
            raise ValueError("hiddenSize must be divisible by heads")
        self.atom_embeddings = nn.ModuleList(nn.Embedding(size, width) for size in ATOM_CARDINALITIES)
        self.cls = nn.Parameter(torch.zeros(1, 1, width))
        self.distance_bias = nn.Embedding(12, heads)
        self.bond_bias = nn.Embedding(7, heads)
        self.blocks = nn.ModuleList(GraphAttentionBlock(width, heads, architecture["feedForward"], architecture["dropout"]) for _ in range(architecture["layers"]))
        self.graph_norm = nn.LayerNorm(width)
        self.fingerprint_encoder = nn.Sequential(nn.Linear(architecture["fingerprintSize"], width), nn.GELU(), nn.Dropout(architecture["dropout"]), nn.Linear(width, width), nn.GELU())
        self.head = nn.Sequential(nn.Linear(width * 2, width), nn.GELU(), nn.Dropout(architecture["dropout"]), nn.Linear(width, 4))
        nn.init.normal_(self.cls, std=0.02)

    def forward(self, atoms, distances, bonds, mask, fingerprint):
        value = sum(embedding(atoms[..., i]) for i, embedding in enumerate(self.atom_embeddings)) / math.sqrt(len(self.atom_embeddings))
        value = torch.cat((self.cls.expand(value.shape[0], -1, -1), value[:, 1:]), dim=1)
        bias = (self.distance_bias(distances) + self.bond_bias(bonds)).permute(0, 3, 1, 2)
        bias = bias.masked_fill(~mask[:, None, None, :], float("-inf"))
        for block in self.blocks:
            value = block(value, bias)
        graph = self.graph_norm(value[:, 0])
        return self.head(torch.cat((graph, self.fingerprint_encoder(fingerprint)), dim=-1))


def build_model(architecture: dict | None = None) -> MolecularTransformer:
    settings = {**DEFAULT_ARCHITECTURE, **(architecture or {})}
    if settings["id"] != MODEL_CODE_VERSION:
        raise ValueError("Unsupported local model architecture")
    return MolecularTransformer(settings)


def predict_logits(model: nn.Module, features: dict[str, torch.Tensor]) -> torch.Tensor:
    return model(**features)
