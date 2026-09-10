# Krita MCP protocol v4

## HTTP endpoints

- `GET /health` — generic, unauthenticated liveness.
- `GET /v4/capabilities` — authenticated capability document.
- `POST /v4/command` — authenticated command envelope.

All other routes and every non-v4 client are rejected.

## Envelope

```json
{
  "protocol_version": 4,
  "request_id": "unique-within-session",
  "session_id": "connection-session",
  "document_id": "optional-session-document-uuid",
  "expected_revision": 12,
  "action": "paint_strokes",
  "params": {}
}
```

`expected_revision` is required for writes. Success replies contain `ok`,
`request_id`, `document_revision`, `result`, and `warnings`. Errors are
typed as `code`, `message`, `details`, and `retryable`. A mutating
request ID is cached in its session; a retry returns the original reply and
cannot duplicate a stroke.

## Actions

Read actions:

- `get_capabilities`, `get_state`
- `list_layers`, `list_brushes`
- `capture_region`

Write actions:

- `create_document`, `open_document`, `save_document`,
  `export_document`, `close_document`
- `create_layer`, `update_layer`, `delete_layer`
- `set_selection_from_mask`, `clear_selection`
- `render_brush_probe`
- `begin_paint_transaction`, `paint_strokes`,
  `commit_paint_transaction`, `rollback_paint_transaction`

The typed FastMCP server exposes the corresponding `krita_*` tools with output
schemas and read-only/destructive annotations.

## Document and node identity

Documents receive session-scoped plugin UUIDs; reopened documents receive new
IDs. Nodes use native Krita UUIDs. The bridge revision increases monotonically.
A stale expected revision fails with `revision_conflict`.

V4 document creation is fixed to RGBA/U16, 300 PPI, and
`sRGB-elle-V2-srgbtrc.icc`. The only non-brush pixels created by AutoPainter
are its neutral substrate. `save_document` performs real KRA save/saveAs;
`export_document` exports PNG; `capture_region` reads
`Document.projection()` and converts explicitly to 8-bit sRGB.

## Native strokes

A stroke always includes ID, line or cubic geometry, preset fingerprint, size,
opacity, flow, blend mode, foreground RGBA, and erase state.

- A line is one `Node.paintLine` with start/end pressure.
- A cubic is one `Node.paintPath` with one or more cubic commands and
  `fixed_pressure=true`.
- Cubic pressure plus tilt, rotation, and speed are validation errors.
- Batches contain at most 32 traced or 64 untraced strokes.

Trace mode writes validated PNG projection crops after each logical stroke.
Any partial batch failure rolls back its transaction and removes partial
traces.

## Transactions and recovery

`begin_paint_transaction` creates a plugin-owned candidate layer above one
paint target. Commit merges or retains it and reports the replacement node ID.
Rollback removes it. Resume may explicitly delete a candidate-prefix orphan,
but cannot delete a candidate owned by a live transaction.

## Limits and paths

The plugin limits bodies to 2 MiB, the queue to 64 commands, and captures to
16 megapixels. It binds only to loopback. Reads and writes resolve through
configured asset and trace roots; relative paths, traversal, and symlink
escapes are rejected. Large masks and captures travel as validated PNG paths,
not JSON/base64.
