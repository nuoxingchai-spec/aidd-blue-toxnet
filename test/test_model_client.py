import copy
import json
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend.model_client import ModelAPIError, ModelClient, ModelConfig, get_model_registry

VALID = {"modelVersion": "fixture-response-v2", "endpoints": [{"id": name, "score": value} for name, value in zip(("herg", "dili", "ames", "clintox"), (0.7, 0.3, 0.2, 0.4))]}


class Handler(BaseHTTPRequestHandler):
    received = []
    overrides = {}

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.received.append({"path": self.path, "body": body, "authorization": self.headers.get("Authorization")})
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/json")
            self.end_headers()
            return
        if self.path == "/slow":
            time.sleep(0.25)
        self.send_response(401 if self.path == "/error" else 200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        payload = copy.deepcopy(self.overrides.get(self.path, VALID))
        if self.path == "/chat":
            payload.update(modelVersion="invented-version", calibration={"status": "validated", "evidenceId": "invented", "datasetVersion": "invented"})
            payload = {"id": "fixture-request", "model": "fixture-snapshot", "choices": [{"message": {"content": json.dumps(payload)}}]}
        raw = b"fixture-secret must not be echoed" if self.path == "/invalid-json" else json.dumps(payload).encode()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass


class ModelClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Handler.received.clear()
        Handler.overrides.clear()

    def client(self, path="/json", **options):
        return ModelClient(ModelConfig(api_url=self.base + path, api_key="fixture-secret", model_name="fixture-model", model_version="configured-fixture-v1", **options))

    def test_json_http_request_and_server_version(self):
        result = self.client().predict("CCO", "structure-id")
        received = Handler.received[0]
        self.assertEqual(received["body"]["smiles"], "CCO")
        self.assertEqual(received["body"]["endpoints"], ["herg", "dili", "ames", "clintox"])
        self.assertEqual(received["authorization"], "Bearer fixture-secret")
        self.assertEqual(result["provenance"]["modelVersion"], "fixture-response-v2")
        self.assertNotIn("fixture-secret", json.dumps(result))

    def test_chat_multimodal_request_and_generated_validation_claims_not_trusted(self):
        result = self.client("/chat", api_format="openai", send_image=True).predict("CCO", "identity", "data:image/png;base64,cG5n")
        content = Handler.received[0]["body"]["messages"][1]["content"]
        self.assertEqual([item["type"] for item in content], ["text", "image_url"])
        self.assertEqual(result["calibration"]["status"], "unknown")
        self.assertEqual(result["provenance"]["modelVersion"], "configured-fixture-v1")
        self.assertEqual(result["provenance"]["returnedModel"], "fixture-snapshot")
        self.assertEqual(result["scoreKind"], "uncalibrated_score")

    def test_invalid_scores_and_endpoint_sets(self):
        cases = []
        for value in (True, float("nan"), -0.1, 1.1):
            payload = copy.deepcopy(VALID)
            payload["endpoints"][0]["score"] = value
            cases.append(payload)
        payload = copy.deepcopy(VALID)
        payload["endpoints"][1]["id"] = "herg"
        cases.extend([payload, {"endpoints": []}])
        for invalid in cases:
            Handler.overrides["/json"] = invalid
            with self.assertRaises(ModelAPIError) as raised:
                self.client().predict("CCO", "identity")
            self.assertEqual(raised.exception.code, "MODEL_INVALID_RESPONSE")

    def test_timeout_http_failure_invalid_json_and_redirect_do_not_leak_secrets(self):
        for path, code, options in [("/slow", "MODEL_TIMEOUT", {"timeout_seconds": 0.1}), ("/error", "MODEL_HTTP_ERROR", {}), ("/invalid-json", "MODEL_INVALID_RESPONSE", {}), ("/redirect", "MODEL_HTTP_ERROR", {})]:
            with self.assertRaises(ModelAPIError) as raised:
                self.client(path, **options).predict("CCO", "identity")
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn("fixture-secret", str(raised.exception))
        self.assertEqual(sum(item["path"] == "/json" for item in Handler.received), 0)

    def test_missing_configuration_does_not_return_fake_predictions(self):
        client = ModelClient(ModelConfig())
        self.assertFalse(client.get_public_config()["configured"])
        with self.assertRaises(ModelAPIError) as raised:
            client.predict("CCO", "identity")
        self.assertEqual(raised.exception.code, "MODEL_NOT_CONFIGURED")
        self.assertFalse(Handler.received)

    def test_provider_registry_and_public_config_only(self):
        registry = get_model_registry({"TOXILENS_QWEN_API_KEY": "fixture-secret", "TOXILENS_QWEN_MODEL_NAME": "vision-fixture", "TOXILENS_QWEN_MODEL_VERSION": "snapshot-fixture", "TOXILENS_QWEN_SEND_IMAGE": "true"})
        self.assertEqual(set(registry), {"openai", "deepseek", "qwen", "glm", "custom"})
        self.assertTrue(registry["qwen"].get_public_config()["configured"])
        self.assertTrue(registry["qwen"].get_public_config()["supportsImage"])
        self.assertNotIn("fixture-secret", json.dumps([value.get_public_config() for value in registry.values()]))

    def test_no_chat_probability_claim_and_no_external_plain_http(self):
        for config in [ModelConfig(api_url="http://example.com", model_name="x", model_version="v"), replace(self.client().config, api_format="openai", score_kind="probability")]:
            with self.assertRaises(ModelAPIError):
                ModelClient(config).predict("CCO", "identity")

    def test_binary_labels_and_prediction_metadata_require_evidence(self):
        with self.assertRaises(ModelAPIError):
            self.client(score_kind="binary_label").predict("CCO", "identity")
        payload = copy.deepcopy(VALID)
        payload["calibration"] = {"status": "validated"}
        Handler.overrides["/json"] = payload
        self.assertEqual(self.client().predict("CCO", "identity")["calibration"]["status"], "unknown")
        payload["calibration"].update(evidenceId="fixture-evidence", datasetVersion="fixture-dataset")
        result = self.client(score_kind="probability").predict("CCO", "identity")
        self.assertEqual(result["calibration"]["source"], "prediction-service-declaration")

    def test_optional_chat_parameters_support_models_that_reject_temperature_or_json_mode(self):
        self.client("/chat", api_format="openai", json_mode=False).predict("CCO", "identity")
        body = Handler.received[0]["body"]
        self.assertNotIn("temperature", body)
        self.assertNotIn("response_format", body)
        self.client("/chat", api_format="openai", temperature=0.5).predict("CCO", "identity")
        self.assertEqual(Handler.received[1]["body"]["temperature"], 0.5)

    def test_uncalibrated_scores_cannot_claim_probability_calibration(self):
        payload = copy.deepcopy(VALID)
        payload["calibration"] = {"status": "validated", "evidenceId": "fixture", "datasetVersion": "fixture"}
        Handler.overrides["/json"] = payload
        self.assertEqual(self.client().predict("CCO", "identity")["calibration"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
