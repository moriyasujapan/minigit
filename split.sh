#!/bin/sh
# ファイルを行の途中で切らずに分割する (Linux / macOS)
#   使い方: ./split.sh ファイル [1パートの最大KB 既定40]
#   出力  : ファイル.part.000, .part.001, ... と ファイル.sha256
set -eu

f=${1:?使い方: ./split.sh ファイル [1パートの最大KB]}
kb=${2:-40}
[ -f "$f" ] || { echo "ファイルがありません: $f" >&2; exit 1; }

rm -f "$f".part.[0-9][0-9][0-9]

# LC_ALL=C で length() をバイト数として数える
LC_ALL=C awk -v max=$((kb * 1024)) -v pre="$f.part." '
BEGIN { n = 0; size = 0; out = sprintf("%s%03d", pre, n) }
{
    len = length($0) + 1
    if (size > 0 && size + len > max) {
        close(out); n++; size = 0
        out = sprintf("%s%03d", pre, n)
    }
    printf "%s\n", $0 > out
    size += len
}' "$f"

if command -v sha256sum >/dev/null 2>&1; then
    h=$(sha256sum "$f" | cut -d' ' -f1)
else
    h=$(shasum -a 256 "$f" | cut -d' ' -f1)
fi
printf '%s  %s\n' "$h" "$(basename "$f")" > "$f.sha256"

for p in "$f".part.[0-9][0-9][0-9]; do
    printf '%s  %s bytes\n' "$p" "$(wc -c < "$p" | tr -d ' ')"
done
echo "SHA256: $h"
