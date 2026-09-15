#!/bin/bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
    printf 'usage: %s RPM_PATH\n' "$0" >&2
    exit 2
fi

rpm_path=$(realpath -- "$1")
if [ ! -f "$rpm_path" ]; then
    printf 'RPM not found: %s\n' "$rpm_path" >&2
    exit 1
fi

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

rpm -K "$rpm_path"

if ! file_list=$(rpm -qlp "$rpm_path"); then
    fail "rpm -qlp query failed"
fi
usr_bin_files=$(awk '/^\/usr\/bin\//' <<< "$file_list")
[ "$usr_bin_files" = "/usr/bin/cooler-btop" ] ||
    fail "RPM must own only /usr/bin/cooler-btop under /usr/bin and must not own /usr/bin/btop"

reject_btop_capability() {
    query=$1
    if ! query_output=$(rpm -qp "$query" "$rpm_path"); then
        fail "rpm -qp $query query failed"
    fi
    if awk '$1 == "btop" { found = 1 } END { exit !found }' <<< "$query_output"; then
        fail "RPM $query contains the btop capability"
    fi
}

reject_btop_capability --provides
reject_btop_capability --conflicts
reject_btop_capability --obsoletes

if ! scripts=$(rpm -qp --scripts "$rpm_path"); then
    fail "rpm -qp --scripts query failed"
fi
[ -z "$scripts" ] || fail "RPM contains package scripts"

if ! triggers=$(rpm -qp --triggers "$rpm_path"); then
    fail "rpm -qp --triggers query failed"
fi
[ -z "$triggers" ] || fail "RPM contains package triggers"

if ! filecaps=$(rpm -qp --qf '[%{FILENAMES}\t%{FILECAPS}\n]' "$rpm_path"); then
    fail "RPM file capability query failed"
fi
[ -n "$filecaps" ] || fail "RPM file capability query returned no entries"
while IFS=$'\t' read -r filename capabilities; do
    [ -n "$filename" ] || fail "RPM file capability query returned an empty filename"
    [ "$capabilities" = "(none)" ] || fail "unexpected file capability: $filename $capabilities"
done <<< "$filecaps"

extract_dir=$(mktemp -d)
trap 'rm -rf "$extract_dir"' EXIT HUP INT TERM
(
    cd "$extract_dir"
    rpm2cpio "$rpm_path" | cpio -idm --quiet
)

python3 - "$extract_dir" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
native_suffixes = {".a", ".dll", ".dylib", ".exe", ".lib", ".node", ".o", ".pyd", ".so"}
native_magics = (
    b"\x7fELF",
    b"MZ",
    b"\xca\xfe\xba\xbe",
    b"\xca\xfe\xba\xbf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xbe\xba\xfe\xca",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xbf\xba\xfe\xca",
    b"!<arch>\n",
)

for path in root.rglob("*"):
    if not path.is_file() or path.is_symlink():
        continue
    relative = "/" + path.relative_to(root).as_posix()
    name = path.name.lower()
    if path.suffix.lower() in native_suffixes or ".so." in name:
        raise SystemExit(f"native payload filename: {relative}")
    with path.open("rb") as stream:
        header = stream.read(8)
    if header.startswith(native_magics):
        raise SystemExit(f"native payload magic: {relative}")

launcher = root / "usr/bin/cooler-btop"
try:
    launcher.read_text(encoding="utf-8")
except (OSError, UnicodeError) as error:
    raise SystemExit(f"console launcher is not text: {error}") from error
PY

set -- "$extract_dir"/usr/lib/python*/site-packages
[ "$#" -eq 1 ] && [ -d "$1" ] || fail "expected exactly one extracted site-packages directory"
site_packages=$1
launcher=$extract_dir/usr/bin/cooler-btop
[ -x "$launcher" ] || fail "extracted cooler-btop launcher is not executable"

version=$(PYTHONPATH="$site_packages" "$launcher" --version)
[ "$version" = "Cooler btop v2.0.0" ] || fail "unexpected extracted launcher version: $version"
