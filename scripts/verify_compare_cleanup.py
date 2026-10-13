"""Explicit maintenance, never kills processes or starts a model/API generation.

Stop the Compare service first. Clearance requires an empty configured loopback
Ollama and no B16/TxGemma worker. --clear-quarantine is a separate operator step.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.compare_adapters import validate_ollama_url
from backend.compare_persistence import CompareRunStore, default_history_path


def process_inventory():
    if os.name == 'nt':
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            r"$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); $p=@(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(python.*|ollama.*)\.exe$' } | Select-Object Name,CommandLine); ConvertTo-Json -InputObject $p -Compress"]
        raw = subprocess.run(command, check=True, capture_output=True, timeout=10, creationflags=0x08000000).stdout
        rows = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(rows, list):
            raise ValueError('Process inspection unavailable')
        if any(not isinstance(row, dict) or not isinstance(row.get('Name'), str)
               or (row.get('CommandLine') is None and not row['Name'].casefold().startswith('ollama'))
               or (row.get('CommandLine') is not None and not isinstance(row['CommandLine'], str)) for row in rows):
            raise ValueError('Some relevant processes cannot be inspected')
        # Ollama existence is established by Name; its command line is not used
        # to prove absence/unload. Uninspectable Python still prevents clearance.
        return [(row['Name'], row.get('CommandLine') or '') for row in rows]
    raw = subprocess.run(['ps', '-A', '-o', 'comm=,args='], check=True, capture_output=True, timeout=10).stdout.decode()
    return [(line.split(None, 1)[0], line.split(None, 1)[1]) for line in raw.splitlines() if len(line.split(None, 1)) == 2]


def verify_cleanup(url, *, inventory=None, transport=None):
    import httpx
    url = validate_ollama_url(url)
    rows = process_inventory() if inventory is None else inventory
    owned_absent = not any(re.search(r'(?:^|\s)-m\s+backend\.(?:compare_worker|txgemma_worker)(?:\s|$)', command) for _, command in rows)
    ollama_process_present = any('ollama' in name.casefold() for name, _ in rows)
    ollama_empty = False
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=3, transport=transport) as client:
            response = client.get(url + '/api/ps')
        if response.status_code != 200 or len(response.content) > 128 * 1024:
            raise ValueError('Ollama state unavailable')
        payload = response.json()
        ollama_empty = isinstance(payload, dict) and isinstance(payload.get('models'), list) and len(payload['models']) == 0
    except httpx.HTTPError:
        # API failure alone never proves unloading. Absence of all relevant
        # inspected Ollama processes is separately required for a stopped app.
        ollama_empty = not ollama_process_present
    if not owned_absent or not ollama_empty:
        raise ValueError('Cleanup cannot be confirmed')
    return {'schema': 'aidd.compare.quarantine-clearance.v1', 'ollamaEmpty': True,
            'ownedWorkersAbsent': True, 'verifiedAt': datetime.now(timezone.utc).isoformat(),
            'scope': 'compare-owned-workers-and-configured-loopback-ollama'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default=str(default_history_path(ROOT)))
    parser.add_argument('--ollama-url', default=os.environ.get('TOXNET_OLLAMA_URL', 'http://127.0.0.1:11434'))
    parser.add_argument('--clear-quarantine', action='store_true')
    args = parser.parse_args()
    try:
        proof = verify_cleanup(args.ollama_url)
        if args.clear_quarantine:
            with CompareRunStore(args.database) as store:
                store.clear_quarantine(proof)
        print(json.dumps({'status': 'verified', 'quarantineCleared': args.clear_quarantine,
                          'scope': proof['scope'], 'globalGpuAvailabilityCertified': False}))
        return 0
    except Exception:
        print(json.dumps({'status': 'unavailable', 'quarantineCleared': False,
                          'errorCode': 'CLEANUP_OR_DATABASE_OWNERSHIP_UNCONFIRMED',
                          'message': 'Stop Compare, confirm Ollama unload and worker exit, then retry maintenance; no state was cleared.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
