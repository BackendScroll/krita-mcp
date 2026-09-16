"""Static acceptance checks for Krita-only code that cannot import headlessly."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


SOURCE_PATH = Path(__file__).parents[1] / "krita-plugin" / "kritamcp" / "__init__.py"


class PluginSourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_only_v4_http_surface_is_registered(self):
        self.assertIn('"/v4/capabilities"', self.source)
        self.assertIn('"/v4/command"', self.source)
        self.assertNotIn('"native_paint_path"', self.source)
        self.assertNotIn('"batch_actions"', self.source)
        self.assertNotIn('"new_canvas"', self.source)

    def _function(self, name):
        return next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        )

    @staticmethod
    def _called_attributes(node):
        return [
            child.func.attr
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
        ]

    def test_logical_stroke_uses_one_native_primitive(self):
        # A stroke must reach Krita as a single native primitive: one delegated
        # line call, or one paintPath — never decomposed into many segments.
        attributes = self._called_attributes(self._function("_paint_one"))
        self.assertEqual(attributes.count("_paint_line"), 1)
        self.assertEqual(attributes.count("paintPath"), 1)
        self.assertEqual(attributes.count("paintLine"), 0)

    def test_paint_line_probes_both_endpoint_signatures(self):
        # paintLine's endpoint type differs between Krita builds, so the helper
        # probes QPointF then falls back to QPoint. The three occurrences are
        # mutually exclusive branches; exactly one runs per stroke.
        paint_line = self._function("_paint_line")
        self.assertEqual(self._called_attributes(paint_line).count("paintLine"), 3)
        self.assertEqual(self._called_attributes(paint_line).count("toPoint"), 2)
        handlers = [n for n in ast.walk(paint_line) if isinstance(n, ast.ExceptHandler)]
        self.assertTrue(
            any(getattr(h.type, "id", None) == "TypeError" for h in handlers),
            "the probe must fall back on TypeError",
        )

    def test_capture_has_an_explicit_pixel_guard(self):
        self.assertIn("MAX_CAPTURE_PIXELS", self.source)
        self.assertIn("capture_too_large", self.source)

    def test_transactions_do_not_snapshot_pixels(self):
        self.assertNotIn("snapshot_paint", self.source)
        self.assertIn("TRANSACTION_PREFIX", self.source)
        self.assertIn("mergeDown", self.source)

    def test_orphan_cleanup_is_explicit_and_refuses_live_transactions(self):
        self.assertIn('params.get("orphan_cleanup") is True', self.source)
        self.assertIn('transaction.get("layer_id") == node_id', self.source)
        self.assertIn('"transaction_active"', self.source)

    def test_trace_bounds_allow_for_textured_scattered_brush_dabs(self):
        self.assertIn("float(size) * 2.0 + 3.0", self.source)

    def test_accept_loop_survives_a_failure_inside_serve_forever(self):
        # An exception escaping serve_forever() ends the accept thread while
        # the listening socket stays bound: the process looks alive and the
        # socket stays in LISTEN, but connections pile up unanswered in the
        # accept queue forever. serve_forever() calls service_actions() and
        # selector.select() outside any try/except, so that escape is real.
        # run() must therefore supervise the loop rather than let it exit.
        run = next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "run"
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "serve_forever"
                for call in ast.walk(node)
            )
        )
        self.assertTrue(
            any(isinstance(node, ast.Try) for node in ast.walk(run)),
            "serve_forever must run inside a try block",
        )
        self.assertTrue(
            any(isinstance(node, (ast.While, ast.For)) for node in ast.walk(run)),
            "a dead accept loop must be rebuilt, not left dead",
        )

    def test_serve_loop_failures_are_not_silent(self):
        # The action-level error log only covers command dispatch. Serve-loop
        # and per-connection failures previously went to stderr, which is
        # invisible inside a running Krita -- that is why the recorded stall
        # left an empty error log.
        self.assertIn('log_unhandled_error(None, "serve_forever")', self.source)
        self.assertIn("def handle_error", self.source)
        self.assertIn("def log_event", self.source)

    def test_every_close_clears_the_dirty_flag_first(self):
        # Document.close() blocks forever on Krita's native "save changes?"
        # dialog for a modified document, and batch mode does not suppress it,
        # so a bare close() on a dirtied document hangs the main thread.
        closes = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "close"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "document"
        ]
        self.assertTrue(closes, "expected at least one document.close() call")
        self.assertEqual(
            self.source.count("document.setModified(False)"),
            len(closes),
            "every document.close() must be preceded by setModified(False)",
        )

    def test_main_thread_drain_is_not_reentrant(self):
        # Krita's main-thread operations pump the Qt event loop, which re-enters
        # the drain timer while a command is still mid-flight. Running a second
        # command from inside the first would interleave two brush operations
        # on one document.
        drain = self._function("process_commands")
        self.assertTrue(
            any(isinstance(node, ast.Try) for node in ast.walk(drain)),
            "the drain must reset its guard in a finally block",
        )
        self.assertIn("_draining", self.source)

    def test_content_neutral_operations_restore_the_dirty_flag(self):
        # A probe adds a layer, paints, captures and removes it; a rollback
        # discards its candidate. Both leave the document content-identical,
        # but Krita still marks it dirty, so a canvas nobody edited prompts
        # "save changes?" on close -- and calibration runs ~144 probes against
        # whatever document is active.
        probe = self._function("_render_brush_probe")
        self.assertIn(
            "modified",
            self._called_attributes(probe),
            "the probe must read the dirty flag before touching the document",
        )
        self.assertIn(
            "setModified",
            self._called_attributes(probe),
            "the probe must restore the dirty flag it found",
        )
        rollback = self._function("_rollback_internal")
        self.assertNotIn(
            "setModified",
            self._called_attributes(rollback),
            "a rollback must NOT restore the dirty flag: the restore shipped"
            " twice and failed live both times (runbook OPEN item), and the"
            " flush+restore dance is where the bridge wedges (see"
            " test_rollback_must_not_block). A rollback is an abort path on a"
            " document that is genuinely dirty afterwards.",
        )
        # The probe keeps its capture-and-restore: it must leave a canvas"
        # nobody edited unmodified.
        self.assertIn(
            "modified",
            self._called_attributes(self._function("_begin_transaction")),
        )
        self.assertIn('transaction["was_modified"] = was_modified', self.source)

    def test_dirty_flag_is_restored_after_the_async_work_is_flushed(self):
        # remove()/refreshProjection() are asynchronous: the scheduler
        # re-marks the document dirty after the call returns, so a
        # setModified() that is not preceded by waitForDone() lands too early
        # and is silently undone. Measured: without the flush the document
        # still read modified=True on the very next command.
        #
        # ROLLBACK IS THE EXCEPTION: it must not block at all -- see
        # test_rollback_must_not_block. The probe keeps the flush+restore.
        calls = self._called_attributes(self._function("_render_brush_probe"))
        self.assertIn("waitForDone", calls, "the probe must flush async work")
        self.assertLess(
            calls.index("waitForDone"),
            calls.index("setModified"),
            "the probe must flush before restoring the dirty flag",
        )

    def test_commit_does_not_clear_the_dirty_flag(self):
        # Committing a transaction is a real edit and must leave the document
        # dirty; only the content-neutral paths restore the flag.
        self.assertNotIn(
            "setModified",
            self._called_attributes(self._function("_commit_transaction")),
        )

    def test_rollback_must_not_block(self):
        # 2026-09-16, proven with py-spy --native and a full eu-stack dump:
        # rollback's refreshProjection() + waitForDone() sat on the Krita main
        # thread inside KisImage::waitForDone while every projection-update
        # worker blocked in QReadWriteLock::lockForRead with no writer -- the
        # modal busy-wait dialog starved the bridge accept loop and the run
        # wedged three times in a row, always at rollback. A rollback is an
        # abort path: it may remove the candidate and return, nothing more.
        rollback = self._function("_rollback_internal")
        self.assertNotIn("refreshProjection", self._called_attributes(rollback))
        self.assertNotIn("waitForDone", self._called_attributes(rollback))

    def test_save_export_and_projection_are_distinct(self):
        self.assertIn("document.saveAs", self.source)
        self.assertIn("document.save()", self.source)
        self.assertIn("document.exportImage", self.source)
        self.assertIn("document.projection", self.source)
        self.assertIn("Format_RGBA8888", self.source)


if __name__ == "__main__":
    unittest.main()


class RefreshProjectionContractTests(unittest.TestCase):
    """refreshProjection blocks in KisImage::waitForDone, which pops a modal
    busy-wait dialog whose nested event loop starves accept().

    Revised 2026-09-16 (night) after a live reproduction: Recv-Q climbed on
    :5678 while Krita's main thread sat idle in do_sys_poll at 0.0% CPU, so
    per-stroke refreshes were removed from the stroke loop entirely. The
    refresh now happens ONCE per capture_region -- roughly 1/42 of the old
    exposure -- which is also the only place correctness demands it, since a
    stale projection makes the critic compare two identical images."""

    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def _function(self, name):
        return next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        )

    @staticmethod
    def _called_attributes(node):
        return [
            child.func.attr
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
        ]

    def test_paint_one_does_not_refresh_the_projection(self):
        self.assertNotIn(
            "refreshProjection",
            self._called_attributes(self._function("_paint_one")),
            "_paint_one sits in the stroke hot loop; a per-stroke "
            "refreshProjection multiplies the modal-wait deadlock exposure "
            "thousands-fold",
        )

    def test_traced_strokes_refresh_before_capture(self):
        """Re-inverted 2026-09-17, same night as the removal above.

        Without the refresh, a meaningful fraction of trace crops came back
        showing no stroke mark at all (std=0.00, perfectly uniform colour --
        measured on run 20260916T220226Z), and the accumulated composite
        diverged from preview.png enough to fail build_stroke_gif's SSIM >=
        0.98 gate on an otherwise fully-painted, 8-phase run.

        The 2026-09-16 removal was correct given what was known then, but
        the accept-loop starvation it was defending against turned out to
        have a different root cause: a MODAL DIALOG (the PNG export options
        dialog, confirmed by the user watching Krita), not this call by
        itself. Two of that dialog's known triggers are closed independently
        -- stop_krita clears ~/.krita-*-autosave.kra
        (autopainter/services.py), and every document now enters batch mode
        at registration (test_documents_enter_batch_mode below) -- so the
        refresh is restored. If Recv-Q climbs again with this in place, that
        is new evidence a third dialog trigger exists; capture it with
        py-spy/eu-stack before removing this a second time.
        """
        func = self._function("_paint_strokes")
        trace_branches = [
            node
            for node in ast.walk(func)
            if isinstance(node, ast.If)
            and "trace_directory" in ast.dump(node.test)
            and "None" in ast.dump(node.test)
        ]
        self.assertTrue(trace_branches, "the trace branch must exist")
        refreshed = any(
            "refreshProjection" in self._called_attributes(node)
            for branch in trace_branches
            for node in ast.walk(branch)
        )
        self.assertTrue(
            refreshed,
            "trace crops must refresh the projection before capture, "
            "inside the trace branch",
        )

    def test_capture_region_refreshes_before_saving(self):
        """A capture must never read a stale projection. With tracing off
        nothing refreshed, so AutoPainter's critic compared two identical
        images: the form_value_planes phase was rejected 60/60 with
        improvement ~0.00003 against a 0.001 gate and contributed no strokes.
        """
        func = self._function("_capture_region")
        called = self._called_attributes(func)
        self.assertIn("refreshProjection", called)
        self.assertIn("waitForDone", called)

    def test_documents_enter_batch_mode(self):
        """exportImage()/saveAs() consult the DOCUMENT's batch flag, not the
        application's. Krita.instance().setBatchmode(True) in setup() runs
        before any document exists, so without this the PNG export options
        dialog blocks the main thread forever -- observed live 2026-09-16 as a
        bridge_stall on export_document that no client timeout could clear.
        """
        self.assertIn("setBatchmode", self._called_attributes(self._function("_set_batchmode")))
        for name in ("_register_document", "_export_document", "_save_document"):
            self.assertIn(
                "_set_batchmode",
                self._called_attributes(self._function(name)),
                f"{name} must put the document in batch mode before it can raise a dialog",
            )

    def test_png_export_specifies_every_property(self):
        """Anything left unset is what Krita opens a modal dialog to ask."""
        source = self.source
        for prop in (
            "alpha", "compression", "indexed", "interlaced",
            "saveSRGBProfile", "forceSRGB", "transparencyFillcolor",
        ):
            self.assertIn(f'setProperty("{prop}"', source, f"PNG export must set {prop}")
