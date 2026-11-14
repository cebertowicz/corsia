#!/bin/bash
# list-macho.sh finds exactly the Mach-O files: universal and thin binaries, a
# path with a space, and not plain text or a Java class (same CAFEBABE magic as
# a universal binary). A path with a newline is refused. macOS only.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/app/sp ace" "$work/app/deep/er"
cp /bin/ls "$work/app/sp ace/universal"
cp /usr/lib/dyld "$work/app/deep/er/dyld"
# A thin (single-architecture) binary: the shape of most Mach-O files in the bundle.
thin_arch="$(lipo -archs /bin/ls | awk '{print $1}')"
lipo -thin "$thin_arch" /bin/ls -output "$work/app/deep/thin"
printf 'text\n' >"$work/app/notes.txt"
printf '\xca\xfe\xba\xbe\x00\x00\x00\x34rest' >"$work/app/Foo.class"

got="$("$here/list-macho.sh" "$work/app" | tr '\0' '\n' | sort)"
want="$(printf '%s\n' "$work/app/deep/er/dyld" "$work/app/deep/thin" "$work/app/sp ace/universal" | sort)"
[ "$got" = "$want" ] || { printf 'FAIL: list-macho found:\n%s\nwanted:\n%s\n' "$got" "$want" >&2; exit 1; }

mkdir "$work/app/bad"$'\n'"name"
if "$here/list-macho.sh" "$work/app" >/dev/null 2>&1; then
  echo "FAIL: list-macho accepted a path with a newline" >&2
  exit 1
fi
echo "list-macho tests passed"
