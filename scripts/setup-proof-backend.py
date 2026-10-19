#!/usr/bin/env python3
"""Start a disposable setup proof backend only after asserting its actual paths."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main() -> None:
    """Verify imported runtime configuration before any listener can answer health."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--mock', action='store_true')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    assert root.is_relative_to(Path('/tmp').resolve()), 'proof root must be under /tmp'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
    from infra import config, database
    paths = {'state_root': str(config.STATE_ROOT.resolve()), 'database': str(Path(database.DB_PATH).resolve()), 'workspace': str(config.WORKSPACE.resolve())}
    assert all(Path(value).is_relative_to(root) for value in paths.values()), 'runtime isolation failed'
    assert args.port not in {1956, 3001}, 'live port refused'
    print('SETUP_PROOF_RUNTIME ' + json.dumps(paths), flush=True)

    import uvicorn
    uvicorn.run('e2e_app:app' if args.mock else 'main:app', host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
