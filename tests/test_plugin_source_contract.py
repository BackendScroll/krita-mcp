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

    def test_logical_stroke_uses_one_native_primitive(self):
        paint_one = next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_paint_one"
        )
        attributes = [
            node.func.attr
            for node in ast.walk(paint_one)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        self.assertEqual(attributes.count("paintLine"), 1)
        self.assertEqual(attributes.count("paintPath"), 1)

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

    def test_save_export_and_projection_are_distinct(self):
        self.assertIn("document.saveAs", self.source)
        self.assertIn("document.save()", self.source)
        self.assertIn("document.exportImage", self.source)
        self.assertIn("document.projection", self.source)
        self.assertIn("Format_RGBA8888", self.source)


if __name__ == "__main__":
    unittest.main()
