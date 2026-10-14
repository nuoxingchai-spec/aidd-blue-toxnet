"""Local Compare history with opt-in content, SQLite migrations and one owner.

Construction has no I/O. Call start() from application lifespan, then explicitly
recover_interrupted(); interrupted runs are never automatically re-executed.
The owner lock coordinates users of this database, not other GPU applications.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def default_history_path(workspace_root, *, env=None):
    """Keep Windows OneDrive history out of automatic cloud synchronization.

    This computes a destination only: it neither creates a directory nor moves
    an older database. Explicit operator paths are handled by the caller.
    """
    workspace = Path(workspace_root).expanduser().absolute()
    synchronized = os.name == "nt" and any(part.casefold() == "onedrive" or part.casefold().startswith(("onedrive-", "onedrive "))
                                            for part in workspace.parts)
    if not synchronized:
        return workspace / "data" / "compare" / "runs.sqlite3"
    values = os.environ if env is None else env
    local_data = values.get("LOCALAPPDATA")
    base = Path(local_data).expanduser().absolute() if local_data else Path.home() / "AppData" / "Local"
    workspace_key = hashlib.sha256(str(workspace).casefold().encode("utf-8")).hexdigest()[:12]
    return base / "AIDD-Blue-ToxNet" / "compare" / workspace_key / "runs.sqlite3"


DEFAULT_DB_PATH = default_history_path(ROOT)
SCHEMA_VERSION = 2
STORE_SCHEMA = "aidd.compare.persisted-run.v1"
RESOURCE_STATE_SCHEMA = "aidd.compare.resource-state.v1"
CLEARANCE_SCHEMA = "aidd.compare.quarantine-clearance.v1"
CLEARANCE_SCOPE = "compare-owned-workers-and-configured-loopback-ollama"
MODEL_IDS = ("toxnet-v3-b16", "txgemma-2b-predict", "ollama-qwen-local", "deepseek-api")
TERMINAL = ("completed", "partial", "failed", "cancelled")
RUN_STATES = ("pending", "running", *TERMINAL)
MODEL_STATES = (*RUN_STATES, "unavailable", "unsupported", "skipped")
SETTINGS = ("mode", "primaryPredictor", "specialistPredictor", "specialistTask", "primaryReporter",
            "comparisonReporter", "includeTox21", "includeEvidence", "evidenceEndpoint", "includeAttribution", "explainEndpoint", "ollamaModel")


class ComparePersistenceError(Exception):
    def __init__(self, code, public_message):
        super().__init__(public_message)
        self.code, self.public_message = code, public_message


def _error(code):
    return ComparePersistenceError(code, "本机 Compare 历史存储不可用；未启动额外模型或自动恢复执行。")


def _text(value, limit=4096):
    if not isinstance(value, str):
        return None
    text = value[:limit]
    text = re.sub(r"(?i)\b(?:sk-[A-Za-z0-9_-]{8,}|hf_[A-Za-z0-9]{8,}|Bearer\s+\S+)", "[REDACTED_CREDENTIAL]", text)
    text = re.sub(r"(?i)\b(?:api[_ -]?key|hf[_ -]?token|access[_ -]?token|password|secret)\s*[:=]\s*[^\s,;]+", "[REDACTED_CREDENTIAL]", text)
    text = re.sub(r"(?i)[a-z]:[\\/][^\s\"'<>\]]+", "[REDACTED_LOCAL_PATH]", text)
    text = re.sub(r"(?<![:\w/])/(?:[^\s\"'<>/\]]+/?)+", "[REDACTED_LOCAL_PATH]", text)
    return text


def _blocked(name):
    compact = re.sub(r"[^a-z0-9]", "", name.lower())
    return (any(word in compact for word in ("apikey", "authorization", "password", "secret", "credential", "accesstoken", "hftoken", "cookie"))
            or compact in ("headers", "env", "environment", "messages", "prompt", "rawreport", "python", "executable")
            or compact.endswith(("path", "directory", "dir", "executable")))


def _safe_tree(value, *, depth=0):
    """Sanitize whitelisted content branches, including explicit full mode."""
    if depth > 18:
        return None
    if value is None or type(value) is bool:
        return value
    if type(value) in (int, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if value.startswith("data:image/"):
            if len(value) > 512 * 1024 or not re.fullmatch(r"data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/]*={0,2}", value):
                return None
            return value
        return _text(value)
    if isinstance(value, list):
        return [_safe_tree(item, depth=depth + 1) for item in value[:4096]]
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 160 or _blocked(key):
                continue
            output[key] = _chemical_text(item) if key.lower() in ("smiles", "canonicalsmiles", "perturbedsmiles", "querysmiles") else _safe_tree(item, depth=depth + 1)
        return output
    return None


def _chemical_text(value):
    """Validate identity without applying prose/path redaction to SMILES syntax."""
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        return None
    from backend.chemistry import canonicalize_smiles
    try:
        canonicalize_smiles(value)
    except (ValueError, TypeError):
        return None
    return value


def _pick(value, names):
    if not isinstance(value, dict):
        return {}
    return {name: _chemical_text(value[name]) if name.lower() in ("smiles", "canonicalsmiles", "perturbedsmiles", "querysmiles") else _safe_tree(value[name]) for name in names if name in value}


def _error_summary(value):
    if not isinstance(value, dict):
        return None
    code = value.get("code")
    return {"code": code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_-]{0,79}", code) else "REDACTED_ERROR"}


def _provenance(value):
    output = _pick(value, ("modelName", "modelVersion", "modelId", "revision", "revisionSource", "artifactSchema",
                           "taskContractVersion", "taskContract", "datasetVersion", "weightsSha256", "manifestSha256",
                           "dataManifestSha256", "splitSha256", "snapshotAuditSha256", "templateSha256", "device", "dtype",
                           "parameterCount", "trainableParameterCount", "frozenEncoderParameterCount", "formalAvailability",
                           "researchOnly", "evaluationType", "isolatedWorker", "runtime", "reportOnly", "requestAttempted",
                           "testFixture", "snapshotSha256", "returnedModel", "researchSteps", "independentReport", "cloudTransferAuthorized",
                           "transferredCategories", "rawExperimentalRowsTransferred", "localPathsTransferred", "licenseStatus", "generationOptions",
                           "sourceKind", "sourceRepository", "sourceRevision", "ggufSha256", "quantization",
                           "upstreamRevisionStatus", "upstreamRevision", "baseModelId", "localPackageVerification",
                           "localPackageVerified", "sourceFile", "ggufSizeBytes", "auditSchema", "promptMode", "sourceAuthenticity",
                           "tokenizerIdentity", "modelContextLength", "rawPrompt"))
    if not isinstance(value, dict):
        return output
    if isinstance(value.get("encoder"), dict):
        output["encoder"] = _pick(value["encoder"], ("source", "revision", "weightsSha256", "dimension", "encoderVersion", "chemistryVersion",
                                                     "rdkitVersion", "precision", "frozenParameterCount", "maxTokens", "truncation"))
    if isinstance(value.get("sourceHashes"), dict):
        output["sourceHashes"] = {key: digest for key, digest in value["sourceHashes"].items()
                                  if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_. /-]{1,160}", key)
                                  and not _blocked(key)
                                  and not re.search(r"(?:^[\/]|^[A-Za-z]:|\.\.)", key)
                                  and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)}
    elif isinstance(value.get("sourceHashes"), list):
        output["sourceHashes"] = [_pick(item, ("role", "sha256", "version")) for item in value["sourceHashes"][:128]]
    return output


def _metrics(value):
    output = _pick(value, ("durationMs", "wallSeconds", "queueWaitMs", "loadAndExecutionSeconds", "predictionSeconds", "elapsedSeconds",
                           "loadSeconds", "generationSeconds", "promptTokens", "completionTokens", "inputTokens", "outputTokens",
                           "tokensPerSecond", "totalDurationNs", "loadDurationNs", "promptEvalDurationNs", "evalDurationNs",
                           "device", "dtype", "costStatus", "requestAttempted", "resourceCleanupVerified", "totalTokens",
                           "totalDurationSeconds", "httpStatusCode", "httpStatus", "httpResponseReceived", "usageStatus", "invalidUsageFields",
                           "httpRequestState", "timeoutPhase", "reportValidation", "cleanupState"))
    if isinstance(value, dict):
        output["cost"] = _pick(value["cost"], ("amount", "currency", "source", "providerReported")) if isinstance(value.get("cost"), dict) else None
        output["gpu"] = _pick(value["gpu"], ("name", "peakAllocatedMiB", "peakReservedMiB", "peakAllocatedBytes", "peakReservedBytes",
                                             "scope", "measurementScope", "measured")) if isinstance(value.get("gpu"), dict) else None
    return output


def _report_summary(value):
    if not isinstance(value, dict):
        return None
    output = _pick(value, ("status", "reportSchema", "snapshotSha256", "verifiedFactIds", "sourceVerification"))
    output["sectionCount"] = len(value["sections"]) if isinstance(value.get("sections"), list) else value.get("sectionCount", 0) if type(value.get("sectionCount")) is int else 0
    output["unverifiedAdditionCount"] = len(value["unverifiedAdditions"]) if isinstance(value.get("unverifiedAdditions"), list) else value.get("unverifiedAdditionCount", 0) if type(value.get("unverifiedAdditionCount")) is int else 0
    return output


def _prediction(value):
    if not isinstance(value, dict):
        return None
    output = _pick(value, ("structureId", "scoreKind", "calibration", "applicability", "contractVersion"))
    output["endpoints"] = [_pick(item, ("id", "score", "positiveClass", "assay")) for item in value.get("endpoints", [])[:16]
                           if isinstance(item, dict)]
    output["provenance"] = _provenance(value.get("provenance"))
    return output


def _data_summary(value):
    if not isinstance(value, dict):
        return None
    output = {}
    if "prediction" in value:
        output["prediction"] = _prediction(value["prediction"])
    if isinstance(value.get("tasks"), list):
        output["tasks"] = [_pick(item, ("taskId", "tdcTask", "outputType", "class", "classLabel", "positive", "probability",
                                         "directComparable", "taskContractVersion", "templateSha256")) for item in value["tasks"][:16]]
    if "structureId" in value:
        output["structureId"] = _text(value["structureId"], 160)
    if "report" in value:
        output["report"] = _report_summary(value["report"])
    for name in ("attribution", "evidence", "structuralAlerts"):
        if isinstance(value.get(name), dict):
            output[name] = _pick(value[name], ("status", "method", "errorCode", "rulesVersion", "sourceKind"))
    return output


def project_record(record):
    """Privacy default; this is a history projection, not a resumable request."""
    if not isinstance(record, dict) or not isinstance(record.get("runId"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", record["runId"]):
        raise _error("COMPARE_PERSISTENCE_INVALID_RECORD")
    if record.get("status") not in RUN_STATES:
        raise _error("COMPARE_PERSISTENCE_INVALID_RECORD")
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    sensitive = request.get("persistSensitiveContent") is True or record.get("persistSensitiveContent") is True
    output = _pick(record, ("contractVersion", "runId", "requestId", "status", "createdAt", "updatedAt", "durationMs", "limitations"))
    output.update(persistenceSchema=STORE_SCHEMA, contentSaved=sensitive)
    output["request"] = _pick(request, SETTINGS)
    output["request"].update(persistSensitiveContent=sensitive, allowCloudTransfer=False)
    output["structure"] = _pick(record.get("structure"), ("structureId",))
    if sensitive:
        output["request"].update(_pick(request, ("smiles", "question")))
        output["structure"].update(_pick(record.get("structure"), ("canonicalSmiles",)))
    output["models"] = {}
    for model_id in MODEL_IDS:
        model = (record.get("models") or {}).get(model_id, {})
        if not isinstance(model, dict):
            continue
        state = model.get("status", "pending")
        if state not in MODEL_STATES:
            state = "failed"
        data = model.get("data")
        output["models"][model_id] = {"status": state, "error": _error_summary(model.get("error")),
            "metrics": _metrics(model.get("metrics")), "provenance": _provenance(model.get("provenance")),
            "data": _pick(data, ("prediction", "structuralAlerts", "attribution", "evidence", "tasks", "structureId", "report")) if sensitive and isinstance(data, dict) else _data_summary(data)}
        # Full mode still applies the provenance whitelist to real predictions.
        if sensitive and isinstance(data, dict) and isinstance(data.get("prediction"), dict):
            prediction = output["models"][model_id]["data"]["prediction"]
            prediction["provenance"] = _provenance(data["prediction"].get("provenance"))
    reports = record.get("reports") or {}
    output["reports"] = {identifier: _pick(value, ("status", "reportSchema", "snapshotSha256", "sections", "verifiedFactIds", "unverifiedAdditions",
                                                 "sourceVerification", "limitations")) if sensitive else _report_summary(value)
                         for identifier, value in reports.items() if identifier in MODEL_IDS and isinstance(value, dict)}
    snapshot = record.get("snapshot")
    output["snapshot"] = _pick(snapshot, ("schema", "structure", "chemistryVersion", "questionScope", "facts", "sources", "sections", "limitations",
                                         "sentInformationCategories", "experimentalRowsIncluded", "privatePathsIncluded", "freeQuestionIncluded", "snapshotSha256")) if sensitive and isinstance(snapshot, dict) else _pick(snapshot, ("schema", "snapshotSha256", "chemistryVersion")) or None
    output["comparison"] = _pick(record.get("comparison"), ("method", "scientificAccuracyRanked", "directPredictionComparison", "sharedSnapshotSha256",
                                                             "independentReporterInputs", "reportersCompleted", "verifiedFactsShared", "sectionOrdersEqual", "unverifiedProseCounts",
                                                             "predictionComparisonReason")) or None
    output["cloudTransfer"] = _pick(record.get("cloudTransfer"), ("allowed", "executed", "requestAttempted", "informationCategories",
                                                                "rawExperimentalRows", "privatePaths", "freeQuestion"))
    output["error"] = _error_summary(record.get("error"))
    output["recovery"] = {**_pick(record.get("recovery"), ("interrupted", "recoveredAt", "originalExecutionUpdatedAt", "automaticExecution")), "contentAvailable": sensitive}
    return output


def _history_item(record, expires_at):
    return {"contractVersion": record.get("contractVersion"), "runId": record["runId"], "status": record["status"],
            "createdAt": record.get("createdAt"), "updatedAt": record.get("updatedAt"), "expiresAtEpoch": expires_at,
            "structureId": (record.get("structure") or {}).get("structureId"), "contentSaved": record.get("contentSaved", False),
            "models": {identifier: {"status": model.get("status"), "error": model.get("error"),
                                    "provenance": _pick(model.get("provenance"), ("testFixture", "modelVersion", "modelId", "revision", "weightsSha256", "snapshotSha256"))}
                       for identifier, model in record.get("models", {}).items()},
            "cloudTransfer": _pick(record.get("cloudTransfer"), ("executed", "requestAttempted")),
            "recovery": record.get("recovery", {})}


def _secure_path(path, *, directory=False):
    """Owner-only POSIX permissions or protected current-user/SYSTEM Windows ACL."""
    if os.name != "nt":
        os.chmod(path, 0o700 if directory else 0o600)
        return
    import ctypes
    from ctypes import wintypes
    advapi, kernel = ctypes.WinDLL("advapi32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    handle, needed = wintypes.HANDLE(), wintypes.DWORD()
    advapi.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(handle)):
        raise _error("COMPARE_PERSISTENCE_PERMISSION_FAILED")
    descriptor, sid_text = ctypes.c_void_p(), wintypes.LPWSTR()
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    try:
        advapi.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
        advapi.GetTokenInformation(handle, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(handle, 1, buffer, needed.value, ctypes.byref(needed)):
            raise _error("COMPARE_PERSISTENCE_PERMISSION_FAILED")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        advapi.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise _error("COMPARE_PERSISTENCE_PERMISSION_FAILED")
        inherit = "OICI" if directory else ""
        sddl = f"D:P(A;{inherit};FA;;;{sid_text.value})(A;{inherit};FA;;;SY)"
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise _error("COMPARE_PERSISTENCE_PERMISSION_FAILED")
        advapi.SetFileSecurityW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
        if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise _error("COMPARE_PERSISTENCE_PERMISSION_FAILED")
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        if sid_text:
            kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        kernel.CloseHandle(handle)


class _OwnerLock:
    def __init__(self, path):
        self.path, self.stream = path, None

    def acquire(self):
        if self.path.is_symlink():
            raise _error("COMPARE_PERSISTENCE_UNSAFE_PATH")
        self.stream = self.path.open("a+b")
        try:
            _secure_path(self.path)
            if self.path.stat().st_size == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ComparePersistenceError):
            self.stream.close()
            self.stream = None
            raise _error("COMPARE_PERSISTENCE_OWNER_CONFLICT") from None

    def release(self):
        if self.stream is None:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None


class CompareRunStore:
    def __init__(self, path=DEFAULT_DB_PATH, *, owner_lock=True, clock=time.time, recovery_ttl_seconds=1800):
        self.path = Path(path).expanduser().absolute() if path is not None else None
        self.use_owner_lock = bool(owner_lock and self.path is not None)
        self.clock, self.recovery_ttl_seconds = clock, recovery_ttl_seconds
        if type(recovery_ttl_seconds) not in (int, float) or not math.isfinite(recovery_ttl_seconds) or not 1 <= recovery_ttl_seconds <= 86400:
            raise _error("COMPARE_PERSISTENCE_INVALID_CONFIG")
        self._db, self._owner, self._closed = None, None, False
        self._mutex = threading.RLock()

    def start(self):
        with self._mutex:
            if self._closed:
                raise _error("COMPARE_PERSISTENCE_CLOSED")
            if self._db is not None:
                return self
            connection = None
            try:
                if self.path is not None:
                    if self.path.is_symlink() or any(parent.is_symlink() for parent in self.path.parents):
                        raise _error("COMPARE_PERSISTENCE_UNSAFE_PATH")
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    _secure_path(self.path.parent, directory=True)
                    if self.use_owner_lock:
                        self._owner = _OwnerLock(self.path.with_suffix(self.path.suffix + ".owner.lock"))
                        self._owner.acquire()
                    if self.path.exists():
                        _secure_path(self.path)
                connection = sqlite3.connect(str(self.path) if self.path else ":memory:", timeout=5, check_same_thread=False)
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute("PRAGMA secure_delete=ON")
                connection.enable_load_extension(False)
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise _error("COMPARE_PERSISTENCE_UNSUPPORTED_SCHEMA")
                if version == 0:
                    connection.execute("CREATE TABLE compare_runs (run_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at REAL NOT NULL, content_saved INTEGER NOT NULL CHECK(content_saved IN (0,1)), record_json TEXT NOT NULL)")
                    connection.execute("CREATE INDEX compare_runs_expiry ON compare_runs(status,expires_at)")
                    connection.execute("CREATE INDEX compare_runs_updated ON compare_runs(updated_at DESC,run_id)")
                    connection.execute("PRAGMA user_version=1")
                    version = 1
                if version == 1:
                    connection.execute("CREATE TABLE service_state (state_key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
                    connection.execute("PRAGMA user_version=2")
                connection.commit()
                if self.path:
                    _secure_path(self.path)
                self._db = connection
                return self
            except (sqlite3.Error, OSError, ComparePersistenceError) as exc:
                if connection is not None:
                    connection.close()
                if self._owner:
                    self._owner.release()
                    self._owner = None
                if isinstance(exc, ComparePersistenceError):
                    raise
                raise _error("COMPARE_PERSISTENCE_OPEN_FAILED") from None

    def upsert(self, record, expires_at_epoch):
        projection = project_record(record)
        if type(expires_at_epoch) not in (int, float) or not math.isfinite(expires_at_epoch) or expires_at_epoch < 0:
            raise _error("COMPARE_PERSISTENCE_INVALID_EXPIRY")
        payload = json.dumps(projection, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(payload.encode()) > 4 * 1024 * 1024:
            raise _error("COMPARE_PERSISTENCE_RECORD_TOO_LARGE")
        with self._mutex:
            self.start()
            try:
                with self._db:
                    self._db.execute("INSERT INTO compare_runs(run_id,status,created_at,updated_at,expires_at,content_saved,record_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,expires_at=excluded.expires_at,content_saved=excluded.content_saved,record_json=excluded.record_json",
                                     (projection["runId"], projection["status"], projection.get("createdAt") or "", projection.get("updatedAt") or "",
                                      expires_at_epoch, int(projection["contentSaved"]), payload))
            except sqlite3.Error:
                raise _error("COMPARE_PERSISTENCE_WRITE_FAILED") from None
        return copy.deepcopy(projection)

    def get(self, run_id, *, now=None):
        timestamp = self.clock() if now is None else now
        with self._mutex:
            self.start()
            row = self._db.execute("SELECT record_json,expires_at,status FROM compare_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row[2] in TERMINAL and row[1] <= timestamp:
                return None
            value = json.loads(row[0])
            value["expiresAtEpoch"] = row[1]
            return value

    def list(self, limit=20, offset=0, *, now=None):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1000000:
            raise _error("COMPARE_PERSISTENCE_INVALID_PAGINATION")
        timestamp = self.clock() if now is None else now
        with self._mutex:
            self.start()
            condition = "(status NOT IN ('completed','partial','failed','cancelled') OR expires_at>?)"
            total = self._db.execute("SELECT count(*) FROM compare_runs WHERE " + condition, (timestamp,)).fetchone()[0]
            rows = self._db.execute("SELECT record_json,expires_at FROM compare_runs WHERE " + condition + " ORDER BY updated_at DESC,run_id ASC LIMIT ? OFFSET ?", (timestamp, limit, offset)).fetchall()
            return {"items": [_history_item(json.loads(row[0]), row[1]) for row in rows], "total": total, "limit": limit, "offset": offset}

    def prune(self, now=None):
        timestamp = self.clock() if now is None else now
        with self._mutex:
            self.start()
            with self._db:
                cursor = self._db.execute("DELETE FROM compare_runs WHERE status IN ('completed','partial','failed','cancelled') AND expires_at<=?", (timestamp,))
                return cursor.rowcount

    def delete(self, run_id):
        """Internal TTL removal; never cancels or starts a model operation."""
        with self._mutex:
            self.start()
            try:
                with self._db:
                    cursor = self._db.execute("DELETE FROM compare_runs WHERE run_id=?", (run_id,))
                    return cursor.rowcount
            except sqlite3.Error:
                raise _error("COMPARE_PERSISTENCE_WRITE_FAILED") from None

    def recover_interrupted(self, now=None):
        timestamp = self.clock() if now is None else now
        recovered_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        with self._mutex:
            self.start()
            with self._db:
                rows = self._db.execute("SELECT run_id,record_json FROM compare_runs WHERE status IN ('pending','running')").fetchall()
                for run_id, payload in rows:
                    record = json.loads(payload)
                    previous_recovery = record.get("recovery") or {}
                    execution_updated_at = (previous_recovery.get("originalExecutionUpdatedAt")
                                            if previous_recovery.get("interrupted") is True and record.get("updatedAt") == previous_recovery.get("recoveredAt")
                                            else record.get("updatedAt"))
                    models = record.get("models", {})
                    successful = any(item.get("status") in ("completed", "partial") for item in models.values())
                    for item in models.values():
                        if item.get("status") in ("pending", "running"):
                            item.update(status="failed", error={"code": "INTERRUPTED"})
                    record.update(status="partial" if successful else "failed", updatedAt=recovered_at, error={"code": "INTERRUPTED"},
                                  recovery={"interrupted": True, "recoveredAt": recovered_at, "automaticExecution": False,
                                            "originalExecutionUpdatedAt": execution_updated_at,
                                            "contentAvailable": record.get("contentSaved", False)})
                    self._db.execute("UPDATE compare_runs SET status=?,updated_at=?,expires_at=?,record_json=? WHERE run_id=?",
                                     (record["status"], recovered_at, timestamp + self.recovery_ttl_seconds,
                                      json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")), run_id))
                return len(rows)

    def has_interrupted_local_execution(self):
        """Inspect every unfinished row, including expired ones, before recovery.

        Running local stages and explicit failed cleanup measurements require
        quarantine, including terminal/expired records. Missing measurements
        are not treated as failures. No stored molecular content is exposed.
        """
        with self._mutex:
            self.start()
            try:
                state = self.get_quarantine_state()
                clearance_epoch = None
                proof = state.get("clearanceProof") if state and state.get("active") is False else None
                if (isinstance(proof, dict) and proof.get("schema") == CLEARANCE_SCHEMA and proof.get("scope") == CLEARANCE_SCOPE
                        and proof.get("ollamaEmpty") is True and proof.get("ownedWorkersAbsent") is True):
                    try:
                        verified = datetime.fromisoformat(proof["verifiedAt"].replace("Z", "+00:00"))
                        if verified.utcoffset() is not None and verified.utcoffset().total_seconds() == 0:
                            clearance_epoch = verified.timestamp()
                    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, OSError):
                        pass  # Missing or malformed clearance never proves release.
                rows = self._db.execute("SELECT record_json,status FROM compare_runs")
                for payload, status in rows:
                    record = json.loads(payload)
                    models = record.get("models") if isinstance(record, dict) else None
                    if not isinstance(models, dict):
                        raise ValueError("Invalid persisted stage state.")
                    # A maintenance proof covers only executions no later than
                    # its actual verification. Startup recovery timestamps do
                    # not turn an already-cleared interruption into a new run.
                    recovery = record.get("recovery") or {}
                    execution_time = (recovery.get("originalExecutionUpdatedAt")
                                      if recovery.get("interrupted") is True and record.get("updatedAt") == recovery.get("recoveredAt")
                                      else record.get("updatedAt"))
                    covered = False
                    if clearance_epoch is not None and isinstance(execution_time, str):
                        try:
                            execution = datetime.fromisoformat(execution_time.replace("Z", "+00:00"))
                            covered = execution.utcoffset() is not None and execution.timestamp() <= clearance_epoch
                        except (ValueError, OverflowError, OSError):
                            pass
                    if covered:
                        continue
                    for model_id in MODEL_IDS[:3]:
                        model = models.get(model_id)
                        if not isinstance(model, dict):
                            continue
                        if status in ("pending", "running") and model.get("status") == "running":
                            return True
                        metrics = model.get("metrics") if isinstance(model.get("metrics"), dict) else {}
                        cleanup = metrics.get("cleanupState") if isinstance(metrics.get("cleanupState"), dict) else {}
                        if cleanup.get("confirmed") is False or metrics.get("resourceCleanupVerified") is False:
                            return True
                return False
            except (sqlite3.Error, ValueError, TypeError):
                raise _error("COMPARE_PERSISTENCE_READ_FAILED") from None

    def get_quarantine_state(self):
        """Safe maintenance metadata; absence is not proof of GPU cleanup."""
        with self._mutex:
            self.start()
            try:
                row = self._db.execute("SELECT value_json FROM service_state WHERE state_key=?", ("local-resource-quarantine",)).fetchone()
                state = json.loads(row[0]) if row else None
                if state is not None and (not isinstance(state, dict) or state.get("schema") != RESOURCE_STATE_SCHEMA or type(state.get("active")) is not bool
                                          or state.get("scope") != CLEARANCE_SCOPE):
                    raise _error("COMPARE_PERSISTENCE_INVALID_RESOURCE_STATE")
                if state and state["active"] and (not isinstance(state.get("reasonCode"), str) or not re.fullmatch(r"(?=.{1,80}$)(?:LOCAL_|COMPARE_|RESOURCE_|INTERRUPTED)[A-Z0-9_-]*", state["reasonCode"])):
                    raise _error("COMPARE_PERSISTENCE_INVALID_RESOURCE_STATE")
                if state:
                    state = _pick(state, ("schema", "active", "reasonCode", "setAt", "updatedAt", "scope", "automaticClearance", "clearedAt", "clearanceProof", "previousClearance"))
                return copy.deepcopy(state)
            except (sqlite3.Error, ValueError, TypeError):
                raise _error("COMPARE_PERSISTENCE_READ_FAILED") from None

    def get_quarantine(self):
        state = self.get_quarantine_state()
        return state.get("reasonCode", "LOCAL_MODEL_CLEANUP_UNCONFIRMED") if state and state["active"] else None

    def set_quarantine(self, reason):
        """Fail closed across restarts; only a bounded error code is persisted."""
        match = re.match(r"([A-Z][A-Z0-9_-]{1,79})(?:[;\s]|$)", reason) if isinstance(reason, str) else None
        code = match.group(1) if match and match.group(1).startswith(("LOCAL_", "COMPARE_", "RESOURCE_", "INTERRUPTED")) else "LOCAL_MODEL_CLEANUP_UNCONFIRMED"
        timestamp = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
        with self._mutex:
            self.start()
            try:
                with self._db:
                    self._db.execute("BEGIN IMMEDIATE")
                    previous = self.get_quarantine_state()
                    state = {"schema": RESOURCE_STATE_SCHEMA, "active": True, "reasonCode": code,
                             "setAt": previous["setAt"] if previous and previous["active"] else timestamp,
                             "updatedAt": timestamp, "scope": CLEARANCE_SCOPE,
                             "automaticClearance": False}
                    if previous and previous.get("clearanceProof"):
                        state["previousClearance"] = {"clearedAt": previous.get("clearedAt"), "proof": previous["clearanceProof"]}
                    self._db.execute("INSERT INTO service_state(state_key,value_json) VALUES(?,?) ON CONFLICT(state_key) DO UPDATE SET value_json=excluded.value_json",
                                     ("local-resource-quarantine", json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))))
                    return copy.deepcopy(state)
            except sqlite3.Error:
                raise _error("COMPARE_PERSISTENCE_WRITE_FAILED") from None

    def clear_quarantine(self, proof):
        """Maintenance-only operation; no route should expose an unlock API.

        The maintenance CLI must actually measure these facts. This validates
        their contract/freshness, never performs or fabricates the measurements.
        A running service retains the owner lock and prevents a second CLI owner.
        """
        required = {"schema", "ollamaEmpty", "ownedWorkersAbsent", "verifiedAt", "scope"}
        if (not isinstance(proof, dict) or set(proof) != required or proof.get("schema") != CLEARANCE_SCHEMA
                or proof.get("ollamaEmpty") is not True or proof.get("ownedWorkersAbsent") is not True
                or proof.get("scope") != CLEARANCE_SCOPE or not isinstance(proof.get("verifiedAt"), str)):
            raise _error("COMPARE_QUARANTINE_CLEARANCE_INVALID")
        try:
            verified = datetime.fromisoformat(proof["verifiedAt"].replace("Z", "+00:00"))
            timestamp = self.clock()
            if verified.utcoffset() is None or verified.utcoffset().total_seconds() != 0 or not -5 <= timestamp - verified.timestamp() <= 300:
                raise ValueError("Clearance proof must be current UTC measurements.")
        except (ValueError, OverflowError, OSError):
            raise _error("COMPARE_QUARANTINE_CLEARANCE_INVALID") from None
        with self._mutex:
            self.start()
            try:
                with self._db:
                    self._db.execute("BEGIN IMMEDIATE")
                    state = self.get_quarantine_state()
                    if state and state["active"] and verified < datetime.fromisoformat(state["updatedAt"]):
                        raise _error("COMPARE_QUARANTINE_CLEARANCE_STALE")
                    state = state or {"schema": RESOURCE_STATE_SCHEMA, "setAt": None, "reasonCode": None}
                    state.update(active=False, clearedAt=datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                                 updatedAt=datetime.fromtimestamp(timestamp, timezone.utc).isoformat(), scope=CLEARANCE_SCOPE,
                                 automaticClearance=False, clearanceProof={**proof, "verifiedAt": verified.isoformat()})
                    self._db.execute("INSERT INTO service_state(state_key,value_json) VALUES(?,?) ON CONFLICT(state_key) DO UPDATE SET value_json=excluded.value_json",
                                     ("local-resource-quarantine", json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":"))))
                    return copy.deepcopy(state)
            except sqlite3.Error:
                raise _error("COMPARE_PERSISTENCE_WRITE_FAILED") from None

    def close(self):
        with self._mutex:
            if self._db is not None:
                self._db.close()
                self._db = None
            if self._owner:
                self._owner.release()
                self._owner = None
            self._closed = True

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
