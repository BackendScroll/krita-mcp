"""count_marked_pixels underpins the bridge's answer to "did this stroke
actually put paint down?".

Before it, paint_strokes returned a bbox derived purely from stroke
GEOMETRY (_bbox_for_geometry) and nothing else, so a stroke that marked
nothing -- clipped entirely by document.setSelection(), or a preset that
renders no dab at the requested size/pressure -- was indistinguishable
from one that painted correctly. A run could report thousands of accepted
strokes while marking almost nothing.

It reads the candidate layer's OWN pixels via Node.pixelData(), which
needs no refreshProjection() and therefore cannot trigger the busy-wait
dialog that deadlocks the bridge (see
docs/agent/auto-painter/evidence/2026-09-17-wedge-backtrace.txt).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

# Load protocol_v4 directly: importing the kritamcp PACKAGE pulls in
# __init__.py, which imports the `krita` module and only exists inside a
# running Krita. Same approach as test_protocol_v4.py.
MODULE_PATH = Path(__file__).parents[1] / "krita-plugin" / "kritamcp" / "protocol_v4.py"
SPEC = importlib.util.spec_from_file_location("krita_protocol_v4_coverage", MODULE_PATH)
protocol = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(protocol)

CHANNEL_DEPTH_BYTES = protocol.CHANNEL_DEPTH_BYTES
count_marked_pixels = protocol.count_marked_pixels
content_hash = protocol.content_hash


def _rgba_u16(pixels: list[int]) -> bytes:
    """Build an RGBA/U16 buffer (8 bytes per pixel) from alpha values."""
    out = bytearray()
    for alpha in pixels:
        out += bytes([0, 0, 0, 0, 0, 0])  # R, G, B
        out += alpha.to_bytes(2, "little")
    return bytes(out)


def _rgba_u8(pixels: list[int]) -> bytes:
    out = bytearray()
    for alpha in pixels:
        out += bytes([0, 0, 0, alpha])
    return bytes(out)


def test_all_transparent_u16_counts_zero():
    assert count_marked_pixels(_rgba_u16([0, 0, 0, 0]), 4, 2) == 0


def test_all_opaque_u16_counts_every_pixel():
    assert count_marked_pixels(_rgba_u16([65535] * 4), 4, 2) == 4


def test_partial_coverage_u16():
    assert count_marked_pixels(_rgba_u16([0, 65535, 0, 300]), 4, 2) == 2


def test_low_but_nonzero_alpha_still_counts_u16():
    # A faint dab is still paint; the low byte alone must register.
    assert count_marked_pixels(_rgba_u16([1]), 1, 2) == 1


def test_u8_layout():
    assert count_marked_pixels(_rgba_u8([0, 255, 7, 0]), 4, 1) == 2


def test_zero_pixels_is_zero_not_unmeasured():
    assert count_marked_pixels(b"", 0, 2) == 0


def test_empty_buffer_for_nonzero_area_is_unmeasured():
    assert count_marked_pixels(b"", 16, 2) == -1


def test_ragged_buffer_is_unmeasured_not_zero():
    # 5 bytes cannot describe 2 RGBA pixels; must report "unknown", never
    # "painted nothing" -- a false zero would slander a working stroke.
    assert count_marked_pixels(b"\x00" * 5, 2, 2) == -1


def test_buffer_too_narrow_for_the_declared_depth_is_unmeasured():
    # 4 bytes/px cannot hold RGBA at 2 bytes per channel.
    assert count_marked_pixels(b"\x00" * 8, 2, 2) == -1


def test_channel_depth_table_covers_the_documents_this_project_creates():
    # apps/auto-painter/autopainter/config.py creates RGBA/U16 documents.
    assert CHANNEL_DEPTH_BYTES["U16"] == 2
    assert CHANNEL_DEPTH_BYTES["U8"] == 1


def test_content_hash_is_stable_and_prefixed():
    buffer = _rgba_u16([0, 65535, 0, 300])
    digest = content_hash(buffer)
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    assert content_hash(buffer) == digest  # deterministic, not salted


def test_content_hash_distinguishes_same_coverage_different_content():
    # get_node_state's whole reason to exist alongside marked_pixels: two
    # buffers with identical occupancy can still hold different paint.
    same_coverage_a = _rgba_u16([65535, 0])
    same_coverage_b = _rgba_u16([0, 65535])
    assert count_marked_pixels(same_coverage_a, 2, 2) == count_marked_pixels(
        same_coverage_b, 2, 2
    )
    assert content_hash(same_coverage_a) != content_hash(same_coverage_b)


# --- trace_diff_rgba (2026-09-18: per-stroke trace via pixelData diff) ---

trace_diff_rgba = protocol.trace_diff_rgba


def _bgra_u16(pixels: list[tuple[int, int, int, int]]) -> bytes:
    """Build an RGBA/U16 buffer in Krita's real storage order -- B, G, R, A
    (see _fill_u16 in the plugin, which packs blue, green, red, alpha)."""
    out = bytearray()
    for b, g, r, a in pixels:
        out += b.to_bytes(2, "little")
        out += g.to_bytes(2, "little")
        out += r.to_bytes(2, "little")
        out += a.to_bytes(2, "little")
    return bytes(out)


def test_trace_diff_only_changed_pixels_carry_color():
    before = _bgra_u16([(0, 0, 0, 0)] * 3)
    after = _bgra_u16([(0, 0, 0, 0), (0, 0, 65535, 65535), (0, 0, 0, 0)])
    out = trace_diff_rgba(before, after, 3, 2)
    assert out is not None and len(out) == 3 * 4
    # Pixel 0 unchanged -> fully transparent.
    assert out[0:4] == b"\x00\x00\x00\x00"
    # Pixel 1: red stroke at full opacity, downshifted to 8 bit, BGRA order.
    assert out[4] == 0  # B
    assert out[5] == 0  # G
    assert out[6] == 255  # R
    assert out[7] == 255  # A
    # Pixel 2 unchanged.
    assert out[8:12] == b"\x00\x00\x00\x00"


def test_trace_diff_unsupported_depth_returns_none():
    raw = b"\x00" * 16
    assert trace_diff_rgba(raw, raw, 1, 4) is None


def test_trace_diff_mismatched_buffers_return_none():
    a = _bgra_u16([(0, 0, 0, 0)] * 2)
    b = _bgra_u16([(0, 0, 0, 0)] * 3)
    assert trace_diff_rgba(a, b, 2, 2) is None
    assert trace_diff_rgba(None, a, 2, 2) is None
    assert trace_diff_rgba(b"", a, 2, 2) is None


def test_trace_diff_u8_no_downshift():
    before = bytes([0, 0, 0, 0] * 2)
    after = bytes([0, 0, 0, 0]) + bytes([0, 255, 0, 128])
    out = trace_diff_rgba(before, after, 2, 1)
    assert out[0:4] == b"\x00\x00\x00\x00"
    assert out[4:8] == bytes([0, 255, 0, 128])  # B,G,R,A unchanged at 8 bit


def test_trace_diff_identical_buffers_all_transparent():
    raw = _bgra_u16([(10, 20, 30, 65535)] * 5)
    out = trace_diff_rgba(raw, raw, 5, 2)
    assert out == b"\x00" * (5 * 4)
