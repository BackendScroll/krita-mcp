# Krita MCP HTTP API

This document describes the HTTP bridge exposed by the Krita plugin. The
plugin listens on `http://localhost:5678` by default and executes requests on
Krita's main thread.

The MCP server in `server.py` wraps the legacy actions as MCP tools. The v3
actions are available through HTTP and are intended for native brush-engine
clients such as AutoPainter.

## Transport

### `GET /health`

Returns a lightweight liveness response:

```json
{
  "status": "ok",
  "plugin": "kritamcp",
  "version": "v3-native"
}
```

### `GET /info`

Returns the configured output directory and the complete action-name list.

### `POST /`

Send one JSON envelope:

```json
{
  "action": "capabilities",
  "params": {}
}
```

Successful actions return a JSON object. An action error returns an object
with an `error` string and HTTP status `500`. Invalid JSON returns `400`; an
unknown GET path returns `404`. Action names are case-sensitive.

Example:

```bash
curl -s http://localhost:5678/health
curl -s http://localhost:5678/ \
  -H 'content-type: application/json' \
  -d '{"action":"capabilities","params":{}}'
```

## Common selectors

The optional `document` field selects a document by its Krita document name.
If omitted, the active document is used. Layer actions accept `node_id`, a
stable Krita node UUID returned by `layer_list`, `layer_create`, and related
actions. If omitted, the active node is used.

## Legacy actions

These actions preserve the original MCP bridge surface:

| Action | Parameters | Result |
| --- | --- | --- |
| `new_canvas` | `width`, `height`, `name`, `background` | New document dimensions and name |
| `set_color` | `color` (`#rrggbb`) | Foreground color |
| `set_brush` | `preset`, `size`, `opacity` | Applied brush settings |
| `stroke` | `points`, `size`, `hardness`, `opacity`, optional `colors`, `taper`, `grain` | Raster stroke summary |
| `fill` | `x`, `y`, `radius`, optional `color` | Filled-circle result |
| `draw_shape` | `shape`, `x`, `y`, `width`, `height`, `fill`, `stroke`, optional `x2`, `y2` | Shape result |
| `get_canvas` | `filename` | PNG path under the configured output directory |
| `undo` / `redo` | none | `{ "status": "ok" }` or an error |
| `clear` | `color` | Cleared canvas color |
| `save` | `path` | Exported file path |
| `get_color_at` | `x`, `y` | RGBA channels and hex color |
| `list_brushes` | `filter`, `limit` | Matching preset names and count |
| `open_file` | `path` | Opened document name and dimensions |

The legacy `stroke` action is a direct pixel renderer. It is not equivalent to
the native brush-engine action below.

## v3 native surface

Call `capabilities` before using v3 actions. The response identifies the
protocol and plugin versions, advertises supported actions, and reports the
runtime limits:

```json
{
  "status": "ok",
  "plugin_version": "v3-native",
  "protocol_version": 3,
  "native_brush_engine": true,
  "per_point_pressure": true,
  "transaction_mode": "pixel_snapshot",
  "transaction_scope": "paint_layer_pixels_only",
  "limits": {
    "max_path_points": 512,
    "max_batch_actions": 128,
    "max_crop_pixels": 16777216
  }
}
```

### `native_paint_path`

Paints consecutive segments with Krita's native brush engine. Parameters:

- `points`: at least two points. Each point is either `[x, y]` or an object
  `{ "x": number, "y": number, "pressure": 0..1 }`.
- `kind`: `paint` or `erase` (default `paint`).
- `preset`, `size`, `opacity`, `flow`, `blending_mode`, `colour`: optional
  temporary brush settings.
- `document`, `node_id`: optional common selectors.

The response includes `points_count`, effective `size` and `opacity`, the
painted `bbox`, and `render_ms`. Each segment is one Krita undo entry.

### `brush_state`

Reads brush state when called with `{}` and applies any supplied fields:
`preset`, `size`, `opacity`, `flow`, `blending_mode`, `eraser`,
`disable_pressure`, `colour`, and `background`. The response returns the
effective preset, size, opacity, flow, blending mode, eraser state, pressure
state, and colors.

### `document_state`

Returns document name/path, dimensions, resolution, color model/depth/profile,
modified state, and the active node ID/name. Use `document` to inspect a
non-active document.

### Layer actions

- `layer_list`: returns the complete recursive layer tree.
- `layer_create`: accepts `name`, `type`, optional `parent_id`, `above_id`,
  and `select`; returns the new node ID.
- `layer_select`: accepts `node_id` or `name`.
- `layer_update`: accepts `name`, `visible`, `locked`, `alpha_locked`,
  `inherit_alpha`, `opacity`, `blend_mode`, `move_x`, and `move_y`.
- `layer_delete`: removes the selected node.
- `layer_duplicate`: clones the selected node and returns its new ID.
- `layer_merge_down`: merges the selected node down and returns the result ID.

Structural changes are not covered by pixel-snapshot rollback.

### Transactions and batches

`begin_transaction` snapshots all existing paint-layer pixels. It accepts an
optional `label` and returns a `transaction_id`. Use that ID with
`commit_transaction` or `rollback_transaction`.

`batch_actions` accepts:

```json
{
  "actions": [
    {"action": "native_paint_path", "params": {"points": [[0, 0], [8, 8]]}},
    {"action": "layer_update", "params": {"node_id": "...", "opacity": 220}}
  ],
  "atomic": true
}
```

Atomic batches are the default. On the first failure they stop and restore the
pixel snapshot. Nested batches are rejected, and atomic batches cannot contain
structural actions (`layer_create`, `layer_delete`, `layer_duplicate`,
`layer_merge_down`, `batch_actions`, or `new_canvas`). Set `atomic` to `false`
when partial progress is intentional.

Transactions are limited to 512 MiB of paint-layer pixel snapshots.

### `capture`

Captures the composite document projection, or a selected node when `node_id`
is supplied. Parameters are `x`, `y`, `w`, `h`, optional `max_side`,
`filename`, and the common `document`/`node_id` selectors. The crop is limited
to 16,777,216 pixels and the output is written as PNG below the configured
allowlisted output root. The response returns the path, final dimensions, and
an integer revision timestamp.

## Compatibility and limits

Clients must negotiate `protocol_version` through `capabilities`; do not infer
v3 support from a plugin name. The current protocol is version 3. The plugin
reports `history_grouping: false`, exposes brush preset names rather than
stable preset IDs, and does not roll back structural layer changes.

