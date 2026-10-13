"""Small structured diagnostic allowlist: never request/response/data logging."""
import json
import logging
import math
import re

LOGGER = logging.getLogger('aidd.compare')


def configure_diagnostics():
    if not LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(message)s'))
        LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False


def diagnostic(*, request_id=None, run_id=None, model_id=None, stage, error_code=None,
               duration_ms=None, cleanup_state=None, provider_request_attempted=False):
    def identifier(value, size=100):
        return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,' + str(size) + '}', value) else None
    value = {'eventSchema': 'aidd.compare.diagnostic.v1', 'requestId': identifier(request_id),
             'runId': identifier(run_id), 'modelId': identifier(model_id), 'stage': identifier(stage),
             'errorCode': identifier(error_code), 'durationMs': duration_ms if type(duration_ms) in (int, float) and math.isfinite(duration_ms) and duration_ms >= 0 else None,
             'providerRequestAttempted': provider_request_attempted is True}
    if isinstance(cleanup_state, dict):
        value['cleanupState'] = {'state': identifier(cleanup_state.get('state')),
                                 'pending': cleanup_state.get('pending') is True,
                                 'confirmed': cleanup_state.get('confirmed') is True,
                                 'errorCode': identifier(cleanup_state.get('errorCode'))}
    else:
        value['cleanupState'] = None
    LOGGER.info(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
    return value
