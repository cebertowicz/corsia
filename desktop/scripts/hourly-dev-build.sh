#!/bin/bash
# Decide whether this scheduled run should build the current dev commit.
set -euo pipefail

if [ "${HOURLY_DEV_BUILD:-}" != true ]; then
  echo 'hourly dev build off'
  exit 0
fi

repo="${GITHUB_REPOSITORY:-cebertowicz/corsia}"
remote="https://github.com/$repo.git"
head_sha="$(git ls-remote "$remote" refs/heads/dev | cut -f1)"
[[ "$head_sha" =~ ^[0-9a-f]{40}$ ]] || { echo 'cannot resolve dev head' >&2; exit 1; }
feed="$(curl --fail --silent --show-error --location "$DEV_UPDATER_ENDPOINT")"
version="$(jq -er '.version | select(type == "string" and test("^[0-9]+\\.[0-9]+\\.[0-9]+-dev\\.[0-9]+$"))' <<<"$feed")"
published_sha="$(git ls-remote "$remote" "refs/tags/v$version" | cut -f1)"
if [ -z "$published_sha" ]; then
  echo "published dev tag v$version is missing; skipping build" >&2
  exit 0
fi
[[ "$published_sha" =~ ^[0-9a-f]{40}$ ]] || { echo 'invalid published dev tag' >&2; exit 1; }
if [ "$head_sha" = "$published_sha" ]; then
  echo 'no new commits'
  exit 0
fi

for state in queued in_progress; do
  runs="$(gh api --paginate --slurp "repos/$repo/actions/workflows/release-macos.yml/runs?status=$state&per_page=100")"
  if jq -e --arg current "${GITHUB_RUN_ID:-}" '[.[].workflow_runs[] | select((.id | tostring) != $current)] | length > 0' <<<"$runs" >/dev/null; then
    echo 'another release build is queued or in progress'
    exit 0
  fi
done
status_json="$(gh api "repos/$repo/commits/$head_sha/status")"
status="$(jq -r '[.statuses[] | select(.context == "dev-build")][0].state // "none"' <<<"$status_json")"
case "$status" in
  success|failure) echo "already attempted $head_sha ($status)"; exit 0 ;;
  none|error|pending) ;;
  *) echo "unexpected dev-build status: $status" >&2; exit 1 ;;
esac

echo "would build $head_sha"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  printf 'commit_sha=%s\nshould_build=true\n' "$head_sha" >> "$GITHUB_OUTPUT"
fi
