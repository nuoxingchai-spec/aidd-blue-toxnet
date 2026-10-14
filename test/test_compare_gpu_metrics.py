"""CPU-only telemetry fixtures, not measurements of any molecular model."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
import unittest

from backend.compare_gpu_metrics import NvidiaSmiSampler, parse_gpu_csv, parse_process_csv


class GpuMetricsTests(unittest.TestCase):
    def test_numeric_csv_and_unavailable_windows_fields(self):
        result = parse_gpu_csv('0, GPU-fixture, 2048, 8192, 45\n1,GPU-other,N/A,16384,[N/A]\n')
        self.assertEqual(result[0]['memoryUsedMiB'], 2048)
        self.assertEqual(result[0]['memoryTotalMiB'], 8192)
        self.assertIsNone(result[1]['memoryUsedMiB'])
        self.assertIsNone(result[1]['utilizationPercent'])
        self.assertNotIn('GPU-fixture', json.dumps(result))

    def test_malformed_nonfinite_and_impossible_rows(self):
        result = parse_gpu_csv('error\nx,uuid,10,20,5\n0,uuid,400,200,5\n1,uuid,nan,100,inf\n')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['index'], 1)
        self.assertIsNone(result[0]['memoryUsedMiB'])

    def test_process_context_never_records_pids_or_names(self):
        result = parse_process_csv('GPU-1, 123456, N/A\nGPU-1,234567,10\nNo running processes found\n')
        self.assertEqual(result, {'computeProcessCount': 2, 'processMemoryAvailableCount': 1})
        self.assertNotIn('123456', json.dumps(result))

    def test_no_driver_is_explicit_unavailable_not_zero(self):
        def absent(*_args, **_kwargs):
            raise FileNotFoundError('fixture missing executable')
        sampler = NvidiaSmiSampler(runner=absent).start()
        sampler._thread.join(timeout=1)
        result = sampler.stop()
        self.assertFalse(result['measured'])
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['exactPeakBytes'])
        self.assertIn('NVIDIA_SMI_NOT_INSTALLED', result['errorCodes'])
        self.assertEqual(sampler.stop(), result)

    def test_bounded_sampler_peak_is_board_only(self):
        def runner(command, **kwargs):
            self.assertEqual(kwargs['timeout'], 2)
            self.assertNotIn('shell', kwargs)
            if command[1].startswith('--query-gpu='):
                return SimpleNamespace(returncode=0, stdout='0,GPU-unit-only,4096,8192,30\n')
            return SimpleNamespace(returncode=0, stdout='GPU-unit-only,123456,N/A\n')
        sampler = NvidiaSmiSampler(runner=runner, max_samples=1, interval_seconds=.05).start()
        sampler._thread.join(timeout=1)
        result = sampler.stop()
        self.assertTrue(result['measured'])
        self.assertEqual(result['gpus'][0]['sampledMaximumUsedMiB'], 4096)
        self.assertTrue(result['sampleCapReached'])
        self.assertIsNone(result['modelAttributedPeakBytes'])
        self.assertIn('other applications', result['scope'])
        self.assertFalse(result['otherProcessContext']['graphicsProcessesCovered'])
        self.assertNotIn('123456', json.dumps(result))

    def test_query_timeout_is_unavailable(self):
        def timeout(command, **_kwargs):
            raise subprocess.TimeoutExpired(command, 2)
        sampler = NvidiaSmiSampler(runner=timeout, max_samples=1)
        # One deterministic sample avoids waiting for a sample cap with no
        # successful rows; a production timeout is bounded by stop().
        sampler._started_clock = 0
        sampler._sample()
        result = sampler.stop()
        self.assertFalse(result['measured'])
        self.assertIn('NVIDIA_SMI_QUERY_UNAVAILABLE', result['errorCodes'])

    def test_invalid_sampling_config_rejected(self):
        for kwargs in ({'interval_seconds': 0}, {'interval_seconds': float('nan')},
                       {'max_samples': True}, {'max_samples': 36001}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                NvidiaSmiSampler(**kwargs)


if __name__ == '__main__':
    unittest.main()
