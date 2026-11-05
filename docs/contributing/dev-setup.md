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

The following example keeps the workspace and application state in `.dev-data` inside the checkout. These are development data: do not commit them or point them at an existing CORSIA installation. Pick unused ports if 43871 or 43872 is already in use.

```bash
mkdir -p .dev-data/workspace .dev-data/state .dev-data/cache .dev-data/logs
export CORSIA_WORKSPACE="$PWD/.dev-data/workspace" CORSIA_STATE_DIR="$PWD/.dev-data/state" CORSIA_TEST_MODE=1 CORSIA_ANALYTICS_DISABLE=1 CORSIA_ALLOW_HOME_WORKSPACE_IN_CI=1 XDG_CACHE_HOME="$PWD/.dev-data/cache"
cd backend
../.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 43871
```

Leave the backend running. In another terminal, from the checkout's `frontend` directory, start the frontend and point its proxy at that backend:

```bash
read -r CORSIA_BACKEND_URL <<< http://127.0.0.1:43871
export CORSIA_BACKEND_URL
npm run dev -- --host 127.0.0.1 --port 43872 --strictPort
```

Open `http://127.0.0.1:43872/` in a browser. If you chose a different frontend port, use that port in the address. Stop each server with Ctrl+C in its terminal.

## A second checkout

For risky changes such as migrations, use a second checkout with its own workspace, state directory, and backend port, as described in the everyday commands in the [README](../../README.md). The second-checkout setup has not been verified by this page.

## Tests

Run a targeted backend test from the checkout root with disposable workspace and state directories. For example, after making `.dev-data` as above:

```bash
CORSIA_WORKSPACE="$PWD/.dev-data/workspace" CORSIA_STATE_DIR="$PWD/.dev-data/state" CORSIA_TEST_MODE=1 CORSIA_ANALYTICS_DISABLE=1 CORSIA_ALLOW_HOME_WORKSPACE_IN_CI=1 PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_config_import_light.py -q
```

Choose tests that cover your change. CI runs the backend full suite; do not run it locally.

Run the frontend tests from `frontend/`:

```bash
npm run test
```

Before a pull request, run the local gate from the checkout root:

```bash
CHECK_PR_PYTHON="$PWD/.venv/bin/python" bash scripts/check-pr
```

The gate reports whether it could check public files for your identifying information. Run the targeted tests for your change separately.

## Next steps

- Read the [contributor guide](README.md).
- Learn how to [write documentation](writing-docs.md).
- Explore [how CORSIA works](../how-it-works/README.md).
- Read the [architecture](../../ARCHITECTURE.md).

Checked against the code at `7a8c55d84`.
