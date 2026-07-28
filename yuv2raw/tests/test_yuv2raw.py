#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yuv2raw 테스트.

    python3 -m unittest discover -s yuv2raw/tests -v
    또는  python3 yuv2raw/tests/test_yuv2raw.py
"""

import importlib.util
import json
import os
import random
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODULE_PATH = os.path.join(ROOT, "yuv2raw.py")

sys.path.insert(0, ROOT)
import yuv2raw as y2r  # noqa: E402


def _load_pure():
    """numpy 를 끈 상태의 두 번째 모듈 인스턴스를 만든다."""
    os.environ["YUV2RAW_NO_NUMPY"] = "1"
    try:
        spec = importlib.util.spec_from_file_location("yuv2raw_pure", MODULE_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        os.environ.pop("YUV2RAW_NO_NUMPY", None)
    return mod


PURE = _load_pure()
HAS_NUMPY = y2r._np is not None


# --------------------------------------------------------------------------
# 테스트용 프레임 생성기
# --------------------------------------------------------------------------

def make_planes(w, h, fmt, seed=0):
    """(Y, U, V) 를 8비트 리스트로 만든다."""
    rnd = random.Random(seed)
    cw, ch = y2r.chroma_size(fmt, w, h)
    y = [rnd.randrange(256) for _ in range(w * h)]
    if fmt.kind == "gray":
        return y, [], []
    u = [rnd.randrange(256) for _ in range(cw * ch)]
    v = [rnd.randrange(256) for _ in range(cw * ch)]
    return y, u, v


def pack(fmt, w, h, y, u, v):
    """Y/U/V 리스트를 해당 포맷의 바이트열로 묶는다."""
    cw, ch = y2r.chroma_size(fmt, w, h)
    if fmt.kind == "gray":
        return bytes(y)
    if fmt.kind == "planar":
        first, second = (u, v) if fmt.order == "yuv" else (v, u)
        return bytes(y) + bytes(first) + bytes(second)
    if fmt.kind == "semiplanar":
        chroma = bytearray(2 * cw * ch)
        first, second = (u, v) if fmt.order == "uv" else (v, u)
        chroma[0::2] = bytes(first)
        chroma[1::2] = bytes(second)
        return bytes(y) + bytes(chroma)
    # packed422
    out = bytearray(w * h * 2)
    if fmt.order == "yuyv":
        out[0::2], out[1::4], out[3::4] = bytes(y), bytes(u), bytes(v)
    elif fmt.order == "yvyu":
        out[0::2], out[3::4], out[1::4] = bytes(y), bytes(u), bytes(v)
    elif fmt.order == "uyvy":
        out[1::2], out[0::4], out[2::4] = bytes(y), bytes(u), bytes(v)
    else:  # vyuy
        out[1::2], out[2::4], out[0::4] = bytes(y), bytes(u), bytes(v)
    return bytes(out)


def solid_i420(w, h, yv, uv, vv):
    return bytes([yv] * (w * h) + [uv] * (w * h // 4) + [vv] * (w * h // 4))


def convert(mod, data, fmt, w, h, out_format="rgb24", matrix="bt601",
            color_range="limited", chroma="nearest"):
    kind = mod.OUT_FORMATS[out_format][0]
    tables = None
    if kind in ("rgb", "bgr", "gray"):
        tables = mod.ColorTables(fmt, mod.out_value_bits(out_format),
                                 matrix, color_range)
    elif kind == "gray12p":
        tables = mod.ColorTables(fmt, 12, matrix, color_range)
    elif kind in ("rgb332", "rgb444", "rgb565", "rgbx32"):
        tables = mod.ColorTables(fmt, 8, matrix, color_range)
    return mod.convert_frame(data, fmt, w, h, out_format, tables, chroma)


def unpack_rgb444(blob, w, h):
    """rgb444 바이트열을 (R,G,B) 4비트 값 리스트로 되돌린다."""
    r = [0] * (w * h)
    g = [0] * (w * h)
    b = [0] * (w * h)
    i = 0
    for row in range(h):
        for col in range(0, w, 2):
            b0, b1, b2 = blob[i], blob[i + 1], blob[i + 2]
            i += 3
            p = row * w + col
            r[p], g[p] = b0 >> 4, b0 & 15
            b[p], r[p + 1] = b1 >> 4, b1 & 15
            g[p + 1], b[p + 1] = b2 >> 4, b2 & 15
    return r, g, b


# --------------------------------------------------------------------------

class TestFormats(unittest.TestCase):

    def test_aliases(self):
        self.assertEqual(y2r.resolve_format("yu12").name, "i420")
        self.assertEqual(y2r.resolve_format("YUY2").name, "yuyv")
        self.assertEqual(y2r.resolve_format("yuv420p").name, "i420")
        self.assertEqual(y2r.resolve_format("y800").name, "gray")
        self.assertEqual(y2r.resolve_format("NV12").name, "nv12")
        self.assertEqual(y2r.resolve_format("nv16").name, "nv16")
        self.assertEqual(y2r.resolve_format("p010").depth, 10)

    def test_depth_suffix(self):
        f = y2r.resolve_format("yuv420p10le")
        self.assertEqual((f.name, f.depth, f.bytes_per_sample), ("i420", 10, 2))
        f = y2r.resolve_format("i420_16")
        self.assertEqual((f.name, f.depth), ("i420", 16))
        f = y2r.resolve_format("nv12-10bit")
        self.assertEqual((f.name, f.depth), ("nv12", 10))

    def test_rejects_unknown_and_be(self):
        with self.assertRaises(ValueError):
            y2r.resolve_format("banana")
        with self.assertRaises(ValueError):
            y2r.resolve_format("yuv420p10be")
        with self.assertRaises(ValueError):
            y2r.resolve_format("yuyv10")     # 패킹 422 는 8비트만

    def test_frame_sizes(self):
        f = y2r.resolve_format
        self.assertEqual(y2r.frame_size(f("i420"), 1920, 1080), 1920 * 1080 * 3 // 2)
        self.assertEqual(y2r.frame_size(f("nv12"), 1920, 1080), 1920 * 1080 * 3 // 2)
        self.assertEqual(y2r.frame_size(f("yuyv"), 1920, 1080), 1920 * 1080 * 2)
        self.assertEqual(y2r.frame_size(f("i422"), 1920, 1080), 1920 * 1080 * 2)
        self.assertEqual(y2r.frame_size(f("i444"), 1920, 1080), 1920 * 1080 * 3)
        self.assertEqual(y2r.frame_size(f("gray"), 1920, 1080), 1920 * 1080)
        self.assertEqual(y2r.frame_size(f("p010"), 1920, 1080), 1920 * 1080 * 3)

    def test_geometry_validation(self):
        with self.assertRaises(ValueError):
            y2r.validate_geometry(y2r.resolve_format("i420"), 1921, 1080)
        with self.assertRaises(ValueError):
            y2r.validate_geometry(y2r.resolve_format("i420"), 1920, 1081)
        # 4:2:2 는 세로 홀수를 허용한다
        y2r.validate_geometry(y2r.resolve_format("i422"), 1920, 1081)


class TestDetection(unittest.TestCase):

    def test_size_from_name(self):
        self.assertEqual(y2r.detect_size_from_name("clip_1920x1080_nv12.yuv"),
                         (1920, 1080))
        self.assertEqual(y2r.detect_size_from_name("a-352X288.yuv"), (352, 288))
        self.assertIsNone(y2r.detect_size_from_name("clip.yuv"))

    def test_format_from_name(self):
        self.assertEqual(y2r.detect_format_from_name("clip_1920x1080_nv12.yuv").name,
                         "nv12")
        self.assertEqual(y2r.detect_format_from_name("cam.uyvy.yuv").name, "uyvy")
        f = y2r.detect_format_from_name("shot_yuv420p10le_3840x2160.yuv")
        self.assertEqual((f.name, f.depth), ("i420", 10))
        f = y2r.detect_format_from_name("shot_nv12_10bit.yuv")
        self.assertEqual((f.name, f.depth), ("nv12", 10))
        self.assertIsNone(y2r.detect_format_from_name("clip.yuv"))

    def test_resolution_not_confused_with_format(self):
        # 해상도 표기의 '420' 같은 숫자를 포맷으로 오인하면 안 된다
        self.assertIsNone(y2r.detect_format_from_name("test_640x420.yuv"))

    def test_size_from_filesize(self):
        fmt = y2r.resolve_format("i420")
        size = y2r.frame_size(fmt, 1920, 1080)
        self.assertEqual(y2r.detect_size_from_filesize(fmt, size), (1920, 1080))

    def test_size_from_filesize_ambiguous(self):
        fmt = y2r.resolve_format("i420")
        # 여러 해상도의 배수가 되는 크기는 추정 대신 오류를 내야 한다
        size = y2r.frame_size(fmt, 1920, 1080) * 64
        with self.assertRaises(ValueError):
            y2r.detect_size_from_filesize(fmt, size)


class TestSizePreserving(unittest.TestCase):
    """copy 모드는 파일 크기가 1바이트도 바뀌지 않아야 한다."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="yuv2raw_copy_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.src_dir = os.path.join(self.dir, "in")
        self.out_dir = os.path.join(self.dir, "out")
        os.makedirs(self.src_dir)

    def _write(self, name, data):
        path = os.path.join(self.src_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _outputs(self):
        return sorted(n for n in os.listdir(self.out_dir) if n.endswith(".raw"))

    def test_bytes_are_identical(self):
        payloads = {}
        for name, fmt_name, w, h, frames in (
                ("a_16x16_i420.yuv", "i420", 16, 16, 2),
                ("b_32x16_nv12.yuv", "nv12", 32, 16, 1),
                ("c_16x16_yuyv.yuv", "yuyv", 16, 16, 3),
                ("d_16x16_i444.yuv", "i444", 16, 16, 1)):
            fmt = y2r.resolve_format(fmt_name)
            blob = b""
            for i in range(frames):
                y, u, v = make_planes(w, h, fmt, seed=i)
                blob += pack(fmt, w, h, y, u, v)
            payloads[name] = blob
            self._write(name, blob)

        rc = y2r.main([self.src_dir, "-o", self.out_dir, "-q",
                       "--out-format", "copy"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._outputs()), 4)

        for name, blob in payloads.items():
            out = os.path.join(self.out_dir, os.path.splitext(name)[0] + ".raw")
            self.assertTrue(os.path.exists(out), out)
            with open(out, "rb") as f:
                self.assertEqual(f.read(), blob, "%s 의 바이트가 달라졌습니다" % name)

    def test_each_file_keeps_its_own_size(self):
        # 파일마다 크기가 달라도 각자의 크기가 그대로 유지되어야 한다
        sizes = {}
        for name, n in (("a.yuv", 1234567), ("b.yuv", 99), ("c.yuv", 5000000)):
            sizes[name] = n
            self._write(name, os.urandom(64) * (n // 64) + b"\x00" * (n % 64))

        y2r.main([self.src_dir, "-o", self.out_dir, "-q", "--out-format", "copy"])
        for name, n in sizes.items():
            out = os.path.join(self.out_dir, os.path.splitext(name)[0] + ".raw")
            self.assertEqual(os.path.getsize(out), n)

    def test_works_without_any_resolution_info(self):
        # 어떤 해상도에도 맞지 않는 크기 - rgb24 는 실패하고 copy 는 성공한다
        self._write("weird.yuv", b"\x77" * 1234567)
        self.assertEqual(
            y2r.main([self.src_dir, "-o", self.out_dir, "-q"]), 1)
        self.assertEqual(
            y2r.main([self.src_dir, "-o", self.out_dir, "-q",
                      "--out-format", "copy"]), 0)
        self.assertEqual(os.path.getsize(os.path.join(self.out_dir, "weird.raw")),
                         1234567)

    def test_wrong_name_resolution_does_not_matter(self):
        # 이름은 2560x2160 이지만 실제는 2560x1440 3프레임
        blob = b"\x55" * (2560 * 1440 * 3 // 2 * 3)
        self._write("cap_2560x2160.yuv", blob)
        rc = y2r.main([self.src_dir, "-o", self.out_dir, "-q",
                       "--out-format", "copy"])
        self.assertEqual(rc, 0)
        out = os.path.join(self.out_dir, "cap_2560x2160.raw")
        self.assertEqual(os.path.getsize(out), len(blob))

    def test_output_name_has_no_unverified_resolution(self):
        self._write("cap_2560x2160.yuv", b"\x55" * (2560 * 1440 * 3 // 2 * 3))
        y2r.main([self.src_dir, "-o", self.out_dir, "-q", "--out-format", "copy"])
        self.assertEqual(self._outputs(), ["cap_2560x2160.raw"])

    def test_sidecar_marks_size_unchanged(self):
        blob = b"\x11" * (16 * 16 * 3 // 2)
        self._write("a_16x16_i420.yuv", blob)
        y2r.main([self.src_dir, "-o", self.out_dir, "-q", "--out-format", "copy"])
        with open(os.path.join(self.out_dir, "a_16x16_i420.raw.json"),
                  encoding="utf-8") as f:
            info = json.load(f)
        self.assertTrue(info["size_unchanged"])
        self.assertEqual(info["source_bytes"], info["output_bytes"])

    def test_frame_limit_still_works(self):
        fmt = y2r.resolve_format("i420")
        one = b""
        y, u, v = make_planes(16, 16, fmt)
        one = pack(fmt, 16, 16, y, u, v)
        self._write("a_16x16_i420.yuv", one * 4)
        y2r.main([self.src_dir, "-o", self.out_dir, "-q",
                  "--out-format", "copy", "--frames", "2"])
        out = os.path.join(self.out_dir, "a_16x16_i420.raw")
        self.assertEqual(os.path.getsize(out), len(one) * 2)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), one * 2)

    def test_planar_output_also_preserves_size(self):
        fmt = y2r.resolve_format("nv12")
        y, u, v = make_planes(32, 16, fmt)
        blob = pack(fmt, 32, 16, y, u, v)
        self._write("a_32x16_nv12.yuv", blob)
        y2r.main([self.src_dir, "-o", self.out_dir, "-q",
                  "--out-format", "planar"])
        out = os.path.join(self.out_dir, "a_32x16_nv12_32x16_planar.raw")
        self.assertEqual(os.path.getsize(out), len(blob))

    def test_size_preserving_list_is_accurate(self):
        # SIZE_PRESERVING 은 입력이 무엇이든 크기가 같아야 한다
        w = h = 16
        for name in y2r.SIZE_PRESERVING:
            for fmt_name in ("i420", "nv12", "i422", "yuyv", "i444", "gray",
                             "yuv420p10le", "p010"):
                fmt = y2r.resolve_format(fmt_name)
                self.assertEqual(y2r.out_frame_size(name, fmt, w, h),
                                 y2r.frame_size(fmt, w, h),
                                 "%s / %s" % (name, fmt_name))

    def test_rgb24_doubles_420_input(self):
        fmt = y2r.resolve_format("i420")
        w = h = 16
        self.assertEqual(y2r.out_frame_size("rgb24", fmt, w, h),
                         y2r.frame_size(fmt, w, h) * 2)

    def test_rgb_same_matches_every_input(self):
        # rgb-same 이 고른 포맷은 그 입력에서 반드시 크기가 같아야 한다
        w = h = 16
        for fmt_name in ("i420", "nv12", "yv12", "i422", "yuyv", "i444",
                         "gray", "yuv420p10le", "yuv422p10le", "yuv444p10le",
                         "p010"):
            fmt = y2r.resolve_format(fmt_name)
            picked = y2r.rgb_same_size_format(fmt)
            self.assertEqual(y2r.out_frame_size(picked, fmt, w, h),
                             y2r.frame_size(fmt, w, h),
                             "%s -> %s" % (fmt_name, picked))

    def test_source_is_untouched(self):
        blob = b"\x42" * 4096
        path = self._write("a.yuv", blob)
        y2r.main([self.src_dir, "-o", self.out_dir, "-q", "--out-format", "copy"])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), blob)


class TestRgbSameSize(unittest.TestCase):
    """RGB 로 바꾸면서도 파일 크기가 그대로여야 한다."""

    W, H = 16, 16

    def _blob(self, fmt_name, frames=1):
        fmt = y2r.resolve_format(fmt_name)
        out = b""
        for i in range(frames):
            y, u, v = make_planes(self.W, self.H, fmt, seed=i)
            out += pack(fmt, self.W, self.H, y, u, v)
        return fmt, out

    def test_bits_per_pixel(self):
        expect = {"gray": 8, "i420": 12, "nv12": 12, "i422": 16, "yuyv": 16,
                  "i444": 24, "yuv420p10le": 24, "yuv422p10le": 32,
                  "yuv444p10le": 48}
        for name, bits in expect.items():
            self.assertEqual(y2r.bits_per_pixel(y2r.resolve_format(name)), bits,
                             "%s 의 픽셀당 비트 수" % name)

    def test_picks_matching_format(self):
        expect = {"i420": "rgb444", "nv12": "rgb444", "i422": "rgb565le",
                  "yuyv": "rgb565le", "i444": "rgb24", "gray": "rgb332",
                  "yuv420p10le": "rgb24", "yuv422p10le": "rgbx32",
                  "yuv444p10le": "rgb48le"}
        for name, out in expect.items():
            self.assertEqual(
                y2r.rgb_same_size_format(y2r.resolve_format(name)), out, name)

    def test_output_size_equals_input_size(self):
        for name in ("i420", "nv12", "i422", "yuyv", "i444", "gray"):
            fmt, blob = self._blob(name)
            out_name = y2r.rgb_same_size_format(fmt)
            out = convert(y2r, blob, fmt, self.W, self.H, out_name)
            self.assertEqual(len(out), len(blob),
                             "%s -> %s 크기가 달라졌습니다" % (name, out_name))

    def test_packed_sizes(self):
        fmt = y2r.resolve_format("i444")
        n = self.W * self.H
        for out_name, expect in (("rgb332", n), ("rgb444", n * 3 // 2),
                                 ("rgb565le", n * 2), ("rgbx32", n * 4),
                                 ("rgb24", n * 3)):
            self.assertEqual(
                y2r.out_frame_size(out_name, fmt, self.W, self.H), expect)
            _, blob = self._blob("i444")
            out = convert(y2r, blob, fmt, self.W, self.H, out_name)
            self.assertEqual(len(out), expect, out_name)

    def test_rgb444_packing_layout(self):
        # 흰색 한 프레임: 모든 채널이 15 여야 하고, 바이트는 전부 0xFF
        fmt = y2r.resolve_format("i420")
        w = h = 4
        data = solid_i420(w, h, 235, 128, 128)
        out = convert(y2r, data, fmt, w, h, "rgb444")
        self.assertEqual(len(out), w * h * 3 // 2)
        self.assertEqual(out, b"\xff" * len(out))
        r, g, b = unpack_rgb444(out, w, h)
        self.assertEqual(set(r) | set(g) | set(b), {15})

    def test_rgb444_black_and_primary(self):
        fmt = y2r.resolve_format("i420")
        w = h = 4
        black = convert(y2r, solid_i420(w, h, 16, 128, 128), fmt, w, h, "rgb444")
        self.assertEqual(black, b"\x00" * len(black))
        red = convert(y2r, solid_i420(w, h, 81, 90, 240), fmt, w, h, "rgb444")
        r, g, b = unpack_rgb444(red, w, h)
        self.assertEqual(r[0], 15)
        self.assertLessEqual(max(g[0], b[0]), 1)

    def test_rgb565_channel_layout(self):
        fmt = y2r.resolve_format("i422")
        w, h = 4, 2
        y = [235] * (w * h)
        u = [128] * (w // 2 * h)
        v = [128] * (w // 2 * h)
        out = convert(y2r, pack(fmt, w, h, y, u, v), fmt, w, h, "rgb565le")
        self.assertEqual(len(out), w * h * 2)
        self.assertEqual(out[0:2], b"\xff\xff")      # 흰색 = 0xFFFF

    def test_rgb332_white_and_black(self):
        fmt = y2r.resolve_format("gray")
        w, h = 4, 2
        white = convert(y2r, bytes([235] * (w * h)), fmt, w, h, "rgb332")
        self.assertEqual(white, b"\xff" * (w * h))
        black = convert(y2r, bytes([16] * (w * h)), fmt, w, h, "rgb332")
        self.assertEqual(black, b"\x00" * (w * h))

    def test_rgbx32_padding_byte(self):
        fmt = y2r.resolve_format("i444")
        _, blob = self._blob("i444")
        out = convert(y2r, blob, fmt, self.W, self.H, "rgbx32")
        self.assertEqual(set(out[3::4]), {255})
        rgb = convert(y2r, blob, fmt, self.W, self.H, "rgb24")
        self.assertEqual(bytes(out[0::4]), bytes(rgb[0::3]))
        self.assertEqual(bytes(out[1::4]), bytes(rgb[1::3]))
        self.assertEqual(bytes(out[2::4]), bytes(rgb[2::3]))

    def test_rgb444_quantizes_rgb24(self):
        # rgb444 값은 rgb24 값을 4비트로 반올림한 것과 같아야 한다
        fmt, blob = self._blob("i420")
        rgb = convert(y2r, blob, fmt, self.W, self.H, "rgb24")
        packed = convert(y2r, blob, fmt, self.W, self.H, "rgb444")
        r, g, b = unpack_rgb444(packed, self.W, self.H)
        for i in range(self.W * self.H):
            for got, full in ((r[i], rgb[i * 3]), (g[i], rgb[i * 3 + 1]),
                              (b[i], rgb[i * 3 + 2])):
                self.assertEqual(got, (full * 15 + 127) // 255)

    def test_odd_width_rejected_for_rgb444(self):
        d = tempfile.mkdtemp(prefix="yuv2raw_444_")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "a.yuv")
        with open(path, "wb") as f:
            f.write(b"\x10" * (15 * 8 * 3))
        opts = y2r.Options(out_format="rgb444")
        with self.assertRaises(y2r.ConversionError):
            y2r.plan_job(path, os.path.join(d, "out"), opts,
                         fmt=y2r.resolve_format("i444"), size=(15, 8))
        # 가로가 짝수면 통과한다
        y2r.plan_job(path, os.path.join(d, "out"), opts,
                     fmt=y2r.resolve_format("i444"), size=(10, 12))

    def test_end_to_end_keeps_file_size(self):
        d = tempfile.mkdtemp(prefix="yuv2raw_same_")
        self.addCleanup(shutil.rmtree, d, True)
        src_dir = os.path.join(d, "in")
        os.makedirs(src_dir)
        sizes = {}
        for name, fmt_name in (("a_16x16_i420.yuv", "i420"),
                               ("b_16x16_i422.yuv", "i422"),
                               ("c_16x16_i444.yuv", "i444")):
            fmt, blob = self._blob(fmt_name, frames=2)
            with open(os.path.join(src_dir, name), "wb") as f:
                f.write(blob)
            sizes[name] = len(blob)
        out_dir = os.path.join(d, "out")
        rc = y2r.main([src_dir, "-o", out_dir, "-q",
                       "--out-format", y2r.RGB_SAME])
        self.assertEqual(rc, 0)
        produced = [n for n in os.listdir(out_dir) if n.endswith(".raw")]
        self.assertEqual(len(produced), 3)
        for name, size in sizes.items():
            match = [n for n in produced if n.startswith(os.path.splitext(name)[0])]
            self.assertEqual(os.path.getsize(os.path.join(out_dir, match[0])),
                             size, "%s 크기가 바뀌었습니다" % name)


class TestContentDetection(unittest.TestCase):
    """파일 이름에 아무 정보가 없어도 내용만으로 포맷과 해상도를 찾는다."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="yuv2raw_content_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _photo(self, w, h):
        """사진 비슷한 휘도 평면.

        판별은 '아래 줄이 윗 줄과 닮았다'는 성질에 기대므로, 세로로도
        이어지는 그림이어야 한다. 줄마다 독립인 잡음은 사진이 아니다.
        """
        rows = []
        for y in range(h):
            row = bytearray(w)
            for x in range(w):
                v = 40 + (y * 120) // h + (x * 60) // w
                v += ((x // 7) * 3 + (y // 5) * 5) % 13
                row[x] = min(235, max(16, v))
            rows.append(bytes(row))
        return rows

    def _write(self, name, blob):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(blob)
        return path

    def test_detects_packed_422(self):
        w, h = 320, 180
        rows = self._photo(w, h)
        blob = bytearray()
        for row in rows:
            line = bytearray(w * 2)
            line[0::2] = row                       # Y
            line[1::4] = bytes([110]) * (w // 2)   # U
            line[3::4] = bytes([140]) * (w // 2)   # V
            blob += line
        path = self._write("noname.yuv", bytes(blob))
        got = y2r.analyze_content(path, len(blob))
        self.assertIsNotNone(got)
        self.assertEqual(got[0].name, "yuyv")
        self.assertEqual(got[1], (w, h))

    def test_detects_uyvy_order(self):
        w, h = 320, 180
        rows = self._photo(w, h)
        blob = bytearray()
        for row in rows:
            line = bytearray(w * 2)
            line[1::2] = row                       # Y 가 홀수 위치
            line[0::4] = bytes([110]) * (w // 2)
            line[2::4] = bytes([140]) * (w // 2)
            blob += line
        path = self._write("noname.yuv", bytes(blob))
        got = y2r.analyze_content(path, len(blob))
        self.assertIsNotNone(got)
        self.assertEqual(got[0].name, "uyvy")
        self.assertEqual(got[1], (w, h))

    def test_detects_planar_420(self):
        w, h = 320, 180
        rows = self._photo(w, h)
        blob = b"".join(rows)
        blob += bytes([120]) * (w // 2 * (h // 2))
        blob += bytes([133]) * (w // 2 * (h // 2))
        path = self._write("noname.yuv", blob)
        got = y2r.analyze_content(path, len(blob))
        self.assertIsNotNone(got)
        self.assertEqual(got[1], (w, h))
        self.assertEqual(got[0].sx, 2)
        self.assertEqual(got[0].sy, 2)

    def test_detects_gray(self):
        w, h = 320, 180
        blob = b"".join(self._photo(w, h))
        path = self._write("noname.yuv", blob)
        got = y2r.analyze_content(path, len(blob))
        self.assertIsNotNone(got)
        self.assertEqual(got[1], (w, h))
        self.assertEqual(got[0].name, "gray")

    def test_filename_wins_over_content(self):
        # 이름에 정보가 있으면 내용 분석을 쓰지 않는다
        w, h = 320, 180
        fmt = y2r.resolve_format("i420")
        yy, u, v = make_planes(w, h, fmt)
        path = self._write("clip_320x180_i420.yuv", pack(fmt, w, h, yy, u, v))
        job = y2r.plan_job(path, os.path.join(self.dir, "out"), y2r.Options())
        self.assertEqual((job.width, job.height), (w, h))
        self.assertEqual(job.fmt.name, "i420")

    def test_end_to_end_without_any_hint(self):
        w, h = 320, 180
        rows = self._photo(w, h)
        blob = bytearray()
        for row in rows:
            line = bytearray(w * 2)
            line[0::2] = row
            line[1::4] = bytes([110]) * (w // 2)
            line[3::4] = bytes([140]) * (w // 2)
            blob += line
        src_dir = os.path.join(self.dir, "in")
        os.makedirs(src_dir)
        with open(os.path.join(src_dir, "Aaa.yuv"), "wb") as f:
            f.write(bytes(blob))
        out_dir = os.path.join(self.dir, "out")
        self.assertEqual(y2r.main([src_dir, "-o", out_dir, "-q"]), 0)
        # 이름에 힌트가 없어도 4:2:2 로 판별되고, 10비트 흑백으로 크기가 유지된다
        out = os.path.join(out_dir, "Aaa_320x180_gray10le.raw")
        self.assertTrue(os.path.exists(out), os.listdir(out_dir))
        self.assertEqual(os.path.getsize(out), len(blob))

    def test_grayscale_output_has_no_colour(self):
        # 흑백 출력은 휘도만 담는다. 크로마가 무엇이든 결과가 같아야 한다.
        w, h = 32, 16
        fmt = y2r.resolve_format("i420")
        yy, _, _ = make_planes(w, h, fmt)
        flat = [128] * (w // 2 * (h // 2))
        wild = [20] * (w // 2 * (h // 2))
        a = convert(y2r, pack(fmt, w, h, yy, flat, flat), fmt, w, h, "gray8")
        b = convert(y2r, pack(fmt, w, h, yy, wild, wild), fmt, w, h, "gray8")
        self.assertEqual(a, b)
        self.assertEqual(len(a), w * h)

    def test_gives_up_on_random_data(self):
        # 규칙성이 없는 데이터에 억지로 답을 내놓으면 안 된다
        blob = bytes(random.Random(1).randrange(256) for _ in range(200000))
        path = self._write("noise.yuv", blob)
        got = y2r.analyze_content(path, len(blob))
        if got is not None:
            self.assertGreater(got[1][0], 0)   # 답을 냈다면 최소한 형식은 맞아야


class TestTenBitGray(unittest.TestCase):
    """10비트 흑백 출력."""

    W, H = 16, 16

    def test_black_and_white_levels(self):
        fmt = y2r.resolve_format("i420")
        w = h = 4
        black = convert(y2r, solid_i420(w, h, 16, 128, 128), fmt, w, h, "gray10le")
        white = convert(y2r, solid_i420(w, h, 235, 128, 128), fmt, w, h, "gray10le")
        import struct
        self.assertEqual(set(struct.unpack("<%dH" % (w * h), black)), {0})
        self.assertEqual(set(struct.unpack("<%dH" % (w * h), white)), {1023})

    def test_never_exceeds_10_bits(self):
        fmt = y2r.resolve_format("i420")
        y = list(range(256)) * (self.W * self.H // 256)
        n = self.W // 2 * (self.H // 2)
        blob = pack(fmt, self.W, self.H, y, [128] * n, [128] * n)
        out = convert(y2r, blob, fmt, self.W, self.H, "gray10le")
        import struct
        vals = struct.unpack("<%dH" % (self.W * self.H), out)
        self.assertLessEqual(max(vals), 1023)

    def test_two_bytes_per_pixel_little_endian(self):
        fmt = y2r.resolve_format("i420")
        w = h = 4
        out = convert(y2r, solid_i420(w, h, 235, 128, 128), fmt, w, h, "gray10le")
        self.assertEqual(len(out), w * h * 2)
        self.assertEqual(out[0:2], b"\xff\x03")      # 1023 = 0x03FF, LE

    def test_keeps_size_for_422_input(self):
        for name in ("i422", "yuyv", "uyvy", "nv16"):
            fmt = y2r.resolve_format(name)
            y, u, v = make_planes(self.W, self.H, fmt)
            blob = pack(fmt, self.W, self.H, y, u, v)
            out = convert(y2r, blob, fmt, self.W, self.H, "gray10le")
            self.assertEqual(len(out), len(blob), name)

    def test_has_no_colour(self):
        fmt = y2r.resolve_format("i420")
        yy, _, _ = make_planes(self.W, self.H, fmt)
        n = self.W // 2 * (self.H // 2)
        a = convert(y2r, pack(fmt, self.W, self.H, yy, [128] * n, [128] * n),
                    fmt, self.W, self.H, "gray10le")
        b = convert(y2r, pack(fmt, self.W, self.H, yy, [20] * n, [240] * n),
                    fmt, self.W, self.H, "gray10le")
        self.assertEqual(a, b)

    def test_12bit_variant(self):
        fmt = y2r.resolve_format("i420")
        w = h = 4
        out = convert(y2r, solid_i420(w, h, 235, 128, 128), fmt, w, h, "gray12le")
        import struct
        self.assertEqual(set(struct.unpack("<%dH" % (w * h), out)), {4095})


class TestGraySameSize(unittest.TestCase):
    """흑백으로 바꾸면서 파일 크기도 유지한다."""

    W, H = 16, 16

    def _blob(self, fmt_name, frames=1):
        fmt = y2r.resolve_format(fmt_name)
        out = b""
        for i in range(frames):
            y, u, v = make_planes(self.W, self.H, fmt, seed=i)
            out += pack(fmt, self.W, self.H, y, u, v)
        return fmt, out

    def test_picks_matching_format(self):
        expect = {"gray": "gray8", "i420": "gray12p", "nv12": "gray12p",
                  "i422": "gray16le", "yuyv": "gray16le"}
        for name, out in expect.items():
            fmt = y2r.resolve_format(name)
            picked, note = y2r.gray_same_size_format(fmt)
            self.assertEqual(picked, out, name)
            self.assertIsNone(note, "%s 는 정확히 맞아야 합니다" % name)

    def test_size_matches_exactly(self):
        for name in ("gray", "i420", "nv12", "yv12", "i422", "yuyv", "uyvy"):
            fmt, blob = self._blob(name)
            picked, _ = y2r.gray_same_size_format(fmt)
            out = convert(y2r, blob, fmt, self.W, self.H, picked)
            self.assertEqual(len(out), len(blob),
                             "%s -> %s 크기가 달라졌습니다" % (name, picked))

    def test_reports_when_it_cannot_match(self):
        for name in ("i444", "yuv420p10le"):
            picked, note = y2r.gray_same_size_format(y2r.resolve_format(name))
            self.assertIsNotNone(note, "%s 는 알려야 합니다" % name)
            self.assertIn("정확히", note)

    def test_output_has_no_colour(self):
        # 크로마를 아무 값으로 바꿔도 결과가 같아야 한다
        w, h = 16, 16
        fmt = y2r.resolve_format("i420")
        yy, _, _ = make_planes(w, h, fmt)
        n = w // 2 * (h // 2)
        for out_name in ("gray8", "gray12p", "gray16le"):
            a = convert(y2r, pack(fmt, w, h, yy, [128] * n, [128] * n),
                        fmt, w, h, out_name)
            b = convert(y2r, pack(fmt, w, h, yy, [20] * n, [240] * n),
                        fmt, w, h, out_name)
            self.assertEqual(a, b, "%s 에 색이 섞였습니다" % out_name)

    def test_gray12p_packing(self):
        w = h = 4
        fmt = y2r.resolve_format("i420")
        white = convert(y2r, solid_i420(w, h, 235, 128, 128), fmt, w, h, "gray12p")
        self.assertEqual(len(white), w * h * 3 // 2)
        self.assertEqual(white, b"\xff" * len(white))    # 4095 가 채워진다
        black = convert(y2r, solid_i420(w, h, 16, 128, 128), fmt, w, h, "gray12p")
        self.assertEqual(black, b"\x00" * len(black))

    def test_gray12p_values_round_trip(self):
        w, h = 16, 16
        fmt = y2r.resolve_format("i420")
        yy, u, v = make_planes(w, h, fmt)
        blob = convert(y2r, pack(fmt, w, h, yy, u, v), fmt, w, h, "gray12p")
        # 12비트 값을 되돌려 gray16le 과 비교한다
        ref = convert(y2r, pack(fmt, w, h, yy, u, v), fmt, w, h, "gray16le")
        import struct
        ref16 = struct.unpack("<%dH" % (w * h), ref)
        got = []
        for i in range(0, len(blob), 3):
            b0, b1, b2 = blob[i], blob[i + 1], blob[i + 2]
            got.append(((b1 & 0x0F) << 8) | b0)
            got.append((b2 << 4) | (b1 >> 4))
        for a, b in zip(got, ref16):
            self.assertLessEqual(abs(a - (b >> 4)), 1)

    def test_odd_width_rejected(self):
        d = tempfile.mkdtemp(prefix="yuv2raw_g12_")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "a.yuv")
        with open(path, "wb") as f:
            f.write(b"\x10" * (15 * 8 * 3))
        opts = y2r.Options(out_format="gray12p")
        with self.assertRaises(y2r.ConversionError):
            y2r.plan_job(path, os.path.join(d, "out"), opts,
                         fmt=y2r.resolve_format("i444"), size=(15, 8))


class TestColorCorrectness(unittest.TestCase):
    """알려진 색이 정확한 RGB 로 나오는지 확인한다."""

    W = H = 4

    def _rgb(self, yv, uv, vv, **kw):
        data = solid_i420(self.W, self.H, yv, uv, vv)
        out = convert(y2r, data, y2r.resolve_format("i420"), self.W, self.H, **kw)
        self.assertEqual(len(out), self.W * self.H * 3)
        return out[0], out[1], out[2]

    def test_limited_black_and_white(self):
        self.assertEqual(self._rgb(16, 128, 128), (0, 0, 0))
        self.assertEqual(self._rgb(235, 128, 128), (255, 255, 255))

    def test_limited_range_clipping(self):
        # 레인지 밖의 값도 절대 넘치지 않는다
        self.assertEqual(self._rgb(0, 128, 128), (0, 0, 0))
        self.assertEqual(self._rgb(255, 128, 128), (255, 255, 255))

    def test_full_range_black_and_white(self):
        self.assertEqual(self._rgb(0, 128, 128, color_range="full"), (0, 0, 0))
        self.assertEqual(self._rgb(255, 128, 128, color_range="full"),
                         (255, 255, 255))

    def test_primary_colors_bt601(self):
        # BT.601 limited 기준 빨강/초록/파랑
        r = self._rgb(81, 90, 240)
        g = self._rgb(145, 54, 34)
        b = self._rgb(41, 240, 110)
        self.assertGreater(r[0], 245); self.assertLess(max(r[1], r[2]), 12)
        self.assertGreater(g[1], 245); self.assertLess(max(g[0], g[2]), 12)
        self.assertGreater(b[2], 245); self.assertLess(max(b[0], b[1]), 12)

    def test_bgr_is_reversed_rgb(self):
        data = solid_i420(self.W, self.H, 81, 90, 240)
        fmt = y2r.resolve_format("i420")
        rgb = convert(y2r, data, fmt, self.W, self.H, out_format="rgb24")
        bgr = convert(y2r, data, fmt, self.W, self.H, out_format="bgr24")
        self.assertEqual(rgb[0:3], bytes(reversed(bgr[0:3])))

    def test_16bit_output_scales(self):
        data = solid_i420(self.W, self.H, 235, 128, 128)
        out = convert(y2r, data, y2r.resolve_format("i420"), self.W, self.H,
                      out_format="rgb48le")
        self.assertEqual(len(out), self.W * self.H * 6)
        self.assertEqual(out[0:2], b"\xff\xff")

    def test_gray_output_matches_rgb_luma(self):
        data = solid_i420(self.W, self.H, 126, 128, 128)
        fmt = y2r.resolve_format("i420")
        rgb = convert(y2r, data, fmt, self.W, self.H, out_format="rgb24")
        gray = convert(y2r, data, fmt, self.W, self.H, out_format="gray8")
        self.assertEqual(len(gray), self.W * self.H)
        self.assertEqual(gray[0], rgb[0])

    def test_matrix_changes_result(self):
        data = solid_i420(self.W, self.H, 128, 200, 60)
        fmt = y2r.resolve_format("i420")
        a = convert(y2r, data, fmt, self.W, self.H, matrix="bt601")
        b = convert(y2r, data, fmt, self.W, self.H, matrix="bt709")
        self.assertNotEqual(a, b)

    def test_matrix_auto_selection(self):
        self.assertEqual(y2r.pick_matrix("auto", 480), "bt601")
        self.assertEqual(y2r.pick_matrix("auto", 1080), "bt709")
        self.assertEqual(y2r.pick_matrix("bt601", 1080), "bt601")


class TestFormatEquivalence(unittest.TestCase):
    """같은 그림을 담은 서로 다른 포맷은 같은 RGB 를 내야 한다."""

    W, H = 16, 8

    def test_420_family(self):
        fmt = y2r.resolve_format("i420")
        y, u, v = make_planes(self.W, self.H, fmt, seed=7)
        ref = convert(y2r, pack(fmt, self.W, self.H, y, u, v), fmt, self.W, self.H)
        for name in ("yv12", "nv12", "nv21"):
            f = y2r.resolve_format(name)
            got = convert(y2r, pack(f, self.W, self.H, y, u, v), f, self.W, self.H)
            self.assertEqual(ref, got, "%s 결과가 i420 과 다릅니다" % name)

    def test_422_family(self):
        fmt = y2r.resolve_format("i422")
        y, u, v = make_planes(self.W, self.H, fmt, seed=11)
        ref = convert(y2r, pack(fmt, self.W, self.H, y, u, v), fmt, self.W, self.H)
        for name in ("yv16", "nv16", "nv61", "yuyv", "uyvy", "yvyu", "vyuy"):
            f = y2r.resolve_format(name)
            got = convert(y2r, pack(f, self.W, self.H, y, u, v), f, self.W, self.H)
            self.assertEqual(ref, got, "%s 결과가 i422 와 다릅니다" % name)

    def test_444_family(self):
        fmt = y2r.resolve_format("i444")
        y, u, v = make_planes(self.W, self.H, fmt, seed=13)
        ref = convert(y2r, pack(fmt, self.W, self.H, y, u, v), fmt, self.W, self.H)
        f = y2r.resolve_format("yv24")
        got = convert(y2r, pack(f, self.W, self.H, y, u, v), f, self.W, self.H)
        self.assertEqual(ref, got)


class TestLosslessOutputs(unittest.TestCase):

    W, H = 16, 8

    def test_planar_output_is_identity_for_planar_input(self):
        fmt = y2r.resolve_format("i420")
        y, u, v = make_planes(self.W, self.H, fmt, seed=3)
        data = pack(fmt, self.W, self.H, y, u, v)
        out = convert(y2r, data, fmt, self.W, self.H, out_format="planar")
        self.assertEqual(out, data)

    def test_planar_output_normalizes_nv12(self):
        i420 = y2r.resolve_format("i420")
        nv12 = y2r.resolve_format("nv12")
        y, u, v = make_planes(self.W, self.H, i420, seed=5)
        out = convert(y2r, pack(nv12, self.W, self.H, y, u, v), nv12,
                      self.W, self.H, out_format="planar")
        self.assertEqual(out, pack(i420, self.W, self.H, y, u, v))

    def test_yuv444_keeps_samples(self):
        fmt = y2r.resolve_format("i444")
        y, u, v = make_planes(self.W, self.H, fmt, seed=9)
        out = convert(y2r, pack(fmt, self.W, self.H, y, u, v), fmt,
                      self.W, self.H, out_format="yuv444")
        self.assertEqual(list(out[0::3]), y)
        self.assertEqual(list(out[1::3]), u)
        self.assertEqual(list(out[2::3]), v)

    def test_yuv444_nearest_upsample_of_420(self):
        fmt = y2r.resolve_format("i420")
        w = h = 4
        y = list(range(16))
        u = [10, 20, 30, 40]
        v = [50, 60, 70, 80]
        out = convert(y2r, pack(fmt, w, h, y, u, v), fmt, w, h,
                      out_format="yuv444")
        self.assertEqual(list(out[0::3]), y)
        self.assertEqual(list(out[1::3]),
                         [10, 10, 20, 20, 10, 10, 20, 20,
                          30, 30, 40, 40, 30, 30, 40, 40])


class TestHighBitDepth(unittest.TestCase):

    W, H = 8, 4

    def _pack16(self, w, h, y, u, v, shift=0):
        import array as _a
        vals = _a.array("H", [s << shift for s in (y + u + v)])
        if sys.byteorder == "big":
            vals.byteswap()
        return vals.tobytes()

    def test_10bit_planar_white(self):
        fmt = y2r.resolve_format("yuv420p10le")
        n = self.W * self.H
        data = self._pack16(self.W, self.H, [940] * n, [512] * (n // 4),
                            [512] * (n // 4))
        out = convert(y2r, data, fmt, self.W, self.H)
        self.assertEqual(out[0:3], b"\xff\xff\xff")

    def test_10bit_planar_black(self):
        fmt = y2r.resolve_format("yuv420p10le")
        n = self.W * self.H
        data = self._pack16(self.W, self.H, [64] * n, [512] * (n // 4),
                            [512] * (n // 4))
        out = convert(y2r, data, fmt, self.W, self.H)
        self.assertEqual(out[0:3], b"\x00\x00\x00")

    def test_p010_msb_alignment(self):
        fmt = y2r.resolve_format("p010")
        n = self.W * self.H
        chroma = []
        for _ in range(n // 4):
            chroma += [512, 512]
        y = [940] * n
        import array as _a
        vals = _a.array("H", [s << 6 for s in (y + chroma)])
        if sys.byteorder == "big":
            vals.byteswap()
        out = convert(y2r, vals.tobytes(), fmt, self.W, self.H)
        self.assertEqual(out[0:3], b"\xff\xff\xff")

    def test_16bit_planar_output_roundtrip(self):
        fmt = y2r.resolve_format("i420_16")
        n = self.W * self.H
        data = self._pack16(self.W, self.H, list(range(n)),
                            [100] * (n // 4), [200] * (n // 4))
        out = convert(y2r, data, fmt, self.W, self.H, out_format="planar")
        self.assertEqual(out, data)


@unittest.skipUnless(HAS_NUMPY, "numpy 가 없어 비교할 대상이 없습니다")
class TestBackendParity(unittest.TestCase):
    """numpy 경로와 순수 파이썬 경로의 출력은 바이트 단위로 같아야 한다."""

    W, H = 24, 16

    def _check(self, fmt_name, out_format, chroma="nearest", matrix="bt709",
               color_range="limited"):
        fmt = y2r.resolve_format(fmt_name)
        y, u, v = make_planes(self.W, self.H, fmt, seed=hash(fmt_name) % 1000)
        data = pack(fmt, self.W, self.H, y, u, v)
        a = convert(y2r, data, fmt, self.W, self.H, out_format, matrix,
                    color_range, chroma)
        b = convert(PURE, data, PURE.resolve_format(fmt_name), self.W, self.H,
                    out_format, matrix, color_range, chroma)
        self.assertEqual(a, b, "%s/%s/%s 에서 두 백엔드 결과가 다릅니다"
                         % (fmt_name, out_format, chroma))

    def test_parity_across_formats(self):
        for name in ("i420", "yv12", "nv12", "nv21", "i422", "nv16",
                     "yuyv", "uyvy", "yvyu", "vyuy", "i444", "gray"):
            self._check(name, "rgb24")

    def test_parity_across_outputs(self):
        for out in sorted(y2r.OUT_FORMATS):
            self._check("nv12", out)

    def test_parity_bilinear(self):
        for name in ("i420", "nv12", "i422", "yuyv"):
            self._check(name, "rgb24", chroma="bilinear")
            self._check(name, "yuv444", chroma="bilinear")

    def test_parity_ranges_and_matrices(self):
        for matrix in ("bt601", "bt709", "bt2020"):
            for rng in ("limited", "full"):
                self._check("i420", "rgb24", matrix=matrix, color_range=rng)

    def test_parity_high_bit_depth(self):
        import array as _a
        for name, shift in (("yuv420p10le", 0), ("p010", 6), ("i420_16", 0)):
            fmt = y2r.resolve_format(name)
            n = self.W * self.H
            rnd = random.Random(42)
            top = (1 << fmt.depth) - 1
            samples = [rnd.randrange(top + 1) for _ in range(n + n // 2)]
            arr = _a.array("H", [s << shift for s in samples])
            if sys.byteorder == "big":
                arr.byteswap()
            data = arr.tobytes()
            for out in ("rgb24", "rgb48le", "gray16le", "yuv444", "planar"):
                a = convert(y2r, data, fmt, self.W, self.H, out)
                b = convert(PURE, data, PURE.resolve_format(name),
                            self.W, self.H, out)
                self.assertEqual(a, b, "%s/%s 백엔드 불일치" % (name, out))


class TestFileConversion(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="yuv2raw_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.src_dir = os.path.join(self.dir, "in")
        os.makedirs(self.src_dir)

    def _write(self, name, data):
        path = os.path.join(self.src_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def _make_yuv(self, name, w, h, frames=1, fmt_name="i420"):
        fmt = y2r.resolve_format(fmt_name)
        payload = b""
        for i in range(frames):
            y, u, v = make_planes(w, h, fmt, seed=i)
            payload += pack(fmt, w, h, y, u, v)
        return self._write(name, payload)

    def test_batch_conversion_creates_new_files_only(self):
        srcs = [self._make_yuv("a_16x16_i420.yuv", 16, 16),
                self._make_yuv("b_16x16_nv12.yuv", 16, 16, fmt_name="nv12"),
                self._make_yuv("c_16x16_yuyv.yuv", 16, 16, fmt_name="yuyv")]
        before = {p: (os.path.getsize(p), open(p, "rb").read()) for p in srcs}

        out_dir = os.path.join(self.dir, "out")
        rc = y2r.main([self.src_dir, "-o", out_dir, "-q",
                       "--out-format", "rgb24"])
        self.assertEqual(rc, 0)

        # 원본은 그대로여야 한다
        for p, (size, content) in before.items():
            self.assertTrue(os.path.exists(p))
            self.assertEqual(os.path.getsize(p), size)
            with open(p, "rb") as f:
                self.assertEqual(f.read(), content)

        produced = sorted(n for n in os.listdir(out_dir) if n.endswith(".raw"))
        self.assertEqual(len(produced), 3)
        for name in produced:
            path = os.path.join(out_dir, name)
            self.assertEqual(os.path.getsize(path), 16 * 16 * 3)
            self.assertTrue(os.path.exists(path + ".json"))

    def test_sidecar_contents(self):
        self._make_yuv("clip_32x16_nv12.yuv", 32, 16, frames=3, fmt_name="nv12")
        out_dir = os.path.join(self.dir, "out")
        self.assertEqual(y2r.main([self.src_dir, "-o", out_dir, "-q",
                                   "--out-format", "rgb24"]), 0)
        sidecar = os.path.join(out_dir, "clip_32x16_nv12_32x16_rgb24.raw.json")
        with open(sidecar, encoding="utf-8") as f:
            info = json.load(f)
        self.assertEqual(info["width"], 32)
        self.assertEqual(info["height"], 16)
        self.assertEqual(info["frames"], 3)
        self.assertEqual(info["source_format"], "nv12")
        self.assertEqual(info["output_format"], "rgb24")
        self.assertEqual(info["output_bytes"], 32 * 16 * 3 * 3)
        self.assertEqual(info["matrix"], "bt601")

    def test_multi_frame_output_size(self):
        self._make_yuv("m_16x16_i420.yuv", 16, 16, frames=5)
        out_dir = os.path.join(self.dir, "out")
        y2r.main([self.src_dir, "-o", out_dir, "-q", "--out-format", "rgb24"])
        path = os.path.join(out_dir, "m_16x16_i420_16x16_rgb24.raw")
        self.assertEqual(os.path.getsize(path), 16 * 16 * 3 * 5)

    def test_frame_limit(self):
        self._make_yuv("m_16x16_i420.yuv", 16, 16, frames=5)
        out_dir = os.path.join(self.dir, "out")
        y2r.main([self.src_dir, "-o", out_dir, "-q", "--frames", "2",
                  "--out-format", "rgb24"])
        path = os.path.join(out_dir, "m_16x16_i420_16x16_rgb24.raw")
        self.assertEqual(os.path.getsize(path), 16 * 16 * 3 * 2)

    def test_truncated_file_is_rejected(self):
        fmt = y2r.resolve_format("i420")
        y, u, v = make_planes(16, 16, fmt)
        data = pack(fmt, 16, 16, y, u, v)[:-7]      # 잘린 파일
        self._write("bad_16x16_i420.yuv", data)
        out_dir = os.path.join(self.dir, "out")
        rc = y2r.main([self.src_dir, "-o", out_dir, "-q"])
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(out_dir) and os.listdir(out_dir))

    def test_truncated_file_with_allow_partial(self):
        fmt = y2r.resolve_format("i420")
        payload = b""
        for i in range(2):
            y, u, v = make_planes(16, 16, fmt, seed=i)
            payload += pack(fmt, 16, 16, y, u, v)
        self._write("bad_16x16_i420.yuv", payload[:-7])
        out_dir = os.path.join(self.dir, "out")
        rc = y2r.main([self.src_dir, "-o", out_dir, "-q", "--allow-partial",
                       "--out-format", "rgb24"])
        self.assertEqual(rc, 0)
        path = os.path.join(out_dir, "bad_16x16_i420_16x16_rgb24.raw")
        self.assertEqual(os.path.getsize(path), 16 * 16 * 3)  # 온전한 1프레임만

    def test_wrong_geometry_is_rejected(self):
        self._make_yuv("odd_15x16_i420.yuv", 16, 16)
        out_dir = os.path.join(self.dir, "out")
        rc = y2r.main([self.src_dir, "-o", out_dir, "-q", "--size", "15x16"])
        self.assertEqual(rc, 1)

    def test_existing_output_is_skipped_then_overwritten(self):
        self._make_yuv("a_16x16_i420.yuv", 16, 16)
        out_dir = os.path.join(self.dir, "out")
        args = ["--out-format", "rgb24"]
        y2r.main([self.src_dir, "-o", out_dir, "-q"] + args)
        path = os.path.join(out_dir, "a_16x16_i420_16x16_rgb24.raw")
        os.utime(path, (0, 0))
        marker = os.path.getmtime(path)

        y2r.main([self.src_dir, "-o", out_dir, "-q"] + args)   # 건너뛰어야 함
        self.assertEqual(os.path.getmtime(path), marker)

        y2r.main([self.src_dir, "-o", out_dir, "-q", "--overwrite"] + args)
        self.assertNotEqual(os.path.getmtime(path), marker)

    def test_default_output_dir_is_new_subfolder(self):
        self._make_yuv("a_16x16_i420.yuv", 16, 16)
        self.assertEqual(y2r.main([self.src_dir, "-q"]), 0)
        out_dir = os.path.join(self.src_dir, "raw_out")
        self.assertTrue(os.path.isdir(out_dir))
        self.assertTrue(os.listdir(out_dir))

    def test_recursive_and_dry_run(self):
        sub = os.path.join(self.src_dir, "nested")
        os.makedirs(sub)
        with open(os.path.join(sub, "n_16x16_i420.yuv"), "wb") as f:
            fmt = y2r.resolve_format("i420")
            y, u, v = make_planes(16, 16, fmt)
            f.write(pack(fmt, 16, 16, y, u, v))
        out_dir = os.path.join(self.dir, "out")
        self.assertEqual(y2r.main([self.src_dir, "-o", out_dir, "-r", "--dry-run"]), 0)
        self.assertFalse(os.path.exists(out_dir))
        self.assertEqual(y2r.main([self.src_dir, "-o", out_dir, "-r", "-q"]), 0)
        self.assertEqual(len([n for n in os.listdir(out_dir) if n.endswith(".raw")]), 1)

    def test_no_partial_file_left_on_failure(self):
        path = self._make_yuv("a_16x16_i420.yuv", 16, 16)
        out_dir = os.path.join(self.dir, "out")
        options = y2r.Options()
        job = y2r.plan_job(path, out_dir, options)
        original = y2r.convert_frame

        def boom(*a, **kw):
            raise RuntimeError("의도적 실패")

        y2r.convert_frame = boom
        try:
            with self.assertRaises(RuntimeError):
                y2r.run_job(job)
        finally:
            y2r.convert_frame = original
        self.assertFalse(os.path.exists(job.dst))
        self.assertFalse(os.path.exists(job.dst + ".part"))

    def test_output_name_variants(self):
        self._make_yuv("a_16x16_i420.yuv", 16, 16)
        out_dir = os.path.join(self.dir, "out")
        y2r.main([self.src_dir, "-o", out_dir, "-q", "--plain-name",
                  "--no-sidecar"])
        self.assertTrue(os.path.exists(os.path.join(out_dir, "a_16x16_i420.raw")))
        self.assertFalse(os.path.exists(
            os.path.join(out_dir, "a_16x16_i420.raw.json")))

    def test_explicit_format_and_size_override_name(self):
        # 이름은 i420 이라고 하지만 실제 데이터는 nv12 인 경우
        fmt = y2r.resolve_format("nv12")
        y, u, v = make_planes(16, 16, fmt)
        self._write("mislabeled_i420.yuv", pack(fmt, 16, 16, y, u, v))
        out_dir = os.path.join(self.dir, "out")
        rc = y2r.main([self.src_dir, "-o", out_dir, "-q", "--out-format",
                       "rgb24", "--size", "16x16", "--format", "nv12"])
        self.assertEqual(rc, 0)
        ref = convert(y2r, pack(fmt, 16, 16, y, u, v), fmt, 16, 16)
        with open(os.path.join(out_dir, "mislabeled_i420_16x16_rgb24.raw"), "rb") as f:
            self.assertEqual(f.read(), ref)

    def test_parallel_jobs(self):
        for i in range(4):
            self._make_yuv("p%d_16x16_i420.yuv" % i, 16, 16)
        out_dir = os.path.join(self.dir, "out")
        self.assertEqual(y2r.main([self.src_dir, "-o", out_dir, "-q", "-j", "2"]), 0)
        self.assertEqual(
            len([n for n in os.listdir(out_dir) if n.endswith(".raw")]), 4)

    def test_default_is_10bit_grayscale(self):
        self._make_yuv("a_16x16_i420.yuv", 16, 16)
        out_dir = os.path.join(self.dir, "out")
        self.assertEqual(y2r.main([self.src_dir, "-o", out_dir, "-q"]), 0)
        out = os.path.join(out_dir, "a_16x16_i420_16x16_gray10le.raw")
        self.assertTrue(os.path.exists(out), os.listdir(out_dir))
        self.assertEqual(os.path.getsize(out), 16 * 16 * 2)   # 16비트 그릇
        self.assertEqual(y2r.Options().out_format, "gray10le")
        self.assertEqual(y2r.out_value_bits("gray10le"), 10)

    def test_default_values_fit_in_10_bits(self):
        self._make_yuv("a_16x16_i420.yuv", 16, 16)
        out_dir = os.path.join(self.dir, "out")
        y2r.main([self.src_dir, "-o", out_dir, "-q"])
        import struct
        with open(os.path.join(out_dir, "a_16x16_i420_16x16_gray10le.raw"),
                  "rb") as f:
            data = f.read()
        vals = struct.unpack("<%dH" % (len(data) // 2), data)
        self.assertLessEqual(max(vals), 1023)
        self.assertGreater(max(vals), 512)      # 실제로 10비트 범위를 쓴다

    def test_default_is_not_colour(self):
        # 기본 출력에는 색 정보가 들어가면 안 된다
        for name in y2r.GRAY_BY_BITS.values():
            self.assertIn(y2r.OUT_FORMATS[name][0], ("gray", "gray12p"), name)

    def test_preview_png_is_written(self):
        self._make_yuv("a_16x16_i420.yuv", 16, 16)
        out_dir = os.path.join(self.dir, "out")
        y2r.main([self.src_dir, "-o", out_dir, "-q", "--preview"])
        png = os.path.join(out_dir, "a_16x16_i420_16x16_gray10le.raw.preview.png")
        self.assertTrue(os.path.exists(png), os.listdir(out_dir))
        with open(png, "rb") as f:
            head = f.read(8)
        self.assertEqual(head, b"\x89PNG\r\n\x1a\n")

    def test_preview_matches_output_color_depth(self):
        # 미리보기는 실제 출력의 색 단계를 반영해야 한다
        rgb = bytes([10, 100, 200] * 4)     # 4비트 격자에 안 맞는 값들
        self.assertEqual(y2r.quantize_preview(rgb, "rgb24"), rgb)
        self.assertEqual(y2r.quantize_preview(rgb, "copy"), rgb)
        reduced = y2r.quantize_preview(rgb, "rgb444")
        self.assertNotEqual(reduced, rgb)
        for v in reduced:
            self.assertEqual(v % 17, 0)     # 4비트 단계는 17의 배수
        # 3-3-2 는 파랑 채널만 4단계
        r332 = y2r.quantize_preview(rgb, "rgb332")
        self.assertEqual(sorted(set(r332[2::3])), [170])   # 200 -> 2/3 단계

    def test_empty_folder(self):
        self.assertEqual(y2r.main([self.src_dir, "-q"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
