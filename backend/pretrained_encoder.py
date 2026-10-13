"""Verified, frozen MoLFormer features. Runtime is strictly local and offline.

Only the reviewed Hugging Face revision below is supported. No AutoModel,
trust_remote_code, pickle checkpoints, network fetch, or floating revision is
used by this module. Upstream code lives in sources/molformer with Apache-2.0
headers and is hashed before import.
"""
from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
from importlib import metadata, util
import json
from pathlib import Path
import re
import sys
from types import ModuleType
from typing import Sequence

import numpy as np
from rdkit import rdBase

from backend.chemistry import CHEMISTRY_VERSION, canonicalize_smiles

MODEL_SOURCE = "ibm-research/MoLFormer-XL-both-10pct"
MODEL_REVISION = "7b12d946c181a37f6012b9dc3b002275de070314"
WEIGHTS_SHA256 = "0795977fe7192c4acdaf052f0e8464af57bc4bb59211271c5e61aaba2637b9c6"
WEIGHTS_BYTES = 187248784
ENCODER_VERSION = "aidd.molformer-frozen.v1"
SNAPSHOT_SCHEMA = "aidd.pretrained.snapshot.v1"
CACHE_SCHEMA = "aidd.embedding-cache.v1"
EMBEDDING_DIMENSION = 768
MAX_TOKENS = 202  # Includes <bos> and <eos>; exceeding inputs are rejected.
REQUIRED_DEPENDENCIES = {"transformers": "4.57.6", "tokenizers": "0.22.2", "safetensors": "0.6.2"}
SOURCE_DIR = Path(__file__).resolve().parents[1] / "sources" / "molformer"
FILE_HASHES = {
    "README.md": "4e679b138ae5c29c17de907d3e3a03050b3eb2a82462917ad80fb6a5ffe63736",
    "config.json": "3ef9eaac8c7ca6282fd6256ed038d151bd4ff42a4ff855367e0d7197bbc1c284",
    "configuration_molformer.py": "d48f6c133b27c3d6d9922acc554c2203aedf1a56d2061bd7aae72c11b7ec7aa5",
    "modeling_molformer.py": "a3f6273bb44709e566ee02987c50372db727de21799c3d297f64b2437f9e32a8",
    "special_tokens_map.json": "f0bb97cedb7e5d9eb744a3b2e499a9c464659e1a363853bfd6056ba126f5c16c",
    "tokenization_molformer.py": "a816f7b6e540f27544e7aa924104d6fc0d5cedcc62117ec9d31d35d0cefbd2e2",
    "tokenization_molformer_fast.py": "1921cf245ceb613a19e9a30ba4aa33e02b328ac6fdbbf63080b775bd8b4c8bdb",
    "tokenizer.json": "3df1f2219653c44fac9fa03b7f788b372eb2544ecc176737bb9aca8411b471a5",
    "tokenizer_config.json": "17e4fb4c6f77f2d9c482cf54fce528d97635e6bb9d0e2ab8b9638cc8a52e6fae",
    "vocab.json": "8592dced2879cb164332b9ec14e99164ba017949a6c05344cb64163d4839dbb2",
}


class EncoderError(ValueError):
    """Missing, incompatible, or unverified encoder inputs/artifacts."""


def file_sha256(path: Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_digest(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def verify_file(path: Path, expected: str) -> None:
    if not Path(path).is_file():
        raise EncoderError(f"Missing pretrained file: {path}; run scripts/fetch_pretrained_encoder.py explicitly.")
    if file_sha256(path) != expected:
        raise EncoderError(f"SHA256 verification failed: {path.name}")


def verify_reviewed_sources() -> None:
    for name, expected in FILE_HASHES.items():
        verify_file(SOURCE_DIR / name, expected)


@lru_cache(maxsize=1)
def _reviewed_tokenizer_contract():
    verify_file(SOURCE_DIR / "tokenizer.json", FILE_HASHES["tokenizer.json"])
    data = json.loads((SOURCE_DIR / "tokenizer.json").read_text(encoding="utf-8"))
    if data["normalizer"] is not None or data["model"]["type"] != "WordLevel":
        raise EncoderError("Unsupported tokenizer normalization or vocabulary type")
    return re.compile(data["pre_tokenizer"]["pretokenizers"][0]["pattern"]["Regex"]), data["model"]["vocab"]


def validate_encoder_smiles(smiles: str) -> str:
    """Label-blind admissibility check, usable without downloaded weights/GPU.

    Canonical isomeric RDKit identity is retained, including salts/isotopes. No
    parent stripping, neutralization, isotope removal, or stereochemistry removal
    occurs. Unknown tokens, discarded syntax, and long sequences are errors.
    """
    canonical = canonicalize_smiles(smiles)
    pattern, vocab = _reviewed_tokenizer_contract()
    tokens = pattern.findall(canonical)
    if "".join(tokens) != canonical:
        raise EncoderError("Tokenizer would discard SMILES syntax; input is unsupported")
    unknown = sorted({token for token in tokens if token not in vocab})
    if unknown:
        raise EncoderError(f"MoLFormer vocabulary does not support tokens: {', '.join(unknown)}")
    if len(tokens) + 2 > MAX_TOKENS:
        raise EncoderError(f"MoLFormer input has {len(tokens) + 2} tokens; maximum is {MAX_TOKENS}. Truncation is forbidden.")
    return canonical


def validate_snapshot(snapshot_dir: Path, *, revision: str = MODEL_REVISION) -> dict:
    if revision != MODEL_REVISION:
        raise EncoderError("Unsupported pretrained model revision; review and pin it before use")
    snapshot = Path(snapshot_dir)
    receipt_path = snapshot / "snapshot.json"
    if not receipt_path.is_file():
        raise EncoderError("Pretrained snapshot receipt missing; explicit verified download/import is required")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("schema") != SNAPSHOT_SCHEMA or receipt.get("source") != MODEL_SOURCE or receipt.get("revision") != MODEL_REVISION:
        raise EncoderError("Unsupported pretrained snapshot source/schema/revision")
    if receipt.get("status") != "verified" or receipt.get("files") != {**FILE_HASHES, "model.safetensors": WEIGHTS_SHA256}:
        raise EncoderError("Snapshot is incomplete or not verified against the pinned file allowlist")
    for name, digest in receipt["files"].items():
        verify_file(snapshot / name, digest)
    if (snapshot / "model.safetensors").stat().st_size != WEIGHTS_BYTES:
        raise EncoderError("Pretrained weight file size mismatch")
    verify_reviewed_sources()
    return receipt


def _check_dependencies() -> dict:
    versions = {}
    for name, expected in REQUIRED_DEPENDENCIES.items():
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError as error:
            raise EncoderError(f"Missing v3 dependency {name}; install requirements-v3.txt in the isolated v3 environment") from error
        if installed != expected:
            raise EncoderError(f"Unsupported {name} version {installed}; reviewed version is {expected}")
        versions[name] = installed
    return versions


def _load_reviewed_classes():
    """Load the exact verified paths in an isolated package, avoiding shadowing.

    The package is created without executing an __init__ file. Only the two
    audited Python files execute; their relative imports stay in this package.
    """
    verify_reviewed_sources()
    package_name = "_aidd_verified_molformer"
    package = ModuleType(package_name)
    package.__path__ = [str(SOURCE_DIR)]
    sys.modules[package_name] = package
    modules = []
    for stem in ("configuration_molformer", "modeling_molformer"):
        name = f"{package_name}.{stem}"
        specification = util.spec_from_file_location(name, SOURCE_DIR / f"{stem}.py")
        module = util.module_from_spec(specification)
        sys.modules[name] = module
        specification.loader.exec_module(module)
        modules.append(module)
    return modules


class PretrainedEncoder:
    def __init__(self, snapshot_dir: Path, device: str = "cpu", *, revision: str = MODEL_REVISION):
        import torch
        if device != "cpu" and not re.fullmatch(r"cuda(?::\d+)?", device):
            raise EncoderError("Device must be cpu or cuda[:index]")
        if device.startswith("cuda") and not torch.cuda.is_available():
            raise EncoderError("CUDA is unavailable in this PyTorch build/driver; use --device cpu or install the correct CUDA wheel")
        versions = _check_dependencies()
        receipt = validate_snapshot(snapshot_dir, revision=revision)
        # Imports use reviewed project files, never snapshot Python or HF dynamic modules.
        configuration, modeling = _load_reviewed_classes()
        from safetensors.torch import load_file
        from transformers import PreTrainedTokenizerFast
        settings = json.loads((Path(snapshot_dir) / "config.json").read_text(encoding="utf-8"))
        settings["deterministic_eval"] = True
        self.model = modeling.MolformerModel(configuration.MolformerConfig(**settings))
        tensors = load_file(str(Path(snapshot_dir) / "model.safetensors"), device="cpu")
        # The checkpoint is MaskedLM; remove only its verified task-specific LM head.
        if any(not (key.startswith("molformer.") or key.startswith("lm_head.")) for key in tensors):
            raise EncoderError("Unexpected pretrained checkpoint tensor namespace")
        encoder_tensors = {key[len("molformer."):]: value for key, value in tensors.items() if key.startswith("molformer.")}
        # Strict loading also verifies learned feature-map buffers, not random substitutes.
        self.model.load_state_dict(encoder_tensors, strict=True)
        del tensors, encoder_tensors
        self.model.eval().requires_grad_(False)
        self.device = device
        try:
            self.model.to(device=device, dtype=torch.float32)
        except torch.cuda.OutOfMemoryError as error:
            raise EncoderError("CUDA memory insufficient for the frozen encoder; use cpu or free GPU memory") from error
        self.tokenizer = PreTrainedTokenizerFast(
            tokenizer_file=str(Path(snapshot_dir) / "tokenizer.json"),
            bos_token="<bos>", eos_token="<eos>", cls_token="<bos>", sep_token="<eos>",
            pad_token="<pad>", unk_token="<unk>", mask_token="<mask>", model_max_length=MAX_TOKENS,
        )
        self.provenance = {
            "source": MODEL_SOURCE, "revision": MODEL_REVISION,
            "weightsSha256": WEIGHTS_SHA256, "files": receipt["files"],
            "dimension": EMBEDDING_DIMENSION, "encoderVersion": ENCODER_VERSION,
            "frozenParameterCount": sum(parameter.numel() for parameter in self.model.parameters()),
            "trainableParameterCount": 0, "chemistryVersion": CHEMISTRY_VERSION,
            "rdkitVersion": rdBase.rdkitVersion, "precision": "float32",
            "pooling": "upstream attention-mask mean including BOS/EOS",
            "maxTokens": MAX_TOKENS, "truncation": "reject", "deterministicEval": True,
            "dependencies": {**versions, "torch": torch.__version__},
            "pretrainingStereochemistry": "upstream pretraining removed isomeric information; no demonstrated stereochemical performance",
        }

    def _prepare_inputs(self, smiles: Sequence[str]):
        canonical = [validate_encoder_smiles(value) for value in smiles]
        # Additional actual-tokenizer checks guard regex/vocabulary mismatch.
        for value in canonical:
            tokens = self.tokenizer.tokenize(value)
            if "".join(tokens) != value or self.tokenizer.unk_token in tokens:
                raise EncoderError("Actual tokenizer cannot preserve this canonical input")
        inputs = self.tokenizer(canonical, padding=True, truncation=False, return_tensors="pt", return_token_type_ids=False)
        if inputs["input_ids"].shape[1] > MAX_TOKENS:
            raise EncoderError("Actual tokenizer exceeds maximum tokens; truncation is forbidden")
        if (inputs["input_ids"] == self.tokenizer.unk_token_id).any():
            raise EncoderError("Actual tokenizer emitted an unsupported <unk> token")
        return {key: value.to(self.device) for key, value in inputs.items() if key in ("input_ids", "attention_mask")}

    def encode(self, smiles: Sequence[str], batch_size: int = 8) -> np.ndarray:
        import torch
        if batch_size < 1:
            raise EncoderError("batch_size must be positive")
        if not smiles:
            return np.empty((0, EMBEDDING_DIMENSION), dtype=np.float32)
        # Validate all inputs before any expensive inference; no silent dropping.
        for value in smiles:
            validate_encoder_smiles(value)
        outputs = []
        try:
            with torch.inference_mode():
                for start in range(0, len(smiles), batch_size):
                    inputs = self._prepare_inputs(smiles[start:start + batch_size])
                    result = self.model(**inputs, return_dict=True).pooler_output
                    outputs.append(result.detach().to(device="cpu", dtype=torch.float32).numpy())
        except torch.cuda.OutOfMemoryError as error:
            raise EncoderError("CUDA memory insufficient while embedding; reduce --batch-size or use --device cpu") from error
        values = np.concatenate(outputs, axis=0)
        if values.shape != (len(smiles), EMBEDDING_DIMENSION) or not np.isfinite(values).all():
            raise EncoderError("Invalid encoder feature shape or non-finite values")
        return values


def _cache_contract(smiles, provenance, source_hash):
    if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise EncoderError("Embedding cache requires the input data SHA256")
    if provenance.get("source") != MODEL_SOURCE or provenance.get("revision") != MODEL_REVISION or provenance.get("weightsSha256") != WEIGHTS_SHA256:
        raise EncoderError("Cache provenance does not describe the supported verified encoder")
    if provenance.get("dimension") != EMBEDDING_DIMENSION or provenance.get("encoderVersion") != ENCODER_VERSION or provenance.get("chemistryVersion") != CHEMISTRY_VERSION or provenance.get("rdkitVersion") != rdBase.rdkitVersion:
        raise EncoderError("Cache feature dimension/encoder/chemistry version mismatch")
    canonical = [validate_encoder_smiles(value) for value in smiles]
    return {
        "schema": CACHE_SCHEMA, "sourceHash": source_hash, "inputOrderHash": _json_digest(list(smiles)),
        "canonicalSmiles": canonical, "canonicalOrderHash": _json_digest(canonical),
        "encoder": provenance, "shape": [len(smiles), EMBEDDING_DIMENSION], "dtype": "float32",
    }


def load_embedding_cache(path: Path, smiles: Sequence[str], expected_provenance: dict, *, source_hash: str) -> np.ndarray:
    path = Path(path)
    sidecar = Path(str(path) + ".json")
    if not path.is_file() or not sidecar.is_file():
        raise EncoderError("Embedding cache or metadata missing")
    expected = _cache_contract(smiles, expected_provenance, source_hash)
    recorded = json.loads(sidecar.read_text(encoding="utf-8"))
    for key, value in expected.items():
        if recorded.get(key) != value:
            raise EncoderError(f"Embedding cache contract mismatch: {key}")
    if recorded.get("cacheKey") != _json_digest(expected):
        raise EncoderError("Embedding cache key mismatch")
    verify_file(path, recorded.get("fileSha256", ""))
    with np.load(path, allow_pickle=False) as data:
        if set(data.files) != {"embeddings"}:
            raise EncoderError("Unexpected embedding cache arrays")
        values = data["embeddings"]
    if list(values.shape) != expected["shape"] or values.dtype != np.float32 or not np.isfinite(values).all():
        raise EncoderError("Embedding cache shape/dtype/finite-value mismatch")
    return values


def cache_embeddings(path: Path, smiles: Sequence[str], encoder: PretrainedEncoder, *, source_hash: str, batch_size: int = 8) -> np.ndarray:
    path = Path(path)
    contract = _cache_contract(smiles, encoder.provenance, source_hash)
    sidecar = Path(str(path) + ".json")
    if path.exists() or sidecar.exists():
        return load_embedding_cache(path, smiles, encoder.provenance, source_hash=source_hash)
    values = encoder.encode(smiles, batch_size=batch_size)
    if values.shape != tuple(contract["shape"]) or values.dtype != np.float32 or not np.isfinite(values).all():
        raise EncoderError("Invalid embeddings; cache was not written")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(str(path) + ".partial")
    with temporary.open("wb") as handle:
        np.savez_compressed(handle, embeddings=values)
    temporary.replace(path)
    sidecar.write_text(json.dumps({**contract, "cacheKey": _json_digest(contract), "fileSha256": file_sha256(path)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return values
