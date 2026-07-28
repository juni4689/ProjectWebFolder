#!/bin/sh
# ---------------------------------------------------------------------
#  YUV -> RAW 일괄 변환기 (macOS / Linux 실행용)
#
#    ./convert_folder.sh /경로/yuv_폴더            # 해상도를 물어봄
#    ./convert_folder.sh /경로/yuv_폴더 2560x1440  # 해상도를 바로 지정
#
#  해상도를 비워 두면 파일 이름과 크기로 자동 판별합니다.
#  결과는 <입력폴더>/raw_out 에 새 파일로 생성되며, 원본은 건드리지 않습니다.
#
#  매번 같은 해상도를 쓴다면 아래 DEFAULT_SIZE 에 적어 두세요.
# ---------------------------------------------------------------------
set -e

DEFAULT_SIZE=""
OPTIONS="--out-format rgb24"

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SCRIPT="$DIR/yuv2raw.py"

if [ ! -f "$SCRIPT" ]; then
    echo "[오류] yuv2raw.py 를 찾을 수 없습니다: $SCRIPT" >&2
    exit 1
fi

TARGET="$1"
if [ -z "$TARGET" ]; then
    printf '변환할 폴더 경로: '
    read -r TARGET
fi
if [ -z "$TARGET" ]; then
    echo "[오류] 입력된 경로가 없습니다." >&2
    exit 1
fi

SIZE="$2"
if [ -z "$SIZE" ]; then
    if [ -n "$DEFAULT_SIZE" ]; then
        printf '해상도 WxH [%s]: ' "$DEFAULT_SIZE"
    else
        printf '해상도 WxH (예: 2560x1440, 그냥 Enter 면 자동 판별): '
    fi
    read -r SIZE
    [ -z "$SIZE" ] && SIZE="$DEFAULT_SIZE"
fi
[ -n "$SIZE" ] && OPTIONS="$OPTIONS --size $SIZE"

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
exec "$PY" "$SCRIPT" "$TARGET" $OPTIONS
