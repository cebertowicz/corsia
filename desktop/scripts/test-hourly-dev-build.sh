#!/bin/bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin"
cat > "$tmp/bin/git" <<'EOF'
#!/bin/bash
case "$*" in
  *refs/heads/dev*) printf '%s\trefs/heads/dev\n' "$TEST_HEAD" ;;
  *refs/tags/*) [ -z "$TEST_PUBLISHED" ] || printf '%s\trefs/tags/v0.4.1-dev.1\n' "$TEST_PUBLISHED" ;;
esac
EOF
cat > "$tmp/bin/curl" <<'EOF'
#!/bin/bash
printf '{"version":"0.4.1-dev.1"}\n'
EOF
cat > "$tmp/bin/gh" <<'EOF'
#!/bin/bash
case "$*" in
  *commits*/status*) printf '{"statuses":%s}\n' "$TEST_STATUSES" ;;
  *status=queued*) printf '[{"workflow_runs":%s}]\n' "$TEST_QUEUED" ;;
  *status=in_progress*) printf '[{"workflow_runs":%s}]\n' "$TEST_RUNNING" ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$tmp/bin/git" "$tmp/bin/curl" "$tmp/bin/gh"
export PATH="$tmp/bin:$PATH" DEV_UPDATER_ENDPOINT=unused GITHUB_REPOSITORY=cebertowicz/corsia GITHUB_RUN_ID=1
export TEST_HEAD=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
export TEST_PUBLISHED=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
export TEST_STATUSES='[]' TEST_QUEUED='[]' TEST_RUNNING='[]' HOURLY_DEV_BUILD=true
check() {
  local label="$1" expected="$2" expected_code="${3:-0}" output code=0
  output="$(bash "$here/hourly-dev-build.sh" 2>&1)" || code=$?
  [ "$code" = "$expected_code" ] || { printf '%s: expected exit %s, got %s\n' "$label" "$expected_code" "$code" >&2; exit 1; }
  [ "$output" = "$expected" ] || { printf '%s: expected %s, got %s\n' "$label" "$expected" "$output" >&2; exit 1; }
  echo "ok: $label"
}
HOURLY_DEV_BUILD=false check off 'hourly dev build off'
TEST_PUBLISHED='' check 'missing published tag' 'published dev tag v0.4.1-dev.1 is missing; skipping build' 1
TEST_PUBLISHED="$TEST_HEAD" check same 'no new commits'
check 'no status' "would build $TEST_HEAD"
TEST_STATUSES='[{"context":"dev-build","state":"success"}]' check success "already attempted $TEST_HEAD (success)"
TEST_STATUSES='[{"context":"dev-build","state":"failure"}]' check 'failure including verify failure' "already attempted $TEST_HEAD (failure)"
TEST_STATUSES='[{"context":"dev-build","state":"error"}]' check cancelled "would build $TEST_HEAD"
TEST_STATUSES='[{"context":"dev-build","state":"pending"}]' TEST_RUNNING='[{"id":1}]' check 'orphaned pending' "would build $TEST_HEAD"
TEST_STATUSES='[{"context":"dev-build","state":"pending"}]' TEST_RUNNING='[{"id":2}]' check 'live pending' 'another release build is queued or in progress'
TEST_QUEUED='[{"id":3,"event":"workflow_dispatch"}]' check 'queued manual dispatch' 'another release build is queued or in progress'
TEST_RUNNING='[{"id":4,"event":"workflow_dispatch","head_branch":"main"}]' check 'live stable build' 'another release build is queued or in progress'
