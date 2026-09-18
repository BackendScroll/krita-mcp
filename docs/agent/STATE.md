---
title: krita-mcp state
project-id: krita-mcp
role: state
date: 2026-09-18
---

# krita-mcp state

Observed 2026-09-18.

- HEAD `98bf9b7`, clean, pushed — matches `origin/main` (`git status -sb`
  shows no ahead/behind).
- **Protocol bumped to v5** (real wire cutover, not a doc relabel):
  `PROTOCOL_VERSION = 5`, routes moved to `/v5/capabilities` + `/v5/command`;
  `/v4/*` now falls through to the generic unrecognised-path handling
  (`426 protocol_mismatch`), same as any other bad path. `get_state` folds
  in the layer tree, writer-lease details, and open transactions. New
  `get_node_state` action reports per-node pixel occupancy
  (`Node.pixelData()`, never the projection) plus a `content_hash` (sha256
  over the same read). Full spec: `docs/STATE_CAPTURE_v5.md`.
- Tests: `50 passed` (`pytest -q tests`), up from 33 on 2026-09-16.
- **Deployed and confirmed live**: pinned in
  `~/Development/NixOS/nixos-config`'s `flake.lock` at
  `98bf9b7d260e7404f1c5685b420bb911108fc597` (matches this HEAD exactly),
  `nh os switch` run, Krita fully restarted. Verified directly —
  `GET /v5/capabilities` answers `protocol_version: 5` with `get_node_state`
  in the `actions` list, and `apps/auto-painter`'s client (flipped to v5 in
  the same session) reaches it end to end via `autopaint doctor`.
- Earlier rollback fix (`cd53254`, 2026-09-16) still holds:
  `_rollback_internal` removes the candidate and returns — no
  `refreshProjection`, no `waitForDone`, no dirty-flag restore.
  `_render_brush_probe` keeps its flush-then-restore (content-neutral path).
  Pinned by `test_rollback_must_not_block`.
- Historical wedge evidence (2026-09-16): projection workers blocked in
  `QReadWriteLock::lockForRead`, no writer — hypothesis-grade; raw dumps must
  accompany any upstream report. A *different*, confirmed root cause for a
  later recurrence (2026-09-17) is documented in the consuming workspace at
  `docs/agent/auto-painter/evidence/2026-09-17-wedge-backtrace.txt` —
  `refreshProjection` → `KisImage::waitForDone` → Krita's busy-wait dialog,
  starving the accept loop because PyKrita holds the GIL across the call.
