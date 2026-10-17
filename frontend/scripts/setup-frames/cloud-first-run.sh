#!/bin/zsh
# Starts the disposable frontend (port 5191, no backend), runs <script> against it, stops the server.
# Usage: cloud-first-run.sh <script.mjs|script.py ...>   e.g. cloud-first-run.sh cloud-first.mjs <outDir> <metricsFile> --assets <dir>
cd "$(dirname "$0")/../.." || exit 1
scripts/setup-frames/dev.sh > "${TMPDIR:-/tmp}/cloud-first-dev.log" 2>&1 &
server=$!
trap 'kill $server 2>/dev/null; pkill -P $server 2>/dev/null' EXIT
for _ in $(seq 1 60); do
	curl -s -o /dev/null --max-time 2 "http://127.0.0.1:5191/" && break
	sleep 1
done
script=$1; shift
node "scripts/setup-frames/$script" "$@"
