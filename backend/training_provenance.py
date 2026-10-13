"""Verify historical training bytes without executing them or rewriting receipts."""
import hashlib
from pathlib import Path
import re
import subprocess


def _git_bytes(root, *arguments):
    try:
        return subprocess.run(['git', *arguments], cwd=root, capture_output=True, check=True).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('Immutable historical Git source bytes unavailable; fetch the recorded commit explicitly') from error


def verify_training_sources(records, root, *, commit=None):
    root = Path(root).resolve()
    if not isinstance(records, dict) or not records:
        raise ValueError('Nonempty recorded training source hashes required')
    if commit is not None:
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise ValueError('Training source reference must be a full immutable Git commit SHA')
        if _git_bytes(root, 'cat-file', '-t', commit).strip() != b'commit':
            raise ValueError('Training source reference is not a commit')
    verified = []
    for name, expected in records.items():
        if not isinstance(name, str) or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('Invalid recorded source path/hash')
        target = (root / name).resolve()
        if Path(name).is_absolute() or not target.is_relative_to(root) or target == root:
            raise ValueError('Training source path escapes repository')
        relative = target.relative_to(root).as_posix()
        if commit is None:
            data = target.read_bytes()
        else:
            # Read-only Git blob; historical Python is never imported or run.
            data = _git_bytes(root, 'show', f'{commit}:{relative}')
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise ValueError(f'Recorded training bytes mismatch: {relative}')
        runtime = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        verified.append({'file': relative, 'trainingSha256': actual, 'runtimeSha256': runtime,
                         'runtimeDiffersFromTraining': runtime != actual})
    return {'mode': 'current-checkout-bytes' if commit is None else 'immutable-git-blob-bytes',
            'commit': commit, 'historicalCodeExecuted': False, 'files': verified}
