import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values
from fastapi.testclient import TestClient

from backend.configuration import ModelConfigurationStore
from backend.main import create_app


class ConfigurationModelHandler(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.received.append((body, self.headers.get("Authorization")))
        response = {"endpoints": [{"id": name, "score": score} for name, score in zip(("herg", "dili", "ames", "clintox"), (0.6, 0.3, 0.2, 0.4))]}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())


class ConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ConfigurationModelHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/predict"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / ".env"
        self.client = TestClient(create_app(config_path=self.path, env={}))
        self.addCleanup(self.client.close)
        ConfigurationModelHandler.received.clear()

    def complete(self, **changes):
        return {"apiUrl": self.url, "modelName": "local-configuration-fixture", "modelVersion": "fixture-v1", "apiFormat": "json", "scoreKind": "uncalibrated_score", "sendImage": False, "timeoutSeconds": 30, "jsonMode": True, "temperature": None, **changes}

    def test_public_defaults_and_saved_key_are_never_echoed(self):
        defaults = self.client.get("/api/providers/openai/config").json()
        self.assertEqual(defaults["apiUrl"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(defaults["apiFormat"], "openai")
        self.assertFalse(defaults["hasApiKey"])
        self.assertFalse(defaults["configured"])
        secret = "fixture-secret-never-returned"
        saved = self.client.post("/api/providers/custom/config", json=self.complete(apiKey=secret))
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()["hasApiKey"])
        for response in (saved, self.client.get("/api/providers/custom/config"), self.client.get("/api/health")):
            self.assertNotIn(secret, response.text)
            self.assertNotIn('"apiKey"', response.text)

    def test_save_updates_live_model_call_without_restart_and_survives_reload(self):
        self.assertEqual(self.client.post("/api/providers/custom/config", json=self.complete(apiKey="first-fixture-key")).status_code, 200)
        self.assertEqual(self.client.post("/api/providers/custom/config", json={"modelName": "updated-model", "apiKey": "second-fixture-key"}).status_code, 200)
        result = self.client.post("/api/analyze", json={"smiles": "OCC", "provider": "custom"})
        self.assertEqual(result.status_code, 200)
        sent, authorization = ConfigurationModelHandler.received[0]
        self.assertEqual(sent["model"], "updated-model")
        self.assertEqual(sent["smiles"], "CCO")
        self.assertEqual(authorization, "Bearer second-fixture-key")
        self.assertEqual(result.json()["provenance"]["model"]["modelName"], "updated-model")
        with TestClient(create_app(config_path=self.path, env={})) as restarted:
            public = restarted.get("/api/providers/custom/config").json()
            self.assertEqual(public["modelName"], "updated-model")
            self.assertTrue(public["hasApiKey"])
            self.assertTrue(public["configured"])

    def test_blank_and_omitted_keys_keep_existing_key_and_preserve_other_provider(self):
        untouched = "# Preserve provider comments\nTOXILENS_QWEN_API_KEY='other-fixture-key'\nOTHER_SETTING='line one\nline two'\n"
        self.path.write_text(untouched, encoding="utf-8")
        secret = "fixture'\\${KEEP_LITERAL}"
        self.client.post("/api/providers/custom/config", json=self.complete(apiKey=secret))
        for update in ({"modelVersion": "fixture-v2"}, {"apiKey": "  ", "modelVersion": "fixture-v3"}, {"apiKey": None}):
            self.assertEqual(self.client.post("/api/providers/custom/config", json=update).status_code, 200)
        content = self.path.read_text(encoding="utf-8")
        self.assertIn(untouched, content)
        persisted = dotenv_values(self.path, interpolate=False)
        self.assertEqual(persisted["TOXILENS_CUSTOM_API_KEY"], secret)
        self.assertEqual(persisted["TOXILENS_QWEN_API_KEY"], "other-fixture-key")
        self.assertEqual(persisted["OTHER_SETTING"], "line one\nline two")

    def test_standard_provider_needs_key_custom_provider_does_not(self):
        missing = self.client.post("/api/providers/qwen/config", json=self.complete(apiFormat="openai"))
        self.assertEqual(missing.status_code, 422)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.client.post("/api/providers/qwen/config", json=self.complete(apiFormat="openai", apiKey="qwen-fixture")).status_code, 200)
        self.assertEqual(self.client.post("/api/providers/custom/config", json=self.complete()).status_code, 200)
        self.assertFalse(self.client.get("/api/providers/custom/config").json()["hasApiKey"])

    def test_invalid_configuration_rejected_without_secret_or_runtime_changes(self):
        self.client.post("/api/providers/custom/config", json=self.complete())
        secret = "private-invalid-fixture"
        for update in ({"apiUrl": "http://remote.example/predict"}, {"apiUrl": "https://user:pass@example.com"}, {"apiFormat": "openai", "scoreKind": "probability"}, {"timeoutSeconds": True}, {"timeoutSeconds": 121}, {"temperature": True}, {"sendImage": "true"}, {"modelName": None}, {"modelVersion": "x\ny"}, {"unknownField": True}):
            response = self.client.post("/api/providers/custom/config", json={**update, "apiKey": secret})
            self.assertEqual(response.status_code, 422, update)
            self.assertNotIn(secret, response.text)
        self.assertFalse(self.client.get("/api/providers/custom/config").json()["hasApiKey"])
        self.assertNotIn(secret, self.path.read_text(encoding="utf-8"))

    def test_configuration_post_requires_json_same_origin_and_known_provider(self):
        for headers in ({"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"}):
            self.assertEqual(self.client.post("/api/providers/custom/config", content=json.dumps(self.complete()), headers=headers).status_code, 415)
        self.assertEqual(self.client.post("/api/providers/custom/config", json=self.complete(), headers={"Origin": "https://foreign.example"}).status_code, 403)
        for method in (self.client.get, lambda path: self.client.post(path, json=self.complete())):
            self.assertEqual(method("/api/providers/unknown/config").status_code, 404)
        self.assertFalse(self.path.exists())

    def test_environment_precedence_is_reported_and_runtime_edits_win_until_restart(self):
        environment = {"TOXILENS_CUSTOM_MODEL_NAME": "environment-model", "TOXILENS_CUSTOM_API_KEY": "environment-fixture-secret"}
        with TestClient(create_app(config_path=self.path, env=environment)) as client:
            saved = client.post("/api/providers/custom/config", json=self.complete(modelName="browser-model")).json()
            self.assertEqual(saved["modelName"], "browser-model")
            self.assertEqual(set(saved["restartOverrideFields"]), {"modelName", "apiKey"})
        with TestClient(create_app(config_path=self.path, env=environment)) as client:
            self.assertEqual(client.get("/api/providers/custom/config").json()["modelName"], "environment-model")
        with TestClient(create_app(config_path=self.path, env={})) as client:
            self.assertEqual(client.get("/api/providers/custom/config").json()["modelName"], "browser-model")
        legacy_path = self.path.parent / ".legacy-env"
        legacy_environment = {"TOXILENS_MODEL_MODEL_NAME": "legacy-environment-model", "TOXILENS_MODEL_API_KEY": "legacy-fixture-key"}
        with TestClient(create_app(config_path=legacy_path, env=legacy_environment)) as client:
            self.assertEqual(client.get("/api/providers/custom/config").json()["modelName"], "legacy-environment-model")
            saved = client.post("/api/providers/custom/config", json=self.complete(modelName="modern-browser-model")).json()
            self.assertEqual(saved["restartOverrideFields"], [])
        with TestClient(create_app(config_path=legacy_path, env=legacy_environment)) as client:
            self.assertEqual(client.get("/api/providers/custom/config").json()["modelName"], "modern-browser-model")

    def test_atomic_write_failure_keeps_prior_file_and_runtime_configuration(self):
        self.client.post("/api/providers/custom/config", json=self.complete())
        before = self.path.read_bytes()
        with patch("backend.configuration.os.replace", side_effect=OSError("fixture-secret-failure")):
            response = self.client.post("/api/providers/custom/config", json={"modelName": "must-not-activate", "apiKey": "must-not-leak"})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("must-not-leak", response.text)
        self.assertNotIn("fixture-secret-failure", response.text)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.get("/api/providers/custom/config").json()["modelName"], "local-configuration-fixture")
        self.assertFalse(list(self.path.parent.glob(".env-write-*")))

    def test_concurrent_provider_updates_preserve_both_transactions(self):
        store = ModelConfigurationStore(self.path, env={})
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(store.save, provider, self.complete(apiKey=provider + "-fixture")) for provider in ("qwen", "glm")]
            self.assertTrue(all(future.result()["configured"] for future in futures))
        persisted = dotenv_values(self.path, interpolate=False)
        self.assertEqual(persisted["TOXILENS_QWEN_API_KEY"], "qwen-fixture")
        self.assertEqual(persisted["TOXILENS_GLM_API_KEY"], "glm-fixture")


if __name__ == "__main__":
    unittest.main()
