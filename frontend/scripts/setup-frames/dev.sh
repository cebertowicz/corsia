#!/bin/zsh
# Disposable frontend for the setup design frames: loopback port 5191 and no backend
# (port 1 refuses connections, so the dev proxy can never reach a real backend).
cd "$(dirname "$0")/../.." || exit 1
export CORSIA_BACKEND_URL="${SETUP_FRAMES_BACKEND:-http://127.0.0.1:1}"
exec npm run dev -- --host 127.0.0.1 --port "${SETUP_FRAMES_PORT:-5191}" --strictPort
