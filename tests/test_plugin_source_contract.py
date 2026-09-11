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

    def test_save_export_and_projection_are_distinct(self):
        self.assertIn("document.saveAs", self.source)
        self.assertIn("document.save()", self.source)
        self.assertIn("document.exportImage", self.source)
        self.assertIn("document.projection", self.source)
        self.assertIn("Format_RGBA8888", self.source)


if __name__ == "__main__":
    unittest.main()
