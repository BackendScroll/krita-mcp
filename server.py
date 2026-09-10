"""Typed async FastMCP wrapper for the Krita MCP protocol-v4 plugin."""

from __future__ import annotations

from pathlib import Path
import os
import re
from typing import Annotated, Literal
from urllib.parse import urlparse
import uuid

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator


PROTOCOL_VERSION = 4
DEFAULT_KRITA_URL = "http://127.0.0.1:5678"
_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _default_token_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        runtime = f"/tmp/runtime-{os.getuid()}"
    return Path(runtime) / "krita-mcp" / "token"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BridgeReply(StrictModel):
    ok: Literal[True]
    request_id: str
    document_revision: int = Field(ge=0)
    result: dict
    warnings: list[str] = Field(default_factory=list)


class BridgeError(StrictModel):
    code: str
    message: str
    details: dict = Field(default_factory=dict)
    retryable: bool = False


Coordinate = tuple[float, float]
Channel = Annotated[int, Field(ge=0, le=255)]


class LineGeometry(StrictModel):
    type: Literal["line"]
    start: Coordinate
    end: Coordinate


class CubicCommand(StrictModel):
    control1: Coordinate
    control2: Coordinate
    end: Coordinate


class CubicGeometry(StrictModel):
    type: Literal["cubic"]
    start: Coordinate
    commands: list[CubicCommand] = Field(min_length=1)


Geometry = Annotated[LineGeometry | CubicGeometry, Field(discriminator="type")]


class LinePressure(StrictModel):
    start: float = Field(ge=0.0, le=1.0)
    end: float = Field(ge=0.0, le=1.0)


class Stroke(StrictModel):
    stroke_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    geometry: Geometry
    preset_id: str = Field(min_length=1)
    size: float = Field(ge=0.1, le=4000.0)
    opacity: float = Field(ge=0.0, le=1.0)
    flow: float = Field(ge=0.0, le=1.0)
    blend_mode: str = Field(min_length=1)
    foreground_rgba: tuple[Channel, Channel, Channel, Channel]
    erase: bool
    pressure: LinePressure | None = None
    fixed_pressure: bool | None = None

    @model_validator(mode="after")
    def validate_geometry_pressure(self):
        if self.geometry.type == "line":
            if self.pressure is None:
                raise ValueError("line strokes require start/end pressure")
            if self.fixed_pressure is not None:
                raise ValueError("fixed_pressure is only valid for cubic strokes")
        else:
            if self.pressure is not None:
                raise ValueError("cubic paintPath strokes do not expose pressure")
            if self.fixed_pressure is not True:
                raise ValueError("cubic strokes require fixed_pressure=true")
        return self


class BridgeClient:
    """One FastMCP-process bridge session with safe retry semantics."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token_path: str | os.PathLike[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("KRITA_URL") or DEFAULT_KRITA_URL).rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS:
            raise ValueError("KRITA_URL must be an HTTP loopback address")
        self.token_path = Path(token_path) if token_path else Path(
            os.environ.get("KRITA_MCP_TOKEN_PATH", str(_default_token_path()))
        )
        self.session_id = str(uuid.uuid4())
        self.revision = 0
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(180.0, connect=5.0),
            transport=transport,
        )

    def _token(self) -> str:
        try:
            stat = self.token_path.stat()
            token = self.token_path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise ToolError(
                "krita_auth_unavailable: start Krita with the v4 plugin so its runtime token exists"
            ) from exc
        if stat.st_mode & 0o077:
            raise ToolError("krita_auth_unsafe: token file permissions must be 0600")
        if not _TOKEN_RE.fullmatch(token):
            raise ToolError("krita_auth_invalid: token file is malformed; restart the plugin")
        return token

    def _headers(self, request_id: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "X-Request-ID": request_id,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _raise_bridge_error(value: object, status_code: int) -> None:
        if isinstance(value, dict) and isinstance(value.get("error"), dict):
            try:
                error = BridgeError.model_validate(value["error"])
            except ValueError:
                pass
            else:
                retry = " (retryable)" if error.retryable else ""
                raise ToolError(f"{error.code}: {error.message}{retry}")
        raise ToolError(f"krita_bridge_error: bridge returned HTTP {status_code}")

    async def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        request_id: str,
        body: dict | None = None,
    ) -> BridgeReply:
        response = None
        for attempt in range(2):
            try:
                response = await self._client.request(
                    method,
                    path,
                    headers=self._headers(request_id),
                    json=body,
                )
                break
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                if attempt == 1:
                    raise ToolError(
                        "krita_unavailable: cannot reach the local v4 bridge; ensure Krita and the plugin are running"
                    ) from exc
        assert response is not None
        try:
            value = response.json()
        except ValueError as exc:
            raise ToolError("krita_invalid_response: bridge returned non-JSON data") from exc
        if response.status_code >= 400 or not isinstance(value, dict) or value.get("ok") is not True:
            self._raise_bridge_error(value, response.status_code)
        try:
            reply = BridgeReply.model_validate(value)
        except ValueError as exc:
            raise ToolError("krita_invalid_response: bridge response failed the v4 schema") from exc
        self.revision = reply.document_revision
        return reply

    async def capabilities(self) -> BridgeReply:
        request_id = str(uuid.uuid4())
        return await self._request(
            "GET", "/v4/capabilities", request_id=request_id
        )

    async def command(
        self,
        action: str,
        params: dict,
        *,
        document_id: str | None = None,
        expected_revision: int | None = None,
    ) -> BridgeReply:
        request_id = str(uuid.uuid4())
        envelope = {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "session_id": self.session_id,
            "action": action,
            "params": params,
        }
        if document_id is not None:
            envelope["document_id"] = document_id
        if expected_revision is not None:
            envelope["expected_revision"] = expected_revision
        return await self._request(
            "POST", "/v4/command", request_id=request_id, body=envelope
        )

    async def aclose(self) -> None:
        await self._client.aclose()


BRIDGE = BridgeClient()
mcp = FastMCP(
    "krita-mcp-v4",
    version="4",
    instructions="Typed native-brush control for a local Krita 6 instance.",
    mask_error_details=True,
    strict_input_validation=True,
)

OUTPUT_SCHEMA = BridgeReply.model_json_schema()
READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
WRITE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
DESTRUCTIVE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}


def _result(reply: BridgeReply, text: str) -> ToolResult:
    return ToolResult(content=text, structured_content=reply.model_dump(mode="json"))


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=READ_ONLY)
async def krita_get_capabilities() -> ToolResult:
    """Get authenticated protocol, document, geometry, and runtime limits."""
    reply = await BRIDGE.capabilities()
    return _result(reply, "Krita v4 capabilities retrieved.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=READ_ONLY)
async def krita_get_state(document_id: str | None = None) -> ToolResult:
    """Get bridge revision, writer state, and optional active document state."""
    reply = await BRIDGE.command("get_state", {}, document_id=document_id)
    return _result(reply, f"Krita state at revision {reply.document_revision}.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=WRITE)
async def krita_create_document(
    width: Annotated[int, Field(ge=1, le=16384)],
    height: Annotated[int, Field(ge=1, le=16384)],
    expected_revision: Annotated[int, Field(ge=0)],
    name: str = "AutoPainter v4",
    background_rgba: tuple[Channel, Channel, Channel, Channel] = (232, 228, 218, 255),
) -> ToolResult:
    """Create an RGBA/U16/300-PPI document in the fixed sRGB-elle profile."""
    reply = await BRIDGE.command(
        "create_document",
        {
            "width": width,
            "height": height,
            "name": name,
            "background_rgba": list(background_rgba),
        },
        expected_revision=expected_revision,
    )
    return _result(reply, f"Created {width}×{height} painterly document.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=WRITE)
async def krita_open_document(
    path: str,
    expected_revision: Annotated[int, Field(ge=0)],
) -> ToolResult:
    """Open a document from a bridge-allowlisted absolute path."""
    reply = await BRIDGE.command(
        "open_document", {"path": path}, expected_revision=expected_revision
    )
    return _result(reply, "Opened Krita document.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=WRITE)
async def krita_save_document(
    document_id: str,
    expected_revision: Annotated[int, Field(ge=0)],
    path: str | None = None,
) -> ToolResult:
    """Perform a real KRA save/saveAs; PNG export is a separate tool."""
    params = {"path": path} if path is not None else {}
    reply = await BRIDGE.command(
        "save_document",
        params,
        document_id=document_id,
        expected_revision=expected_revision,
    )
    return _result(reply, "Saved layered KRA document.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=WRITE)
async def krita_export_document(
    document_id: str,
    path: str,
    expected_revision: Annotated[int, Field(ge=0)],
) -> ToolResult:
    """Export the document to a 16-bit PNG at an allowlisted absolute path."""
    reply = await BRIDGE.command(
        "export_document",
        {"path": path},
        document_id=document_id,
        expected_revision=expected_revision,
    )
    return _result(reply, "Exported document PNG.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=DESTRUCTIVE)
async def krita_close_document(
    document_id: str,
    expected_revision: Annotated[int, Field(ge=0)],
) -> ToolResult:
    """Close a document after the caller has saved its committed state."""
    reply = await BRIDGE.command(
        "close_document",
        {},
        document_id=document_id,
        expected_revision=expected_revision,
    )
    return _result(reply, "Closed Krita document.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=READ_ONLY)
async def krita_list_layers(document_id: str) -> ToolResult:
    """Return the document layer tree using native node UUIDs."""
    reply = await BRIDGE.command("list_layers", {}, document_id=document_id)
    return _result(reply, "Retrieved Krita layer tree.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=WRITE)
async def krita_create_layer(
    document_id: str,
    name: str,
    expected_revision: Annotated[int, Field(ge=0)],
    type: Literal["paintlayer", "grouplayer", "selectionmask"] = "paintlayer",
    parent_id: str | None = None,
    above_id: str | None = None,
    select: bool = True,
) -> ToolResult:
    """Create a paint/group/selection-mask node in the layer tree."""
    reply = await BRIDGE.command(
        "create_layer",
        {
            "name": name,
            "type": type,
            "parent_id": parent_id,
            "above_id": above_id,
            "select": select,
        },
        document_id=document_id,
        expected_revision=expected_revision,
    )
    return _result(reply, f"Created {type} '{name}'.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=WRITE)
async def krita_update_layer(
    document_id: str,
    node_id: str,
    expected_revision: Annotated[int, Field(ge=0)],
    name: str | None = None,
    visible: bool | None = None,
    locked: bool | None = None,
    opacity: Annotated[int | None, Field(ge=0, le=255)] = None,
    blend_mode: str | None = None,
) -> ToolResult:
    """Update explicit metadata on one native node UUID."""
    params = {
        key: value
        for key, value in {
            "node_id": node_id,
            "name": name,
            "visible": visible,
            "locked": locked,
            "opacity": opacity,
            "blend_mode": blend_mode,
        }.items()
        if value is not None
    }
    reply = await BRIDGE.command(
        "update_layer",
        params,
        document_id=document_id,
        expected_revision=expected_revision,
    )
    return _result(reply, "Updated Krita layer.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=DESTRUCTIVE)
async def krita_delete_layer(
    document_id: str,
    node_id: str,
    expected_revision: Annotated[int, Field(ge=0)],
    orphan_cleanup: bool = False,
) -> ToolResult:
    """Delete a layer, optionally cleaning a non-live plugin orphan on resume."""
    reply = await BRIDGE.command(
        "delete_layer",
        {"node_id": node_id, "orphan_cleanup": orphan_cleanup},
        document_id=document_id,
        expected_revision=expected_revision,
    )
    return _result(reply, "Deleted Krita layer.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=WRITE)
async def krita_set_selection_from_mask(
    document_id: str,
    path: str,
    expected_revision: Annotated[int, Field(ge=0)],
    selection_mask_node_id: str | None = None,
) -> ToolResult:
    """Load a full-canvas grayscale PNG into the document selection."""
    reply = await BRIDGE.command(
        "set_selection_from_mask",
        {"path": path, "selection_mask_node_id": selection_mask_node_id},
        document_id=document_id,
        expected_revision=expected_revision,
    )
    return _result(reply, "Applied semantic selection mask.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=WRITE)
async def krita_clear_selection(
    document_id: str,
    expected_revision: Annotated[int, Field(ge=0)],
) -> ToolResult:
    """Clear the current document selection."""
    reply = await BRIDGE.command(
        "clear_selection",
        {},
        document_id=document_id,
        expected_revision=expected_revision,
    )
    return _result(reply, "Cleared Krita selection.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=READ_ONLY)
async def krita_list_brushes(query: str = "") -> ToolResult:
    """List installed presets with stable content-aware resource IDs."""
    reply = await BRIDGE.command("list_brushes", {"query": query})
    return _result(reply, "Retrieved installed Krita brush catalog.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=WRITE)
async def krita_render_brush_probe(
    document_id: str,
    preset_id: str,
    path: str,
    expected_revision: Annotated[int, Field(ge=0)],
    size: Annotated[float, Field(ge=0.1, le=4000.0)] = 64.0,
    max_side: Annotated[int, Field(ge=1, le=4096)] = 1280,
) -> ToolResult:
    """Render a temporary pressure-response probe and capture it as PNG."""
    reply = await BRIDGE.command(
        "render_brush_probe",
        {"preset_id": preset_id, "path": path, "size": size, "max_side": max_side},
        document_id=document_id,
        expected_revision=expected_revision,
    )
    return _result(reply, "Rendered brush calibration probe.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=WRITE)
async def krita_begin_paint_transaction(
    document_id: str,
    target_layer_id: str,
    expected_revision: Annotated[int, Field(ge=0)],
    label: str = "candidate",
) -> ToolResult:
    """Create a plugin-owned ephemeral candidate paint layer."""
    reply = await BRIDGE.command(
        "begin_paint_transaction",
        {"target_layer_id": target_layer_id, "label": label},
        document_id=document_id,
        expected_revision=expected_revision,
    )
    return _result(reply, "Began ephemeral paint transaction.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=WRITE)
async def krita_paint_strokes(
    document_id: str,
    transaction_id: str,
    strokes: Annotated[list[Stroke], Field(min_length=1, max_length=64)],
    expected_revision: Annotated[int, Field(ge=0)],
    trace: bool = False,
    trace_directory: str | None = None,
) -> ToolResult:
    """Paint up to 32 traced or 64 untraced native line/cubic strokes atomically."""
    if trace and len(strokes) > 32:
        raise ToolError("batch_limit: traced batches contain at most 32 strokes")
    if trace and not trace_directory:
        raise ToolError("trace_directory_required: traced strokes require an absolute trace directory")
    reply = await BRIDGE.command(
        "paint_strokes",
        {
            "transaction_id": transaction_id,
            "strokes": [stroke.model_dump(mode="json", exclude_none=True) for stroke in strokes],
            "trace": trace,
            "trace_directory": trace_directory,
        },
        document_id=document_id,
        expected_revision=expected_revision,
    )
    return _result(reply, f"Painted {len(strokes)} native logical strokes.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=DESTRUCTIVE)
async def krita_commit_paint_transaction(
    document_id: str,
    transaction_id: str,
    expected_revision: Annotated[int, Field(ge=0)],
    mode: Literal["merge", "retain"] = "merge",
    name: str | None = None,
) -> ToolResult:
    """Commit a candidate by merging into or retaining it above its role layer."""
    reply = await BRIDGE.command(
        "commit_paint_transaction",
        {"transaction_id": transaction_id, "mode": mode, "name": name},
        document_id=document_id,
        expected_revision=expected_revision,
    )
    return _result(reply, "Committed paint transaction.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=DESTRUCTIVE)
async def krita_rollback_paint_transaction(
    document_id: str,
    transaction_id: str,
    expected_revision: Annotated[int, Field(ge=0)],
) -> ToolResult:
    """Delete the transaction candidate layer and all its uncommitted marks."""
    reply = await BRIDGE.command(
        "rollback_paint_transaction",
        {"transaction_id": transaction_id},
        document_id=document_id,
        expected_revision=expected_revision,
    )
    return _result(reply, "Rolled back paint transaction.")


@mcp.tool(output_schema=OUTPUT_SCHEMA, annotations=WRITE)
async def krita_capture_region(
    document_id: str,
    path: str,
    bbox: tuple[int, int, int, int] | None = None,
    max_side: Annotated[int | None, Field(ge=1, le=4096)] = None,
) -> ToolResult:
    """Capture the color-managed composite projection to an 8-bit sRGB PNG."""
    reply = await BRIDGE.command(
        "capture_region",
        {"path": path, "bbox": list(bbox) if bbox else None, "max_side": max_side},
        document_id=document_id,
    )
    return _result(reply, "Captured color-managed composite region.")


if __name__ == "__main__":
    mcp.run()
