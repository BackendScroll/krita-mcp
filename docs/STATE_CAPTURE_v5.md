# Krita MCP protocol v5: state capture

This is the spec for protocol v5 — a real wire-protocol bump, not a
documentation-only relabeling. It supersedes the "State" coverage in
[`API_v5.md`](./API_v5.md), which now also documents v5's envelope/paths
directly; that file remains the full action-by-action reference for
everything unrelated to state capture (documents, layers, brushes, strokes,
transactions).

**Status: implemented in the vendored/mirrored plugin source, not yet
deployed.** See [Deploy sequencing](#deploy-sequencing) before touching the
client. `docs/AUTOPAINTER/STATE.md` (or wherever this run's handover notes
live) should say which side of that line is currently true.

## What actually changed on the wire

- `protocol_version` in every envelope must now be `5`. `4` is rejected the
  same as any other wrong value (`protocol_mismatch`) — **this is a clean
  cutover, not a dual-version bridge.** There is no request shape that a v4
  and a v5 bridge both accept.
- HTTP routes moved: `GET /v5/capabilities`, `POST /v5/command`. The old
  `/v4/capabilities` and `/v4/command` paths are no longer special-cased —
  they fall through to the generic "any other path" handling, `426
  protocol_mismatch`, exactly like a bare `GET /` always has.
- `plugin_version` in the capabilities document reads `"v5"`.
- Two new read actions/fields, both covered in full below: `get_state`'s
  reply grew three fields, and there's a new `get_node_state` action.

Everything else — action names, request/response shapes for documents,
layers, brushes, strokes, transactions, the `capture_region` PNG path — is
**unchanged** from v4. This is not a rewrite; only the state-capture surface
and the version number moved.

### What did NOT move, and why

`protocol_v4.py` keeps its filename. `V4RequestHandler`,
`TRANSACTION_PREFIX` (`__krita_mcp_v4_candidate__`), and other internal
Python names keep their `v4` spelling. None of these are observable on the
wire — a client cannot tell the module is still called `protocol_v4.py` any
more than it could tell what the file was called under v4. Renaming them
for cosmetic consistency was considered and rejected: it touches roughly
nine files for zero behavior change, and widens the diff between this pass
and the next person's `git blame` for no reader-facing benefit. If a v6
ever needs a real behavioral rewrite of this module, that is the point to
rename it, not before.

## `get_state` (existing action, response reshaped)

One call now answers "what does the bridge think is true right now" in
full, where v4 required `get_state` for the writer lease plus `list_layers`
for the tree plus manual transaction bookkeeping on the client side.

```json
{
  "bridge_revision": 812,
  "writer_lease": true,
  "writer_lease_details": {
    "session_id": "autopaint-worker-3f2a",
    "seconds_since_touch": 4.201,
    "seconds_remaining": 115.799
  },
  "document": {
    "document_id": "5e5b...",
    "name": "AutoPainter v4",
    "file_path": "/home/.../final.kra",
    "width": 3000,
    "height": 2000,
    "resolution_ppi": 300.0,
    "color_model": "RGBA",
    "color_depth": "U16",
    "color_profile": "sRGB-elle-V2-srgbtrc.icc",
    "modified": true,
    "active_node_id": "f9a1..."
  },
  "layers": {
    "id": "root-uuid", "name": "root", "type": "grouplayer",
    "visible": true, "locked": false, "opacity": 255, "blend_mode": "normal",
    "children": [ "...same shape as list_layers, recursively..." ]
  },
  "transactions": [
    {
      "transaction_id": "b1c2...",
      "document_id": "5e5b...",
      "session_id": "autopaint-worker-3f2a",
      "candidate_layer_id": "cand-uuid",
      "target_layer_id": "role-layer-uuid",
      "label": "form_value_planes/region-04"
    }
  ]
}
```

Field notes:

- `writer_lease_details` is `null` when nobody holds the writer lease.
  `seconds_remaining` hitting 0 means the next writer acquisition will
  succeed even if the previous holder is still technically alive but has
  stopped touching the lease (crashed mid-command, network partition) — this
  is what a client should poll instead of guessing at the 120 s session
  timeout from `SESSION_TIMEOUT_SECONDS` in isolation.
- `layers` is `null` when there is no active/addressed document (same
  condition that makes `document` `null`). Otherwise it is exactly what
  `list_layers`'s `root` field returns — `list_layers` is not removed, it is
  now redundant with `get_state` for a caller that wants both document and
  tree in one round trip.
- `transactions` is scoped to the resolved document (or, with no document
  resolvable, every open transaction bridge-wide — useful for "did a
  crashed client leave anything dangling" without needing a document handle
  first). It never includes pixel content; a transaction's candidate
  layer's actual content is queried separately via `get_node_state`.

Deadlock safety: `_get_state` calls no Krita paint or projection API at
all — it reads node/document metadata and the in-process `ProtocolState`
lock. Enforced by `test_state_capture_actions_never_touch_the_projection`.

## `get_node_state` (new action)

```json
// request params
{ "node_id": "f9a1...", "bbox": [0, 0, 3000, 2000] }   // bbox optional, defaults to the full canvas

// response
{
  "document_id": "5e5b...",
  "node": {
    "id": "f9a1...", "name": "04 form_value_planes", "type": "paintlayer",
    "visible": true, "locked": false, "opacity": 255, "blend_mode": "normal",
    "children": []
  },
  "bbox": [0, 0, 3000, 2000],
  "marked_pixels": 812004,
  "total_pixels": 6000000,
  "coverage": 0.135334,
  "content_hash": "sha256:9f1c...  (64 hex chars)"
}
```

`marked_pixels`/`total_pixels`/`coverage` are `-1`/`total`/`-1.0` and
`content_hash` is `null` when the read could not be measured (an unreadable
node, or a layout `count_marked_pixels` doesn't recognise) — **that is
"unmeasured", not "empty".** Same convention as `paint_strokes`' per-stroke
`painted_pixels`, and for the same reason: a silent `0` would be
indistinguishable from a genuinely empty layer, and a `-1` masquerading as
`0` would make an empty-canvas bug look confirmed when it was actually just
an unread buffer.

### `content_hash`: why occupancy alone isn't enough

`coverage` answers "how much of this region has paint." It cannot answer
"is the paint still the same paint." Two reads with identical coverage can
differ in content — a region repainted a different color at the same
opacity, or a stroke undone and redone identically-shaped but
differently-colored. `content_hash` is `"sha256:" + hexdigest` over the
*exact same* `Node.pixelData()` buffer already read for occupancy — one
read serves both fields, so this costs nothing extra beyond the hash
computation itself (see `content_hash()` in `protocol_v4.py`, and
`_node_snapshot` in the plugin, which is the one place that reads pixels
for `get_node_state` — `_paint_one`'s hot-path per-stroke telemetry still
uses the cheaper `_alpha_coverage`, deliberately, since hashing every
stroke's bbox in a batch of dozens would be overhead nothing reads).

A client comparing two `get_node_state` calls: same `coverage`, same
`content_hash` → nothing changed. Same `coverage`, different `content_hash`
→ something was repainted without changing how much area it covers — worth
a second look even though the occupancy-only signal would have said
"fine."

Restricting `bbox` to a region (rather than the default full canvas) is the
cheap way to answer "did painting land inside the intended selection"
without scanning pixels you don't care about — both `count_marked_pixels`
and the hash are `O(bbox area)`, not `O(document area)`.

This is a `paintlayer`-shaped query: a `grouplayer` node has no paint device
of its own, so `Node.pixelData()` on one raises inside `_read_node_pixels`,
which is caught and reported as unmeasured rather than propagating an
error — querying a group is a caller mistake, not a bridge fault, and the
unmeasured convention already means "don't trust this number" without a
special error path.

Deadlock safety: same as `get_state` — `_get_node_state` never calls
`refreshProjection()` or `waitForDone()`, verified by the same contract
test.

## `capabilities` addition

```json
"state_capture": {
  "unified_state": "get_state",
  "node_occupancy": "get_node_state",
  "occupancy_source": "pixel_data",
  "stroke_occupancy_fields": ["painted_pixels", "coverage"],
  "node_occupancy_fields": ["marked_pixels", "coverage", "content_hash"]
}
```

Two separate field lists because the two occupancy sources are shaped
differently: `paint_strokes` results carry `painted_pixels` (a delta —
pixels marked *by this stroke*, before/after subtraction inside a batch);
`get_node_state` carries `marked_pixels` (an absolute count over the
requested bbox, not a delta) plus `content_hash`, which nothing on the
stroke path computes. A client probing what a given bridge build supports
can branch on this key instead of hardcoding an assumption about protocol
version.

## Intended callers (not wired in this pass)

Both are real, named follow-ups — flagged here so the next person doesn't
have to reconstruct the motivation, but neither is implemented:

- **Orchestrator automation.** `apps/auto-painter/autopainter/orchestrator.py`
  already logs a warning when a stroke batch's `painted_pixels` come back
  mostly zero (see `_paint_candidate` in that file). A natural next step is
  calling `get_node_state` on a region's role layer right after committing
  its transaction, comparing `content_hash` against a baseline taken before
  the phase started, and treating "coverage rose but hash-equal to a known
  empty baseline" as a harder failure than a log line. This needs its own
  design: what threshold fails a batch outright vs. only warns, and whether
  a failure should trigger the existing stall-recovery path or a new one.
- **Resume-checkpoint validation.** `autopaint resume` currently trusts
  that a checkpoint `.kra`'s layer tree matches what the run state expects
  (this is the class of bug behind the `KeyError` seen when
  `TARGET_REGIONS` changed under a stale checkpoint — see the auto-painter
  state notes). `get_node_state` on each expected role layer, right after
  reopening the checkpoint and before resuming painting, could catch a
  region-ID mismatch or an unexpectedly-empty layer before the run burns
  time repainting into the wrong place. Also not wired in this pass.

## What this still does not cover

- **No incremental/delta state.** `bridge_revision` is a single global
  monotonic counter; there is no per-node revision or "what changed since
  revision N" query. A client that wants to know whether a *specific* layer
  changed still has to call `get_node_state` before and after and compare.
  This was considered for this pass and explicitly deferred in favor of
  content hashing — see the brainstorming decision this spec came out of.
- **Still no cheap full-canvas thumbnail.** `capture_region` remains the
  only way to get an actual image, and still costs one
  `refreshProjection()` + `waitForDone()`. `get_node_state` tells you
  *whether* and *whether-it's-still-the-same*, not *what it looks like*.

## Deploy sequencing

v5 being a **clean cutover** (no dual-version support, per the design
decision behind this pass) means the client and plugin must agree on the
protocol version atomically — there is no version an old client and a new
plugin, or vice versa, can both speak. Concretely, this creates an ordering
constraint the usual [4-step plugin deploy flow](../../../CLAUDE.md) doesn't
have on its own:

1. **Plugin side (this pass):** implemented in the vendored copy
   (`.pi/mcp/krita-mcp/`) and mirrored into the clone
   (`~/Development/Tools/krita-mcp`), both test suites green. Not pushed,
   not deployed. The **client** (`apps/auto-painter/autopainter/config.py`,
   `krita.py`) is deliberately untouched and still speaks v4 at `/v4/` — so
   the currently-deployed plugin (still v4) keeps working with it exactly
   as before. Nothing about this step is observable outside the repo.
2. **Your step:** push the clone, bump `krita-mcp` in `nixos-config`'s
   `flake.nix`, `nix flake update krita-mcp`, `nh os test`/`switch`, then a
   full Krita restart (not just closing the document — the Python plugin
   must reload). Confirm live: `curl -H "Authorization: Bearer $(cat
   $XDG_RUNTIME_DIR/krita-mcp/token)" http://127.0.0.1:5678/v5/capabilities`
   answers with `"protocol_version": 5`.
3. **Client flip (after step 2 is confirmed, not before):** update
   `PROTOCOL_VERSION` in `config.py` and the `/v4/` literals in `krita.py`
   to `v5`, in one commit, immediately after confirming step 2 is live.

**The gap between step 2 and step 3 is a real, planned outage** —
`autopaint` (still speaking v4) will fail every command against a plugin
that now only answers v5, for however long it takes to land the step-3
commit after your restart finishes. It should be minutes if done
back-to-back in the same sitting, not hours, and no run should be actively
painting when you restart Krita — anything mid-flight will stall and need
`autopaint resume` once the client is flipped. This is the accepted cost of
a clean cutover instead of a transition period where the bridge speaks
both versions; a dual-version bridge was considered and rejected as more
bridge-side complexity than one short, scheduled outage is worth.
