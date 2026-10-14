"""Optional, bounded nvidia-smi board sampling; never a model VRAM estimate.

This reads the NVIDIA driver only. It neither imports torch nor manages GPU
processes. Memory includes the desktop and other applications; sampled maxima
can miss short peaks and must never be called an exact model memory peak.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import math
import subprocess
import threading
import time


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _number(value):
    try:
        result = float(value.strip())
    except (ValueError, TypeError, AttributeError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def parse_gpu_csv(text):
    """Parse numeric GPU fields without retaining model names or local paths."""
    output = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) != 5:
            continue
        index, _uuid, used, total, utilization = (field.strip() for field in row)
        if not index.isdigit():
            continue
        used, total, utilization = _number(used), _number(total), _number(utilization)
        if used is not None and total is not None and used > total:
            continue
        output.append({'index': int(index), 'memoryUsedMiB': used,
                       'memoryTotalMiB': total,
                       'utilizationPercent': utilization if utilization is None or utilization <= 100 else None})
    return output


def parse_process_csv(text):
    """Counts are background context, not proof of ownership or attribution.

    Windows WDDM frequently returns N/A process memory. Do not convert that to
    zero, and do not retain executable names, paths, or user process IDs.
    """
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) != 3:
            continue
        _uuid, pid, memory = (field.strip() for field in row)
        if pid.isdigit():
            rows.append({'memoryUsedMiB': _number(memory)})
    return {'computeProcessCount': len(rows),
            'processMemoryAvailableCount': sum(row['memoryUsedMiB'] is not None for row in rows)}


class NvidiaSmiSampler:
    """Sample one bounded execution window. No nvidia-smi is a valid outcome.

    The optional runner supports deterministic CPU tests, not benchmark data.
    ``stop`` is idempotent. Queries have bounded timeouts and the daemon thread
    owns no GPU resources. At the sample cap, aggregate existing samples only.
    """
    def __init__(self, *, interval_seconds=0.25, max_samples=7200, runner=None):
        if (type(interval_seconds) not in (int, float) or not math.isfinite(interval_seconds)
                or not 0.05 <= interval_seconds <= 10):
            raise ValueError('GPU sample interval must be within 0.05..10 seconds')
        if type(max_samples) is not int or not 1 <= max_samples <= 36000:
            raise ValueError('GPU sample cap must be within 1..36000')
        self.interval, self.max_samples = float(interval_seconds), max_samples
        self.runner = runner or subprocess.run
        self.samples, self.error_codes = [], set()
        self.attempt_count = 0
        self._stop, self._thread = threading.Event(), None
        self.started_at = self.ended_at = None
        self._started_clock = None
        self._result = None

    def _query(self, query):
        result = self.runner(['nvidia-smi', query, '--format=csv,noheader,nounits'],
                             capture_output=True, text=True, timeout=2, check=False,
                             **({'creationflags': subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, 'CREATE_NO_WINDOW') else {}))
        if result.returncode:
            raise RuntimeError('NVIDIA_SMI_QUERY_FAILED')
        return result.stdout

    def _sample(self):
        self.attempt_count += 1
        try:
            gpus = parse_gpu_csv(self._query('--query-gpu=index,uuid,memory.used,memory.total,utilization.gpu'))
            if not gpus:
                self.error_codes.add('NVIDIA_SMI_NO_VALID_GPU_ROWS')
                return
            processes = {'computeProcessCount': None, 'processMemoryAvailableCount': None}
            try:
                processes = parse_process_csv(self._query('--query-compute-apps=gpu_uuid,pid,used_memory'))
            except (OSError, RuntimeError, subprocess.SubprocessError):
                self.error_codes.add('NVIDIA_SMI_PROCESS_CONTEXT_UNAVAILABLE')
            self.samples.append({'elapsedSeconds': time.perf_counter() - self._started_clock,
                                 'gpus': gpus, **processes})
        except FileNotFoundError:
            self.error_codes.add('NVIDIA_SMI_NOT_INSTALLED')
            self._stop.set()
        except (OSError, RuntimeError, subprocess.SubprocessError):
            self.error_codes.add('NVIDIA_SMI_QUERY_UNAVAILABLE')

    def _loop(self):
        while not self._stop.is_set() and self.attempt_count < self.max_samples:
            self._sample()
            if self._stop.wait(self.interval):
                break

    def start(self):
        if self._thread is not None:
            raise RuntimeError('GPU sampler cannot be started twice')
        self.started_at, self._started_clock = _utc(), time.perf_counter()
        self._thread = threading.Thread(target=self._loop, name='compare-nvidia-smi-sampler', daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._result is not None:
            return self._result
        self._stop.set()
        if self._thread is not None:
            # A GPU query and optional process query each have a 2s timeout.
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                self.error_codes.add('NVIDIA_SMI_SAMPLER_STOP_TIMEOUT')
        self.ended_at = _utc()
        gpus = []
        samples = list(self.samples)
        for index in sorted({row['index'] for sample in samples for row in sample['gpus']}):
            rows = [row for sample in samples for row in sample['gpus'] if row['index'] == index]
            memories = [row['memoryUsedMiB'] for row in rows if row['memoryUsedMiB'] is not None]
            totals = [row['memoryTotalMiB'] for row in rows if row['memoryTotalMiB'] is not None]
            gpus.append({'index': index, 'sampledMaximumUsedMiB': max(memories) if memories else None,
                         'firstObservedUsedMiB': memories[0] if memories else None,
                         'lastObservedUsedMiB': memories[-1] if memories else None,
                         'totalMiB': totals[-1] if totals else None, 'sampleCount': len(rows)})
        process_counts = [sample['computeProcessCount'] for sample in samples if sample['computeProcessCount'] is not None]
        self._result = {'schema': 'aidd.compare.board-memory-sampling.v1',
                        'status': 'sampled' if gpus else 'unavailable', 'measured': bool(gpus),
                        'scope': 'entire GPU board during this window; includes desktop and other applications; not per-model allocation',
                        'startedAt': self.started_at, 'endedAt': self.ended_at,
                        'requestedIntervalSeconds': self.interval, 'sampleCount': len(samples),
                        'attemptCount': self.attempt_count,
                        'memoryMeasured': any(row['sampledMaximumUsedMiB'] is not None for row in gpus),
                        'sampleCapReached': self.attempt_count >= self.max_samples, 'gpus': gpus,
                        'exactPeakBytes': None, 'modelAttributedPeakBytes': None,
                        'samplingLimit': 'sampled maximum only; short peaks may be missed; query time adds to requested interval',
                        'otherProcessContext': {'maximumObservedComputeProcessCount': max(process_counts) if process_counts else None,
                                                'ownership': 'not-attributed; counts include the benchmark and external compute processes',
                                                'graphicsProcessesCovered': False, 'processNamesOrPathsRecorded': False},
                        'errorCodes': sorted(self.error_codes)}
        return self._result

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()
