"""Immutable startup source receipts, never an attestation of scientific accuracy."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def capture_runtime(root=ROOT):
    root = Path(root)
    files = sorted([*root.glob('backend/compare_*.py'), root / 'backend/main.py',
                    root / 'backend/chemistry.py', root / 'backend/multitask_model.py'])
    hashes = {file.relative_to(root).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
              for file in files if file.is_file()}
    try:
        options = {'cwd': root, 'capture_output': True, 'text': True, 'timeout': 3}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        revision = subprocess.run(['git', 'rev-parse', 'HEAD'], **options)
        dirty = subprocess.run(['git', 'status', '--porcelain', '--', *hashes], **options)
        commit = revision.stdout.strip() if revision.returncode == 0 else None
        modified = bool(dirty.stdout.strip()) if dirty.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        commit, modified = None, None
    return {'schema': 'aidd.compare.runtime-source.v1', 'capturedAt': datetime.now(timezone.utc).isoformat(),
            'gitCommit': commit, 'sourceFilesDifferFromCommit': modified,
            'sourceSha256': hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'sourceFiles': hashes, 'processId': os.getpid(), 'pythonVersion': sys.version.split()[0],
            'scope': 'startup source-file snapshot; immutable for this service instance; not hardware or scientific validation'}


STARTUP_RUNTIME = capture_runtime()
