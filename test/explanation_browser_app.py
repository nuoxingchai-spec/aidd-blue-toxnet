"""Loopback browser-verification app using REAL v2 weights, not fixture scores.

No external model provider is registered and no API key is read. Port 4176 is
temporary QA; it does not replace or restart the workbench on 4173.
"""
from pathlib import Path
import torch
torch.set_num_threads(6)
from backend.local_model import LocalModelClient
from backend.main import create_app

ROOT = Path(__file__).resolve().parents[1]
local = LocalModelClient(env={})
app = create_app(registry={'local': local}, default_provider='local', local_client=local,
                 config_path=ROOT / 'artifacts/explanation-no-config.env',
                 env={'TOXNET_EVIDENCE_INDEX_DIR': str(ROOT / 'data/evidence-index')})
