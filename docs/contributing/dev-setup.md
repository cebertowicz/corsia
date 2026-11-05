# Developer setup

Choose where to work, run CORSIA with separate development data, and run targeted tests.

## Requirements

See the [requirements table](../../README.md#getting-started). This setup uses Python 3.11 and Node.js with npm.

## Choose where to work

- **Your everyday checkout** at `~/.corsia`, if you installed CORSIA from source. Make your feature branch there; `corsia restart` serves your edits, uncommitted ones included, against your real data. Commit or stash them before **Settings → System → Update now**, which refuses while the checkout has uncommitted edits.
- **A separate checkout** with its own development data, set up below. Use it for anything that could harm your data, such as a migration, and for running tests.

Try your change in the browser. The desktop app does not run your checkout's backend, even under `tauri dev`, and a desktop app that has taken over your source install stops it from serving your checkout.

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

The following example keeps the development workspace and application state in `~/corsia-dev-data`, outside the checkout so they can't be committed. Never point `CORSIA_WORKSPACE` or `CORSIA_STATE_DIR` at the CORSIA you use day to day. They move the workspace, database, logs and state, but not everything: if CORSIA is installed on this computer, the development instance still reads that install's `~/.corsia/config.json`, so Settings shows that install's network setup. Pick unused ports if 43871 or 43872 is already in use.

```bash
mkdir -p ~/corsia-dev-data/workspace ~/corsia-dev-data/state
export CORSIA_WORKSPACE=~/corsia-dev-data/workspace CORSIA_STATE_DIR=~/corsia-dev-data/state BACKEND_PORT=43871
cd backend
../.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 43871
```

Before it serves, the backend builds the frontend if the checkout has no current build, so the first start prints build output and takes longer. While the checkout has uncommitted edits it has no current build, so every start rebuilds. Wait for `Application startup complete`.

`BACKEND_PORT` must match `--port`. The backend uses it to address itself, so agent tool calls and sign-in callbacks reach this backend and not the default port, 1956, where your everyday CORSIA may be running.

Leave the backend running. In another terminal, from the checkout's `frontend` directory, start the frontend and point its proxy at that backend:

```bash
export CORSIA_BACKEND_URL=http://127.0.0.1:43871
npm run dev -- --host 127.0.0.1 --port 43872 --strictPort
```

Open `http://127.0.0.1:43872/` in a browser. If you chose a different frontend port, use that port in the address. Stop each server with Ctrl+C in its terminal.

## Tests

To check that the setup works, run one backend test from the checkout root with fresh temporary workspace and state directories:

```bash
CORSIA_TEST_ROOT=$(mktemp -d)
mkdir -p "$CORSIA_TEST_ROOT/workspace" "$CORSIA_TEST_ROOT/state"
CORSIA_WORKSPACE="$CORSIA_TEST_ROOT/workspace" CORSIA_STATE_DIR="$CORSIA_TEST_ROOT/state" PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_config_import_light.py -q
rm -rf "$CORSIA_TEST_ROOT"
```

How to test a change and what to run before you push are in [Testing and review](testing.md).

## Next steps

Continue with the [reading order](README.md#reading-order).

Checked against the code at `310115261`.
