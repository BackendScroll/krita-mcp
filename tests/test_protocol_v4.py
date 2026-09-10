"""Contract and security tests for the protocol-v4 bridge core."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


MODULE_PATH = (
    Path(__file__).parents[1]
    / "krita-plugin"
    / "kritamcp"
    / "protocol_v4.py"
)
SPEC = importlib.util.spec_from_file_location("krita_protocol_v4", MODULE_PATH)
protocol = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(protocol)


class EnvelopeTests(unittest.TestCase):
    def request(self, **overrides):
        value = {
            "protocol_version": 4,
            "request_id": "request-1",
            "session_id": "session-1",
            "document_id": "document-1",
            "expected_revision": 0,
            "action": "create_layer",
            "params": {"name": "Mass", "type": "paintlayer"},
        }
        value.update(overrides)
        return value

    def test_rejects_non_v4_and_unknown_actions(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_request(self.request(protocol_version=3))
        self.assertEqual(caught.exception.code, "protocol_mismatch")

        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_request(self.request(action="stroke"))
        self.assertEqual(caught.exception.code, "unknown_action")

    def test_writes_require_expected_revision_but_reads_do_not(self):
        write = self.request()
        write.pop("expected_revision")
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_request(write)
        self.assertEqual(caught.exception.code, "expected_revision_required")

        read = self.request(action="get_state")
        read.pop("expected_revision")
        validated = protocol.validate_request(read)
        self.assertEqual(validated["action"], "get_state")

    def test_body_size_is_bounded_before_json_decoding(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.parse_json_body(b"{}", protocol.MAX_BODY_BYTES + 1)
        self.assertEqual(caught.exception.code, "body_too_large")

    def test_rejects_unsupported_sensors_and_batch_overflow(self):
        cubic = {
            "stroke_id": "curve-1",
            "preset_id": "preset:abc",
            "size": 20.0,
            "opacity": 0.8,
            "flow": 0.7,
            "blend_mode": "normal",
            "foreground_rgba": [20, 30, 40, 255],
            "erase": False,
            "fixed_pressure": True,
            "geometry": {
                "type": "cubic",
                "start": [0, 0],
                "commands": [{"control1": [1, 1], "control2": [2, 2], "end": [3, 3]}],
            },
            "pressure": {"start": 0.5, "end": 1.0},
        }
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_strokes([cubic], traced=False)
        self.assertEqual(caught.exception.code, "unsupported_sensor")

        line = {
            "stroke_id": "line-1",
            "preset_id": "preset:abc",
            "size": 20.0,
            "opacity": 0.8,
            "flow": 0.7,
            "blend_mode": "normal",
            "foreground_rgba": [20, 30, 40, 255],
            "erase": False,
            "geometry": {"type": "line", "start": [0, 0], "end": [1, 1]},
            "pressure": {"start": 0.5, "end": 1.0},
        }
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_strokes([line] * 33, traced=True)
        self.assertEqual(caught.exception.code, "batch_limit")
        protocol.validate_strokes([line] * 64, traced=False)

        missing_brush = dict(line)
        missing_brush.pop("preset_id")
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_strokes([missing_brush], traced=False)
        self.assertEqual(caught.exception.code, "invalid_brush_settings")

        curve_without_fixed_pressure = dict(cubic)
        curve_without_fixed_pressure.pop("pressure")
        curve_without_fixed_pressure["fixed_pressure"] = False
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_strokes([curve_without_fixed_pressure], traced=False)
        self.assertEqual(caught.exception.code, "unsupported_sensor")


class SessionStateTests(unittest.TestCase):
    def test_auth_revision_and_idempotent_write_cache(self):
        state = protocol.ProtocolState(token="a" * 64)
        state.authenticate("Bearer " + "a" * 64)

        calls = []

        def execute(action, params):
            calls.append((action, params))
            return {"created": True}

        request = {
            "protocol_version": 4,
            "request_id": "same-request",
            "session_id": "writer",
            "document_id": "doc",
            "expected_revision": 0,
            "action": "create_layer",
            "params": {"name": "Mass"},
        }
        first = state.dispatch(request, execute)
        second = state.dispatch(request, execute)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["document_revision"], 1)

        stale = dict(request, request_id="stale", expected_revision=0)
        with self.assertRaises(protocol.ProtocolError) as caught:
            state.dispatch(stale, execute)
        self.assertEqual(caught.exception.code, "revision_conflict")

        reused = dict(request, params={"name": "Other"})
        with self.assertRaises(protocol.ProtocolError) as caught:
            state.dispatch(reused, execute)
        self.assertEqual(caught.exception.code, "request_id_conflict")

    def test_writer_lease_and_transaction_ownership(self):
        state = protocol.ProtocolState(token="b" * 64)
        state.obtain_writer("one")
        with self.assertRaises(protocol.ProtocolError) as caught:
            state.obtain_writer("two")
        self.assertEqual(caught.exception.code, "writer_busy")

        state.register_transaction("txn", "one", "doc", "candidate")
        state.require_transaction("txn", "one", "doc")
        with self.assertRaises(protocol.ProtocolError) as caught:
            state.require_transaction("txn", "two", "doc")
        self.assertEqual(caught.exception.code, "transaction_owner_mismatch")
        state.release_session("one")
        self.assertIsNone(state.writer_session)


class FilesystemTests(unittest.TestCase):
    def test_confines_paths_and_rejects_symlink_escape(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            assets = base / "assets"
            traces = base / "traces"
            outside = base / "outside"
            assets.mkdir()
            traces.mkdir()
            outside.mkdir()
            (outside / "secret.png").write_bytes(b"secret")
            (assets / "link").symlink_to(outside, target_is_directory=True)

            guard = protocol.PathGuard([assets], [assets, traces])
            self.assertEqual(
                guard.resolve_write(assets / "ok.png"),
                (assets / "ok.png").resolve(),
            )
            with self.assertRaises(protocol.ProtocolError):
                guard.resolve_write(assets / ".." / "outside" / "bad.png")
            with self.assertRaises(protocol.ProtocolError):
                guard.resolve_read(assets / "link" / "secret.png")
            with self.assertRaises(protocol.ProtocolError):
                guard.resolve_write(Path("relative.png"))

    def test_stable_brush_ids_include_filename_and_hash(self):
        first = protocol.brush_fingerprint("preset", "Dry", "a.kpp", "11" * 32)
        second = protocol.brush_fingerprint("preset", "Dry", "b.kpp", "11" * 32)
        third = protocol.brush_fingerprint("preset", "Dry", "a.kpp", "22" * 32)
        self.assertEqual(first, protocol.brush_fingerprint("preset", "Dry", "a.kpp", "11" * 32))
        self.assertEqual(len({first, second, third}), 3)


class TokenTests(unittest.TestCase):
    def test_token_file_is_atomic_private_and_reused(self):
        with TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "krita-mcp" / "token"
            first = protocol.ensure_token(token_path)
            second = protocol.ensure_token(token_path)
            self.assertEqual(first, second)
            self.assertEqual(len(first), 64)
            self.assertEqual(os.stat(token_path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
