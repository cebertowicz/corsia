# Focus-open baseline harness

Measurement only; no product code. Baseline: `origin/train/product-experience` aa8d2155.

Setup (disposable roots, mock backend serving the built `frontend/dist`):

    python3.11 -m venv .venv && .venv/bin/pip install -r backend/requirements.lock
    (cd frontend && npm ci && npm run build)
    R=$(mktemp -d); mkdir $R/ws $R/state
    export CORSIA_WORKSPACE=$R/ws CORSIA_STATE_DIR=$R/state CORSIA_TEST_MODE=1 CORSIA_ANALYTICS_DISABLE=1
    # account + token: scripts/corsia.d/accounts.sh seed_disposable_account; keep the token in a file
    (cd backend && CORSIA_E2E_MOCK=1 ../.venv/bin/python -m uvicorn e2e_app:app --port 3231) &
    cd backend
    PYTHONPATH=. ../.venv/bin/python ../benchmarks/focus_open/seed.py --api http://127.0.0.1:3231 --token-file TOKEN [--tail working] > threads.json
    node ../benchmarks/focus_open/measure.mjs --base http://127.0.0.1:3231 --token-file TOKEN --threads threads.json --runs 3 --out browser.json
    PYTHONPATH=. ../.venv/bin/python ../benchmarks/focus_open/server_timing.py threads.json --pages 13 --reps 5

`seed.py` titles threads `focus-baseline <label>`; `measure.mjs` clicks that title in the sidebar.
Run `seed.py` once per `--tail` (idle, working) and merge the maps; the first batch of this run was
seeded before the label carried the tail suffix, so its titles were renamed `-idle` in the DB.
`results/` holds the run these numbers came from.

## Focus walk timing and the scan cap

    PYTHONPATH=. ../.venv/bin/python ../benchmarks/focus_open/seed.py --api http://127.0.0.1:3231 --token-file TOKEN \
        --prod-bodies --sizes 20000,58000 --ratios 0p7pct,2pct --tail idle > threads.json
    PYTHONPATH=. ../.venv/bin/python ../benchmarks/focus_open/focus_walk_timing.py threads.json --caps 4000,12000

`focus_walk_timing.py` walks the `view=focus` chain in process and reports walk time per page, rows scanned and
ms per scanned row; `FOCUS_SCAN_CAP` is set from it. Run `measure.mjs` once against a build of the base and once
against the change to get the before/after table. `results-focus-open/` holds the run behind the cap.
