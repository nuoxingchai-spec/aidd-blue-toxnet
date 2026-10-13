"""Explicit persistent MOCK browser fixture; never invokes actual model APIs.

Use a temporary local directory. No predicted fixture score is model evidence.
"""
import argparse
from pathlib import Path

from backend.main import create_app
from backend.compare_schemas import MODEL_IDS
from test.compare_fixture import fixture_service
from test.test_api import FakeLocalModel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--port', type=int, default=4186)
    parser.add_argument('--report-delay', type=float, default=12)
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    service = fixture_service(persistence_path=directory / 'runs.sqlite3',
        presets_path=directory / 'presets.json')
    service.adapters[MODEL_IDS[0]].delay = .2
    service.adapters[MODEL_IDS[2]].delay = .2
    service.adapters[MODEL_IDS[3]].delay = args.report_delay
    app = create_app(registry={'local': FakeLocalModel()}, env={},
        config_path=directory / '.env', compare_service=service)

    @app.get('/fixture/stats')
    async def stats():
        return {'testFixture': True, 'actualModelsExecuted': False,
            'calls': {key: len(adapter.calls) for key, adapter in service.adapters.items()}}

    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
