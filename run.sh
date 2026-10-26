#!/bin/sh
# minigit を起動する (Mac / Linux)
#   使い方: ./run.sh [ポート 既定 8080]
cd "$(dirname "$0")"
export MINIGIT_ROOT="${MINIGIT_ROOT:-$PWD/repos}"
exec python3 minigit.py "${1:-8080}"
