import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from backend.training_provenance import verify_training_sources


class TrainingProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.file = self.root / 'model.py'
        self.file.write_bytes(b'# test fixture, never executed\n')
        self.records = {'model.py': hashlib.sha256(self.file.read_bytes()).hexdigest()}
    def tearDown(self): self.temp.cleanup()

    def test_current_bytes_and_changed_source_refusal(self):
        self.assertFalse(verify_training_sources(self.records, self.root)['historicalCodeExecuted'])
        self.file.write_bytes(b'# altered fixture\n')
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            verify_training_sources(self.records, self.root)

    def test_unsafe_paths_and_floating_refs_rejected(self):
        for name in ('../escape.py', str(self.file.resolve())):
            with self.assertRaises(ValueError): verify_training_sources({name:'a'*64}, self.root)
        for ref in ('main', 'abcd', 'a'*64):
            with self.assertRaises(ValueError): verify_training_sources(self.records, self.root, commit=ref)

    @unittest.skipUnless(shutil.which('git'), 'Git required for immutable source audit')
    def test_historical_blob_validated_without_execution_and_runtime_difference_recorded(self):
        def git(*args):
            return subprocess.run(['git', *args], cwd=self.root, capture_output=True, check=True).stdout
        git('init'); git('add', 'model.py')
        git('-c', 'user.name=Test Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture')
        ref = git('rev-parse', 'HEAD').decode().strip()
        self.file.write_bytes(b'raise RuntimeError("fixture must never execute")\n')
        out = verify_training_sources(self.records, self.root, commit=ref)
        self.assertEqual(out['commit'], ref)
        self.assertTrue(out['files'][0]['runtimeDiffersFromTraining'])
        self.assertFalse(out['historicalCodeExecuted'])
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            verify_training_sources({'model.py':'a'*64}, self.root, commit=ref)
