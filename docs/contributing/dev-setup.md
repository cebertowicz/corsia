# Developer setup

Set up a checkout, run CORSIA with separate development data, and run targeted tests.

## Requirements

See the [requirements table](../../README.md#getting-started). This setup uses Python 3.11 and Node.js with npm.

## Clone and install

Choose a directory for the source checkout:

```bash
git clone --branch dev https://github.com/cebertowicz/corsia.git ~/corsia-src
cd ~/corsia-src
python3.11 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock
```

Install the frontend dependencies:

```bash
cd frontend
npm ci
cd ..
```

## Run the app

The following example keeps the workspace and application state in `.dev-data` inside the checkout. Do not commit these development data. For risky changes such as migrations, use the same setup in a second checkout with its own workspace, state directory, and ports. Never point `CORSIA_WORKSPACE` or `CORSIA_STATE_DIR` at the CORSIA you use day to day. Pick unused ports if 43871 or 43872 is already in use.

```bash
mkdir -p .dev-data/workspace .dev-data/state .dev-data/cache .dev-data/logs
export CORSIA_WORKSPACE="$PWD/.dev-data/workspace" CORSIA_STATE_DIR="$PWD/.dev-data/state" XDG_CACHE_HOME="$PWD/.dev-data/cache"
cd backend
../.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 43871
```

Leave the backend running. In another terminal, from the checkout's `frontend` directory, start the frontend and point its proxy at that backend:

```bash
export CORSIA_BACKEND_URL=http://127.0.0.1:43871
npm run dev -- --host 127.0.0.1 --port 43872 --strictPort
```

Open `http://127.0.0.1:43872/` in a browser. If you chose a different frontend port, use that port in the address. Stop each server with Ctrl+C in its terminal.

## Tests

Run a targeted backend test from the checkout root with fresh temporary workspace and state directories:

```bash
CORSIA_TEST_ROOT=$(mktemp -d)
mkdir -p "$CORSIA_TEST_ROOT/workspace" "$CORSIA_TEST_ROOT/state"
CORSIA_WORKSPACE="$CORSIA_TEST_ROOT/workspace" CORSIA_STATE_DIR="$CORSIA_TEST_ROOT/state" PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_config_import_light.py -q
rm -rf "$CORSIA_TEST_ROOT"
```

Choose tests that cover your change. CI runs the backend full suite; do not run it locally.

Run the frontend tests from `frontend/`:

```bash
npm run test
```

Before a pull request, run the local gate from the checkout root:

```bash
bash scripts/check-pr
```

Fix anything the gate marks as FAIL. Run the targeted tests for your change separately.

## Next steps

- Read the [contributor guide](README.md).
- Learn how to [write documentation](writing-docs.md).
- Explore [how CORSIA works](../how-it-works/README.md).
- Read the [architecture](../../ARCHITECTURE.md).

Checked against the code at `7a8c55d84`.
