"""SQLite fixtures only: no real model, cloud API or historical data writes."""
import copy
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from backend.compare_persistence import (ComparePersistenceError, CompareRunStore,
                                         CLEARANCE_SCHEMA, CLEARANCE_SCOPE, SCHEMA_VERSION, default_history_path, project_record)


def fixture_record(run_id="fixture-run", status="completed", *, full=False):
    report = {"status": "verified-structured-report", "reportSchema": "aidd.compare.report.v1", "snapshotSha256": "d" * 64,
              "verifiedFactIds": ["model:herg"], "sourceVerification": {"existingFields": "exact-match", "newScientificFactsVerified": False},
              "sections": [{"id": "prediction", "text": "private generated prose", "commentary": ["research commentary"]}],
              "unverifiedAdditions": [{"text": "private generated prose"}]}
    prediction = {"canonicalSmiles": "CCO", "structureId": "a" * 64, "endpoints": [{"id": "herg", "score": .123}],
                  "scoreKind": "uncalibrated_score", "provenance": {"modelVersion": "fixture-model-v1", "weightsSha256": "b" * 64,
                  "sourceHashes": [{"role": "training-fixture", "sha256": "c" * 64, "path": "C:\\private\\data.csv"}],
                  "apiKey": "sk-private-fixture123", "modelDir": "C:\\private\\model"}}
    return {"contractVersion": "aidd.multimodel.compare.v1", "runId": run_id, "status": status,
            "createdAt": "2026-10-14T00:00:00+00:00", "updatedAt": "2026-10-14T00:00:01+00:00",
            "request": {"smiles": "OCC", "question": "private free question", "mode": "compare", "includeTox21": True,
                        "allowCloudTransfer": True, "persistSensitiveContent": full, "apiKey": "sk-private-fixture123"},
            "structure": {"canonicalSmiles": "CCO", "structureId": "a" * 64, "imageDataUrl": "data:image/png;base64,PRIVATEIMAGE"},
            "models": {"toxnet-v3-b16": {"status": "completed", "data": {"prediction": prediction,
                      "evidence": {"status": "available", "results": [{"smiles": "c1ccccc1", "label": 1, "source": "private experimental row"}]},
                      "attribution": {"status": "available", "method": "fragment-test", "imageDataUrl": "data:image/png;base64,PRIVATEIMAGE"}},
                      "provenance": prediction["provenance"], "metrics": {"durationMs": 5, "gpu": {"measured": True, "peakAllocatedBytes": 1234},
                      "headers": {"Authorization": "Bearer private-fixture"}}, "error": None},
                      "ollama-qwen-local": {"status": "completed", "data": {"report": report}, "metrics": {}, "provenance": {}, "error": None}},
            "snapshot": {"schema": "aidd.compare.facts.v1", "snapshotSha256": "d" * 64, "structure": {"canonicalSmiles": "CCO", "structureId": "a" * 64},
                         "facts": [{"text": "private scientific fact", "value": .123}]},
            "reports": {"ollama-qwen-local": report}, "comparison": {"scientificAccuracyRanked": False},
            "cloudTransfer": {"allowed": True, "executed": True, "requestAttempted": True},
            "error": {"code": "SAFE_ERROR", "message": "private failure C:\\private\\file.csv sk-private-fixture123"},
            "apiKey": "sk-private-fixture123", "headers": {"Authorization": "Bearer private-fixture"}}


class ProjectionTests(unittest.TestCase):
    def test_minimum_projection_drops_content_keys_paths_and_experiment_rows(self):
        original = fixture_record()
        saved = project_record(original)
        serialized = json.dumps(saved)
        for value in ("CCO", "OCC", "private free question", "PRIVATEIMAGE", "private generated prose", "c1ccccc1",
                      "private experimental row", "private scientific fact", "sk-private", "Authorization", "apiKey", "C:\\private"):
            self.assertNotIn(value, serialized)
        self.assertEqual(saved["models"]["toxnet-v3-b16"]["data"]["prediction"]["endpoints"], [{"id": "herg", "score": .123}])
        self.assertEqual(saved["models"]["toxnet-v3-b16"]["provenance"]["weightsSha256"], "b" * 64)
        self.assertFalse(saved["contentSaved"])
        self.assertFalse(saved["recovery"]["contentAvailable"])
        self.assertFalse(saved["request"]["allowCloudTransfer"])
        self.assertTrue(saved["cloudTransfer"]["requestAttempted"])
        self.assertEqual(saved["reports"]["ollama-qwen-local"]["sectionCount"], 1)
        self.assertEqual(saved["error"], {"code": "SAFE_ERROR"})
        self.assertEqual(original["request"]["question"], "private free question")

    def test_full_opt_in_restores_content_but_never_credentials_or_paths(self):
        saved = project_record(fixture_record(full=True))
        self.assertTrue(saved["contentSaved"])
        self.assertTrue(saved["recovery"]["contentAvailable"])
        self.assertEqual(saved["structure"]["canonicalSmiles"], "CCO")
        self.assertEqual(saved["request"]["question"], "private free question")
        self.assertEqual(saved["reports"]["ollama-qwen-local"]["sections"][0]["text"], "private generated prose")
        serialized = json.dumps(saved)
        for value in ("sk-private", "apiKey", "Authorization", "C:\\private", "modelDir", "headers"):
            self.assertNotIn(value, serialized)
        self.assertFalse(saved["request"]["allowCloudTransfer"])

    def test_recognizable_credentials_and_paths_redacted_from_full_prose(self):
        record = fixture_record(full=True)
        record["request"]["question"] = "token hf_abcdefghijklmnop and sk-abcdefghijklmnop api_key=abcdefghi C:\\private\\a.csv /private/data.csv"
        text = project_record(record)["request"]["question"]
        for secret in ("hf_abcdefgh", "sk-abcdefgh", "abcdefghi", "C:\\private", "/private/data"):
            self.assertNotIn(secret, text)
        self.assertIn("REDACTED_CREDENTIAL", text)
        self.assertIn("REDACTED_LOCAL_PATH", text)

    def test_explicit_boolean_is_required_for_sensitive_content(self):
        for value in ("true", 1, None):
            record = fixture_record()
            record["request"]["persistSensitiveContent"] = value
            self.assertFalse(project_record(record)["contentSaved"])

    def test_invalid_run_identity_and_state_refused(self):
        for key, value in (("runId", "x';DROP TABLE compare_runs;--"), ("status", "fake-completed")):
            record = fixture_record()
            record[key] = value
            with self.assertRaises(ComparePersistenceError):
                project_record(record)

    def test_full_valid_stereochemistry_salts_and_isotopes_keep_original_syntax(self):
        for smiles in (r"F/C=C\F", "C[C@H](O)C(=O)O", "C[NH3+].[Cl-]", "[13CH3]CO"):
            record = fixture_record(full=True)
            record["request"]["smiles"] = smiles
            record["structure"]["canonicalSmiles"] = smiles
            record["models"]["toxnet-v3-b16"]["data"]["prediction"]["canonicalSmiles"] = smiles
            record["snapshot"]["structure"]["canonicalSmiles"] = smiles
            saved = project_record(record)
            self.assertEqual(saved["request"]["smiles"], smiles)
            self.assertEqual(saved["structure"]["canonicalSmiles"], smiles)
            self.assertEqual(saved["snapshot"]["structure"]["canonicalSmiles"], smiles)
            self.assertEqual(saved["models"]["toxnet-v3-b16"]["data"]["prediction"]["canonicalSmiles"], smiles)

    def test_full_snapshot_keeps_original_digest_and_unchanged_fact_content(self):
        record = fixture_record(full=True)
        body = {"schema": "aidd.compare.facts.v1", "structure": {"canonicalSmiles": r"F/C=C\F", "structureId": "a" * 64},
                "chemistryVersion": "fixture-chemistry", "facts": [{"id": "fixture:class", "text": "已核验事实", "value": .123}],
                "sources": [{"id": "fixture", "kind": "model-computation"}], "sections": [], "limitations": []}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()).hexdigest()
        record["snapshot"] = {**body, "snapshotSha256": digest}
        saved = project_record(record)["snapshot"]
        actual_body = {key: value for key, value in saved.items() if key != "snapshotSha256"}
        self.assertEqual(saved["snapshotSha256"], digest)
        self.assertEqual(actual_body, body)
        self.assertEqual(hashlib.sha256(json.dumps(actual_body, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()).hexdigest(), digest)

    def test_measurement_research_settings_and_mock_identity_preserved(self):
        record = fixture_record()
        record["request"].update(evidenceEndpoint="herg", includeAttribution=False)
        model = record["models"]["toxnet-v3-b16"]
        model["provenance"].update(testFixture=True, snapshotSha256="d" * 64, returnedModel="provider-returned-fixture",
                                   researchSteps={"evidenceRequested": True, "evidenceEndpoint": "herg", "attributionRequested": False})
        measurements = {"totalTokens": 99, "httpStatusCode": 429, "httpResponseReceived": True, "usageStatus": "provider-reported",
                        "httpRequestState": "attempted", "reportValidation": "failed", "cleanupState": {"state": "released", "confirmed": True}}
        model["metrics"].update(measurements)
        saved = project_record(record)
        self.assertEqual(saved["request"]["evidenceEndpoint"], "herg")
        self.assertFalse(saved["request"]["includeAttribution"])
        for key, value in measurements.items():
            self.assertEqual(saved["models"]["toxnet-v3-b16"]["metrics"][key], value)
        self.assertTrue(saved["models"]["toxnet-v3-b16"]["provenance"]["testFixture"])
        self.assertEqual(saved["models"]["toxnet-v3-b16"]["provenance"]["snapshotSha256"], "d" * 64)


class DefaultPathTests(unittest.TestCase):
    def test_normal_workspace_path_is_local_data_directory_without_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "ordinary-project"
            selected = default_history_path(workspace, env={"LOCALAPPDATA": str(Path(directory) / "local")})
            self.assertEqual(selected, workspace / "data" / "compare" / "runs.sqlite3")
            self.assertFalse(workspace.exists())

    @unittest.skipUnless(os.name == "nt", "Windows OneDrive defaults")
    def test_onedrive_workspaces_use_localappdata_and_distinct_workspace_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root, local = Path(directory), Path(directory) / "local"
            selected = []
            for folder in ("OneDrive", "ONEDRIVE-test", "OneDrive - Organization"):
                workspace = root / folder / "project"
                destination = default_history_path(workspace, env={"LOCALAPPDATA": str(local)})
                self.assertTrue(destination.is_relative_to(local))
                self.assertEqual(destination.name, "runs.sqlite3")
                self.assertEqual(len(destination.parent.name), 12)
                self.assertFalse(destination.exists())
                selected.append(destination)
            self.assertEqual(len(set(selected)), 3)
            self.assertFalse(local.exists())

    @unittest.skipUnless(os.name == "nt", "Windows OneDrive defaults")
    def test_onedrive_like_substring_does_not_relocate_normal_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "my-onedrive-example" / "project"
            self.assertEqual(default_history_path(workspace), workspace / "data" / "compare" / "runs.sqlite3")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.path = self.directory / "private" / "runs.sqlite3"
        self.store = CompareRunStore(self.path, clock=lambda: 100, recovery_ttl_seconds=300)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_lazy_initialization_version_and_durable_round_trip(self):
        self.assertFalse(self.path.parent.exists())
        self.store.start()
        self.assertEqual(self.store._db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(self.store._db.execute("PRAGMA secure_delete").fetchone()[0], 1)
        self.store.upsert(fixture_record(), 1000)
        self.store.close()
        self.store = CompareRunStore(self.path, clock=lambda: 100)
        self.assertEqual(self.store.get("fixture-run")["models"]["toxnet-v3-b16"]["data"]["prediction"]["endpoints"][0]["score"], .123)
        self.assertEqual(self.store.get("fixture-run")["expiresAtEpoch"], 1000)

    def test_update_same_id_is_transactional_and_no_duplicate_row(self):
        self.store.upsert(fixture_record(status="pending"), 500)
        self.store.upsert(fixture_record(status="completed"), 1000)
        self.assertEqual(self.store.list()["total"], 1)
        self.assertEqual(self.store.get("fixture-run")["status"], "completed")

    def test_switching_sensitive_content_off_removes_old_plaintext_from_database(self):
        self.store.upsert(fixture_record(full=True), 1000)
        self.assertIn(b"private generated prose", self.path.read_bytes())
        self.store.upsert(fixture_record(full=False), 1000)
        self.assertNotIn(b"private generated prose", self.path.read_bytes())
        self.assertNotIn(b"private free question", self.path.read_bytes())
        self.assertFalse(self.store.get("fixture-run")["contentSaved"])

    def test_read_and_delete_parameters_cannot_inject_sql(self):
        self.store.upsert(fixture_record(), 1000)
        attack = "x' OR 1=1; DROP TABLE compare_runs;--"
        self.assertIsNone(self.store.get(attack))
        self.assertEqual(self.store.delete(attack), 0)
        self.assertEqual(self.store.list()["total"], 1)
        self.assertEqual(self.store.delete("fixture-run"), 1)
        self.assertIsNone(self.store.get("fixture-run"))

    def test_list_is_paginated_summary_even_for_full_content(self):
        self.store.upsert(fixture_record("a", full=True), 1000)
        self.store.upsert(fixture_record("b"), 1000)
        result = self.store.list(limit=1, offset=1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["items"][0]["runId"], "b")
        full_summary = self.store.list()["items"][0]
        self.assertTrue(full_summary["contentSaved"])
        self.assertNotIn("CCO", json.dumps(full_summary))
        self.assertNotIn("private generated prose", json.dumps(full_summary))
        for limit, offset in ((True, 0), (0, 0), (101, 0), (1, -1), (1, True)):
            with self.assertRaises(ComparePersistenceError):
                self.store.list(limit, offset)

    def test_terminal_expiry_and_prune_do_not_silently_drop_live_run(self):
        self.store.upsert(fixture_record("done"), 100)
        self.store.upsert(fixture_record("active", status="running"), 50)
        self.assertIsNone(self.store.get("done"))
        self.assertIsNotNone(self.store.get("active"))
        self.assertEqual(self.store.list()["total"], 1)
        self.assertEqual(self.store.prune(100), 1)
        self.assertIsNotNone(self.store.get("active"))

    def test_restart_recovery_partial_keeps_completed_stages_no_execution(self):
        record = fixture_record(status="running")
        record["models"]["ollama-qwen-local"]["status"] = "running"
        self.store.upsert(record, 50)
        self.store.close()
        self.store = CompareRunStore(self.path, clock=lambda: 100, recovery_ttl_seconds=300)
        self.assertEqual(self.store.recover_interrupted(), 1)
        recovered = self.store.get("fixture-run")
        self.assertEqual(recovered["status"], "partial")
        self.assertEqual(recovered["models"]["toxnet-v3-b16"]["status"], "completed")
        self.assertEqual(recovered["models"]["ollama-qwen-local"]["error"], {"code": "INTERRUPTED"})
        self.assertFalse(recovered["recovery"]["automaticExecution"])
        self.assertFalse(recovered["recovery"]["contentAvailable"])
        self.assertEqual(recovered["expiresAtEpoch"], 400)
        self.assertEqual(self.store.recover_interrupted(), 0)

    def test_interrupted_local_execution_is_detected_even_when_persisted_expiry_passed(self):
        record = fixture_record(status="running")
        record["models"]["ollama-qwen-local"]["status"] = "running"
        self.store.upsert(record, 1)
        self.assertTrue(self.store.has_interrupted_local_execution())
        self.store.recover_interrupted()
        self.assertFalse(self.store.has_interrupted_local_execution())

    def test_pending_stages_completed_runs_and_cloud_only_running_do_not_imply_local_worker(self):
        record = fixture_record(status="pending")
        for item in record["models"].values():
            item["status"] = "pending"
        record["models"]["deepseek-api"] = {"status": "running", "data": None}
        self.store.upsert(record, 1)
        self.assertFalse(self.store.has_interrupted_local_execution())
        self.store.upsert(fixture_record("done"), 1)
        self.assertFalse(self.store.has_interrupted_local_execution())

    def test_expired_terminal_record_with_explicit_failed_cleanup_stays_detectable(self):
        for metric in ({"cleanupState": {"state": "quarantined", "confirmed": False}},
                       {"cleanupState": {"state": "cleaning", "confirmed": False}},
                       {"resourceCleanupVerified": False}):
            record = fixture_record(status="cancelled")
            record["models"]["ollama-qwen-local"]["metrics"].update(metric)
            self.store.upsert(record, 0)
            self.assertTrue(self.store.has_interrupted_local_execution())
        self.store.delete("fixture-run")
        self.assertFalse(self.store.has_interrupted_local_execution())

    def test_absent_null_or_successful_cleanup_is_not_interpreted_as_failure(self):
        for metrics in ({}, {"cleanupState": None}, {"cleanupState": {"confirmed": None}},
                        {"cleanupState": {"confirmed": True}, "resourceCleanupVerified": True}):
            record = fixture_record()
            record["models"]["ollama-qwen-local"]["metrics"] = metrics
            self.store.upsert(record, 0)
            self.assertFalse(self.store.has_interrupted_local_execution())

    def test_verified_clearance_covers_old_terminal_failure_but_new_unsafe_execution_blocks(self):
        record = fixture_record(status="failed")
        record["updatedAt"] = datetime.fromtimestamp(90, timezone.utc).isoformat()
        record["models"]["ollama-qwen-local"]["metrics"]["resourceCleanupVerified"] = False
        self.store.upsert(record, 0)
        self.assertTrue(self.store.has_interrupted_local_execution())
        self.store.set_quarantine("LOCAL_MODEL_CLEANUP_UNCONFIRMED")
        self.store.clear_quarantine(self.clearance_proof())
        self.assertFalse(self.store.has_interrupted_local_execution())
        self.store.close()
        self.store = CompareRunStore(self.path, clock=lambda: 103)
        self.assertFalse(self.store.has_interrupted_local_execution())
        record["updatedAt"] = datetime.fromtimestamp(102, timezone.utc).isoformat()
        self.store.upsert(record, 1000)
        self.assertTrue(self.store.has_interrupted_local_execution())

    def test_clearance_coverage_keeps_original_execution_time_through_restart_recovery(self):
        record = fixture_record(status="running")
        original_time = datetime.fromtimestamp(90, timezone.utc).isoformat()
        record["updatedAt"] = original_time
        record["models"]["ollama-qwen-local"]["status"] = "running"
        record["models"]["ollama-qwen-local"]["metrics"]["cleanupState"] = {"state": "cleaning", "confirmed": False}
        self.store.upsert(record, 0)
        self.store.set_quarantine("INTERRUPTED_LOCAL_MODEL")
        self.store.clear_quarantine(self.clearance_proof())
        self.assertFalse(self.store.has_interrupted_local_execution())
        self.store.recover_interrupted(now=110)
        recovered = self.store.get("fixture-run")
        self.assertEqual(recovered["recovery"]["originalExecutionUpdatedAt"], original_time)
        self.assertFalse(self.store.has_interrupted_local_execution())
        self.store.close()
        self.store = CompareRunStore(self.path, clock=lambda: 120)
        self.assertFalse(self.store.has_interrupted_local_execution())
        recovered["updatedAt"] = datetime.fromtimestamp(120, timezone.utc).isoformat()
        recovered["status"] = "running"
        recovered["models"]["ollama-qwen-local"]["status"] = "running"
        self.store.upsert(recovered, 1000)
        self.assertTrue(self.store.has_interrupted_local_execution())

    def test_restart_recovery_without_success_is_failed_and_full_option_preserved(self):
        record = fixture_record(status="pending", full=True)
        for item in record["models"].values():
            item["status"] = "pending"
            item["data"] = None
        self.store.upsert(record, 50)
        self.assertEqual(self.store.recover_interrupted(), 1)
        recovered = self.store.get("fixture-run")
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["error"], {"code": "INTERRUPTED"})
        self.assertTrue(recovered["contentSaved"])
        self.assertEqual(recovered["request"]["smiles"], "OCC")
        self.assertTrue(recovered["recovery"]["contentAvailable"])

    def test_completed_history_unchanged_by_recovery(self):
        self.store.upsert(fixture_record(), 1000)
        before = self.store.get("fixture-run")
        self.assertEqual(self.store.recover_interrupted(), 0)
        self.assertEqual(before, self.store.get("fixture-run"))

    def test_unknown_future_schema_rejected_without_replacing_records(self):
        self.path.parent.mkdir()
        with closing(sqlite3.connect(self.path)) as database:
            database.execute("PRAGMA user_version=99")
            database.execute("CREATE TABLE future_fixture(value TEXT)")
            database.execute("INSERT INTO future_fixture VALUES('keep-me')")
            database.commit()
        with self.assertRaises(ComparePersistenceError) as caught:
            self.store.start()
        self.assertEqual(caught.exception.code, "COMPARE_PERSISTENCE_UNSUPPORTED_SCHEMA")
        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual(database.execute("SELECT value FROM future_fixture").fetchone()[0], "keep-me")

    def test_version_one_migration_keeps_existing_history_and_creates_service_state(self):
        self.path.parent.mkdir()
        record = project_record(fixture_record())
        with closing(sqlite3.connect(self.path)) as database:
            database.execute("CREATE TABLE compare_runs (run_id TEXT PRIMARY KEY,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,expires_at REAL NOT NULL,content_saved INTEGER NOT NULL CHECK(content_saved IN (0,1)),record_json TEXT NOT NULL)")
            database.execute("INSERT INTO compare_runs VALUES(?,?,?,?,?,?,?)", (record["runId"], record["status"], record["createdAt"], record["updatedAt"], 1000, 0, json.dumps(record)))
            database.execute("PRAGMA user_version=1")
            database.commit()
        self.store.start()
        self.assertEqual(self.store._db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(self.store.get("fixture-run")["models"]["toxnet-v3-b16"]["data"]["prediction"]["endpoints"][0]["score"], .123)
        self.assertIsNone(self.store.get_quarantine())
        self.store.set_quarantine("INTERRUPTED_LOCAL_MODEL")
        self.assertEqual(self.store.get_quarantine(), "INTERRUPTED_LOCAL_MODEL")

    def clearance_proof(self, timestamp=100):
        return {"schema": CLEARANCE_SCHEMA, "ollamaEmpty": True, "ownedWorkersAbsent": True,
                "verifiedAt": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(), "scope": CLEARANCE_SCOPE}

    def test_quarantine_survives_restart_history_recovery_and_ttl_pruning(self):
        self.store.upsert(fixture_record(), 100)
        self.store.set_quarantine("LOCAL_MODEL_CLEANUP_UNCONFIRMED; private CCO C:\\private\\model sk-private-fixture123")
        self.assertEqual(self.store.get_quarantine(), "LOCAL_MODEL_CLEANUP_UNCONFIRMED")
        self.assertNotIn("CCO", json.dumps(self.store.get_quarantine_state()))
        self.assertNotIn("C:\\private", json.dumps(self.store.get_quarantine_state()))
        self.store.close()
        self.store = CompareRunStore(self.path, clock=lambda: 100)
        self.store.start()
        self.assertEqual(self.store.recover_interrupted(), 0)
        self.assertEqual(self.store.prune(), 1)
        self.assertEqual(self.store.get_quarantine(), "LOCAL_MODEL_CLEANUP_UNCONFIRMED")
        self.assertFalse(self.store.get_quarantine_state()["automaticClearance"])

    def test_clearance_requires_verified_true_fields_scope_and_current_timestamp(self):
        self.store.set_quarantine("LOCAL_MODEL_CLEANUP_UNCONFIRMED")
        for key, value in (("ollamaEmpty", False), ("ownedWorkersAbsent", 1), ("scope", "all-GPU-applications"),
                           ("schema", "unknown"), ("verifiedAt", "not-a-time"),
                           ("verifiedAt", datetime.fromtimestamp(50, timezone.utc).isoformat())):
            proof = {**self.clearance_proof(), key: value}
            with self.assertRaises(ComparePersistenceError):
                self.store.clear_quarantine(proof)
            self.assertEqual(self.store.get_quarantine(), "LOCAL_MODEL_CLEANUP_UNCONFIRMED")
        with self.assertRaises(ComparePersistenceError):
            self.store.clear_quarantine({**self.clearance_proof(), "pid": 123})

    def test_clearance_is_recorded_and_survives_restart_without_auto_unlock(self):
        self.store.set_quarantine("INTERRUPTED_LOCAL_MODEL")
        state = self.store.clear_quarantine(self.clearance_proof())
        self.assertFalse(state["active"])
        self.assertIsNone(self.store.get_quarantine())
        self.assertEqual(state["clearanceProof"]["scope"], CLEARANCE_SCOPE)
        self.assertEqual(state["clearedAt"], datetime.fromtimestamp(100, timezone.utc).isoformat())
        self.store.close()
        self.store = CompareRunStore(self.path, clock=lambda: 101)
        self.assertIsNone(self.store.get_quarantine())
        self.assertEqual(self.store.get_quarantine_state()["clearanceProof"]["verifiedAt"], datetime.fromtimestamp(100, timezone.utc).isoformat())
        self.store.set_quarantine("LOCAL_MODEL_CLEANUP_UNCONFIRMED")
        self.assertEqual(self.store.get_quarantine(), "LOCAL_MODEL_CLEANUP_UNCONFIRMED")
        self.assertIn("previousClearance", self.store.get_quarantine_state())
        with self.assertRaises(ComparePersistenceError) as caught:
            self.store.clear_quarantine(self.clearance_proof())
        self.assertEqual(caught.exception.code, "COMPARE_QUARANTINE_CLEARANCE_STALE")

    def test_quarantine_write_transaction_rolls_back_on_storage_failure(self):
        self.store.set_quarantine("INTERRUPTED_LOCAL_MODEL")
        self.store._db.execute("CREATE TRIGGER fixture_state_reject BEFORE UPDATE ON service_state BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
        self.store._db.commit()
        with self.assertRaises(ComparePersistenceError):
            self.store.clear_quarantine(self.clearance_proof())
        self.assertEqual(self.store.get_quarantine(), "INTERRUPTED_LOCAL_MODEL")

    def test_write_failure_rolls_back_and_does_not_erase_existing_history(self):
        self.store.upsert(fixture_record(), 1000)
        self.store._db.execute("CREATE TRIGGER fixture_reject BEFORE INSERT ON compare_runs BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
        self.store._db.commit()
        with self.assertRaises(ComparePersistenceError) as caught:
            self.store.upsert(fixture_record("fail"), 1000)
        self.assertEqual(caught.exception.code, "COMPARE_PERSISTENCE_WRITE_FAILED")
        self.assertIsNone(self.store.get("fail"))
        self.assertEqual(self.store.list()["total"], 1)

    def test_owner_lock_rejects_second_store_and_releases_on_close(self):
        self.store.start()
        second = CompareRunStore(self.path)
        with self.assertRaises(ComparePersistenceError) as caught:
            second.start()
        self.assertEqual(caught.exception.code, "COMPARE_PERSISTENCE_OWNER_CONFLICT")
        second.close()
        self.store.close()
        self.store = CompareRunStore(self.path)
        self.store.start()

    def test_owner_lock_rejects_another_process(self):
        self.store.start()
        code = ("from backend.compare_persistence import CompareRunStore,ComparePersistenceError; import sys\n"
                "store=CompareRunStore(sys.argv[1])\n"
                "try:\n store.start(); print('UNEXPECTED_OWNER')\n"
                "except ComparePersistenceError as e:\n print(e.code)\n"
                "finally:\n store.close()\n")
        result = subprocess.run([sys.executable, "-c", code, str(self.path)], capture_output=True, text=True, timeout=15,
                                **({"creationflags": 0x08000000} if os.name == "nt" else {}))
        self.assertEqual(result.returncode, 0)
        self.assertIn("COMPARE_PERSISTENCE_OWNER_CONFLICT", result.stdout)

    def test_closed_store_refuses_new_writes(self):
        self.store.close()
        with self.assertRaises(ComparePersistenceError) as caught:
            self.store.upsert(fixture_record(), 1000)
        self.assertEqual(caught.exception.code, "COMPARE_PERSISTENCE_CLOSED")

    def test_memory_store_needs_no_file_or_owner_lock(self):
        memory = CompareRunStore(None, clock=lambda: 100)
        try:
            memory.upsert(fixture_record(), 1000)
            self.assertEqual(memory.list()["total"], 1)
            self.assertIsNone(memory._owner)
            self.assertFalse(self.path.parent.exists())
        finally:
            memory.close()

    def test_bad_expiry_is_rejected_before_opening_storage(self):
        for expiry in (float("nan"), float("inf"), -1, True):
            with self.assertRaises(ComparePersistenceError):
                self.store.upsert(fixture_record(), expiry)
        self.assertFalse(self.path.parent.exists())

    @unittest.skipUnless(os.name == "nt", "Windows-specific protected ACL check")
    def test_windows_private_database_acl_is_protected_with_two_trustees(self):
        import ctypes
        from ctypes import wintypes
        self.store.start()
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        needed = wintypes.DWORD()
        advapi.GetFileSecurityW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
        advapi.GetFileSecurityW(str(self.path), 4, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        self.assertTrue(advapi.GetFileSecurityW(str(self.path), 4, buffer, needed.value, ctypes.byref(needed)))
        control, revision = wintypes.WORD(), wintypes.DWORD()
        advapi.GetSecurityDescriptorControl.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD))
        self.assertTrue(advapi.GetSecurityDescriptorControl(buffer, ctypes.byref(control), ctypes.byref(revision)))
        self.assertTrue(control.value & 0x1000)  # SE_DACL_PROTECTED: no inherited broad grants.
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        advapi.GetSecurityDescriptorDacl.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL))
        self.assertTrue(advapi.GetSecurityDescriptorDacl(buffer, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)))
        class AclHeader(ctypes.Structure):
            _fields_ = [("revision", ctypes.c_ubyte), ("reserved", ctypes.c_ubyte), ("size", ctypes.c_ushort),
                        ("ace_count", ctypes.c_ushort), ("reserved2", ctypes.c_ushort)]
        self.assertEqual(ctypes.cast(dacl, ctypes.POINTER(AclHeader)).contents.ace_count, 2)

    @unittest.skipIf(os.name == "nt", "POSIX mode check; Windows uses protected ACL instead")
    def test_posix_private_directory_and_database_modes(self):
        self.store.start()
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
