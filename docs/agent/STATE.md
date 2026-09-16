---
title: krita-mcp state
project-id: krita-mcp
role: state
date: 2026-09-16
---

# krita-mcp state

Observed 2026-09-16.

- HEAD `cd53254` — rollback made non-blocking: `_rollback_internal` removes
  the candidate, finishes the transaction, returns; no `refreshProjection`,
  no `waitForDone`, no dirty-flag restore. `_render_brush_probe` keeps its
  flush-then-restore (content-neutral path).
- Contract tests: `test_rollback_must_not_block` pins the fix; suite is
  `33 passed`.
- Pushed to `origin/main` (matches `git ls-remote`); pinned in
  `~/Development/NixOS/nixos-config` (`flake.nix`/`flake.lock`).
- Deployment: not installed until `nh os test` + Krita restart; verify via
  `flatten_layer` in `GET /v4/capabilities`.
- Historical wedge evidence (2026-09-16): projection workers blocked in
  `QReadWriteLock::lockForRead`, no writer — hypothesis-grade; raw dumps must
  accompany any upstream report.
