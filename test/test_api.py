import unittest
from fastapi.testclient import TestClient
from backend.main import CONTRACT_VERSION, create_app
from backend.model_client import ModelAPIError


class FakeModel:
    def __init__(self, name="fixture", image=False):
        self.name, self.image, self.calls = name, image, []

    def get_public_config(self):
        return {"configured": True, "providerFormat": "json", "modelName": self.name, "version": "test-fixture-v1", "supportsImage": self.image}

    def predict(self, canonical, identity, image_data_url=None):
        self.calls.append((canonical, identity, image_data_url))
        return {"endpoints": [{"id": endpoint, "score": value} for endpoint, value in zip(("herg", "dili", "ames", "clintox"), (0.8, 0.4, 0.2, 0.3))], "provenance": {"modelName": self.name, "modelVersion": "test-fixture-v1", "scoreKind": "uncalibrated_score"}, "calibration": {"status": "unknown"}, "applicability": {"status": "unknown"}}


class FakeLocalModel(FakeModel):
    def get_model_info(self):
        return {"configured": True, "modelName": self.name, "parameterCount": 10, "status": "ready"}


class APITests(unittest.TestCase):
    def setUp(self):
        self.first, self.second = FakeModel(), FakeModel("vision-fixture", True)
        self.local = FakeLocalModel("local-test-fixture")
        self.client = TestClient(create_app({"custom": self.first, "qwen": self.second}, "custom", local_client=self.local))

    def test_rules_equivalent_structures_preserve_scores_and_reference_separation(self):
        left = self.client.post("/api/analyze", json={"smiles": "CC(=O)NC1=CC=C(O)C=C1", "mode": "rules"}).json()
        right = self.client.post("/api/analyze", json={"smiles": "CC(=O)Nc1ccc(O)cc1", "mode": "rules"}).json()
        self.assertEqual(left["structureId"], right["structureId"])
        self.assertEqual(left["endpoints"], right["endpoints"])
        self.assertEqual(left["provenance"]["contractVersion"], CONTRACT_VERSION)
        self.assertIsNone(left["provenance"]["model"])
        self.assertEqual(left["calibration"]["status"], "unknown")
        self.assertFalse(self.first.calls)
        dili = next(e for e in left["endpoints"] if e["id"] == "dili")
        self.assertEqual(dili["score"], dili["ruleScore"])
        self.assertNotEqual(dili["score"], dili["referenceScore"])

    def test_model_mode_uses_selected_provider_and_excludes_rule_explanation(self):
        response = self.client.post("/api/analyze", json={"smiles": "OCC", "provider": "qwen"})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertFalse(self.first.calls)
        self.assertEqual(self.second.calls[0][0], "CCO")
        self.assertTrue(self.second.calls[0][2].startswith("data:image/png;base64,"))
        self.assertEqual(result["endpoints"][0]["score"], 0.8)
        self.assertEqual(result["endpoints"][0]["explanationSource"], "none")
        self.assertEqual(result["endpoints"][0]["drivers"], [])
        self.assertEqual(result["provenance"]["model"]["providerId"], "qwen")
        self.assertAlmostEqual(result["overall"], 0.6875, delta=0.001)

    def test_invalid_molecules_fail_before_model_call(self):
        for smiles in ("CC=", "CC()", "C11", "CC(C)(C)(C)C"):
            response = self.client.post("/api/analyze", json={"smiles": smiles})
            self.assertEqual(response.status_code, 422)
        self.assertFalse(self.first.calls)

    def test_invalid_contract_and_unknown_provider_rejected(self):
        for payload in ({"smiles": True}, {"smiles": "CCO", "mode": "invalid"}, {"smiles": "CCO", "apiKey": "do-not-accept"}, {"smiles": "CCO", "provider": "missing"}):
            self.assertEqual(self.client.post("/api/analyze", json=payload).status_code, 422)

    def test_health_and_static_do_not_expose_backend_or_environment(self):
        health = self.client.get("/api/health").json()
        self.assertEqual({item["id"] for item in health["modelProviders"]}, {"custom", "qwen", "local"})
        self.assertNotIn("apiKey", str(health))
        for path in ("/.env", "/requirements.txt", "/backend/model_client.py", "/.git/config"):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_local_provider_routes_to_own_model_without_api_configuration(self):
        info = self.client.get("/api/local-model/info").json()
        self.assertEqual(info["modelName"], "local-test-fixture")
        response = self.client.post("/api/analyze", json={"smiles": "OCC", "provider": "local"})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(self.local.calls[0][0], "CCO")
        self.assertEqual(result["provenance"]["model"]["providerId"], "local")
        self.assertEqual(result["endpoints"][0]["evidence"], "本地自主模型输出")
        self.assertEqual(result["endpoints"][0]["explanationSource"], "none")
        self.assertFalse(self.first.calls)
        self.assertFalse(self.second.calls)

    def test_foreign_origin_cannot_issue_paid_model_calls(self):
        response = self.client.post("/api/analyze", json={"smiles": "CCO"}, headers={"Origin": "https://external.example"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.first.calls)


if __name__ == "__main__":
    unittest.main()
