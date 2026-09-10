"""FastMCP v4 schema, transport, and annotation tests."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

import httpx
from fastmcp.exceptions import ToolError


SERVER_PATH = Path(__file__).parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("krita_mcp_server_v4", SERVER_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = server
SPEC.loader.exec_module(server)


EXPECTED_TOOLS = {
    "krita_get_capabilities",
    "krita_get_state",
    "krita_create_document",
    "krita_open_document",
    "krita_save_document",
    "krita_export_document",
    "krita_close_document",
    "krita_list_layers",
    "krita_create_layer",
    "krita_update_layer",
    "krita_delete_layer",
    "krita_set_selection_from_mask",
    "krita_clear_selection",
    "krita_list_brushes",
    "krita_render_brush_probe",
    "krita_begin_paint_transaction",
    "krita_paint_strokes",
    "krita_commit_paint_transaction",
    "krita_rollback_paint_transaction",
    "krita_capture_region",
}


class SchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_tool_surface_has_output_schemas(self):
        tools = await server.mcp.list_tools(run_middleware=False)
        by_name = {tool.name: tool for tool in tools}
        self.assertEqual(set(by_name), EXPECTED_TOOLS)
        for name, tool in by_name.items():
            self.assertIsNotNone(tool.output_schema, name)
            self.assertEqual(tool.output_schema.get("type"), "object", name)

    async def test_annotations_match_side_effects(self):
        tools = await server.mcp.list_tools(run_middleware=False)
        by_name = {tool.name: tool for tool in tools}
        self.assertTrue(by_name["krita_get_state"].annotations.read_only_hint)
        self.assertFalse(by_name["krita_create_document"].annotations.read_only_hint)
        self.assertTrue(by_name["krita_delete_layer"].annotations.destructive_hint)
        self.assertTrue(
            by_name["krita_rollback_paint_transaction"].annotations.destructive_hint
        )
        self.assertFalse(by_name["krita_capture_region"].annotations.read_only_hint)

    def test_geometry_is_a_discriminated_union(self):
        line = server.Stroke.model_validate(
            {
                "stroke_id": "one",
                "geometry": {"type": "line", "start": [0, 0], "end": [10, 10]},
                "pressure": {"start": 0.2, "end": 0.8},
                "preset_id": "preset:abc",
                "size": 12,
                "opacity": 0.8,
                "flow": 0.7,
                "blend_mode": "normal",
                "foreground_rgba": [1, 2, 3, 255],
                "erase": False,
            }
        )
        self.assertEqual(line.geometry.type, "line")
        with self.assertRaises(ValueError):
            server.Stroke.model_validate(
                {
                    **line.model_dump(),
                    "geometry": {
                        "type": "cubic",
                        "start": [0, 0],
                        "commands": [
                            {"control1": [1, 1], "control2": [2, 2], "end": [3, 3]}
                        ],
                    },
                    "pressure": {"start": 1, "end": 1},
                    "fixed_pressure": True,
                }
            )


class BridgeClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_retry_reuses_request_id(self):
        with TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "token"
            token_path.write_text("c" * 64 + "\n", encoding="ascii")
            os.chmod(token_path, 0o600)
            bodies = []

            def handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content)
                bodies.append(body)
                self.assertEqual(request.headers["authorization"], "Bearer " + "c" * 64)
                if len(bodies) == 1:
                    raise httpx.ReadTimeout("retry", request=request)
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "request_id": body["request_id"],
                        "document_revision": 8,
                        "result": {"saved": True},
                        "warnings": [],
                    },
                )

            client = server.BridgeClient(
                base_url="http://127.0.0.1:5678",
                token_path=token_path,
                transport=httpx.MockTransport(handler),
            )
            reply = await client.command(
                "save_document",
                {"path": "/tmp/run/final.kra"},
                expected_revision=7,
                document_id="doc",
            )
            await client.aclose()
            self.assertEqual(reply.document_revision, 8)
            self.assertEqual(len(bodies), 2)
            self.assertEqual(bodies[0]["request_id"], bodies[1]["request_id"])

    async def test_typed_bridge_error_becomes_safe_tool_error(self):
        with TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "token"
            token_path.write_text("d" * 64 + "\n", encoding="ascii")
            os.chmod(token_path, 0o600)

            def handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content)
                return httpx.Response(
                    409,
                    json={
                        "ok": False,
                        "request_id": body["request_id"],
                        "error": {
                            "code": "revision_conflict",
                            "message": "expected revision is stale",
                            "details": {"actual": 4},
                            "retryable": True,
                        },
                    },
                )

            client = server.BridgeClient(
                base_url="http://127.0.0.1:5678",
                token_path=token_path,
                transport=httpx.MockTransport(handler),
            )
            with self.assertRaisesRegex(ToolError, "revision_conflict"):
                await client.command("create_layer", {}, expected_revision=3)
            await client.aclose()

    async def test_refuses_to_send_bearer_token_off_loopback(self):
        with TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "token"
            token_path.write_text("e" * 64 + "\n", encoding="ascii")
            os.chmod(token_path, 0o600)
            with self.assertRaises(ValueError):
                server.BridgeClient(
                    base_url="https://example.test",
                    token_path=token_path,
                )


if __name__ == "__main__":
    unittest.main()
