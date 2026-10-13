"""Explicit local research report; never substitute external model predictions."""
import argparse
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.configuration import ModelConfigurationStore
from backend.local_model import LocalModelClient
from backend.multitask_model import MultitaskModelClient
from backend.explanation_service import ExplanationService
from backend.model_client import ModelAPIError

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--smiles', required=True); p.add_argument('--endpoint', default='herg')
    p.add_argument('--model', choices=('v2','v3'), default='v2'); p.add_argument('--steps', type=int, default=32)
    p.add_argument('--include-language', action='store_true'); p.add_argument('--language-provider')
    p.add_argument('--output', type=Path)
    a = p.parse_args()
    if a.output and a.output.exists(): p.error('Output exists; choose a new file and preserve history')
    config = ModelConfigurationStore(ROOT / '.env')
    service = ExplanationService(LocalModelClient(env=config.startup_values), config,
                                 research_client=MultitaskModelClient.from_env(config.startup_values))
    try:
        result = service.explain(a.smiles, endpoint=a.endpoint, model=a.model, steps=a.steps,
                                 include_language=a.include_language, language_provider=a.language_provider)
    except (ModelAPIError, ValueError) as e:
        p.exit(2, f'{getattr(e,"code","INVALID_INPUT")}: {e}\n')
    text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True); a.output.write_text(text, encoding='utf-8')
        print(json.dumps({'status':result['status'],'reportVersion':result['report']['reportVersion'],'output':str(a.output)}))
    else: print(text)
