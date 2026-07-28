#!/bin/sh
# ---------------------------------------------------------------------
#  YUV -> RAW 일괄 변환기 (macOS / Linux 실행용)
#
#    ./convert_folder.sh /경로/yuv_폴더
#
#  결과는 <입력폴더>/raw_out 에 새 파일로 생성되며, 원본은 건드리지 않습니다.
# ---------------------------------------------------------------------
set -e

OPTIONS="--out-format rgb24"
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SCRIPT="$DIR/yuv2raw.py"

if [ ! -f "$SCRIPT" ]; then
    echo "[오류] yuv2raw.py 를 찾을 수 없습니다: $SCRIPT" >&2
    exit 1
fi

TARGET="$1"
if [ -z "$TARGET" ]; then
    printf '변환할 폴더 경로를 입력하세요: '
    read -r TARGET
fi
if [ -z "$TARGET" ]; then
    echo "입력된 경로가 없습니다." >&2
    exit 1
fi

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
exec "$PY" "$SCRIPT" "$TARGET" $OPTIONS
