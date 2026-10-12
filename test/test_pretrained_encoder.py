"""Small contract tests. Synthetic fixtures stay in TemporaryDirectory only."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from rdkit import rdBase
import torch

from backend.chemistry import CHEMISTRY_VERSION
from backend.pretrained_encoder import (
    CACHE_SCHEMA, EMBEDDING_DIMENSION, ENCODER_VERSION, EncoderError, FILE_HASHES,
    MODEL_REVISION, MODEL_SOURCE, PretrainedEncoder, SOURCE_DIR, WEIGHTS_SHA256,
    _check_dependencies, cache_embeddings, file_sha256, load_embedding_cache,
    validate_encoder_smiles, validate_snapshot, verify_file, verify_reviewed_sources,
)


class SyntheticTestEncoder:
    """Contract fixture only. Never used by production extraction or artifacts."""
    def __init__(self):
        self.provenance = {
            "source": MODEL_SOURCE, "revision": MODEL_REVISION, "weightsSha256": WEIGHTS_SHA256,
            "dimension": EMBEDDING_DIMENSION, "encoderVersion": ENCODER_VERSION,
            "chemistryVersion": CHEMISTRY_VERSION, "rdkitVersion": rdBase.rdkitVersion,
            "testFixture": "synthetic-unit-test-only",
        }
        self.calls = 0

    def encode(self, smiles, batch_size=8):
        self.calls += 1
        return np.stack([np.full(EMBEDDING_DIMENSION, len(s), dtype=np.float32) for s in smiles])


class PretrainedContractTests(unittest.TestCase):
    def test_upstream_source_hashes(self):
        verify_reviewed_sources()

    def test_valid_equivalent_smiles_share_canonical(self):
        self.assertEqual(validate_encoder_smiles("OCC"), validate_encoder_smiles("CCO"))
        self.assertEqual(validate_encoder_smiles("C1=CC=CC=C1"), "c1ccccc1")

    def test_invalid_smiles_is_rejected(self):
        for smiles in ("C1CC", "C#", "[C](C)(C)(C)(C)C", "*", "CCO sample"):
            with self.subTest(smiles=smiles), self.assertRaises(ValueError):
                validate_encoder_smiles(smiles)

    def test_salt_is_not_stripped(self):
        self.assertEqual(validate_encoder_smiles("[Na+].CCO"), "CCO.[Na+]")
        self.assertNotEqual(validate_encoder_smiles("CCO.[Na+]"), "CCO")

    def test_stereochemical_identity_is_preserved(self):
        left = validate_encoder_smiles("C[C@H](N)C(=O)O")
        right = validate_encoder_smiles("C[C@@H](N)C(=O)O")
        self.assertNotEqual(left, right)
        self.assertIn("@", left)

    def test_supported_isotopes_are_preserved(self):
        self.assertIn("13", validate_encoder_smiles("[13CH3]CO"))
        self.assertEqual(validate_encoder_smiles("[2H]O[2H]"), "[2H]O[2H]")

    def test_unknown_isotope_token_is_explicit_error(self):
        with self.assertRaisesRegex(EncoderError, "vocabulary"):
            validate_encoder_smiles("[999CH3]C")

    def test_sequence_is_never_truncated(self):
        with self.assertRaisesRegex(EncoderError, "Truncation is forbidden"):
            validate_encoder_smiles("C(=O)" * 80)

    def test_float_revision_is_rejected(self):
        with self.assertRaisesRegex(EncoderError, "Unsupported.*revision"):
            validate_snapshot(Path("missing"), revision="main")

    def test_missing_snapshot_is_error_without_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(EncoderError, "receipt missing"):
                validate_snapshot(Path(directory))

    def test_unverified_snapshot_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p / "snapshot.json").write_text(json.dumps({"schema": "aidd.pretrained.snapshot.v1", "source": MODEL_SOURCE,
                "revision": MODEL_REVISION, "status": "downloading", "files": {}}), encoding="utf-8")
            with self.assertRaisesRegex(EncoderError, "incomplete"):
                validate_snapshot(p)

    def test_sha_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "bad.safetensors"
            p.write_bytes(b"not real weights")
            with self.assertRaisesRegex(EncoderError, "SHA256"):
                verify_file(p, WEIGHTS_SHA256)

    def test_missing_cuda_is_explicit(self):
        with patch("torch.cuda.is_available", return_value=False):
            with self.assertRaisesRegex(EncoderError, "CUDA is unavailable"):
                PretrainedEncoder(Path("missing"), device="cuda")

    def test_unsupported_device(self):
        with self.assertRaisesRegex(EncoderError, "Device must"):
            PretrainedEncoder(Path("missing"), device="mps")

    def test_missing_or_changed_dependency(self):
        from importlib.metadata import PackageNotFoundError
        with patch("backend.pretrained_encoder.metadata.version", side_effect=PackageNotFoundError("transformers")):
            with self.assertRaisesRegex(EncoderError, "Missing v3 dependency"):
                _check_dependencies()
        with patch("backend.pretrained_encoder.metadata.version", return_value="999"):
            with self.assertRaisesRegex(EncoderError, "Unsupported transformers"):
                _check_dependencies()

    def test_empty_encoding_dimension(self):
        encoder = object.__new__(PretrainedEncoder)
        self.assertEqual(encoder.encode([]).shape, (0, EMBEDDING_DIMENSION))
        with self.assertRaisesRegex(EncoderError, "batch_size"):
            encoder.encode(["CCO"], batch_size=0)

    def test_batch_order_and_attention_mask_forwarding(self):
        encoder = object.__new__(PretrainedEncoder)
        encoder.device = "cpu"
        seen = []
        # A small test-only model checks mask and input-order flow, never chemistry predictions.
        def prepare(values):
            seen.extend(values)
            ids = torch.tensor([[len(s), 2] for s in values])
            return {"input_ids": ids, "attention_mask": torch.tensor([[1, 0] for _ in values])}
        def model(input_ids, attention_mask, return_dict):
            self.assertTrue(torch.equal(attention_mask[:, 1], torch.zeros(len(input_ids), dtype=torch.long)))
            self.assertFalse(torch.is_grad_enabled())
            values = input_ids[:, :1].float().expand(-1, EMBEDDING_DIMENSION)
            return SimpleNamespace(pooler_output=values)
        encoder._prepare_inputs = prepare
        encoder.model = model
        smiles = ["CCO", "C", "CCCC", "N"]
        actual = encoder.encode(smiles, batch_size=2)
        self.assertEqual(seen, smiles)
        self.assertEqual(actual[:, 0].tolist(), [3, 1, 4, 1])

    def test_real_tokenizer_json_contract_without_weights(self):
        # Optional installed-package check is run by the v3 environment; no model download.
        try:
            from tokenizers import Tokenizer
        except ImportError:
            self.skipTest("tokenizers v3 optional dependency is not installed")
        tokenizer = Tokenizer.from_file(str(SOURCE_DIR / "tokenizer.json"))
        left = tokenizer.encode(validate_encoder_smiles("CCO"))
        self.assertEqual(left.tokens, ["<bos>", "C", "C", "O", "<eos>"])
        right = tokenizer.encode(validate_encoder_smiles("[13CH3]CO"))
        self.assertIn("[13CH3]", right.tokens)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "synthetic-test-only.npz"
        self.encoder = SyntheticTestEncoder()
        self.smiles = ["CCO", "N", "CCCC"]
        self.source_hash = "1" * 64
        self.values = cache_embeddings(self.path, self.smiles, self.encoder, source_hash=self.source_hash)

    def tearDown(self):
        self.directory.cleanup()

    def test_shape_and_reuse(self):
        self.assertEqual(self.values.shape, (3, 768))
        reread = cache_embeddings(self.path, self.smiles, self.encoder, source_hash=self.source_hash)
        np.testing.assert_array_equal(reread, self.values)
        self.assertEqual(self.encoder.calls, 1)

    def test_order_change_rejected(self):
        with self.assertRaisesRegex(EncoderError, "inputOrderHash"):
            load_embedding_cache(self.path, list(reversed(self.smiles)), self.encoder.provenance, source_hash=self.source_hash)

    def test_equivalent_different_source_spelling_rejected(self):
        with self.assertRaisesRegex(EncoderError, "inputOrderHash"):
            load_embedding_cache(self.path, ["OCC", "N", "CCCC"], self.encoder.provenance, source_hash=self.source_hash)

    def test_data_hash_change_rejected(self):
        with self.assertRaisesRegex(EncoderError, "sourceHash"):
            load_embedding_cache(self.path, self.smiles, self.encoder.provenance, source_hash="2" * 64)

    def test_dimension_chemistry_revision_and_weight_changes_rejected(self):
        for key, replacement in (("dimension", 777), ("chemistryVersion", "another"),
                                 ("revision", "main"), ("weightsSha256", "3" * 64)):
            provenance = deepcopy(self.encoder.provenance)
            provenance[key] = replacement
            with self.subTest(key=key), self.assertRaises(EncoderError):
                load_embedding_cache(self.path, self.smiles, provenance, source_hash=self.source_hash)

    def test_corrupt_cache_file_rejected(self):
        self.path.write_bytes(self.path.read_bytes()[:-2] + b"xx")
        with self.assertRaisesRegex(EncoderError, "SHA256"):
            load_embedding_cache(self.path, self.smiles, self.encoder.provenance, source_hash=self.source_hash)

    def test_wrong_cache_metadata_shape_rejected(self):
        sidecar = Path(str(self.path) + ".json")
        metadata = json.loads(sidecar.read_text())
        metadata["shape"] = [2, 768]
        sidecar.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(EncoderError, "shape"):
            load_embedding_cache(self.path, self.smiles, self.encoder.provenance, source_hash=self.source_hash)

    def test_data_hash_required(self):
        with self.assertRaisesRegex(EncoderError, "input data SHA256"):
            load_embedding_cache(self.path, self.smiles, self.encoder.provenance, source_hash="missing")


if __name__ == "__main__":
    unittest.main()
