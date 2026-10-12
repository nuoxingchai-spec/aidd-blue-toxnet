"""Test-only HTTP upstream and API app. NOT a real prediction model.

Run .venv/Scripts/python.exe -m test.browser_fixture, then visit port 4174.
The normal application on port 4173 is unaffected. No external calls or keys.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import uvicorn
from backend.main import create_app
from backend.model_client import ModelClient, ModelConfig


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert payload["smiles"] and payload["structureId"]
        assert payload["imageDataUrl"].startswith("data:image/png;base64,")
        time.sleep(1)
        if payload["smiles"] == "N#N":
            self.send_response(503)
            self.end_headers()
            return
        result = {"modelVersion": "browser-fixture-v1", "endpoints": [{"id": identifier, "score": score} for identifier, score in zip(("herg", "dili", "ames", "clintox"), (0.2, 0.7, 0.4, 0.5))]}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(json.dumps(result).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    worker = threading.Thread(target=fixture.serve_forever, daemon=True)
    worker.start()
    client = ModelClient(ModelConfig(api_url=f"http://127.0.0.1:{fixture.server_port}/predict", model_name="browser-test-fixture-not-real-model", model_version="browser-fixture-v1", send_image=True))
    try:
        uvicorn.run(create_app({"custom": client}, "custom"), host="127.0.0.1", port=4174)
    finally:
        fixture.shutdown()
        fixture.server_close()
