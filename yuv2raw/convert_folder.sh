#!/bin/sh
# ---------------------------------------------------------------------
#  YUV -> RAW 일괄 변환기 (macOS / Linux 실행용)
#
#    ./convert_folder.sh /경로/yuv_폴더                 # 물어보면서 진행
#    ./convert_folder.sh /경로/yuv_폴더 copy            # 크기 그대로
#    ./convert_folder.sh /경로/yuv_폴더 rgb24 2560x1440 # RGB24 + 해상도 지정
#
#  copy  : 바이트를 그대로 옮깁니다. 파일 크기가 1바이트도 바뀌지 않습니다.
#  rgb24 : YUV 를 RGB 로 변환합니다. 파일이 약 2배로 커집니다.
#
#  결과는 <입력폴더>/raw_out 에 새 파일로 생성되며, 원본은 건드리지 않습니다.
# ---------------------------------------------------------------------
set -e

DEFAULT_SIZE=""
EXTRA=""

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

OUTFMT="$2"
if [ -z "$OUTFMT" ]; then
    echo "출력 방식:"
    echo "  1) 크기 그대로 - 바이트를 그대로 옮김 (기본)"
    echo "  2) RGB24      - RGB 로 변환, 파일이 약 2배로 커짐"
    printf '선택 [1]: '
    read -r CHOICE
    if [ "$CHOICE" = "2" ]; then OUTFMT="rgb24"; else OUTFMT="copy"; fi
fi
OPTIONS="--out-format $OUTFMT"

if [ "$OUTFMT" = "rgb24" ]; then
    SIZE="$3"
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
fi

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python
exec "$PY" "$SCRIPT" "$TARGET" $OPTIONS $EXTRA
