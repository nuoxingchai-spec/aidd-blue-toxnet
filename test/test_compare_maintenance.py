"""Maintenance safety fixtures: no live daemon, model, or real quarantine change."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from backend.compare_adapters import CompareAdapterError
from backend.compare_persistence import CLEARANCE_SCHEMA, CLEARANCE_SCOPE, CompareRunStore
from scripts import verify_compare_cleanup as maintenance


def proof():
    return {"schema": CLEARANCE_SCHEMA, "ollamaEmpty": True, "ownedWorkersAbsent": True,
            "verifiedAt": datetime.now(timezone.utc).isoformat(), "scope": CLEARANCE_SCOPE}


class InventoryTests(unittest.TestCase):
    def test_windows_inventory_regex_matches_real_python_and_ollama_executable_names(self):
        rows = [{"Name": "python.exe", "CommandLine": "python.exe -m backend.compare_worker"},
                {"Name": "ollama.exe", "CommandLine": "ollama.exe serve"}]
        with patch.object(maintenance.os, "name", "nt"), patch.object(maintenance.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(rows).encode())) as execute:
            self.assertEqual(maintenance.process_inventory(), [(row["Name"], row["CommandLine"]) for row in rows])
        command = execute.call_args.args[0][-1]
        expression = re.search(r"-match\s+'([^']+)'", command).group(1)
        # PowerShell -match uses case-insensitive .NET regex; this simple pattern
        # has the same escape semantics in Python and catches double-escaping.
        compiled = re.compile(expression, re.IGNORECASE)
        for name in ("python.exe", "pythonw.exe", "python3.exe", "PYTHON.EXE", "ollama.exe", "ollama app.exe"):
            self.assertIsNotNone(compiled.fullmatch(name), expression)
        for name in ("powershell.exe", "pythonxexe", "ollamaxexe", "python.exe.tmp"):
            self.assertIsNone(compiled.fullmatch(name), expression)
        self.assertEqual(execute.call_args.kwargs["creationflags"], 0x08000000)
        self.assertNotIn("Stop-Process", command)
        self.assertIn("OutputEncoding", command)
        self.assertIn("UTF8Encoding", command)

    def test_uninspectable_relevant_process_inventory_fails_closed(self):
        for rows in ([{"Name": "python.exe", "CommandLine": None}], [{"Name": "python.exe", "CommandLine": 123}],
                     {"Name": "python.exe", "CommandLine": "fixture"}):
            with patch.object(maintenance.os, "name", "nt"), patch.object(maintenance.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(rows).encode())):
                with self.assertRaises(ValueError):
                    maintenance.process_inventory()

    def test_ollama_name_proves_presence_even_when_command_line_is_uninspectable(self):
        rows = [{"Name": "ollama.exe", "CommandLine": None}, {"Name": "ollama app.exe", "CommandLine": None}]
        with patch.object(maintenance.os, "name", "nt"), patch.object(maintenance.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(rows).encode())):
            self.assertEqual(maintenance.process_inventory(), [("ollama.exe", ""), ("ollama app.exe", "")])

    def test_windows_utf8_bom_and_unicode_process_command_line_are_decoded(self):
        rows = [{"Name": "python.exe", "CommandLine": '"C:\\测试夹具\\python.exe" -m backend.compare_worker'}]
        output = ("\ufeff" + json.dumps(rows, ensure_ascii=False)).encode("utf-8")
        with patch.object(maintenance.os, "name", "nt"), patch.object(maintenance.subprocess, "run", return_value=SimpleNamespace(stdout=output)):
            self.assertEqual(maintenance.process_inventory()[0][1], rows[0]["CommandLine"])

    def test_posix_inventory_contains_worker_and_daemon_without_real_process_query(self):
        output = b"python3 python3 -m backend.txgemma_worker\nollama /usr/bin/ollama serve\n"
        with patch.object(maintenance.os, "name", "posix"), patch.object(maintenance.subprocess, "run", return_value=SimpleNamespace(stdout=output)):
            self.assertEqual(maintenance.process_inventory(), [("python3", "python3 -m backend.txgemma_worker"), ("ollama", "/usr/bin/ollama serve")])


class CleanupProofTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def transport(self, status=200, payload=None, body=None):
        def respond(request):
            self.calls.append((request.method, str(request.url)))
            if body is not None:
                return httpx.Response(status, content=body)
            return httpx.Response(status, json={"models": []} if payload is None else payload)
        return httpx.MockTransport(respond)

    def unreachable(self):
        def respond(request):
            self.calls.append((request.method, str(request.url)))
            raise httpx.ConnectError("fixture daemon unavailable", request=request)
        return httpx.MockTransport(respond)

    def test_empty_loopback_api_and_no_worker_produces_narrow_proof_without_generation(self):
        result = maintenance.verify_cleanup("http://127.0.0.1:11434", inventory=[("python.exe", "python unrelated.py")], transport=self.transport())
        self.assertEqual(result["schema"], CLEARANCE_SCHEMA)
        self.assertEqual(result["scope"], CLEARANCE_SCOPE)
        self.assertTrue(result["ollamaEmpty"])
        self.assertTrue(result["ownedWorkersAbsent"])
        self.assertEqual(self.calls, [("GET", "http://127.0.0.1:11434/api/ps")])
        self.assertEqual(set(result), {"schema", "scope", "ollamaEmpty", "ownedWorkersAbsent", "verifiedAt"})

    def test_b16_or_txgemma_worker_prevents_clearance_even_with_empty_ollama(self):
        for module in ("backend.compare_worker", "backend.txgemma_worker"):
            with self.subTest(module=module):
                with self.assertRaises(ValueError):
                    maintenance.verify_cleanup("http://127.0.0.1:11434", inventory=[("python.exe", f'"C:\\fixture\\python.exe" -m {module}')], transport=self.transport())

    def test_unreachable_api_with_ollama_process_does_not_prove_unloaded(self):
        with self.assertRaises(ValueError):
            maintenance.verify_cleanup("http://127.0.0.1:11434", inventory=[("ollama.exe", "ollama serve")], transport=self.unreachable())
        with self.assertRaises(ValueError):
            maintenance.verify_cleanup("http://127.0.0.1:11434", inventory=[("ollama.exe", "")], transport=self.unreachable())
        result = maintenance.verify_cleanup("http://127.0.0.1:11434", inventory=[("ollama.exe", "")], transport=self.transport())
        self.assertTrue(result["ollamaEmpty"])

    def test_stopped_daemon_requires_both_api_failure_and_absent_processes(self):
        result = maintenance.verify_cleanup("http://127.0.0.1:11434", inventory=[], transport=self.unreachable())
        self.assertTrue(result["ollamaEmpty"])
        self.assertTrue(result["ownedWorkersAbsent"])
        with self.assertRaises(ValueError):
            maintenance.verify_cleanup("http://127.0.0.1:11434", inventory=[("python.exe", "python -m backend.compare_worker")], transport=self.unreachable())

    def test_nonloopback_https_credentials_query_and_path_are_refused_before_http(self):
        for url in ("http://evil.example:11434", "http://192.168.1.3:11434", "https://127.0.0.1:11434",
                    "http://user:password@localhost:11434", "http://localhost:11434?api_key=fixture", "http://localhost:11434/private"):
            with self.subTest(url=url), self.assertRaises(CompareAdapterError):
                maintenance.verify_cleanup(url, inventory=[], transport=self.transport())
        self.assertEqual(self.calls, [])

    def test_busy_models_malformed_body_http_failure_and_oversize_are_refused(self):
        transports = [self.transport(payload={"models": [{"name": "fixture-qwen", "size_vram": 100}]}),
                      self.transport(payload={"models": "not-an-inventory"}),
                      self.transport(payload=[]), self.transport(status=503),
                      self.transport(body=b"not-json"), self.transport(body=b"x" * (128 * 1024 + 1)),
                      self.transport(status=302)]
        for transport in transports:
            with self.assertRaises(ValueError):
                maintenance.verify_cleanup("http://localhost:11434", inventory=[], transport=transport)
        self.assertTrue(all(method == "GET" and url.endswith("/api/ps") for method, url in self.calls))

    def test_process_inspection_failure_cannot_generate_clearance_proof(self):
        with patch.object(maintenance, "process_inventory", side_effect=ValueError("fixture inventory inaccessible")):
            with self.assertRaises(ValueError):
                maintenance.verify_cleanup("http://127.0.0.1:11434", transport=self.transport())
        self.assertEqual(self.calls, [])


class MaintenanceCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "runs.sqlite3"

    def tearDown(self):
        self.directory.cleanup()

    def call(self, *flags):
        output = io.StringIO()
        with patch.object(maintenance.sys, "argv", ["verify_compare_cleanup", "--database", str(self.database), *flags]), redirect_stdout(output):
            code = maintenance.main()
        return code, json.loads(output.getvalue())

    def test_default_verification_never_opens_database_or_changes_quarantine(self):
        with patch.object(maintenance, "verify_cleanup", return_value=proof()), patch.object(maintenance, "CompareRunStore") as store:
            code, result = self.call()
        self.assertEqual(code, 0)
        self.assertFalse(result["quarantineCleared"])
        self.assertFalse(result["globalGpuAvailabilityCertified"])
        store.assert_not_called()
        self.assertFalse(self.database.exists())

    def test_failed_verification_prevents_any_database_unlock_attempt(self):
        with patch.object(maintenance, "verify_cleanup", side_effect=ValueError("fixture failure")), patch.object(maintenance, "CompareRunStore") as store:
            code, result = self.call("--clear-quarantine")
        self.assertEqual(code, 1)
        self.assertFalse(result["quarantineCleared"])
        store.assert_not_called()

    def test_explicit_clear_updates_only_temporary_fixture_with_verified_proof(self):
        with CompareRunStore(self.database) as store:
            store.set_quarantine("INTERRUPTED_LOCAL_EXECUTION")
        with patch.object(maintenance, "verify_cleanup", side_effect=lambda _: proof()):
            code, result = self.call("--clear-quarantine")
        self.assertEqual(code, 0)
        self.assertTrue(result["quarantineCleared"])
        with CompareRunStore(self.database) as store:
            self.assertIsNone(store.get_quarantine())
            self.assertEqual(store.get_quarantine_state()["clearanceProof"]["scope"], CLEARANCE_SCOPE)

    def test_running_database_owner_blocks_cli_clear_and_preserves_quarantine(self):
        with CompareRunStore(self.database) as owner:
            owner.set_quarantine("LOCAL_MODEL_CLEANUP_UNCONFIRMED")
            with patch.object(maintenance, "verify_cleanup", side_effect=lambda _: proof()):
                code, result = self.call("--clear-quarantine")
            self.assertEqual(code, 1)
            self.assertFalse(result["quarantineCleared"])
            self.assertEqual(owner.get_quarantine(), "LOCAL_MODEL_CLEANUP_UNCONFIRMED")
            self.assertNotIn(str(self.database), json.dumps(result))


if __name__ == "__main__":
    unittest.main()
