#!/bin/bash
# Print every Mach-O file under a directory, NUL-separated.
# usage: list-macho.sh <dir>
#
# The same test as `file -b <path> | grep -q Mach-O`, but one `file` process per
# xargs batch instead of two processes per file: the bundle holds tens of
# thousands of files, and the per-file scan took most of the ~9 minutes the
# release build spent sealing the app. `file --print0` ends each path with NUL;
# a universal binary adds "(for architecture ...)" lines with no NUL, so a
# record's path is the text after the last newline before its NUL. A path
# containing a newline is refused rather than misread.
set -euo pipefail

[ "$#" -eq 1 ] || { echo "usage: $0 <dir>" >&2; exit 64; }
dir="$1"
[ -d "$dir" ] || { echo "error: not a directory: $dir" >&2; exit 1; }
[ -z "$(find "$dir" -name $'*\n*' -print -quit)" ] || { echo "error: a path under $dir contains a newline" >&2; exit 1; }

path=""
find "$dir" -type f -print0 | xargs -0 file -r --print0 -- | while IFS= read -r -d '' chunk || [ -n "$chunk" ]; do
  if [ -n "$path" ]; then
    desc="${chunk%%$'\n'*}"
    case "$desc" in *Mach-O*) printf '%s\0' "$path" ;; esac
  fi
  path="${chunk##*$'\n'}"
done
