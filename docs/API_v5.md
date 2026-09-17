# Krita MCP API — v5

This supersedes [`API.md`](./API.md) as the reference for the bridge's
current behavior. **v5 is a real wire-protocol bump**: every envelope must
carry `"protocol_version": 5`, and the HTTP routes moved to `/v5/*`. It is a
clean cutover, not a dual-version bridge — v4 requests are rejected exactly
like any other malformed request, not accepted alongside v5. See
[`STATE_CAPTURE_v5.md`](./STATE_CAPTURE_v5.md#deploy-sequencing) for why
that matters and the staged rollout it requires (the client and plugin
cannot update independently the way earlier additive changes could).

What changed since `API.md` (v4) was written:

- `paint_strokes` results now report **measured pixel coverage** per stroke
  (`painted_pixels`, `coverage`) — see [Native strokes](#native-strokes).
- `flatten_layer` — a write action shipped earlier — was missing from
  `API.md`'s action list entirely; included here.
- Every action's actual request/response shape is documented below.
  `API.md` described the envelope and constraints but not per-action
  payloads.
- `get_state` now folds in the full layer tree and open transactions, and a
  new `get_node_state` action reports per-node pixel occupancy on demand —
  both deadlock-safe, neither touches the projection. Full spec, rationale,
  and what's deliberately still out of scope:
  [`STATE_CAPTURE_v5.md`](./STATE_CAPTURE_v5.md). The `get_state` shape below
  is kept in sync with that doc, not duplicated field-by-field.

## HTTP endpoints

- `GET /health` — unauthenticated liveness. `{"status": "ok"}`.
- `GET /v5/capabilities` — authenticated capability document (see below).
- `POST /v5/command` — authenticated command envelope.

Path validation runs *before* authentication: an unauthenticated request to
`/v5/capabilities` or `/v5/command` gets `401`; any other path — including a
bare `GET /` and the now-retired `/v4/*` — gets `426 protocol_mismatch`, not
`401`.

## Envelope

Request:

```json
{
  "protocol_version": 5,
  "request_id": "unique-within-session",
  "session_id": "connection-session",
  "document_id": "optional-session-document-uuid",
  "expected_revision": 12,
  "action": "paint_strokes",
  "params": {}
}
```

`request_id` and `session_id` must match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`.
`expected_revision` is a required non-negative integer for every write
action; its absence is `400 expected_revision_required`. A mutating request
ID is cached in its session — a client retry with the same `request_id`
returns the original reply and cannot duplicate a stroke.

Success reply:

```json
{
  "ok": true,
  "request_id": "...",
  "document_revision": 13,
  "result": {},
  "warnings": []
}
```

Error reply:

```json
{
  "ok": false,
  "request_id": "...",
  "error": {
    "code": "invalid_brush_settings",
    "message": "...",
    "details": {},
    "retryable": false
  }
}
```

`retryable: true` means the same request is safe to resend as-is (e.g.
`writer_busy`, `bridge_stall`-adjacent timeouts); `false` means the request
itself is wrong and resending it unchanged will fail the same way.

## Actions

Read actions (`READ_ACTIONS`):

- `get_capabilities`, `get_state`, `get_node_state`
- `list_layers`, `list_brushes`
- `capture_region`

Write actions (`WRITE_ACTIONS` — require `expected_revision`):

- `create_document`, `open_document`, `save_document`, `export_document`,
  `close_document`
- `create_layer`, `update_layer`, `delete_layer`, **`flatten_layer`**
- `set_selection_from_mask`, `clear_selection`
- `render_brush_probe`
- `begin_paint_transaction`, `paint_strokes`, `commit_paint_transaction`,
  `rollback_paint_transaction`

An action outside this union is `unknown_action` with the full supported set
in `details.supported_actions`.

## Capabilities (`get_capabilities`)

```json
{
  "protocol_version": 5,
  "plugin_version": "v5",
  "actions": ["capture_region", "clear_selection", "..."],
  "document": {
    "color_model": "RGBA",
    "color_depth": "U16",
    "color_profile": "sRGB-elle-V2-srgbtrc.icc",
    "resolution_ppi": 300.0
  },
  "geometry": ["line", "cubic"],
  "line_pressure": true,
  "cubic_pressure": false,
  "unsupported_sensors": ["rotation", "speed", "tilt"],
  "transactions": "ephemeral_candidate_layer",
  "captures": "png_path",
  "state_capture": {
    "unified_state": "get_state",
    "node_occupancy": "get_node_state",
    "occupancy_source": "pixel_data",
    "stroke_occupancy_fields": ["painted_pixels", "coverage"],
    "node_occupancy_fields": ["marked_pixels", "coverage", "content_hash"]
  },
  "limits": {
    "max_body_bytes": 2097152,
    "max_queue_depth": 64,
    "max_traced_strokes": 32,
    "max_untraced_strokes": 64,
    "max_capture_pixels": 16777216
  },
  "krita_version": "6.0.0",
  "profile_available": true
}
```

`krita_version` and `profile_available` require Krita's main thread and are
only present on the matching `get_capabilities` *command* reply — the static
`GET /v5/capabilities` HTTP response omits them unless the plugin fills them
in from a prior command (see `KritaClient.capabilities()` in the Python
client, which calls `get_capabilities` itself when they're missing).

## Document and node identity

Documents receive session-scoped plugin UUIDs; reopening a document yields a
new ID. Nodes use native Krita UUIDs. `document_revision` increases
monotonically on every accepted write; a stale `expected_revision` fails
`409 revision_conflict`.

`create_document` is fixed to RGBA/U16, 300 PPI,
`sRGB-elle-V2-srgbtrc.icc`, canvas dimensions in `[1, 16384]`. It fills a
"00 Ground" substrate layer to `background_rgba` (default opaque
`[232, 228, 218, 255]`) and verifies the created document's actual color
settings match what was requested — a mismatch is
`document_invariant_failed`, not a silent wrong-profile document.

`_document_state` shape, returned by `create_document`, `open_document`, and
nested under `document` in `get_state`'s reply (`get_state` also carries
`writer_lease_details`, the full `layers` tree, and `transactions` — see
[`STATE_CAPTURE_v5.md`](./STATE_CAPTURE_v5.md#get_state-existing-action-response-reshaped)):

```json
{
  "document_id": "...",
  "name": "AutoPainter v4 — ...",
  "file_path": "",
  "width": 3840,
  "height": 2160,
  "resolution_ppi": 300.0,
  "color_model": "RGBA",
  "color_depth": "U16",
  "color_profile": "sRGB-elle-V2-srgbtrc.icc",
  "modified": true,
  "active_node_id": "...",
  "substrate_node_id": "..."
}
```

(`substrate_node_id` only on `create_document`'s reply.)

`save_document` performs a real `saveAs`/`save` to a `.kra` path (or the
document's existing file name if no `path` is given) and returns
`{document_id, path}`. `export_document` requires a `.png` path, always
specifies every `InfoObject` property explicitly (an unset property raises
Krita's own PNG-options dialog, which blocks the main thread), and returns
`{document_id, path, color_depth}`. `capture_region` reads
`Document.projection()` (or `projection(*bbox)` when a bbox is given) and
converts explicitly to 8-bit sRGB `RGBA8888` before saving — the no-bbox form
returns a null image on this Krita build, so callers needing the full canvas
must still pass an explicit `[0, 0, width, height]`.

`capture_region` — `{bbox? [x, y, w, h], path, max_side?}` →
`{document_id, path, width, height}`. `bbox` must have positive integer
width/height when given; `path` must end `.png`; requested pixel area over
`max_capture_pixels` is `capture_too_large`, checked before the projection
is read. **This action calls `refreshProjection()` + `waitForDone()` once
per call** — the same operation that, done per-stroke, produced the
main-thread deadlock in `docs/agent/auto-painter/evidence/2026-09-17-wedge-backtrace.txt`
(`QDialog::exec` inside Krita's busy-wait dialog, holding the GIL so the
bridge's accept loop starves). Once per capture is a deliberate, much
smaller exposure than once per stroke, not a guarantee it cannot happen —
a client capturing very frequently reintroduces the same risk at a smaller
scale.

## Layers

`create_layer` — `{name, type: paintlayer|grouplayer|selectionmask,
parent_id?, above_id?, select?}` → `{document_id, node}`. `update_layer` —
`{node_id, name?, visible?, locked?, opacity? (0-255), blend_mode?}` →
`{document_id, node}`. `delete_layer` — `{node_id, orphan_cleanup?}` →
`{document_id, deleted_node_id}`; deleting a plugin-owned candidate layer
(name prefixed `__krita_mcp_v4_candidate__`) requires `orphan_cleanup: true`
and fails `transaction_active` if a live transaction still owns it.
`list_layers` returns the full node tree from `document.rootNode()`, each
node shaped:

```json
{
  "id": "...", "name": "...", "type": "paintlayer",
  "visible": true, "locked": false, "opacity": 255,
  "blend_mode": "normal", "children": []
}
```

This tree shape carries no pixel information — a `paintlayer` node listed
here may be empty or fully painted, indistinguishably. `get_node_state`
(see [`STATE_CAPTURE_v5.md`](./STATE_CAPTURE_v5.md#get_node_state-new-action))
answers that question for one node on request, via `Node.pixelData()`
directly, with no projection refresh.

`flatten_layer` — `{node_id}` → `{document_id, node_id, name}`. Collapses a
group into one paint layer via Krita's own `flatten_layer` application
action (no reliable Node-level flatten exists for nested layers in this
build) and restores the group's original name if flattening dropped it.
Refuses a plugin-owned candidate node. This action's absence from the
client's `WRITE_ACTIONS` once meant every call 400'd
`expected_revision_required`, silently disabling AutoPainter's layer-budget
consolidation — see `test_write_action_contract.py` on the client side.

## Selection

`set_selection_from_mask` — `{path, selection_mask_node_id?}`: `path` must be
a grayscale-convertible PNG exactly matching the document's dimensions;
non-matching size is `mask_size_mismatch`. Sets `document.setSelection()`
from the mask and, only if `selection_mask_node_id` names a `selectionmask`
node, also copies the selection onto that node — a bookkeeping side effect,
not something anything reads back (the paint-time selection is always the
live `document.setSelection()` value, re-driven from a mask PNG on every
attempt). `clear_selection` clears the document's current selection if one
exists.

## Native strokes

A stroke is:

```json
{
  "stroke_id": "...",
  "preset_id": "preset:<64-hex>",
  "size": 12.0,
  "opacity": 0.8,
  "flow": 0.9,
  "blend_mode": "normal",
  "foreground_rgba": [10, 20, 30, 255],
  "erase": false,
  "geometry": { "type": "line", "start": [0, 0], "end": [1, 1] },
  "pressure": { "start": 0.5, "end": 0.5 }
}
```

- Line geometry: `{type: "line", start, end}` plus a required `pressure`
  object with `start`/`end` each in `[0, 1]`.
- Cubic geometry: `{type: "cubic", start, commands: [{control1, control2,
  end}, ...]}`; must NOT include `pressure` and must set
  `"fixed_pressure": true` at the stroke level instead — cubic strokes use a
  calibrated fixed-pressure preset, not per-point pressure.
- `size` in `[0.1, 4000.0]`; `opacity`, `flow` in `[0.0, 1.0]`;
  `foreground_rgba` four ints in `[0, 255]`; `blend_mode`/`preset_id`/
  `stroke_id` non-empty strings.
- `tilt`, `rotation`, `speed` on a stroke are `unsupported_sensor` — the
  Krita Node API cannot honour pen-tilt/rotation/speed data.
- Batches: at most `MAX_TRACED_STROKES` (32) strokes when `trace: true`, or
  `MAX_UNTRACED_STROKES` (64) when `trace: false`.

`paint_strokes` params: `{transaction_id, strokes: [...], trace: bool,
trace_directory?}`. Result:

```json
{
  "document_id": "...",
  "transaction_id": "...",
  "strokes": [
    {
      "stroke_id": "...",
      "effective_brush_fingerprint": "preset:<hash>",
      "bbox": [120, 340, 48, 48],
      "render_ms": 3,
      "painted_pixels": 812,
      "coverage": 0.352,
      "trace_path": "/path/to/trace.png"
    }
  ]
}
```

- `bbox` is the geometry-derived paint extent (start/end/control points plus
  a `2×size + 3` padding margin), clamped to the canvas — not a measurement
  of what actually got marked.
- **`painted_pixels` and `coverage` are new**: alpha-channel pixels marked on
  the candidate layer by *this* stroke (before/after delta via
  `Node.pixelData()`), and that count divided by the bbox's pixel area.
  `painted_pixels: -1` / `coverage: -1.0` means the bridge could not measure
  (never send this as `0`, which would misreport an unmeasured stroke as one
  that definitely painted nothing). This is the fix for a real gap: previously
  a stroke clipped entirely by the current selection, or a preset that
  renders no dab at the given size/pressure, was indistinguishable from a
  normal stroke — every stroke reported a bbox regardless of whether
  anything was actually painted underneath it. Reading pixels this way needs
  no `document.refreshProjection()`, which is the operation that can trigger
  Krita's busy-wait "image is busy" dialog and deadlock the bridge's accept
  loop (see `docs/agent/auto-painter/evidence/2026-09-17-wedge-backtrace.txt`
  in the workspace repo) — so this measurement is strictly safer than a
  projection capture, not just more informative.
- `trace_path` is present only when the batch was traced (`trace: true`);
  absent entirely for untraced batches, not `null`.
- A failure partway through a batch rolls back the whole transaction and
  deletes any trace PNGs already written for that batch.

## Brushes

`list_brushes` — `{query?}` → `{brushes: [{preset_id, resource_type: "preset",
name, filename, file_sha256}], count}`, filtered case-insensitively by
`query` against `name` if given, sorted by name then `preset_id`.
`preset_id` is `brush_fingerprint("preset", name, filename, file_hash)` — two
installed presets colliding on that identity is `resource_collision`, not a
silent overwrite.

`render_brush_probe` — `{preset_id, path, size? (default 64.0), max_side?
(default 1280)}` → `{document_id, preset_id, probe_strokes: [...5 stroke
results...], path, width, height}`. Paints 5 strokes at pressures
`0.2, 0.4, 0.6, 0.8, 1.0` on a scratch layer (each stroke result is the same
shape `paint_strokes` returns per stroke, now including `painted_pixels`/
`coverage`), captures the full-canvas projection to `path`, then removes the
scratch layer and restores the document's dirty flag to whatever it was
before the probe ran — but see the known issue below.

## Transactions and recovery

`begin_paint_transaction` — `{target_layer_id, label?}` → `{document_id,
transaction_id, candidate_layer_id}`. Creates a plugin-owned candidate paint
layer (name-prefixed `__krita_mcp_v4_candidate__`) as a sibling above the
target layer; `target_layer_id` must name a `paintlayer`.

`commit_paint_transaction` — `{transaction_id, mode: merge|retain, name?}` →
`{document_id, transaction_id, replacement_node_id, mode}`. `merge` drives
Krita's own `merge_layer` application action (candidate `Node.mergeDown()`
is unreliable for a candidate nested in a semantic group on this build);
`retain` just renames the candidate to `name` (or the transaction's
`label`) and keeps it as its own layer.

`rollback_paint_transaction` — `{transaction_id}` → `{document_id,
transaction_id, rolled_back: true}`. Removal of the candidate node is
deferred to the next Qt event-loop tick (`QTimer.singleShot(0, ...)`) rather
than run synchronously, because `Node.remove()` can block on the image
scheduler right after a heavy `paint_strokes` batch — the handler itself
always returns immediately.

Resuming into an existing document (`open_document`) may explicitly
`delete_layer` a candidate-prefixed orphan with `orphan_cleanup: true`, but
cannot delete one still owned by a live transaction.

## Limits and paths

- Body: 2 MiB (`MAX_BODY_BYTES`). Over that: `413 body_too_large`, checked
  against both the declared `Content-Length` and the actual bytes read.
- Queue: 64 pending commands (`MAX_QUEUE_DEPTH`). Over that: `503
  queue_full`, `retryable: true`.
- Captures: 16 megapixels (`MAX_CAPTURE_PIXELS`), checked against the
  requested bbox (or full canvas) before the projection is even read.
- Binds only to `127.0.0.1`. Reads/writes resolve strictly inside the
  configured asset and trace roots — relative paths, `..` traversal, and
  symlink escapes are rejected by `PathGuard` before touching disk.
- Large payloads (masks, captures, traces) always travel as validated PNG
  file paths on local disk, never inline as JSON/base64.

## Known issue carried into this version

`render_brush_probe`'s document-modified-flag restore does not reliably
work live — a probe can still leave a canvas nobody touched prompting "save
changes?" on close, despite `setModified(False)` being called. Two prior
fixes shipped and both failed under live reproduction; see the workspace's
`docs/AUTOPAINTER/STATE.md` / `docs/agent/auto-painter/State.md` before
attempting a third.
