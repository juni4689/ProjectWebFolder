#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yuv2raw - YUV 파일을 RAW 파일로 일괄 변환하는 도구.

폴더 안의 모든 .yuv 파일을 한 번에 읽어서 RAW 파일로 변환한다.
원본 파일은 절대 수정하지 않고, 항상 별도의 출력 폴더에 새 파일을 만든다.

핵심 설계(= 영상이 "깨지지" 않게 하는 장치):
  * 파일 크기가 (프레임 크기 x 정수)가 아니면 변환을 거부한다. 해상도나
    포맷을 잘못 짚었다는 뜻이므로, 깨진 결과물을 만드는 대신 오류를 낸다.
  * 서브샘플링(4:2:0 / 4:2:2)에 맞지 않는 해상도도 거부한다.
  * 색공간 변환은 BT.601/709/2020 행렬과 limited/full 레인지를 정확히
    구분해서 적용하고, 결과는 항상 0..max 로 클리핑한다.
  * 출력은 임시 파일에 쓴 뒤 크기를 검증하고 나서 최종 이름으로 바꾼다.
    (변환 중 중단되어도 반쪽짜리 .raw 가 남지 않는다.)
  * 변환 결과를 다시 열 때 필요한 정보(해상도/포맷/프레임 수)를 담은
    사이드카 .json 을 함께 만든다.

numpy 가 설치되어 있으면 자동으로 빠른 경로를 사용하고, 없으면 순수
파이썬 구현으로 동작한다. 두 경로의 출력 바이트는 완전히 동일하다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from array import array
from datetime import datetime, timezone

try:  # 선택적 가속기. 없어도 동작한다.
    import numpy as _np
except Exception:  # pragma: no cover - numpy 미설치 환경
    _np = None

# 환경변수로 numpy 경로를 끌 수 있다(두 경로의 결과 비교 및 문제 격리용).
if os.environ.get("YUV2RAW_NO_NUMPY"):
    _np = None

VERSION = "1.7.0"

FIX = 16          # 고정소수점 비트 수
FIX_ONE = 1 << FIX


# --------------------------------------------------------------------------
# 입력 포맷 정의
# --------------------------------------------------------------------------

class YuvFormat(object):
    """입력 YUV 포맷 한 가지를 기술한다.

    kind        : 'planar' | 'semiplanar' | 'packed422' | 'gray'
    sx, sy      : 크로마 서브샘플링 계수 (4:2:0 이면 2,2 / 4:2:2 면 2,1)
    order       : planar 는 'yuv'|'yvu', semiplanar 는 'uv'|'vu',
                  packed422 는 'yuyv'|'uyvy'|'yvyu'|'vyuy'
    depth       : 유효 비트 수 (8, 10, 12, 16)
    msb_aligned : True 면 16비트 컨테이너의 상위 비트에 값이 정렬됨(P010 방식)
    """

    __slots__ = ("name", "kind", "sx", "sy", "order", "depth", "msb_aligned")

    def __init__(self, name, kind, sx, sy, order, depth=8, msb_aligned=False):
        self.name = name
        self.kind = kind
        self.sx = sx
        self.sy = sy
        self.order = order
        self.depth = depth
        self.msb_aligned = msb_aligned

    @property
    def bytes_per_sample(self):
        return 1 if self.depth <= 8 else 2

    @property
    def container_bits(self):
        return 8 if self.depth <= 8 else 16

    @property
    def shift(self):
        """컨테이너에서 실제 값을 얻기 위해 오른쪽으로 밀어야 하는 비트 수."""
        return (16 - self.depth) if self.msb_aligned else 0

    def with_depth(self, depth, msb_aligned=False):
        return YuvFormat(self.name, self.kind, self.sx, self.sy, self.order,
                         depth, msb_aligned)

    def describe(self):
        d = "%d-bit" % self.depth
        if self.msb_aligned:
            d += " (MSB 정렬)"
        sub = {(2, 2): "4:2:0", (2, 1): "4:2:2", (1, 1): "4:4:4"}.get(
            (self.sx, self.sy), "%d:%d" % (self.sx, self.sy))
        if self.kind == "gray":
            sub = "4:0:0"
        return "%s (%s, %s, %s)" % (self.name, self.kind, sub, d)


def _f(name, kind, sx, sy, order, depth=8, msb_aligned=False):
    return YuvFormat(name, kind, sx, sy, order, depth, msb_aligned)


#: 표준 포맷 정의. 키는 정규화된 이름.
FORMATS = {
    # 4:2:0 평면(planar)
    "i420": _f("i420", "planar", 2, 2, "yuv"),
    "yv12": _f("yv12", "planar", 2, 2, "yvu"),
    # 4:2:0 반평면(semi-planar)
    "nv12": _f("nv12", "semiplanar", 2, 2, "uv"),
    "nv21": _f("nv21", "semiplanar", 2, 2, "vu"),
    # 4:2:2 평면
    "i422": _f("i422", "planar", 2, 1, "yuv"),
    "yv16": _f("yv16", "planar", 2, 1, "yvu"),
    "nv16": _f("nv16", "semiplanar", 2, 1, "uv"),
    "nv61": _f("nv61", "semiplanar", 2, 1, "vu"),
    # 4:2:2 패킹(packed)
    "yuyv": _f("yuyv", "packed422", 2, 1, "yuyv"),
    "uyvy": _f("uyvy", "packed422", 2, 1, "uyvy"),
    "yvyu": _f("yvyu", "packed422", 2, 1, "yvyu"),
    "vyuy": _f("vyuy", "packed422", 2, 1, "vyuy"),
    # 4:4:4 평면
    "i444": _f("i444", "planar", 1, 1, "yuv"),
    "yv24": _f("yv24", "planar", 1, 1, "yvu"),
    # 흑백
    "gray": _f("gray", "gray", 1, 1, "y"),
    # 10/16비트 반평면 (MSB 정렬)
    "p010": _f("p010", "semiplanar", 2, 2, "uv", 10, True),
    "p016": _f("p016", "semiplanar", 2, 2, "uv", 16, False),
    "p210": _f("p210", "semiplanar", 2, 1, "uv", 10, True),
}

#: 자주 쓰이는 다른 이름들.
FORMAT_ALIASES = {
    "yu12": "i420", "iyuv": "i420", "yuv420p": "i420", "yuv420": "i420",
    "420p": "i420", "420": "i420", "j420": "i420",
    "yv12p": "yv12",
    "nv12m": "nv12", "nv21m": "nv21",
    "yuv422p": "i422", "422p": "i422", "yuv422": "i422", "422": "i422",
    "yuy2": "yuyv", "yuvs": "yuyv", "v422": "yuyv",
    "uyvy422": "uyvy", "y422": "uyvy", "hdyc": "uyvy",
    "yuv444p": "i444", "444p": "i444", "yuv444": "i444", "444": "i444",
    "y8": "gray", "y800": "gray", "mono": "gray", "luma": "gray",
    "grey": "gray", "gray8": "gray", "y_only": "gray",
}


def resolve_format(name):
    """사용자가 준 포맷 이름을 YuvFormat 으로 바꾼다.

    'yuv420p10le', 'i420_10bit', 'nv12-10' 같은 비트수 접미사도 처리한다.
    """
    if isinstance(name, YuvFormat):
        return name
    key = str(name).strip().lower().replace("-", "_")

    depth = None
    # 비트수 접미사 분리: yuv420p10le / i420_10bit / nv12_10 ...
    m = re.match(r"^(?P<base>.+?)[_]?(?P<bits>8|10|12|14|16)(?:bit|b)?(?P<end>le|be)?$", key)
    if m and m.group("base") not in FORMATS and m.group("base") not in FORMAT_ALIASES:
        pass  # base 가 모르는 이름이면 아래 통짜 조회에 맡긴다.
    if m:
        base = m.group("base").rstrip("_")
        if m.group("end") == "be":
            raise ValueError("빅엔디안(be) 입력은 지원하지 않습니다: %s" % name)
        if base in FORMATS or base in FORMAT_ALIASES:
            key, depth = base, int(m.group("bits"))
        elif base.endswith("p") and base[:-1] in FORMAT_ALIASES:
            key, depth = base[:-1], int(m.group("bits"))

    key = FORMAT_ALIASES.get(key, key)
    if key not in FORMATS:
        raise ValueError("알 수 없는 입력 포맷입니다: %s" % name)

    fmt = FORMATS[key]
    if depth is not None and depth != fmt.depth:
        if fmt.kind == "packed422":
            raise ValueError("패킹 4:2:2 포맷(%s)은 8비트만 지원합니다." % key)
        if fmt.msb_aligned:
            raise ValueError("%s 는 비트수를 바꿀 수 없습니다." % key)
        fmt = fmt.with_depth(depth)
    return fmt


def frame_size(fmt, width, height):
    """한 프레임의 바이트 수."""
    cw, ch = chroma_size(fmt, width, height)
    samples = width * height + (0 if fmt.kind == "gray" else 2 * cw * ch)
    return samples * fmt.bytes_per_sample


def chroma_size(fmt, width, height):
    if fmt.kind == "gray":
        return 0, 0
    return width // fmt.sx, height // fmt.sy


def validate_geometry(fmt, width, height):
    if width <= 0 or height <= 0:
        raise ValueError("해상도가 올바르지 않습니다: %dx%d" % (width, height))
    if fmt.sx > 1 and width % fmt.sx:
        raise ValueError("%s 는 가로 해상도가 %d의 배수여야 합니다 (현재 %d)."
                         % (fmt.name, fmt.sx, width))
    if fmt.sy > 1 and height % fmt.sy:
        raise ValueError("%s 는 세로 해상도가 %d의 배수여야 합니다 (현재 %d)."
                         % (fmt.name, fmt.sy, height))


# --------------------------------------------------------------------------
# 출력 포맷 정의
# --------------------------------------------------------------------------

#: 이름 -> (채널 수, 출력 비트수, 종류)
OUT_FORMATS = {
    "copy":     ("copy", 1, 8, "원본 바이트 그대로 (파일 크기가 1바이트도 안 바뀜)"),
    "rgb24":    ("rgb", 3, 8,  "RGB 인터리브 8비트"),
    "bgr24":    ("bgr", 3, 8,  "BGR 인터리브 8비트"),
    "rgb48le":  ("rgb", 3, 16, "RGB 인터리브 16비트 리틀엔디안"),
    "bgr48le":  ("bgr", 3, 16, "BGR 인터리브 16비트 리틀엔디안"),
    "rgb332":   ("rgb332", 1, 8, "RGB 3-3-2비트 (픽셀당 1바이트)"),
    "rgb444":   ("rgb444", 0, 0, "RGB 4-4-4비트 (2픽셀당 3바이트)"),
    "rgb565le": ("rgb565", 1, 16, "RGB 5-6-5비트 리틀엔디안 (픽셀당 2바이트)"),
    "rgbx32":   ("rgbx32", 4, 8, "RGB 8-8-8비트 + 패딩 1바이트 (픽셀당 4바이트)"),
    "gray8":    ("gray", 1, 8,  "휘도만 8비트"),
    "gray16le": ("gray", 1, 16, "휘도만 16비트 리틀엔디안"),
    "yuv444":   ("yuv444", 3, 0, "Y,U,V 인터리브 (색공간 변환 없음, 원본 비트수 유지)"),
    "planar":   ("planar", 3, 0, "Y,U,V 평면 그대로 (원본 서브샘플링/비트수 유지)"),
}

MATRICES = {
    "bt601":  (0.299,  0.114),
    "bt709":  (0.2126, 0.0722),
    "bt2020": (0.2627, 0.0593),
}


#: 프레임당 바이트 수가 입력과 완전히 같은 출력 포맷(= 파일 크기가 안 바뀜)
SIZE_PRESERVING = ("copy", "planar")

#: --out-format 에 쓸 수 있는 특수 값. 입력의 픽셀당 비트 수와 같은 RGB
#: 패킹을 골라 주므로, RGB 로 바꾸면서도 파일 크기가 그대로 유지된다.
RGB_SAME = "rgb-same"

#: 픽셀당 비트 수 -> 크기가 정확히 같아지는 RGB 패킹
RGB_BY_BITS = {
    8:  "rgb332",
    12: "rgb444",
    16: "rgb565le",
    24: "rgb24",
    32: "rgbx32",
    48: "rgb48le",
}

#: 채널당 비트 수가 8보다 작아 색이 뭉개지는 출력(경고를 띄운다)
REDUCED_COLOR = {
    "rgb332":   "채널당 3-3-2비트, 256색",
    "rgb444":   "채널당 4비트, 4096색",
    "rgb565le": "채널당 5-6-5비트, 65536색",
}


def bits_per_pixel(fmt):
    """입력 포맷의 픽셀당 비트 수. 4:2:0 8비트면 12, 4:2:2 8비트면 16."""
    bits = 8 * fmt.bytes_per_sample
    if fmt.kind == "gray":
        return bits
    n = fmt.sx * fmt.sy
    return (n + 2) * bits // n


def rgb_same_size_format(fmt):
    """이 입력과 파일 크기가 정확히 같아지는 RGB 출력 포맷 이름."""
    bits = bits_per_pixel(fmt)
    name = RGB_BY_BITS.get(bits)
    if name is None:
        raise ConversionError(
            "%s(픽셀당 %d비트)와 크기가 같아지는 RGB 포맷이 없습니다. "
            "--out-format 으로 직접 골라 주세요." % (fmt.name, bits))
    return name


def out_frame_size(out_format, fmt, width, height):
    kind, nch, bits, _ = OUT_FORMATS[out_format]
    if kind in ("planar", "copy"):
        return frame_size(fmt, width, height)
    if kind == "rgb444":
        return width * height * 3 // 2
    if kind == "yuv444":
        return width * height * 3 * fmt.bytes_per_sample
    return width * height * nch * (bits // 8)


def pick_matrix(name, height):
    if name != "auto":
        return name
    # 관례: SD 는 BT.601, HD 이상은 BT.709
    return "bt709" if height >= 720 else "bt601"


# --------------------------------------------------------------------------
# 색공간 변환용 룩업 테이블
# --------------------------------------------------------------------------

class ColorTables(object):
    """YUV -> RGB 정수 변환 테이블 묶음.

    인덱스는 '컨테이너 값'(8비트면 0..255, 16비트 컨테이너면 0..65535)이고
    값은 16비트 고정소수점이다. ylut 에는 반올림 상수와 클램프 테이블
    오프셋이 미리 더해져 있어서, 변환 루프는 다음 한 줄로 끝난다.

        R = clamp[(ylut[Y] + rv[V]) >> 16]
    """

    __slots__ = ("ylut", "rv", "gu", "gv", "bu", "clamp", "out_max", "out_bits")

    def __init__(self, fmt, out_bits, matrix, color_range):
        depth = fmt.depth
        shift = fmt.shift
        smax = (1 << depth) - 1
        cmid = 1 << (depth - 1)
        scale = 1 << (depth - 8)
        out_max = (1 << out_bits) - 1

        kr, kb = MATRICES[matrix]
        kg = 1.0 - kr - kb

        if color_range == "limited":
            y_off = 16.0 * scale
            y_gain = 1.0 / (219.0 * scale)
            c_gain = 1.0 / (224.0 * scale)
        else:
            y_off = 0.0
            y_gain = 1.0 / smax
            c_gain = 1.0 / smax

        n = 1 << fmt.container_bits
        # 컨테이너 값 -> 실제 샘플 값 (범위를 벗어나면 클램프)
        sample = [min(smax, c >> shift) for c in range(n)]

        def fixed(x):
            return int(round(x * out_max * FIX_ONE))

        y_raw = [fixed((s - y_off) * y_gain) for s in sample]
        rv = [fixed(2.0 * (1.0 - kr) * ((s - cmid) * c_gain)) for s in sample]
        gu = [fixed(-2.0 * (1.0 - kb) * kb / kg * ((s - cmid) * c_gain)) for s in sample]
        gv = [fixed(-2.0 * (1.0 - kr) * kr / kg * ((s - cmid) * c_gain)) for s in sample]
        bu = [fixed(2.0 * (1.0 - kb) * ((s - cmid) * c_gain)) for s in sample]

        # 클램프 테이블이 덮어야 하는 정수 범위를 실제 테이블에서 구한다.
        y_lo, y_hi = min(y_raw), max(y_raw)
        lo = min(y_lo + min(rv),
                 y_lo + min(gu) + min(gv),
                 y_lo + min(bu),
                 y_lo)
        hi = max(y_hi + max(rv),
                 y_hi + max(gu) + max(gv),
                 y_hi + max(bu),
                 y_hi)
        lo = (lo >> FIX) - 2
        hi = (hi >> FIX) + 2

        bias = (FIX_ONE >> 1) - (lo * FIX_ONE)   # 반올림 + 클램프 오프셋
        self.ylut = [v + bias for v in y_raw]
        self.rv, self.gu, self.gv, self.bu = rv, gu, gv, bu
        self.out_max = out_max
        self.out_bits = out_bits

        size = hi - lo + 1
        values = [0 if (i + lo) < 0 else (out_max if (i + lo) > out_max else (i + lo))
                  for i in range(size)]
        self.clamp = bytes(values) if out_bits <= 8 else array("H", values)

        if _np is not None:
            # 8비트 출력이면 int32 로 충분하다(중간 배열이 절반 크기가 된다).
            dt = _np.int32 if out_bits <= 8 else _np.int64
            self.ylut = _np.array(self.ylut, dtype=dt)
            self.rv = _np.array(self.rv, dtype=dt)
            self.gu = _np.array(self.gu, dtype=dt)
            self.gv = _np.array(self.gv, dtype=dt)
            self.bu = _np.array(self.bu, dtype=dt)
            self.clamp = _np.frombuffer(
                bytes(self.clamp) if out_bits <= 8 else self.clamp.tobytes(),
                dtype=_np.uint8 if out_bits <= 8 else "<u2")


# --------------------------------------------------------------------------
# 프레임 언패킹 (파일 바이트 -> Y/U/V 평면)
# --------------------------------------------------------------------------

class Planes(object):
    """한 프레임의 Y/U/V 평면. samples 는 bytes 또는 array('H') 또는 ndarray."""

    __slots__ = ("y", "u", "v", "width", "height", "cwidth", "cheight")

    def __init__(self, y, u, v, width, height, cwidth, cheight):
        self.y, self.u, self.v = y, u, v
        self.width, self.height = width, height
        self.cwidth, self.cheight = cwidth, cheight


def _as_samples(data, fmt):
    """바이트열을 샘플 시퀀스로 만든다(8비트면 그대로, 아니면 16비트 LE)."""
    if fmt.bytes_per_sample == 1:
        return data
    arr = array("H")
    arr.frombytes(data)
    if sys.byteorder == "big":
        arr.byteswap()
    return arr


def unpack_frame(data, fmt, width, height):
    """한 프레임 바이트열을 Y/U/V 평면으로 분해한다."""
    cw, ch = chroma_size(fmt, width, height)
    if _np is not None:
        dtype = _np.uint8 if fmt.bytes_per_sample == 1 else _np.dtype("<u2")
        buf = _np.frombuffer(data, dtype=dtype)
        y, u, v = _unpack_np(buf, fmt, width, height, cw, ch)
    else:
        buf = _as_samples(data, fmt)
        y, u, v = _unpack_py(buf, fmt, width, height, cw, ch)
    return Planes(y, u, v, width, height, cw, ch)


def _unpack_np(buf, fmt, w, h, cw, ch):
    n = w * h
    if fmt.kind == "gray":
        return buf[:n].reshape(h, w), None, None
    if fmt.kind == "planar":
        a = buf[n:n + cw * ch].reshape(ch, cw)
        b = buf[n + cw * ch:n + 2 * cw * ch].reshape(ch, cw)
        u, v = (a, b) if fmt.order == "yuv" else (b, a)
        return buf[:n].reshape(h, w), u, v
    if fmt.kind == "semiplanar":
        c = buf[n:n + 2 * cw * ch].reshape(ch, cw * 2)
        a, b = c[:, 0::2], c[:, 1::2]
        u, v = (a, b) if fmt.order == "uv" else (b, a)
        return buf[:n].reshape(h, w), u, v
    # packed422
    rows = buf[:w * h * 2].reshape(h, w * 2)
    if fmt.order == "yuyv":
        return rows[:, 0::2], rows[:, 1::4], rows[:, 3::4]
    if fmt.order == "yvyu":
        return rows[:, 0::2], rows[:, 3::4], rows[:, 1::4]
    if fmt.order == "uyvy":
        return rows[:, 1::2], rows[:, 0::4], rows[:, 2::4]
    return rows[:, 1::2], rows[:, 2::4], rows[:, 0::4]  # vyuy


def _unpack_py(buf, fmt, w, h, cw, ch):
    n = w * h
    if fmt.kind == "gray":
        return buf[:n], None, None
    if fmt.kind == "planar":
        a = buf[n:n + cw * ch]
        b = buf[n + cw * ch:n + 2 * cw * ch]
        u, v = (a, b) if fmt.order == "yuv" else (b, a)
        return buf[:n], u, v
    if fmt.kind == "semiplanar":
        c = buf[n:n + 2 * cw * ch]
        a, b = c[0::2], c[1::2]
        u, v = (a, b) if fmt.order == "uv" else (b, a)
        return buf[:n], u, v
    # packed422: 행 폭이 4바이트의 배수이므로 전체 버퍼에 대한 스트라이드
    # 슬라이싱만으로 안전하게 분해된다(가로 해상도가 짝수임은 검증 완료).
    rows = buf[:w * h * 2]
    if fmt.order == "yuyv":
        return rows[0::2], rows[1::4], rows[3::4]
    if fmt.order == "yvyu":
        return rows[0::2], rows[3::4], rows[1::4]
    if fmt.order == "uyvy":
        return rows[1::2], rows[0::4], rows[2::4]
    return rows[1::2], rows[2::4], rows[0::4]  # vyuy


# --------------------------------------------------------------------------
# 크로마 업샘플링
# --------------------------------------------------------------------------

def upsample_chroma(plane, cw, ch, w, h, sx, sy, method, bps):
    """크로마 평면을 휘도 해상도로 확대한다."""
    if sx == 1 and sy == 1:
        return plane
    if _np is not None:
        return _upsample_np(plane, w, h, sx, sy, method)
    return _upsample_py(plane, cw, ch, w, h, sx, sy, method, bps)


def _upsample_np(plane, w, h, sx, sy, method):
    if method == "nearest":
        out = plane
        if sx > 1:
            out = _np.repeat(out, sx, axis=1)
        if sy > 1:
            out = _np.repeat(out, sy, axis=0)
        return out
    # bilinear: MPEG-2 방식의 좌측/상단 코사이팅 (3:1 가중치)
    p = plane.astype(_np.int32)
    if sx > 1:
        left = _np.concatenate([p[:, :1], p[:, :-1]], axis=1)
        right = _np.concatenate([p[:, 1:], p[:, -1:]], axis=1)
        a = (3 * p + left + 2) >> 2
        b = (3 * p + right + 2) >> 2
        out = _np.empty((p.shape[0], p.shape[1] * 2), dtype=_np.int32)
        out[:, 0::2] = a
        out[:, 1::2] = b
        p = out
    if sy > 1:
        up = _np.concatenate([p[:1, :], p[:-1, :]], axis=0)
        down = _np.concatenate([p[1:, :], p[-1:, :]], axis=0)
        a = (3 * p + up + 2) >> 2
        b = (3 * p + down + 2) >> 2
        out = _np.empty((p.shape[0] * 2, p.shape[1]), dtype=_np.int32)
        out[0::2, :] = a
        out[1::2, :] = b
        p = out
    return p


def _new_buf(count, bps):
    return bytearray(count) if bps == 1 else array("H", bytes(2 * count))


def _upsample_py(plane, cw, ch, w, h, sx, sy, method, bps):
    if method == "nearest":
        out = _new_buf(w * h, bps)
        for cy in range(ch):
            row = plane[cy * cw:(cy + 1) * cw]
            if sx == 2:
                wide = _new_buf(w, bps)
                wide[0::2] = row
                wide[1::2] = row
            else:
                wide = row
            for k in range(sy):
                y = cy * sy + k
                out[y * w:(y + 1) * w] = wide
        return bytes(out) if bps == 1 else out

    # bilinear
    if sx == 2:
        wide_rows = []
        for cy in range(ch):
            row = plane[cy * cw:(cy + 1) * cw]
            a = [(3 * row[i] + row[i - 1 if i > 0 else 0] + 2) >> 2 for i in range(cw)]
            b = [(3 * row[i] + row[i + 1 if i < cw - 1 else cw - 1] + 2) >> 2
                 for i in range(cw)]
            merged = _new_buf(w, bps)
            merged[0::2] = bytes(a) if bps == 1 else array("H", a)
            merged[1::2] = bytes(b) if bps == 1 else array("H", b)
            wide_rows.append(merged)
    else:
        wide_rows = [plane[cy * cw:(cy + 1) * cw] for cy in range(ch)]

    out = _new_buf(w * h, bps)
    if sy == 1:
        for cy in range(ch):
            out[cy * w:(cy + 1) * w] = wide_rows[cy]
    else:
        for cy in range(ch):
            cur = wide_rows[cy]
            prev = wide_rows[cy - 1 if cy > 0 else 0]
            nxt = wide_rows[cy + 1 if cy < ch - 1 else ch - 1]
            top = [(3 * c + p + 2) >> 2 for c, p in zip(cur, prev)]
            bot = [(3 * c + nn + 2) >> 2 for c, nn in zip(cur, nxt)]
            y = cy * 2
            out[y * w:(y + 1) * w] = bytes(top) if bps == 1 else array("H", top)
            out[(y + 1) * w:(y + 2) * w] = bytes(bot) if bps == 1 else array("H", bot)
    return bytes(out) if bps == 1 else out


# --------------------------------------------------------------------------
# 프레임 변환
# --------------------------------------------------------------------------

def convert_frame(data, fmt, width, height, out_format, tables, chroma_method):
    """한 프레임 바이트열을 목표 RAW 포맷 바이트열로 변환한다."""
    kind = OUT_FORMATS[out_format][0]
    if kind == "copy":
        return data          # 손대지 않는다. 들어온 바이트가 그대로 나간다.
    planes = unpack_frame(data, fmt, width, height)

    if kind == "planar":
        return _pack_planar(planes, fmt)
    if kind == "gray":
        return _convert_gray(planes, tables)

    if fmt.kind == "gray":
        # 흑백 입력을 컬러로 변환할 때는 중립 크로마를 채워 넣는다.
        neutral = 1 << (fmt.container_bits - 1) if fmt.msb_aligned else (1 << (fmt.depth - 1))
        u = v = _const_plane(neutral, width, height, fmt.bytes_per_sample)
    else:
        u = upsample_chroma(planes.u, planes.cwidth, planes.cheight, width, height,
                            fmt.sx, fmt.sy, chroma_method, fmt.bytes_per_sample)
        v = upsample_chroma(planes.v, planes.cwidth, planes.cheight, width, height,
                            fmt.sx, fmt.sy, chroma_method, fmt.bytes_per_sample)

    if kind == "yuv444":
        return _pack_yuv444(planes.y, u, v, width, height, fmt.bytes_per_sample)
    if kind in ("rgb332", "rgb444", "rgb565", "rgbx32"):
        r, g, b = _rgb_planes(planes.y, u, v, width, height, tables)
        return _pack_low_rgb(r, g, b, width, height, kind)
    return _convert_rgb(planes.y, u, v, width, height, tables, kind == "bgr")


def _const_plane(value, w, h, bps):
    if _np is not None:
        return _np.full((h, w), value,
                        dtype=_np.uint8 if bps == 1 else _np.dtype("<u2"))
    if bps == 1:
        return bytes([value]) * (w * h)
    return array("H", [value]) * (w * h)


def _pack_planar(planes, fmt):
    if _np is not None:
        parts = [_np.ascontiguousarray(planes.y).tobytes()]
        if planes.u is not None:
            parts.append(_np.ascontiguousarray(planes.u).tobytes())
            parts.append(_np.ascontiguousarray(planes.v).tobytes())
        return b"".join(parts)
    parts = [_to_bytes(planes.y, fmt.bytes_per_sample)]
    if planes.u is not None:
        parts.append(_to_bytes(planes.u, fmt.bytes_per_sample))
        parts.append(_to_bytes(planes.v, fmt.bytes_per_sample))
    return b"".join(parts)


def _to_bytes(seq, bps):
    if bps == 1:
        return bytes(seq)
    arr = seq if isinstance(seq, array) else array("H", seq)
    if sys.byteorder == "big":
        arr = array("H", arr)
        arr.byteswap()
    return arr.tobytes()


def _pack_yuv444(y, u, v, w, h, bps):
    if _np is not None:
        dtype = _np.uint8 if bps == 1 else _np.dtype("<u2")
        out = _np.empty((h, w, 3), dtype=dtype)
        out[:, :, 0] = y
        out[:, :, 1] = u
        out[:, :, 2] = v
        return out.tobytes()
    out = _new_buf(w * h * 3, bps)
    out[0::3] = y if bps == 1 else array("H", y)
    out[1::3] = u if bps == 1 else array("H", u)
    out[2::3] = v if bps == 1 else array("H", v)
    return _to_bytes(out, bps)


def _convert_gray(planes, tables):
    if _np is not None:
        idx = tables.ylut[planes.y] >> FIX
        return tables.clamp[idx].tobytes()
    ylut, clamp = tables.ylut, tables.clamp
    vals = [clamp[ylut[s] >> FIX] for s in planes.y]
    return bytes(vals) if tables.out_bits <= 8 else array("H", vals).tobytes()


_DEPTH_LUTS = {}


def depth_lut(bits):
    """8비트 값을 bits 비트로 줄이는 표(반올림)."""
    lut = _DEPTH_LUTS.get(bits)
    if lut is None:
        top = (1 << bits) - 1
        lut = [(v * top + 127) // 255 for v in range(256)]
        if _np is not None:
            lut = _np.array(lut, dtype=_np.uint16)
        _DEPTH_LUTS[bits] = lut
    return lut


def _rgb_planes(y, u, v, w, h, tables):
    """8비트 R, G, B 를 채널별로 돌려준다(패킹 포맷용)."""
    if _np is not None:
        yv = tables.ylut[y]
        return (tables.clamp[(yv + tables.rv[v]) >> FIX],
                tables.clamp[(yv + tables.gu[u] + tables.gv[v]) >> FIX],
                tables.clamp[(yv + tables.bu[u]) >> FIX])
    ylut, clamp = tables.ylut, tables.clamp
    rv, gu, gv, bu = tables.rv, tables.gu, tables.gv, tables.bu
    rr, gg, bb = [], [], []
    for row in range(h):
        s = row * w
        e = s + w
        yr, ur, vr = y[s:e], u[s:e], v[s:e]
        rr.append(bytes(clamp[(ylut[a] + rv[c]) >> FIX] for a, c in zip(yr, vr)))
        gg.append(bytes(clamp[(ylut[a] + gu[b] + gv[c]) >> FIX]
                        for a, b, c in zip(yr, ur, vr)))
        bb.append(bytes(clamp[(ylut[a] + bu[b]) >> FIX] for a, b in zip(yr, ur)))
    return b"".join(rr), b"".join(gg), b"".join(bb)


def _pack_low_rgb(r, g, b, w, h, kind):
    """8비트 R,G,B 를 좁은 RGB 포맷으로 눌러 담는다.

    rgb444 는 두 픽셀을 3바이트에 담는다(R0G0 B0R1 G1B1). 이 배치라야
    픽셀당 12비트가 되어 4:2:0 입력과 파일 크기가 정확히 같아진다.
    """
    if _np is not None:
        return _pack_low_rgb_np(r, g, b, w, h, kind)
    return _pack_low_rgb_py(r, g, b, w, h, kind)


def _pack_low_rgb_np(r, g, b, w, h, kind):
    if kind == "rgbx32":
        out = _np.empty((h, w, 4), dtype=_np.uint8)
        out[:, :, 0], out[:, :, 1], out[:, :, 2] = r, g, b
        out[:, :, 3] = 255
        return out.tobytes()
    if kind == "rgb332":
        r3, g3, b2 = depth_lut(3)[r], depth_lut(3)[g], depth_lut(2)[b]
        return ((r3 << 5) | (g3 << 2) | b2).astype(_np.uint8).tobytes()
    if kind == "rgb565":
        r5, g6, b5 = depth_lut(5)[r], depth_lut(6)[g], depth_lut(5)[b]
        packed = ((r5 << 11) | (g6 << 5) | b5).astype("<u2")
        return packed.tobytes()
    # rgb444
    r4 = depth_lut(4)[r].reshape(h, w)
    g4 = depth_lut(4)[g].reshape(h, w)
    b4 = depth_lut(4)[b].reshape(h, w)
    out = _np.empty((h, w // 2, 3), dtype=_np.uint8)
    out[:, :, 0] = (r4[:, 0::2] << 4) | g4[:, 0::2]
    out[:, :, 1] = (b4[:, 0::2] << 4) | r4[:, 1::2]
    out[:, :, 2] = (g4[:, 1::2] << 4) | b4[:, 1::2]
    return out.tobytes()


def _pack_low_rgb_py(r, g, b, w, h, kind):
    chunks = []
    for row in range(h):
        s = row * w
        e = s + w
        rr, gg, bb = r[s:e], g[s:e], b[s:e]
        if kind == "rgbx32":
            out = bytearray(w * 4)
            out[0::4], out[1::4], out[2::4] = rr, gg, bb
            out[3::4] = b"\xff" * w
            chunks.append(bytes(out))
        elif kind == "rgb332":
            l3, l2 = depth_lut(3), depth_lut(2)
            chunks.append(bytes((l3[a] << 5) | (l3[c] << 2) | l2[d]
                                for a, c, d in zip(rr, gg, bb)))
        elif kind == "rgb565":
            l5, l6 = depth_lut(5), depth_lut(6)
            vals = array("H", ((l5[a] << 11) | (l6[c] << 5) | l5[d]
                               for a, c, d in zip(rr, gg, bb)))
            chunks.append(_to_bytes(vals, 2))
        else:  # rgb444
            l4 = depth_lut(4)
            out = bytearray(w // 2 * 3)
            out[0::3] = bytes((l4[a] << 4) | l4[c]
                              for a, c in zip(rr[0::2], gg[0::2]))
            out[1::3] = bytes((l4[a] << 4) | l4[c]
                              for a, c in zip(bb[0::2], rr[1::2]))
            out[2::3] = bytes((l4[a] << 4) | l4[c]
                              for a, c in zip(gg[1::2], bb[1::2]))
            chunks.append(bytes(out))
    return b"".join(chunks)


def _convert_rgb(y, u, v, w, h, tables, bgr):
    if _np is not None:
        return _convert_rgb_np(y, u, v, w, h, tables, bgr)
    return _convert_rgb_py(y, u, v, w, h, tables, bgr)


def _convert_rgb_np(y, u, v, w, h, tables, bgr):
    yv = tables.ylut[y]
    r = tables.clamp[(yv + tables.rv[v]) >> FIX]
    g = tables.clamp[(yv + tables.gu[u] + tables.gv[v]) >> FIX]
    b = tables.clamp[(yv + tables.bu[u]) >> FIX]
    out = _np.empty((h, w, 3), dtype=r.dtype)
    if bgr:
        out[:, :, 0], out[:, :, 1], out[:, :, 2] = b, g, r
    else:
        out[:, :, 0], out[:, :, 1], out[:, :, 2] = r, g, b
    return out.tobytes()


def _convert_rgb_py(y, u, v, w, h, tables, bgr):
    ylut, clamp = tables.ylut, tables.clamp
    rv, gu, gv, bu = tables.rv, tables.gu, tables.gv, tables.bu
    bps = 1 if tables.out_bits <= 8 else 2
    i0, i2 = (2, 0) if bgr else (0, 2)

    chunks = []
    for row in range(h):
        s = row * w
        e = s + w
        yr, ur, vr = y[s:e], u[s:e], v[s:e]
        out = _new_buf(w * 3, bps)
        red = (clamp[(ylut[a] + rv[c]) >> FIX] for a, c in zip(yr, vr))
        grn = (clamp[(ylut[a] + gu[b] + gv[c]) >> FIX]
               for a, b, c in zip(yr, ur, vr))
        blu = (clamp[(ylut[a] + bu[b]) >> FIX] for a, b in zip(yr, ur))
        if bps == 1:
            out[i0::3] = bytes(red)
            out[1::3] = bytes(grn)
            out[i2::3] = bytes(blu)
        else:
            out[i0::3] = array("H", red)
            out[1::3] = array("H", grn)
            out[i2::3] = array("H", blu)
        chunks.append(_to_bytes(out, bps))
    return b"".join(chunks)


# --------------------------------------------------------------------------
# 자동 감지
# --------------------------------------------------------------------------

#: 파일 크기로 해상도를 추정할 때 시도해 볼 후보들.
COMMON_RESOLUTIONS = [
    (7680, 4320), (4096, 2160), (3840, 2160), (2560, 1440), (2048, 1080),
    (1920, 1200), (1920, 1080), (1600, 1200), (1440, 1080), (1280, 1024),
    (1280, 960), (1280, 800), (1280, 720), (1024, 768), (960, 540),
    (800, 600), (720, 576), (720, 480), (704, 576), (704, 480),
    (640, 480), (640, 360), (352, 288), (352, 240), (320, 240),
    (176, 144), (4032, 3024), (3264, 2448), (2592, 1944), (2048, 1536),
]

_SIZE_RE = re.compile(r"(?<![0-9])(\d{2,5})\s*[xX*×]\s*(\d{2,5})(?![0-9])")


def detect_size_from_name(name):
    m = _SIZE_RE.search(name)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def detect_format_from_name(name):
    """파일 이름에서 포맷 토큰을 찾는다. 없으면 None."""
    low = name.lower()
    # 해상도 표기(1920x1080)는 포맷 탐색에서 제외해 오탐을 막는다.
    low = _SIZE_RE.sub("__", low)

    candidates = list(FORMATS.keys()) + list(FORMAT_ALIASES.keys())
    # 긴 이름부터 검사해야 'yuv420p10le' 가 'yuv420' 보다 먼저 잡힌다.
    candidates.sort(key=len, reverse=True)

    depth_token = None
    dm = re.search(r"(?<![0-9])(10|12|16)\s*(?:bit|b)(?![a-z0-9])", low)
    if dm:
        depth_token = dm.group(1)

    for cand in candidates:
        if re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(cand), low):
            name_out = cand
            if depth_token and cand not in ("p010", "p016", "p210"):
                try:
                    return resolve_format("%s%s" % (name_out, depth_token))
                except ValueError:
                    pass
            return resolve_format(name_out)

    # 'yuv420p10le' 처럼 붙어 있는 표기
    m = re.search(r"yuv(420|422|444)p(8|10|12|16)?(le)?", low)
    if m:
        base = {"420": "i420", "422": "i422", "444": "i444"}[m.group(1)]
        if m.group(2):
            return resolve_format("%s%s" % (base, m.group(2)))
        return resolve_format(base)
    return None


# --------------------------------------------------------------------------
# 파일 내용으로 포맷과 해상도 알아내기
#
# 파일 이름에 아무 정보가 없어도, 바이트 배열의 규칙성만으로 상당 부분을
# 알아낼 수 있다. 사진은 한 줄 아래 픽셀과 값이 비슷하므로, 줄 길이(stride)
# 만큼 떨어진 바이트끼리 비교하면 그 지점에서 차이가 뚜렷하게 작아진다.
# 픽셀당 바이트 수도 같은 방법으로 알 수 있다. 예를 들어 YUYV 는 4바이트마다
# 같은 성분이 돌아오므로 4바이트 시프트에서 차이가 가장 작다.
# --------------------------------------------------------------------------

def _mean_abs_diff(buf, shift, samples=4096):
    """buf 를 shift 만큼 밀어서 겹쳤을 때의 평균 절대 차이."""
    span = len(buf) - shift
    if span <= 0:
        return 255.0
    if _np is not None:
        arr = _np.frombuffer(buf, dtype=_np.uint8)
        step = max(1, span // samples)
        a = arr[:span:step].astype(_np.int16)
        b = arr[shift:shift + span:step].astype(_np.int16)
        return float(_np.mean(_np.abs(a - b)))
    step = max(1, span // samples)
    total = count = 0
    for i in range(0, span, step):
        total += abs(buf[i] - buf[i + shift])
        count += 1
    return total / float(count or 1)


def _component_period(buf):
    """픽셀 성분이 몇 바이트마다 되풀이되는지 추정한다.

    'packed422' 는 YUYV 계열(2바이트/픽셀)을 뜻한다. 판별 근거는 짝수 위치와
    홀수 위치의 산포 차이다. 평면 포맷이나 흑백이면 두 위치가 모두 휘도라
    통계가 비슷하지만, YUYV 는 한쪽이 휘도, 다른 쪽이 크로마라 크게 다르다.
    """
    _, s_even = _channel_stats(buf, 2, 0)
    _, s_odd = _channel_stats(buf, 2, 1)
    hi, lo = max(s_even, s_odd), min(s_even, s_odd)
    if hi > 2.0 * max(lo, 1.0):
        # 산포가 큰 쪽이 진짜 그림인지 확인한다. 16비트 샘플의 하위 바이트는
        # 산포는 크지만 이웃끼리 전혀 닮지 않아서 여기서 갈린다.
        offset = 0 if s_even >= s_odd else 1
        image = buf[offset::2]
        if _mean_abs_diff(image, 1) < 0.5 * hi:
            return "packed422"
        return 2
    d1 = _mean_abs_diff(buf, 1)
    d3 = _mean_abs_diff(buf, 3)
    if d1 > 0.001 and d3 < 0.7 * d1:
        return 3
    return 1


def _channel_stats(buf, period, offset):
    """주기 안 특정 위치의 평균과 표준편차."""
    vals = buf[offset::period]
    if not vals:
        return 0.0, 0.0
    if _np is not None:
        arr = _np.frombuffer(vals, dtype=_np.uint8).astype(float)
        return float(arr.mean()), float(arr.std())
    step = max(1, len(vals) // 4096)
    picked = vals[::step]
    mean = sum(picked) / float(len(picked))
    var = sum((v - mean) ** 2 for v in picked) / float(len(picked))
    return mean, var ** 0.5


def _find_stride(buf, file_size, multiple_of, lo=64, hi=1 << 16):
    """한 줄의 바이트 수를 찾는다.

    줄 수가 정수여야 하므로 파일 크기의 약수만 후보로 본다. 진짜 줄 길이에서는
    점수가 뾰족하게 낮아진다는 점(바로 옆 시프트보다 확연히 낮음)을 기준으로
    삼는다. 단순히 점수가 가장 낮은 곳을 고르면, 그림이 완만할 때 아주 짧은
    시프트가 이기는 문제가 생긴다.
    """
    step = max(2, multiple_of)
    cands = [x for x in range(lo, min(hi, file_size // 4) + 1)
             if file_size % x == 0 and x % multiple_of == 0]
    scored = []
    for cand in cands:
        base = _mean_abs_diff(buf, cand, 2048)
        if base < 0.05:
            continue
        near = 0.5 * (_mean_abs_diff(buf, cand - step, 1024)
                      + _mean_abs_diff(buf, cand + step, 1024))
        scored.append((near / base, cand, base))
    if len(scored) < 3:
        return None
    # 진짜 줄 길이는 두 가지를 동시에 만족한다.
    #   1) 옆 시프트보다 뚜렷하게 낮다(뾰족함)
    #   2) 모든 후보 중 차이가 가장 작다
    # 두 줄, 세 줄 간격은 뾰족하긴 해도 1)의 값이 더 크므로 걸러진다.
    sharp = [(base, cand) for dip, cand, base in scored if dip >= 1.15]
    if not sharp:
        return None
    best_base, best_cand = min(sharp)
    bases = sorted(base for _, cand, base in scored)
    median = bases[len(bases) // 2]
    if best_base > 0.7 * median:
        return None                  # 뚜렷한 답이 아니다. 추측하지 않는다.
    return best_cand


def analyze_content(path, file_size, sample_limit=1 << 21):
    """파일 내용만 보고 (포맷, (가로, 세로)) 를 추정한다. 실패하면 None."""
    if file_size < 4096:
        return None
    with open(path, "rb") as f:
        buf = f.read(min(file_size, sample_limit))

    period = _component_period(buf)

    if period == "packed422":
        stride = _find_stride(buf, file_size, 2)
        if not stride:
            return None
        width, rows = stride // 2, file_size // stride
        if width < 16 or rows < 16:
            return None
        # 4바이트 주기 안에서 Y 는 값이 크게 요동치고 크로마는 128 근처에 몰린다
        std = [_channel_stats(buf, 4, off)[1] for off in range(4)]
        y_first = (std[0] + std[2]) > (std[1] + std[3])
        return resolve_format("yuyv" if y_first else "uyvy"), (width, rows)

    if period != 1:
        return None                  # 16비트 샘플이나 3바이트 인터리브는 다루지 않는다

    stride = _find_stride(buf, file_size, 1)
    if not stride or stride < 16:
        return None
    width, rows = stride, file_size // stride

    # 평면 포맷: Y 뒤에 오는 크로마 영역은 128 근처에 몰려 있다는 점으로 가른다
    y_mean, y_std = _channel_stats(buf[:min(len(buf), stride * 8)], 1, 0)
    best = None
    for name, num, den in (("i420", 3, 2), ("i422", 2, 1), ("i444", 3, 1),
                           ("gray", 1, 1)):
        if (rows * den) % num:
            continue
        height = rows * den // num
        if height < 16 or not (0.2 <= width / float(height) <= 5.0):
            continue
        fmt = resolve_format(name)
        if fmt.sx > 1 and width % fmt.sx:
            continue
        if fmt.sy > 1 and height % fmt.sy:
            continue
        if name == "gray":
            # 흑백이라면 파일 뒤쪽도 앞쪽과 같은 성질이어야 한다. 평면 YUV 를
            # 흑백으로 잘못 본 경우에는 뒤쪽(크로마)의 산포가 뚝 떨어진다.
            cut = len(buf) * 2 // 3
            h_mean, h_std = _channel_stats(buf[:cut], 1, 0)
            t_mean, t_std = _channel_stats(buf[cut:], 1, 0)
            score = (0.3 * abs(t_mean - h_mean) / 128.0
                     + abs(t_std - h_std) / max(h_std, 1.0))
        else:
            start = width * height
            if start + 4096 > len(buf):
                continue
            c_mean, c_std = _channel_stats(buf[start:start + 65536], 1, 0)
            # 크로마다울수록 점수가 낮다
            score = abs(c_mean - 128) / 128.0 + c_std / max(y_std, 1.0)
        if best is None or score < best[0]:
            best = (score, fmt, (width, height))
    if best is None:
        return None
    if best[1].kind != "gray" and best[0] > 1.2:
        return None                  # 크로마로 보기 어렵다. 추측하지 않는다.
    return best[1], best[2]


def detect_size_from_filesize(fmt, file_size):
    """파일 크기가 딱 떨어지는 해상도 후보를 찾는다.

    반환값은 (width, height). 후보를 하나로 좁히지 못하면 예외를 낸다.
    """
    matches = []
    for w, h in COMMON_RESOLUTIONS:
        if fmt.sx > 1 and w % fmt.sx:
            continue
        if fmt.sy > 1 and h % fmt.sy:
            continue
        fs = frame_size(fmt, w, h)
        if fs and file_size % fs == 0:
            matches.append((w, h, file_size // fs))
    if not matches:
        raise ValueError(
            "파일 크기(%d바이트)에 맞는 해상도를 찾지 못했습니다. "
            "--size 로 직접 지정해 주세요." % file_size)
    if len(matches) == 1:
        return matches[0][0], matches[0][1]
    single = [m for m in matches if m[2] == 1]
    if len(single) == 1:
        return single[0][0], single[0][1]
    top = ", ".join("%dx%d(%d프레임)" % m for m in matches[:6])
    raise ValueError(
        "해상도 후보가 여러 개라 자동 판별할 수 없습니다: %s. "
        "--size WxH 로 지정해 주세요." % top)


# --------------------------------------------------------------------------
# 파일 단위 변환
# --------------------------------------------------------------------------

class ConversionError(Exception):
    pass


class Job(object):
    """파일 하나의 변환 계획."""

    __slots__ = ("src", "dst", "fmt", "width", "height", "frames", "options",
                 "guessed_format", "guessed_size", "out_format")

    def __init__(self, src, dst, fmt, width, height, frames, options,
                 guessed_format=False, guessed_size=False, out_format=None):
        self.src = src
        self.dst = dst
        self.fmt = fmt
        self.width = width
        self.height = height
        self.frames = frames
        self.options = options
        self.guessed_format = guessed_format
        self.guessed_size = guessed_size
        # rgb-same 처럼 입력에 따라 정해지는 값이 있어서 파일마다 따로 둔다
        self.out_format = out_format or options.out_format


class Options(object):
    __slots__ = ("out_format", "matrix", "color_range", "chroma", "overwrite",
                 "allow_partial", "max_frames", "sidecar", "buffer_frames",
                 "preview")

    def __init__(self, out_format="gray8", matrix="auto", color_range="limited",
                 chroma="nearest", overwrite=False, allow_partial=False,
                 max_frames=0, sidecar=True, buffer_frames=1, preview=False):
        self.out_format = out_format
        self.matrix = matrix
        self.color_range = color_range
        self.chroma = chroma
        self.overwrite = overwrite
        self.allow_partial = allow_partial
        self.max_frames = max_frames
        self.sidecar = sidecar
        self.buffer_frames = buffer_frames
        self.preview = preview


def fits_exactly(fmt, size, file_size):
    """이 해상도로 읽었을 때 파일이 프레임 단위로 딱 떨어지는가."""
    if not geometry_ok(fmt, size):
        return False
    fsize = frame_size(fmt, size[0], size[1])
    return bool(fsize) and file_size % fsize == 0


def geometry_ok(fmt, size):
    try:
        validate_geometry(fmt, size[0], size[1])
    except ValueError:
        return False
    return True


def plan_job(src, out_dir, options, fmt=None, size=None, plain_name=False):
    """파일 하나에 대한 변환 계획을 세운다(실제 변환은 하지 않는다)."""
    base = os.path.basename(src)
    stem = os.path.splitext(base)[0]
    file_size = os.path.getsize(src)
    if file_size == 0:
        raise ConversionError("빈 파일입니다.")

    # 우선순위: 옵션 > 파일 이름 > 파일 내용 > 파일 크기
    guessed_format = False
    from_content = None
    if fmt is None or size is None:
        name_fmt = detect_format_from_name(base)
        name_size = detect_size_from_name(base)
        if (fmt is None and name_fmt is None) or (size is None and name_size is None):
            try:
                from_content = analyze_content(src, file_size)
            except (OSError, ValueError):
                from_content = None

    if fmt is None:
        fmt = detect_format_from_name(base)
        if fmt is None and from_content is not None:
            fmt = from_content[0]
        if fmt is None:
            fmt = FORMATS["i420"]
            guessed_format = True

    # rgb-same 은 입력의 픽셀당 비트 수를 보고 크기가 같아지는 RGB 를 고른다.
    out_format = options.out_format
    if out_format == RGB_SAME:
        out_format = rgb_same_size_format(fmt)

    # copy 모드는 바이트를 그대로 옮기므로 해상도를 몰라도 변환에 지장이 없다.
    # 해상도는 사이드카에 남길 참고 정보로만 쓰고, 못 알아내도 실패시키지 않는다.
    copy_mode = out_format == "copy"

    guessed_size = False
    if size is None:
        size = detect_size_from_name(base)
        if size is None and from_content is not None:
            # 내용 분석은 포맷과 해상도를 짝으로 내놓는다. 둘이 어긋나면 쓰지 않는다.
            cand = from_content[1]
            if fits_exactly(fmt, cand, file_size):
                size = cand
                guessed_size = "content"
        if size is None:
            try:
                size = detect_size_from_filesize(fmt, file_size)
                guessed_size = True
            except ValueError:
                if not copy_mode:
                    raise
                size = (0, 0)

    width, height = size

    if width and height:
        if copy_mode and not geometry_ok(fmt, size):
            width, height = 0, 0        # 참고 정보로도 쓸 수 없는 값
        else:
            validate_geometry(fmt, width, height)

    if width and OUT_FORMATS[out_format][0] == "rgb444" and width % 2:
        raise ConversionError(
            "rgb444 는 두 픽셀을 3바이트에 담으므로 가로 해상도가 짝수여야 "
            "합니다 (현재 %d)." % width)

    if width and height:
        fsize = frame_size(fmt, width, height)
        frames, remainder = divmod(file_size, fsize)
        if remainder and copy_mode:
            # 바이트 복사에는 영향이 없다. 해상도 정보만 못 믿는 것으로 처리한다.
            width, height, frames, remainder = 0, 0, 1, 0
    else:
        fsize, frames, remainder = file_size, 1, 0

    if remainder:
        if not options.allow_partial:
            raise ConversionError(
                "파일 크기(%d)가 프레임 크기(%d)의 배수가 아닙니다. "
                "해상도(%dx%d)나 포맷(%s)이 실제와 다를 수 있습니다. "
                "그래도 진행하려면 --allow-partial 을 쓰세요."
                % (file_size, fsize, width, height, fmt.name))
        if frames == 0:
            raise ConversionError(
                "프레임 하나 분량(%d바이트)도 되지 않는 파일입니다(%d바이트)."
                % (fsize, file_size))
    if frames == 0:
        raise ConversionError("변환할 프레임이 없습니다.")
    if options.max_frames and width:
        frames = min(frames, options.max_frames)

    # copy 모드에서는 해상도를 검증하지 않으므로 파일 이름에 적어 넣지 않는다.
    if plain_name or not width or copy_mode:
        out_name = "%s.raw" % stem
    else:
        out_name = "%s_%dx%d_%s.raw" % (stem, width, height, out_format)
    dst = os.path.join(out_dir, out_name)

    if os.path.abspath(dst) == os.path.abspath(src):
        raise ConversionError("출력 경로가 입력 파일과 같습니다. -o 로 다른 폴더를 지정하세요.")

    return Job(src, dst, fmt, width, height, frames, options,
               guessed_format, guessed_size, out_format)


def run_job(job):
    """계획된 변환을 실제로 수행한다. 원본 파일은 읽기 전용으로만 연다."""
    opts = job.options
    fmt = job.fmt
    kind = OUT_FORMATS[job.out_format][0]
    matrix = pick_matrix(opts.matrix, job.height)

    tables = None
    if kind in ("rgb", "bgr", "gray"):
        out_bits = OUT_FORMATS[job.out_format][2]
        tables = ColorTables(fmt, out_bits, matrix, opts.color_range)
    elif kind in ("rgb332", "rgb444", "rgb565", "rgbx32"):
        # 패킹 포맷은 8비트로 계산한 뒤 채널별로 눌러 담는다
        tables = ColorTables(fmt, 8, matrix, opts.color_range)

    source_bytes = os.path.getsize(job.src)
    if kind == "copy":
        # 해상도를 알든 모르든 바이트를 그대로 옮긴다.
        in_fsize = out_fsize = (frame_size(fmt, job.width, job.height)
                                if job.width else source_bytes)
        expected = min(out_fsize * job.frames, source_bytes)
    else:
        in_fsize = frame_size(fmt, job.width, job.height)
        out_fsize = out_frame_size(job.out_format, fmt, job.width, job.height)
        expected = out_fsize * job.frames

    out_dir = os.path.dirname(job.dst)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp = job.dst + ".part"

    started = time.time()
    written = 0
    try:
        with open(job.src, "rb") as fin, open(tmp, "wb") as fout:
            if kind == "copy":
                if opts.preview and job.width:
                    _write_preview(job.dst + ".preview.png", fin.read(in_fsize),
                                   fmt, job, matrix, opts)
                    fin.seek(0)
                # 큰 파일도 메모리에 다 올리지 않도록 조각내어 옮긴다.
                remaining = expected
                while remaining > 0:
                    chunk = fin.read(min(1 << 20, remaining))
                    if not chunk:
                        raise ConversionError("원본을 끝까지 읽지 못했습니다.")
                    fout.write(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
            else:
                for index in range(job.frames):
                    data = fin.read(in_fsize)
                    if len(data) != in_fsize:
                        raise ConversionError("프레임을 끝까지 읽지 못했습니다.")
                    if opts.preview and index == 0:
                        _write_preview(job.dst + ".preview.png", data, fmt, job,
                                       matrix, opts)
                    out = convert_frame(data, fmt, job.width, job.height,
                                        job.out_format, tables, opts.chroma)
                    if len(out) != out_fsize:
                        raise ConversionError(
                            "내부 오류: 출력 프레임 크기가 %d 이어야 하는데 %d 입니다."
                            % (out_fsize, len(out)))
                    fout.write(out)
                    written += len(out)
            fout.flush()
            os.fsync(fout.fileno())

        if written != expected:
            raise ConversionError(
                "출력 크기 검증 실패: %d != %d" % (written, expected))
        os.replace(tmp, job.dst)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise

    info = {
        "tool": "yuv2raw %s" % VERSION,
        "created": datetime.now(timezone.utc).astimezone().isoformat(),
        "source": os.path.basename(job.src),
        "source_bytes": source_bytes,
        "source_format": fmt.name if kind != "copy" else None,
        "source_bit_depth": fmt.depth if kind != "copy" else None,
        "width": job.width or None,
        "height": job.height or None,
        "frames": job.frames if job.width else None,
        "output": os.path.basename(job.dst),
        "output_format": job.out_format,
        "output_bytes_per_frame": out_fsize,
        "output_bytes": written,
        "size_unchanged": written == source_bytes,
        "matrix": matrix if tables is not None else None,
        "range": opts.color_range if tables is not None else None,
        "chroma_upsample": opts.chroma if fmt.sx * fmt.sy > 1 else None,
    }
    if opts.sidecar:
        with open(job.dst + ".json", "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
            f.write("\n")
    info["elapsed"] = time.time() - started
    return info


def _write_preview(path, data, fmt, job, matrix, opts):
    """첫 프레임을 PNG 로 저장한다.

    뷰어 설정과 무관하게 변환 결과를 눈으로 확인할 수 있게 해 준다.
    출력 포맷의 채널 비트를 그대로 반영하므로 실제 .raw 에 담긴 색 단계가 보인다.
    """
    tables = ColorTables(fmt, 8, matrix, opts.color_range)
    if OUT_FORMATS[job.out_format][0] == "gray":
        # 출력이 흑백이면 미리보기도 흑백이어야 한다
        luma = convert_frame(data, fmt, job.width, job.height, "gray8", tables,
                             opts.chroma)
        rgb = bytearray(len(luma) * 3)
        rgb[0::3] = luma
        rgb[1::3] = luma
        rgb[2::3] = luma
        rgb = bytes(rgb)
    else:
        rgb = convert_frame(data, fmt, job.width, job.height, "rgb24", tables,
                            opts.chroma)
    write_png(path, quantize_preview(rgb, job.out_format), job.width, job.height)


def _worker(payload):
    """멀티프로세싱용 진입점. (성공여부, 정보/에러메시지) 를 돌려준다."""
    job, index = payload
    try:
        return index, True, run_job(job)
    except Exception as exc:  # 한 파일이 실패해도 나머지는 계속 간다.
        return index, False, str(exc)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def planned_output_bytes(job):
    """이 계획대로 변환하면 출력 파일이 몇 바이트가 되는지."""
    src_bytes = os.path.getsize(job.src)
    if not job.width:
        return src_bytes
    total = out_frame_size(job.out_format, job.fmt,
                           job.width, job.height) * job.frames
    if OUT_FORMATS[job.out_format][0] == "copy":
        return min(total, src_bytes)
    return total


#: 출력 포맷별 채널당 비트 수(미리보기에서 실제 손실을 그대로 보여주기 위함)
CHANNEL_BITS = {
    "rgb332": (3, 3, 2),
    "rgb444": (4, 4, 4),
    "rgb565le": (5, 6, 5),
}


def quantize_preview(rgb, out_format):
    """8비트 RGB 를 출력 포맷의 채널 비트로 눌렀다가 되돌린다.

    미리보기 PNG 가 실제 .raw 에 담긴 색 단계를 그대로 보여주게 만든다.
    """
    bits = CHANNEL_BITS.get(out_format)
    if bits is None:
        return rgb
    luts = []
    for n in bits:
        top = (1 << n) - 1
        down = [(v * top + 127) // 255 for v in range(256)]
        luts.append([q * 255 // top for q in down])
    out = bytearray(rgb)
    for ch in range(3):
        lut = luts[ch]
        out[ch::3] = bytes(lut[v] for v in rgb[ch::3])
    return bytes(out)


def write_png(path, rgb, width, height):
    """8비트 RGB 바이트열을 PNG 로 저장한다(표준 라이브러리만 사용)."""
    import struct
    import zlib

    raw = bytearray()
    stride = width * 3
    for row in range(height):
        raw.append(0)                                   # 필터 없음
        raw += rgb[row * stride:(row + 1) * stride]

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def collect_inputs(paths, patterns, recursive):
    """입력 경로(파일/폴더) 목록에서 변환 대상 파일을 모은다."""
    import fnmatch

    found = []
    for path in paths:
        if os.path.isfile(path):
            found.append(path)
            continue
        if not os.path.isdir(path):
            raise ConversionError("경로를 찾을 수 없습니다: %s" % path)
        if recursive:
            for root, dirs, files in os.walk(path):
                dirs.sort()
                for name in sorted(files):
                    if any(fnmatch.fnmatch(name.lower(), p.lower()) for p in patterns):
                        found.append(os.path.join(root, name))
        else:
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                if os.path.isfile(full) and any(
                        fnmatch.fnmatch(name.lower(), p.lower()) for p in patterns):
                    found.append(full)

    seen = set()
    unique = []
    for f in found:
        key = os.path.abspath(f)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def parse_size(text):
    m = re.match(r"^\s*(\d+)\s*[xX*×]\s*(\d+)\s*$", text)
    if not m:
        raise argparse.ArgumentTypeError("해상도는 WxH 형식이어야 합니다 (예: 1920x1080)")
    return int(m.group(1)), int(m.group(2))


def build_parser():
    p = argparse.ArgumentParser(
        prog="yuv2raw",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="폴더 안의 YUV 파일을 한 번에 RAW 파일로 변환합니다. "
                    "원본은 수정하지 않고 새 파일만 만듭니다.",
        epilog="""예시:
  # 폴더 전체를 RGB24 raw 로 변환 (결과는 <폴더>/raw_out 에 생성)
  python yuv2raw.py C:\\yuv_files

  # 해상도/포맷을 직접 지정하고 출력 폴더도 따로 지정
  python yuv2raw.py ./in -o ./out --size 1920x1080 --format nv12

  # 하위 폴더까지, 색공간 변환 없이 YUV444 인터리브로
  python yuv2raw.py ./in -r --out-format yuv444

  # 무엇을 어떻게 변환할지 미리 확인만
  python yuv2raw.py ./in --dry-run
""")
    p.add_argument("inputs", nargs="*", metavar="경로",
                   help="변환할 폴더 또는 파일 (여러 개 지정 가능)")
    p.add_argument("-o", "--output", metavar="폴더",
                   help="출력 폴더 (기본: 입력 폴더 아래 raw_out)")
    p.add_argument("--size", type=parse_size, metavar="WxH",
                   help="입력 해상도. 생략하면 파일 이름과 크기로 자동 판별")
    p.add_argument("--format", metavar="FMT",
                   help="입력 YUV 포맷. 생략하면 파일 이름으로 자동 판별 (기본 추정값: i420)")
    p.add_argument("--out-format", default="gray8",
                   choices=sorted(OUT_FORMATS) + [RGB_SAME], metavar="FMT",
                   help="출력 RAW 포맷 (기본: gray8 = 흑백 8비트). "
                        "흑백이면서 크기까지 유지하려면 4:2:2 입력에 gray16le, "
                        "컬러가 필요하면 rgb24 나 rgb-same. --list-formats 참고")
    p.add_argument("--matrix", default="auto",
                   choices=["auto", "bt601", "bt709", "bt2020"],
                   help="색변환 행렬 (기본: auto = 720p 이상이면 bt709)")
    p.add_argument("--range", dest="color_range", default="limited",
                   choices=["limited", "full"],
                   help="입력 YUV 레인지 (기본: limited = TV 레인지 16~235)")
    p.add_argument("--chroma", default="nearest", choices=["nearest", "bilinear"],
                   help="크로마 업샘플링 방식 (기본: nearest)")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="하위 폴더까지 훑는다")
    p.add_argument("--pattern", default="*.yuv",
                   help="대상 파일 패턴, 쉼표로 여러 개 (기본: *.yuv)")
    p.add_argument("--frames", type=int, default=0, metavar="N",
                   help="파일당 최대 N 프레임만 변환 (0 = 전부)")
    p.add_argument("--overwrite", action="store_true",
                   help="출력 파일이 이미 있으면 덮어쓴다 (기본: 건너뜀)")
    p.add_argument("--allow-partial", action="store_true",
                   help="파일 끝의 불완전한 프레임을 버리고 진행한다")
    p.add_argument("--plain-name", action="store_true",
                   help="출력 이름을 <원본이름>.raw 로 (기본은 해상도/포맷을 덧붙임)")
    p.add_argument("--no-sidecar", action="store_true",
                   help="출력 정보를 담은 .json 사이드카를 만들지 않는다")
    p.add_argument("-j", "--jobs", default="auto", metavar="N",
                   help="동시에 변환할 파일 수 (기본: auto)")
    p.add_argument("--preview", action="store_true",
                   help="변환 결과 첫 프레임을 PNG 로도 저장한다. 뷰어 설정과 "
                        "무관하게 결과를 눈으로 확인할 수 있다")
    p.add_argument("--dry-run", action="store_true",
                   help="실제로 변환하지 않고 계획만 출력한다")
    p.add_argument("-q", "--quiet", action="store_true", help="진행 로그를 줄인다")
    p.add_argument("--list-formats", action="store_true",
                   help="지원하는 입출력 포맷을 출력하고 끝낸다")
    p.add_argument("--version", action="version", version="yuv2raw %s" % VERSION)
    return p


def print_formats():
    print("입력 YUV 포맷:")
    for name in sorted(FORMATS):
        print("  %-8s %s" % (name, FORMATS[name].describe()))
    print("\n  비트수 접미사를 붙일 수 있습니다: i420_10, yuv420p10le, nv12_16 ...")
    print("  다른 이름(별칭)도 인식합니다: yu12, iyuv, yuy2, y800 ...")
    print("\n출력 RAW 포맷:")
    for name in sorted(OUT_FORMATS):
        mark = " *" if name in SIZE_PRESERVING else "  "
        print(" %s %-9s %s" % (mark, name, OUT_FORMATS[name][3]))
    print("  %-11s %s" % (RGB_SAME,
                          "입력과 크기가 같아지는 RGB 를 자동으로 고름"))
    print("\n  * 표시는 파일 크기가 입력과 완전히 같은 출력입니다.")
    print("\n입력 픽셀당 비트 수 -> 크기가 같아지는 RGB (%s 가 고르는 값):" % RGB_SAME)
    for bits in sorted(RGB_BY_BITS):
        name = RGB_BY_BITS[bits]
        note = REDUCED_COLOR.get(name, "색 손실 없음")
        print("  %2d비트  %-9s %s" % (bits, name, note))
    print("  예) 4:2:0 8비트 = 12비트/픽셀 -> rgb444")


def resolve_jobs(files, args, options):
    """모든 입력 파일에 대해 계획을 세우고 (성공목록, 실패목록) 을 돌려준다."""
    fmt = resolve_format(args.format) if args.format else None
    jobs, failures = [], []
    used_names = {}

    for src in files:
        out_dir = args.output or os.path.join(
            os.path.dirname(os.path.abspath(src)), "raw_out")
        try:
            job = plan_job(src, out_dir, options, fmt=fmt, size=args.size,
                           plain_name=args.plain_name)
        except (ConversionError, ValueError) as exc:
            failures.append((src, str(exc)))
            continue

        key = os.path.abspath(job.dst)
        if key in used_names:
            failures.append((src, "출력 이름이 %s 와 겹칩니다."
                             % os.path.basename(used_names[key])))
            continue
        used_names[key] = src
        jobs.append(job)
    return jobs, failures


def resolve_jobs_count(value, job_count):
    if value == "auto":
        if _np is not None:
            return 1  # numpy 경로는 이미 충분히 빠르다
        return max(1, min(job_count, (os.cpu_count() or 1)))
    n = int(value)
    return max(1, n)


def make_console_safe():
    """콘솔이 표현하지 못하는 글자가 섞여도 변환이 중단되지 않게 한다.

    윈도우 명령 프롬프트는 CP949 같은 좁은 코드페이지를 쓴다. 파일 이름에
    그 코드페이지에 없는 글자가 있으면 print 하나 때문에 전체 작업이
    UnicodeEncodeError 로 죽을 수 있어서, 표시할 수 없는 글자는 대체 문자로
    찍고 넘어가게 한다.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # 파이썬 3.6 이거나 리다이렉트된 스트림


def main(argv=None):
    make_console_safe()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_formats:
        print_formats()
        return 0

    if not args.inputs:
        parser.error("변환할 폴더나 파일을 하나 이상 지정하세요.")

    options = Options(
        out_format=args.out_format, matrix=args.matrix,
        color_range=args.color_range, chroma=args.chroma,
        overwrite=args.overwrite, allow_partial=args.allow_partial,
        max_frames=max(0, args.frames), sidecar=not args.no_sidecar,
        preview=args.preview)

    patterns = [p.strip() for p in args.pattern.split(",") if p.strip()]

    try:
        files = collect_inputs(args.inputs, patterns, args.recursive)
    except ConversionError as exc:
        print("오류: %s" % exc, file=sys.stderr)
        return 2

    if not files:
        print("변환할 파일이 없습니다. (패턴: %s)" % ", ".join(patterns), file=sys.stderr)
        return 1

    jobs, failures = resolve_jobs(files, args, options)

    skipped = []
    if not args.overwrite:
        keep = []
        for job in jobs:
            if os.path.exists(job.dst):
                skipped.append(job)
            else:
                keep.append(job)
        jobs = keep

    if not args.quiet:
        backend = "numpy" if _np is not None else "순수 파이썬"
        print("yuv2raw %s (%s 백엔드)" % (VERSION, backend))
        print("대상 파일 %d개, 변환 예정 %d개, 건너뜀 %d개, 계획 실패 %d개"
              % (len(files), len(jobs), len(skipped), len(failures)))
        print("출력 포맷: %s / 행렬: %s / 레인지: %s / 크로마: %s"
              % (args.out_format, args.matrix, args.color_range, args.chroma))
        print("-" * 72)

    for job in jobs:
        notes = []
        if job.guessed_format and job.width:
            notes.append("포맷을 알 수 없어 i420 으로 가정")
        if job.guessed_size == "content":
            notes.append("포맷과 해상도를 파일 내용으로 판별")
        elif job.guessed_size:
            notes.append("해상도를 파일 크기로 추정")
        if job.out_format in REDUCED_COLOR:
            notes.append("색 단계 줄어듦: %s" % REDUCED_COLOR[job.out_format])
        if options.out_format == RGB_SAME:
            notes.append("크기 유지를 위해 %s 선택" % job.out_format)
        if not args.quiet or args.dry_run:
            src_bytes = os.path.getsize(job.src)
            out_total = planned_output_bytes(job)
            if job.width:
                geom = "%dx%d %s, %d프레임" % (job.width, job.height,
                                              job.fmt.name, job.frames)
            else:
                geom = "해상도 확인 안 함 (바이트를 그대로 옮김)"
            print("  %s -> %s" % (os.path.basename(job.src), os.path.basename(job.dst)))
            print("      %s, 입력 %s -> 출력 %s%s%s"
                  % (geom, human_bytes(src_bytes), human_bytes(out_total),
                     "  (크기 동일)" if out_total == src_bytes else "",
                     (" [%s]" % "; ".join(notes)) if notes else ""))

    for job in skipped:
        print("  건너뜀(이미 있음): %s" % os.path.basename(job.dst))
    for src, msg in failures:
        print("  실패: %s -> %s" % (os.path.basename(src), msg), file=sys.stderr)

    if args.dry_run:
        print("\n--dry-run 이므로 파일을 만들지 않았습니다.")
        return 1 if failures else 0

    if not jobs:
        return 1 if failures else 0

    if not args.quiet:
        print("-" * 72)

    njobs = resolve_jobs_count(args.jobs, len(jobs))
    results = [None] * len(jobs)

    if njobs > 1 and len(jobs) > 1:
        import concurrent.futures as cf
        try:
            with cf.ProcessPoolExecutor(max_workers=njobs) as pool:
                for index, ok, payload in pool.map(
                        _worker, [(job, i) for i, job in enumerate(jobs)]):
                    results[index] = (ok, payload)
        except Exception as exc:  # 프로세스 풀을 못 쓰면 순차 실행으로 되돌린다
            if not args.quiet:
                print("병렬 실행에 실패해 순차로 진행합니다: %s" % exc, file=sys.stderr)
            results = [None] * len(jobs)
            njobs = 1

    if njobs == 1 or any(r is None for r in results):
        for i, job in enumerate(jobs):
            if results[i] is not None:
                continue
            _, ok, payload = _worker((job, i))
            results[i] = (ok, payload)

    done = 0
    for i, job in enumerate(jobs):
        ok, payload = results[i]
        if ok:
            done += 1
            if not args.quiet:
                print("[%d/%d] %s  (%s, %.2fs)"
                      % (i + 1, len(jobs), os.path.basename(job.dst),
                         human_bytes(payload["output_bytes"]), payload["elapsed"]))
        else:
            failures.append((job.src, payload))
            print("[%d/%d] 실패: %s -> %s"
                  % (i + 1, len(jobs), os.path.basename(job.src), payload),
                  file=sys.stderr)

    if not args.quiet:
        print("-" * 72)
        print("완료: %d개 변환, %d개 실패, %d개 건너뜀"
              % (done, len(failures), len(skipped)))
        if done:
            print("원본 파일은 하나도 수정되지 않았습니다.")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
