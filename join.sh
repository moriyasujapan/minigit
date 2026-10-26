#!/bin/sh
# split.sh / split.cmd で分割したファイルを結合する (Linux / macOS)
#   使い方: ./join.sh [元のファイル名 既定 minigit.py]
#   ファイル.sha256 があればハッシュを照合する
set -eu

f=${1:-minigit.py}
set -- "$f".part.[0-9][0-9][0-9]
[ -f "$1" ] || { echo "分割ファイルがありません: $f.part.000 ..." >&2; exit 1; }

cat "$@" > "$f"
echo "$# 個を結合しました -> $f ($(wc -c < "$f" | tr -d ' ') bytes)"

if [ -f "$f.sha256" ]; then
    expected=$(cut -d' ' -f1 "$f.sha256")
    if command -v sha256sum >/dev/null 2>&1; then
        actual=$(sha256sum "$f" | cut -d' ' -f1)
    else
        actual=$(shasum -a 256 "$f" | cut -d' ' -f1)
    fi
    if [ "$expected" = "$actual" ]; then
        echo "SHA256 一致: $actual"
    else
        echo "SHA256 不一致! 期待値 $expected / 実際 $actual" >&2
        echo "(改行コードが変換された場合も不一致になります)" >&2
        exit 2
    fi
fi
