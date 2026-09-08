"""
Krita MCP Bridge - HTTP server for external paint commands in Krita
Allows Claude (or any MCP client) to paint by sending commands to this plugin.
"""

from krita import *
from PyQt6.QtCore import (QTimer, QThread, pyqtSignal, QPointF, QRectF,
                          QByteArray, QUuid, QPoint, Qt)
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QMessageBox
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import os
import time
import uuid

from PyQt6.QtGui import QImage

# Configuration - customize these as needed
SERVER_PORT = 5678
CANVAS_OUTPUT_DIR = os.path.expanduser("~/krita-mcp-output")

# v3 protocol constants. Bump PROTOCOL_VERSION when the contract changes;
# clients must negotiate via the `capabilities` action instead of guessing.
PROTOCOL_VERSION = 3
PLUGIN_VERSION = "v3-native"

MAX_PATH_POINTS = 512
MAX_BATCH_ACTIONS = 128
MAX_CROP_PIXELS = 16 * 1024 * 1024  # 64 MiB raw RGBA; includes a 4096² crop
MAX_TRANSACTION_BYTES = 512 * 1024 * 1024  # snapshot memory guard
STRUCTURAL_ACTIONS = {
    "layer_create", "layer_delete", "layer_duplicate",
    "layer_merge_down", "batch_actions", "new_canvas",
}

# Filesystem writes (save/export/capture) are restricted to these roots.
# Extra roots can be added via KRITA_MCP_ALLOWED_ROOTS (colon-separated).
def _allowed_roots():
    roots = [CANVAS_OUTPUT_DIR]
    extra = os.environ.get("KRITA_MCP_ALLOWED_ROOTS", "")
    roots.extend(os.path.expanduser(p) for p in extra.split(":") if p)
    return [os.path.realpath(r) for r in roots]

ALLOWED_ROOTS = _allowed_roots()


def _path_allowed(filepath):
    """Reject writes outside the allowlisted roots (no traversal, no symlinks)."""
    real = os.path.realpath(os.path.abspath(filepath))
    for root in ALLOWED_ROOTS:
        if real == root or real.startswith(root + os.sep):
            return real
    return None


def _hex_rgb(hx):
    hx = hx.lstrip("#")
    return (int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16))


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))

class CommandQueue:
    """Thread-safe command queue for passing commands from HTTP thread to main thread."""
    def __init__(self):
        self.queue = []
        self.results = {}
        self.lock = threading.Lock()
        self.result_event = threading.Event()

    def push(self, command_id, command):
        with self.lock:
            self.queue.append((command_id, command))

    def pop(self):
        with self.lock:
            if self.queue:
                return self.queue.pop(0)
            return None

    def set_result(self, command_id, result):
        with self.lock:
            self.results[command_id] = result
        self.result_event.set()

    def get_result(self, command_id, timeout=120):
        """Wait for result with timeout.

        The default timeout of 120s is important — canvas export and save
        operations can take a long time on large canvases. The original 30s
        default caused frequent timeouts. The MCP server's send_command()
        timeout must match or exceed this value.
        """
        start = threading.Event()
        for _ in range(int(timeout * 10)):  # Check every 100ms
            with self.lock:
                if command_id in self.results:
                    result = self.results.pop(command_id)
                    return result
            self.result_event.wait(0.1)
            self.result_event.clear()
        return {"error": "Timeout waiting for command execution"}

# Global command queue
command_queue = CommandQueue()
command_counter = 0

class PaintRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for paint commands."""

    def log_message(self, format, *args):
        # Suppress HTTP logging
        pass

    def send_json_response(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        """Handle GET requests - mainly for health check."""
        parsed = urlparse(self.path)

        if parsed.path == '/health':
            self.send_json_response({"status": "ok", "plugin": "kritamcp",
                                    "version": PLUGIN_VERSION})
        elif parsed.path == '/info':
            self.send_json_response({
                "status": "ok",
                "canvas_dir": CANVAS_OUTPUT_DIR,
                "commands": [
                    # legacy (v2) surface — kept for compatibility
                    "new_canvas", "set_color", "set_brush", "stroke",
                    "fill", "draw_shape", "get_canvas", "undo", "redo",
                    "clear", "save", "get_color_at", "list_brushes",
                    "open_file", "close_document",
                    # v3 native surface (see `capabilities` action)
                    "capabilities", "native_paint_path", "brush_state",
                    "document_state", "layer_list", "layer_create",
                    "layer_select", "layer_update", "layer_delete",
                    "layer_duplicate", "layer_merge_down",
                    "begin_transaction", "commit_transaction",
                    "rollback_transaction", "batch_actions", "capture",
                ]
            })
        else:
            self.send_json_response({"error": "Unknown endpoint"}, 404)

    def do_POST(self):
        """Handle POST requests - paint commands."""
        global command_counter

        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')

        try:
            command = json.loads(body)
        except json.JSONDecodeError:
            self.send_json_response({"error": "Invalid JSON"}, 400)
            return

        # Assign command ID and queue it
        command_counter += 1
        command_id = command_counter
        command_queue.push(command_id, command)

        # Wait for result from main thread
        result = command_queue.get_result(command_id)

        if "error" in result:
            self.send_json_response(result, 500)
        else:
            self.send_json_response(result)


class ServerThread(QThread):
    """Thread to run HTTP server without blocking Krita UI."""

    def __init__(self, port):
        super().__init__()
        self.port = port
        self.server = None

    def run(self):
        self.server = HTTPServer(('localhost', self.port), PaintRequestHandler)
        self.server.serve_forever()

    def stop(self):
        if self.server:
            self.server.shutdown()


class KritaMCPExtension(Extension):
    """Main Krita extension class."""

    def __init__(self, parent):
        super().__init__(parent)
        self.server_thread = None
        self.timer = None
        self.current_brush_size = 20
        self.current_opacity = 1.0
        # v3: transaction_id -> {node_id: {x, y, w, h, data}} pixel snapshots
        self.transactions = {}

    def setup(self):
        """Called when extension is loaded."""
        pass

    def createActions(self, window):
        """Called when a new window is created."""
        # Ensure output directory exists
        os.makedirs(CANVAS_OUTPUT_DIR, exist_ok=True)

        # Start HTTP server
        if self.server_thread is None:
            self.server_thread = ServerThread(SERVER_PORT)
            self.server_thread.start()
            print(f"[KritaMCP] HTTP server started on port {SERVER_PORT}")

        # Start timer to process command queue
        if self.timer is None:
            self.timer = QTimer()
            self.timer.timeout.connect(self.process_commands)
            self.timer.start(50)  # Check every 50ms

    def process_commands(self):
        """Process commands from queue in main thread."""
        item = command_queue.pop()
        if item is None:
            return

        command_id, command = item
        result = self.execute_command(command)
        command_queue.set_result(command_id, result)

    def execute_command(self, command):
        """Execute a paint command and return result."""
        try:
            action = command.get("action")
            params = command.get("params", {})

            if action == "new_canvas":
                return self.cmd_new_canvas(params)
            elif action == "set_color":
                return self.cmd_set_color(params)
            elif action == "set_brush":
                return self.cmd_set_brush(params)
            elif action == "stroke":
                return self.cmd_stroke(params)
            elif action == "fill":
                return self.cmd_fill(params)
            elif action == "draw_shape":
                return self.cmd_draw_shape(params)
            elif action == "get_canvas":
                return self.cmd_get_canvas(params)
            elif action == "undo":
                return self.cmd_undo(params)
            elif action == "redo":
                return self.cmd_redo(params)
            elif action == "clear":
                return self.cmd_clear(params)
            elif action == "save":
                return self.cmd_save(params)
            elif action == "get_color_at":
                return self.cmd_get_color_at(params)
            elif action == "list_brushes":
                return self.cmd_list_brushes(params)
            elif action == "open_file":
                return self.cmd_open_file(params)
            elif action == "close_document":
                return self.cmd_close_document(params)
            elif action == "capabilities":
                return self.cmd_capabilities(params)
            elif action == "native_paint_path":
                return self.cmd_native_paint_path(params)
            elif action == "brush_state":
                return self.cmd_brush_state(params)
            elif action == "document_state":
                return self.cmd_document_state(params)
            elif action == "layer_list":
                return self.cmd_layer_list(params)
            elif action == "layer_create":
                return self.cmd_layer_create(params)
            elif action == "layer_select":
                return self.cmd_layer_select(params)
            elif action == "layer_update":
                return self.cmd_layer_update(params)
            elif action == "layer_delete":
                return self.cmd_layer_delete(params)
            elif action == "layer_duplicate":
                return self.cmd_layer_duplicate(params)
            elif action == "layer_merge_down":
                return self.cmd_layer_merge_down(params)
            elif action == "begin_transaction":
                return self.cmd_begin_transaction(params)
            elif action == "commit_transaction":
                return self.cmd_commit_transaction(params)
            elif action == "rollback_transaction":
                return self.cmd_rollback_transaction(params)
            elif action == "batch_actions":
                return self.cmd_batch_actions(params)
            elif action == "capture":
                return self.cmd_capture(params)
            else:
                return {"error": f"Unknown action: {action}"}

        except Exception as e:
            return {"error": str(e)}

    def get_active_document(self):
        """Get active document or return None."""
        app = Krita.instance()
        return app.activeDocument()

    def get_active_view(self):
        """Get active view or return None."""
        app = Krita.instance()
        window = app.activeWindow()
        if window:
            return window.activeView()
        return None

    def get_active_layer(self):
        """Get active paint layer."""
        doc = self.get_active_document()
        if doc:
            return doc.activeNode()
        return None

    def cmd_new_canvas(self, params):
        """Create a new canvas."""
        width = params.get("width", 800)
        height = params.get("height", 600)
        name = params.get("name", "New Canvas")
        bg_color = params.get("background", "#1a1a2e")

        app = Krita.instance()

        # Create document with background color
        doc = app.createDocument(width, height, name, "RGBA", "U8", "", 120.0)

        window = app.activeWindow()
        if window:
            window.addView(doc)

        # Create a paint layer
        root = doc.rootNode()
        layer = doc.createNode("paint", "paintlayer")
        root.addChildNode(layer, None)

        # Fill background using pixel data
        color = QColor(bg_color)
        r, g, b = color.red(), color.green(), color.blue()

        # Create pixel data for entire canvas (BGRA format)
        pixel_data = bytes([b, g, r, 255] * (width * height))
        layer.setPixelData(pixel_data, 0, 0, width, height)

        doc.refreshProjection()

        return {"status": "ok", "width": width, "height": height, "name": name}

    def cmd_set_color(self, params):
        """Set foreground color."""
        color_hex = params.get("color", "#ffffff")

        view = self.get_active_view()
        if not view:
            return {"error": "No active view"}

        color = QColor(color_hex)
        mc = ManagedColor.fromQColor(color, view.canvas())
        view.setForeGroundColor(mc)

        return {"status": "ok", "color": color_hex}

    def cmd_set_brush(self, params):
        """Set brush preset and size."""
        preset_name = params.get("preset", None)
        size = params.get("size", None)
        opacity = params.get("opacity", None)

        view = self.get_active_view()
        if not view:
            return {"error": "No active view"}

        if preset_name:
            # Find brush preset
            presets = Krita.instance().resources("preset")
            found = None
            for name, preset in presets.items():
                if preset_name.lower() in name.lower():
                    found = preset
                    break
            if found:
                view.setCurrentBrushPreset(found)
            else:
                return {"error": f"Brush preset not found: {preset_name}"}

        if size is not None:
            self.current_brush_size = size
            view.setBrushSize(size)

        if opacity is not None:
            self.current_opacity = opacity
            # Opacity is set per-stroke, store for later

        return {"status": "ok", "preset": preset_name, "size": size, "opacity": opacity}

    def cmd_stroke(self, params):
        """Paint a stroke along points using pixel-level drawing with soft edges."""
        points = params.get("points", [])
        brush_size = params.get("size", self.current_brush_size)
        hardness = params.get("hardness", 0.5)  # 0.0 = very soft, 1.0 = hard edge
        opacity = params.get("opacity", 1.0)
        colors_in = params.get("colors") or []
        taper_in = params.get("taper") or []
        grain = max(0.0, min(1.0, float(params.get("grain", 0.0) or 0.0)))

        if len(points) < 2:
            return {"error": "Need at least 2 points for a stroke"}

        layer = self.get_active_layer()
        if not layer:
            return {"error": "No active layer"}

        doc = self.get_active_document()
        view = self.get_active_view()

        if not view:
            return {"error": "No active view"}

        # Get current foreground color
        fg = view.foregroundColor()
        qcolor = fg.colorForCanvas(view.canvas())
        r, g, b = qcolor.red(), qcolor.green(), qcolor.blue()

        def hex_rgb(hx):
            hx = hx.lstrip("#")
            return (int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16))

        point_colors = None
        if len(colors_in) == len(points):
            try:
                point_colors = [hex_rgb(c) for c in colors_in]
            except Exception:
                point_colors = None
        point_tapers = None
        if len(taper_in) == len(points):
            try:
                point_tapers = [max(0.05, float(t)) for t in taper_in]
            except Exception:
                point_tapers = None

        import math
        import random
        rng = random.Random(4242)

        def point_radius(i):
            t = point_tapers[i] if point_tapers else 1.0
            return max(1, int(round(brush_size * t / 2.0)))

        max_radius = max(point_radius(i) for i in range(len(points)))

        width = doc.width()
        height = doc.height()

        # Calculate bounding box for all points plus max brush radius
        min_x = max(0, int(min(p[0] for p in points)) - max_radius - 2)
        min_y = max(0, int(min(p[1] for p in points)) - max_radius - 2)
        max_x = min(width, int(max(p[0] for p in points)) + max_radius + 2)
        max_y = min(height, int(max(p[1] for p in points)) + max_radius + 2)

        w = max_x - min_x
        h = max_y - min_y

        if w <= 0 or h <= 0:
            return {"error": "Stroke out of bounds"}

        # Get existing pixel data for the affected region
        existing = layer.pixelData(min_x, min_y, w, h)
        pixels = bytearray(existing)

        import math

        def falloff(dist, radius):
            if radius <= 0:
                return 0.0
            d = dist / radius
            if hardness >= 1.0:
                return 1.0
            if d < hardness:
                return 1.0
            fall = (d - hardness) / (1.0 - hardness)
            return max(0.0, 1.0 - fall)

        def stamp(cx, cy, radius, col):
            """One soft stamp with optional grain, blending onto the layer."""
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    dist_sq = dx*dx + dy*dy
                    if dist_sq > radius*radius:
                        continue
                    px = int(cx) + dx - min_x
                    py = int(cy) + dy - min_y
                    if not (0 <= px < w and 0 <= py < h):
                        continue
                    af = falloff(math.sqrt(dist_sq), radius)
                    a = int(255 * af * opacity)
                    if a <= 0:
                        continue
                    idx = (py * w + px) * 4
                    er, eg, eb = pixels[idx+2], pixels[idx+1], pixels[idx]
                    cr, cg, cb = col
                    if grain > 0.0:
                        n = 1.0 + rng.uniform(-grain, grain)
                        cr = min(255, max(0, int(cr * n)))
                        cg = min(255, max(0, int(cg * n)))
                        cb = min(255, max(0, int(cb * n)))
                    blend = a / 255.0
                    pixels[idx]   = int(eb * (1 - blend) + cb * blend)
                    pixels[idx+1] = int(eg * (1 - blend) + cg * blend)
                    pixels[idx+2] = int(er * (1 - blend) + cr * blend)
                    pixels[idx+3] = max(pixels[idx+3], a)

        def stamp_segment(i, j):
            x1, y1 = points[i]
            x2, y2 = points[j]
            r1, r2 = point_radius(i), point_radius(j)
            c1 = point_colors[i] if point_colors else (r, g, b)
            c2 = point_colors[j] if point_colors else (r, g, b)
            dist = math.hypot(x2 - x1, y2 - y1)
            steps = max(1, int(dist / max(1, min(r1, r2) / 3.0)))
            for k in range(steps + 1):
                t = k / steps
                cx = x1 + (x2 - x1) * t
                cy = y1 + (y2 - y1) * t
                rad = max(1, int(round(r1 + (r2 - r1) * t)))
                col = (int(c1[0] + (c2[0] - c1[0]) * t),
                       int(c1[1] + (c2[1] - c1[1]) * t),
                       int(c1[2] + (c2[2] - c1[2]) * t))
                stamp(cx, cy, rad, col)

        for i in range(len(points)):
            stamp(points[i][0], points[i][1], point_radius(i),
                  point_colors[i] if point_colors else (r, g, b))
            if i > 0:
                stamp_segment(i - 1, i)

        layer.setPixelData(bytes(pixels), min_x, min_y, w, h)
        doc.refreshProjection()

        return {"status": "ok", "points_count": len(points), "hardness": hardness,
                "colors": point_colors is not None, "taper": point_tapers is not None,
                "grain": grain}

    def cmd_fill(self, params):
        """Fill a circular area with current color."""
        x = params.get("x", 0)
        y = params.get("y", 0)
        radius = params.get("radius", 50)

        layer = self.get_active_layer()
        if not layer:
            return {"error": "No active layer"}

        doc = self.get_active_document()
        view = self.get_active_view()

        if not view:
            return {"error": "No active view"}

        # Get current foreground color
        fg = view.foregroundColor()
        qcolor = fg.colorForCanvas(view.canvas())
        r, g, b = qcolor.red(), qcolor.green(), qcolor.blue()

        # Paint a filled circle using pixel data
        # Create a bounding box
        x1 = max(0, x - radius)
        y1 = max(0, y - radius)
        x2 = min(doc.width(), x + radius)
        y2 = min(doc.height(), y + radius)
        w = x2 - x1
        h = y2 - y1

        if w <= 0 or h <= 0:
            return {"error": "Fill area out of bounds"}

        # Get existing pixel data
        existing = layer.pixelData(x1, y1, w, h)
        pixels = bytearray(existing)

        # Draw circle
        for py in range(h):
            for px in range(w):
                # Check if point is in circle
                dx = (x1 + px) - x
                dy = (y1 + py) - y
                if dx*dx + dy*dy <= radius*radius:
                    idx = (py * w + px) * 4
                    pixels[idx] = b      # B
                    pixels[idx+1] = g    # G
                    pixels[idx+2] = r    # R
                    pixels[idx+3] = 255  # A

        layer.setPixelData(bytes(pixels), x1, y1, w, h)
        doc.refreshProjection()

        return {"status": "ok", "x": x, "y": y, "radius": radius}

    def cmd_draw_shape(self, params):
        """Draw a shape (rectangle, ellipse, line)."""
        shape = params.get("shape", "rectangle")
        x = params.get("x", 0)
        y = params.get("y", 0)
        width = params.get("width", 100)
        height = params.get("height", 100)
        fill = params.get("fill", True)

        layer = self.get_active_layer()
        if not layer:
            return {"error": "No active layer"}

        doc = self.get_active_document()
        view = self.get_active_view()

        if not view:
            return {"error": "No active view"}

        # Get current foreground color
        fg = view.foregroundColor()
        qcolor = fg.colorForCanvas(view.canvas())
        r, g, b = qcolor.red(), qcolor.green(), qcolor.blue()

        if shape == "line":
            # Draw line using pixel data
            x2 = params.get("x2", x + width)
            y2 = params.get("y2", y + height)
            line_width = params.get("line_width", 2)

            # Calculate bounding box
            x1_bound = max(0, int(min(x, x2)) - line_width)
            y1_bound = max(0, int(min(y, y2)) - line_width)
            x2_bound = min(doc.width(), int(max(x, x2)) + line_width)
            y2_bound = min(doc.height(), int(max(y, y2)) + line_width)
            w = x2_bound - x1_bound
            h = y2_bound - y1_bound

            if w > 0 and h > 0:
                existing = layer.pixelData(x1_bound, y1_bound, w, h)
                pixels = bytearray(existing)

                # Draw line with thickness
                dist = max(abs(x2 - x), abs(y2 - y))
                steps = max(1, int(dist))
                radius = max(1, line_width // 2)

                for i in range(steps + 1):
                    t = i / steps if steps > 0 else 0
                    cx = x + t * (x2 - x)
                    cy = y + t * (y2 - y)
                    for dy in range(-radius, radius + 1):
                        for dx in range(-radius, radius + 1):
                            if dx*dx + dy*dy <= radius*radius:
                                px = int(cx) + dx - x1_bound
                                py = int(cy) + dy - y1_bound
                                if 0 <= px < w and 0 <= py < h:
                                    idx = (py * w + px) * 4
                                    pixels[idx] = b
                                    pixels[idx+1] = g
                                    pixels[idx+2] = r
                                    pixels[idx+3] = 255

                layer.setPixelData(bytes(pixels), x1_bound, y1_bound, w, h)
        elif shape == "rectangle" and fill:
            # Draw filled rectangle using pixel data
            x1 = max(0, int(x))
            y1 = max(0, int(y))
            x2 = min(doc.width(), int(x + width))
            y2 = min(doc.height(), int(y + height))
            w = x2 - x1
            h = y2 - y1

            if w > 0 and h > 0:
                pixel_data = bytes([b, g, r, 255] * (w * h))
                layer.setPixelData(pixel_data, x1, y1, w, h)
        elif shape == "ellipse" and fill:
            # Draw filled ellipse using pixel data
            cx = x + width / 2
            cy = y + height / 2
            rx = width / 2
            ry = height / 2

            x1 = max(0, int(x))
            y1 = max(0, int(y))
            x2 = min(doc.width(), int(x + width))
            y2 = min(doc.height(), int(y + height))
            w = x2 - x1
            h = y2 - y1

            if w > 0 and h > 0:
                existing = layer.pixelData(x1, y1, w, h)
                pixels = bytearray(existing)

                for py in range(h):
                    for px in range(w):
                        # Check if point is in ellipse
                        dx = (x1 + px - cx) / rx if rx > 0 else 0
                        dy = (y1 + py - cy) / ry if ry > 0 else 0
                        if dx*dx + dy*dy <= 1:
                            idx = (py * w + px) * 4
                            pixels[idx] = b
                            pixels[idx+1] = g
                            pixels[idx+2] = r
                            pixels[idx+3] = 255

                layer.setPixelData(bytes(pixels), x1, y1, w, h)
        else:
            return {"error": f"Shape '{shape}' with current options not supported"}

        doc.refreshProjection()

        return {"status": "ok", "shape": shape}

    def cmd_get_canvas(self, params):
        """Export current canvas to file and return path."""
        filename = params.get("filename", "canvas.png")

        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}

        # Ensure filename has extension
        if not filename.endswith('.png'):
            filename += '.png'

        filepath = os.path.join(CANVAS_OUTPUT_DIR, filename)

        # Export image (batch mode suppresses export dialog)
        doc.setBatchmode(True)
        doc.exportImage(filepath, InfoObject())
        doc.setBatchmode(False)

        return {"status": "ok", "path": filepath}

    def cmd_undo(self, params):
        """Undo last action."""
        app = Krita.instance()
        action = app.action('edit_undo')
        if action:
            action.trigger()
            return {"status": "ok"}
        return {"error": "Could not trigger undo"}

    def cmd_redo(self, params):
        """Redo last undone action."""
        app = Krita.instance()
        action = app.action('edit_redo')
        if action:
            action.trigger()
            return {"status": "ok"}
        return {"error": "Could not trigger redo"}

    def cmd_clear(self, params):
        """Clear the canvas."""
        layer = self.get_active_layer()
        if not layer:
            return {"error": "No active layer"}

        doc = self.get_active_document()

        # Get canvas dimensions
        width = doc.width()
        height = doc.height()

        # Clear by filling with background color
        bg_color = params.get("color", "#1a1a2e")
        color = QColor(bg_color)
        r, g, b = color.red(), color.green(), color.blue()

        # Fill entire layer with color
        pixel_data = bytes([b, g, r, 255] * (width * height))
        layer.setPixelData(pixel_data, 0, 0, width, height)

        doc.refreshProjection()

        return {"status": "ok", "color": bg_color}

    def cmd_save(self, params):
        """Save to specific path."""
        filepath = params.get("path")
        if not filepath:
            return {"error": "No path specified"}

        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}

        # Batch mode suppresses export dialog
        doc.setBatchmode(True)
        doc.exportImage(filepath, InfoObject())
        doc.setBatchmode(False)

        return {"status": "ok", "path": filepath}

    def cmd_get_color_at(self, params):
        """Get color at specific pixel (eyedropper)."""
        x = params.get("x", 0)
        y = params.get("y", 0)

        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}

        # Get projection pixel data at point
        layer = doc.rootNode()
        pixel_data = layer.projectionPixelData(x, y, 1, 1)

        if len(pixel_data) >= 4:
            # RGBA
            b, g, r, a = pixel_data[0], pixel_data[1], pixel_data[2], pixel_data[3]
            hex_color = "#{:02x}{:02x}{:02x}".format(r, g, b)
            return {"status": "ok", "color": hex_color, "r": r, "g": g, "b": b, "a": a}

        return {"error": "Could not read pixel"}

    def cmd_list_brushes(self, params):
        """List available brush presets."""
        filter_str = params.get("filter", "")
        limit = params.get("limit", 50)

        presets = Krita.instance().resources("preset")
        brush_list = []

        for name, preset in presets.items():
            if filter_str.lower() in name.lower():
                brush_list.append(name)
                if len(brush_list) >= limit:
                    break

        return {"status": "ok", "brushes": brush_list, "count": len(brush_list)}

    def cmd_close_document(self, params):
        """Close a document (default: active). Returns to the previous doc."""
        doc, err = self._resolve_document(params)
        if err:
            return err
        if not doc:
            return {"error": "No active document"}
        name = doc.name()
        if not doc.close():
            return {"error": f"Could not close document: {name}"}
        return {"status": "ok", "closed": name}

    def cmd_open_file(self, params):
        """Open an existing file in Krita."""
        filepath = params.get("path")
        if not filepath:
            return {"error": "No path specified"}

        if not os.path.exists(filepath):
            return {"error": f"File not found: {filepath}"}

        app = Krita.instance()

        # Open the document
        doc = app.openDocument(filepath)
        if not doc:
            return {"error": f"Failed to open: {filepath}"}

        # Add view to active window
        window = app.activeWindow()
        if window:
            window.addView(doc)

        return {"status": "ok", "path": filepath, "name": doc.name(), "width": doc.width(), "height": doc.height()}

    # ------------------------------------------------------------------ v3
    # Native surface. See docs/AUTOPAINTER/KRITA_MCP_SERVER_REQUIREMENTS.md.
    # Capabilities must be queried before using these actions; never assume.

    def cmd_capabilities(self, params):
        return {
            "status": "ok",
            "plugin": "kritamcp",
            "plugin_version": PLUGIN_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "krita_version": Krita.instance().version(),
            "actions": [
                "native_paint_path", "brush_state", "document_state",
                "layer_list", "layer_create", "layer_select",
                "layer_update", "layer_delete", "layer_duplicate",
                "layer_merge_down", "begin_transaction",
                "commit_transaction", "rollback_transaction",
                "batch_actions", "capture", "save", "get_color_at",
                "list_brushes", "open_file", "new_canvas",
                "close_document",
            ],
            "native_brush_engine": True,
            "per_point_pressure": True,
            "reserved_fields": ["tilt", "rotation", "speed"],
            "history_grouping": False,
            "stable_resource_ids": False,  # scripting API exposes names only
            "stable_node_ids": True,       # QUuid via nodeByUniqueID
            "transaction_mode": "pixel_snapshot",
            "transaction_scope": "paint_layer_pixels_only",
            "transaction_notes": (
                "Rollback restores pixel data of existing paint layers. "
                "Structural changes (layer create/delete/merge) are NOT "
                "rolled back; animated documents are unsupported."),
            "allowlisted_roots": ALLOWED_ROOTS,
            "limits": {
                "max_path_points": MAX_PATH_POINTS,
                "max_batch_actions": MAX_BATCH_ACTIONS,
                "max_crop_pixels": MAX_CROP_PIXELS,
            },
        }

    def _resolve_document(self, params):
        """Return (doc, error). Honours params['document'] (document name)."""
        doc_name = params.get("document")
        if not doc_name:
            return self.get_active_document(), None
        for d in Krita.instance().documents():
            if d.name() == doc_name:
                return d, None
        return None, {"error": f"Document not found: {doc_name}"}

    def _resolve_node(self, doc, node_id=None):
        """Return (node, error). Honours params['node_id'] (node QUuid)."""
        if not node_id:
            return doc.activeNode(), None
        node = doc.nodeByUniqueID(QUuid(node_id))
        if node is None:
            return None, {"error": f"Node not found: {node_id}"}
        return node, None

    def _apply_preset(self, view, preset_name):
        found = None
        for name, preset in Krita.instance().resources("preset").items():
            if name == preset_name:
                found = preset
                break
        if found is None:
            for name, preset in Krita.instance().resources("preset").items():
                if preset_name.lower() in name.lower():
                    found = preset
                    break
        if not found:
            return False
        view.setCurrentBrushPreset(found)
        return True

    # None = probe on first paint; True/False = Krita build's paintLine takes
    # QPoint (5.2-style bindings, e.g. Krita 6 PyQt6) or QPointF (master).
    _paint_segment_uses_qpoint = None

    def _paint_segment(self, layer, x1, y1, p1, x2, y2, p2, stroke_style):
        """One paintLine segment, tolerant to both endpoint-type signatures.

        The probe raises before painting anything, so no stroke is
        partially drawn by a failed attempt.
        """
        if self._paint_segment_uses_qpoint is None:
            try:
                layer.paintLine(QPointF(x1, y1), QPointF(x2, y2), p1, p2,
                                stroke_style)
                self._paint_segment_uses_qpoint = False
                return
            except TypeError:
                self._paint_segment_uses_qpoint = True
        if self._paint_segment_uses_qpoint:
            layer.paintLine(QPoint(int(round(x1)), int(round(y1))),
                            QPoint(int(round(x2)), int(round(y2))),
                            p1, p2, stroke_style)
        else:
            layer.paintLine(QPointF(x1, y1), QPointF(x2, y2), p1, p2,
                            stroke_style)

    def cmd_native_paint_path(self, params):
        """Pressure-aware path painted by Krita's native brush engine.

        Uses Node.paintLine() per consecutive point pair. The current brush
        preset, size, opacity, flow and colours are taken from canvas view
        resources (set here from the request). Each segment is one undo
        entry; grouping is not exposed by the scripting API.
        """
        view = self.get_active_view()
        if not view:
            return {"error": "No active view"}
        doc, err = self._resolve_document(params)
        if err:
            return err
        layer, err = self._resolve_node(doc, params.get("node_id"))
        if err:
            return err
        if layer is None:
            return {"error": "No active layer"}

        points = params.get("points", [])
        if len(points) < 2:
            return {"error": "Need at least 2 points for a path"}
        if len(points) > MAX_PATH_POINTS:
            return {"error": f"Too many path points (max {MAX_PATH_POINTS})"}

        norm_points = []
        for p in points:
            if isinstance(p, dict):
                px, py = float(p.get("x", 0)), float(p.get("y", 0))
                pr = _clamp(float(p.get("pressure", 1.0)), 0.0, 1.0)
            else:
                px, py = float(p[0]), float(p[1])
                pr = 1.0
            norm_points.append((px, py, pr))

        kind = params.get("kind", "paint")
        if kind not in ("paint", "erase"):
            return {"error": f"Unknown path kind: {kind}"}

        prev_eraser = view.eraserMode()
        prev_pressure_disabled = view.disablePressure()
        try:
            preset_name = params.get("preset")
            if preset_name and not self._apply_preset(view, preset_name):
                return {"error": f"Brush preset not found: {preset_name}"}
            if params.get("size") is not None:
                view.setBrushSize(_clamp(float(params["size"]), 1.0, 4000.0))
            if params.get("opacity") is not None:
                view.setPaintingOpacity(_clamp(float(params["opacity"]), 0.0, 1.0))
            if params.get("flow") is not None:
                view.setPaintingFlow(_clamp(float(params["flow"]), 0.0, 1.0))
            if params.get("blending_mode"):
                view.setCurrentBlendingMode(str(params["blending_mode"]))
            if params.get("colour"):
                color = QColor(str(params["colour"]))
                view.setForeGroundColor(
                    ManagedColor.fromQColor(color, view.canvas()))
            # Per-point pressures must reach the brush engine.
            view.setDisablePressure(False)
            view.setEraserMode(kind == "erase")

            stroke_style = "ForegroundColor" if kind == "paint" else "None"
            start = time.time()
            for i in range(1, len(norm_points)):
                x1, y1, p1 = norm_points[i - 1]
                x2, y2, p2 = norm_points[i]
                self._paint_segment(layer, x1, y1, p1, x2, y2, p2,
                                    stroke_style)
            doc.refreshProjection()
            elapsed_ms = int((time.time() - start) * 1000)
        finally:
            view.setEraserMode(prev_eraser)
            view.setDisablePressure(prev_pressure_disabled)

        size = view.brushSize()
        pad = size / 2.0 + 2.0
        xs = [p[0] for p in norm_points]
        ys = [p[1] for p in norm_points]
        bbox = [
            max(0, int(min(xs) - pad)),
            max(0, int(min(ys) - pad)),
            min(doc.width(), int(max(xs) + pad) + 1) - max(0, int(min(xs) - pad)),
            min(doc.height(), int(max(ys) + pad) + 1) - max(0, int(min(ys) - pad)),
        ]
        return {
            "status": "ok",
            "kind": kind,
            "points_count": len(norm_points),
            "size": size,
            "opacity": view.paintingOpacity(),
            "flow": view.paintingFlow(),
            "bbox": bbox,
            "render_ms": elapsed_ms,
        }

    def cmd_brush_state(self, params):
        """Read (and optionally write) brush/tool state. Empty params = read."""
        view = self.get_active_view()
        if not view:
            return {"error": "No active view"}

        if params.get("preset"):
            if not self._apply_preset(view, params["preset"]):
                return {"error": f"Brush preset not found: {params['preset']}"}
        if params.get("size") is not None:
            view.setBrushSize(_clamp(float(params["size"]), 1.0, 4000.0))
        if params.get("opacity") is not None:
            view.setPaintingOpacity(_clamp(float(params["opacity"]), 0.0, 1.0))
        if params.get("flow") is not None:
            view.setPaintingFlow(_clamp(float(params["flow"]), 0.0, 1.0))
        if params.get("blending_mode"):
            view.setCurrentBlendingMode(str(params["blending_mode"]))
        if params.get("eraser") is not None:
            view.setEraserMode(bool(params["eraser"]))
        if params.get("disable_pressure") is not None:
            view.setDisablePressure(bool(params["disable_pressure"]))
        if params.get("colour"):
            view.setForeGroundColor(ManagedColor.fromQColor(
                QColor(str(params["colour"])), view.canvas()))
        if params.get("background"):
            view.setBackGroundColor(ManagedColor.fromQColor(
                QColor(str(params["background"])), view.canvas()))

        preset = view.currentBrushPreset()
        fg = view.foregroundColor().colorForCanvas(view.canvas())
        bg = view.backgroundColor().colorForCanvas(view.canvas())
        return {
            "status": "ok",
            "preset": preset.name() if preset else None,
            "preset_id": preset.name() if preset else None,  # names only today
            "size": view.brushSize(),
            "opacity": view.paintingOpacity(),
            "flow": view.paintingFlow(),
            "blending_mode": view.currentBlendingMode(),
            "eraser": view.eraserMode(),
            "disable_pressure": view.disablePressure(),
            "colour": "#{:02x}{:02x}{:02x}".format(fg.red(), fg.green(), fg.blue()),
            "background": "#{:02x}{:02x}{:02x}".format(bg.red(), bg.green(), bg.blue()),
        }

    def cmd_document_state(self, params):
        doc, err = self._resolve_document(params)
        if err:
            return err
        if not doc:
            return {"error": "No active document"}
        active = doc.activeNode()
        return {
            "status": "ok",
            "document_id": doc.name(),  # scripting API exposes names only
            "name": doc.name(),
            "file_path": doc.fileName(),
            "width": doc.width(),
            "height": doc.height(),
            "resolution": doc.resolution(),
            "color_model": doc.colorModel(),
            "color_depth": doc.colorDepth(),
            "color_profile": doc.colorProfile(),
            "active_node_id": active.uniqueId().toString() if active else None,
            "active_node_name": active.name() if active else None,
            "modified": doc.modified(),
        }

    def _node_entry(self, node):
        entry = {
            "id": node.uniqueId().toString(),
            "name": node.name(),
            "type": node.type(),
            "visible": node.visible(),
            "opacity": node.opacity(),
            "blend_mode": node.blendingMode(),
            "locked": node.locked(),
            "alpha_locked": node.alphaLocked(),
            "inherit_alpha": node.inheritAlpha(),
            "children": [],
        }
        for child in node.childNodes():
            entry["children"].append(self._node_entry(child))
        return entry

    def cmd_layer_list(self, params):
        doc, err = self._resolve_document(params)
        if err:
            return err
        if not doc:
            return {"error": "No active document"}
        return {"status": "ok", "root": self._node_entry(doc.rootNode())}

    def cmd_layer_create(self, params):
        doc, err = self._resolve_document(params)
        if err:
            return err
        if not doc:
            return {"error": "No active document"}
        name = params.get("name", "layer")
        node_type = params.get("type", "paintlayer")
        parent, err = self._resolve_node(doc, params.get("parent_id"))
        if err:
            return err
        node = doc.createNode(name, node_type)
        if node is None:
            return {"error": f"Could not create node of type {node_type}"}
        if params.get("above_id"):
            above, aerr = self._resolve_node(doc, params["above_id"])
            if aerr:
                return aerr
            parent.addChildNode(node, above)
        else:
            parent.addChildNode(node, None)
        if params.get("select", True):
            doc.setActiveNode(node)
        doc.refreshProjection()
        return {"status": "ok", "id": node.uniqueId().toString(),
                "name": node.name(), "type": node.type()}

    def cmd_layer_select(self, params):
        doc, err = self._resolve_document(params)
        if err:
            return err
        if not doc:
            return {"error": "No active document"}
        node, err = self._resolve_node(doc, params.get("node_id"))
        if err:
            return err
        if node is None:
            node = doc.nodeByName(params.get("name", ""))
            if node is None:
                return {"error": "Node not found"}
        doc.setActiveNode(node)
        return {"status": "ok", "id": node.uniqueId().toString(),
                "name": node.name()}

    def cmd_layer_update(self, params):
        doc, err = self._resolve_document(params)
        if err:
            return err
        if not doc:
            return {"error": "No active document"}
        node, err = self._resolve_node(doc, params.get("node_id"))
        if err:
            return err
        if node is None:
            return {"error": "Node not found"}
        if params.get("name"):
            node.setName(str(params["name"]))
        if params.get("visible") is not None:
            node.setVisible(bool(params["visible"]))
        if params.get("locked") is not None:
            node.setLocked(bool(params["locked"]))
        if params.get("alpha_locked") is not None:
            node.setAlphaLocked(bool(params["alpha_locked"]))
        if params.get("inherit_alpha") is not None:
            node.setInheritAlpha(bool(params["inherit_alpha"]))
        if params.get("opacity") is not None:
            node.setOpacity(_clamp(int(params["opacity"]), 0, 255))
        if params.get("blend_mode"):
            node.setBlendingMode(str(params["blend_mode"]))
        if params.get("move_x") or params.get("move_y"):
            node.move(int(params.get("move_x", 0)), int(params.get("move_y", 0)))
        doc.refreshProjection()
        return {"status": "ok", "id": node.uniqueId().toString()}

    def cmd_layer_delete(self, params):
        doc, err = self._resolve_document(params)
        if err:
            return err
        if not doc:
            return {"error": "No active document"}
        node, err = self._resolve_node(doc, params.get("node_id"))
        if err:
            return err
        if node is None:
            return {"error": "Node not found"}
        ok = node.remove()
        doc.refreshProjection()
        return {"status": "ok"} if ok else {"error": "remove() failed"}

    def cmd_layer_duplicate(self, params):
        doc, err = self._resolve_document(params)
        if err:
            return err
        if not doc:
            return {"error": "No active document"}
        node, err = self._resolve_node(doc, params.get("node_id"))
        if err:
            return err
        if node is None:
            return {"error": "Node not found"}
        clone = node.clone()
        parent = node.parentNode()
        if not parent.addChildNode(clone, node):
            return {"error": "addChildNode failed"}
        doc.refreshProjection()
        return {"status": "ok", "id": clone.uniqueId().toString(),
                "name": clone.name()}

    def cmd_layer_merge_down(self, params):
        doc, err = self._resolve_document(params)
        if err:
            return err
        if not doc:
            return {"error": "No active document"}
        node, err = self._resolve_node(doc, params.get("node_id"))
        if err:
            return err
        if node is None:
            return {"error": "Node not found"}
        merged = node.mergeDown()
        if merged is None:
            return {"error": "mergeDown failed"}
        doc.refreshProjection()
        return {"status": "ok", "id": merged.uniqueId().toString(),
                "name": merged.name()}

    def _snapshot_paint_layers(self, doc, node):
        snapshot = {}
        w, h = doc.width(), doc.height()
        for child in node.childNodes():
            snapshot.update(self._snapshot_paint_layers(doc, child))
        if node.type() in ("paintlayer",) and node.hasExtents():
            snapshot[node.uniqueId().toString()] = {
                "x": 0, "y": 0, "w": w, "h": h,
                "data": bytes(node.pixelData(0, 0, w, h)),
            }
        return snapshot

    def cmd_begin_transaction(self, params):
        doc, err = self._resolve_document(params)
        if err:
            return err
        if not doc:
            return {"error": "No active document"}
        tid = str(uuid.uuid4())
        snapshot = self._snapshot_paint_layers(doc, doc.rootNode())
        total_bytes = sum(s["w"] * s["h"] * 4 for s in snapshot.values())
        if total_bytes > MAX_TRANSACTION_BYTES:
            return {
                "error": (
                    f"Transaction snapshot would need ~{total_bytes // (1024 * 1024)} MB "
                    f"(limit {MAX_TRANSACTION_BYTES // (1024 * 1024)} MB); "
                    "reduce canvas size or paint layers"),
            }
        self.transactions[tid] = {
            "document": doc.name(),
            "label": params.get("label", ""),
            "snapshot": snapshot,
            "created": time.time(),
        }
        return {"status": "ok", "transaction_id": tid,
                "nodes": len(self.transactions[tid]["snapshot"])}

    def cmd_commit_transaction(self, params):
        tid = params.get("transaction_id")
        if tid not in self.transactions:
            return {"error": f"Unknown transaction: {tid}"}
        nodes = len(self.transactions.pop(tid)["snapshot"])
        return {"status": "ok", "transaction_id": tid, "released_nodes": nodes}

    def cmd_rollback_transaction(self, params):
        tid = params.get("transaction_id")
        if tid not in self.transactions:
            return {"error": f"Unknown transaction: {tid}"}
        txn = self.transactions.pop(tid)
        doc, err = self._resolve_document({"document": txn["document"]})
        if err or not doc:
            return {"error": "Document for transaction is gone"}
        restored = 0
        for node_id, snap in txn["snapshot"].items():
            node = doc.nodeByUniqueID(QUuid(node_id))
            if node is None:
                continue
            node.setPixelData(QByteArray(snap["data"]), snap["x"], snap["y"],
                              snap["w"], snap["h"])
            restored += 1
        doc.refreshProjection()
        return {"status": "ok", "transaction_id": tid, "restored_nodes": restored}

    def cmd_batch_actions(self, params):
        """Execute a list of {action, params} in order, with optional atomicity.

        atomic=true wraps the batch in a pixel-snapshot transaction and rolls
        back if any action errors, so a batch is accepted or rejected as one
        candidate — the AutoPainter contract.
        """
        actions = params.get("actions", [])
        if not actions:
            return {"error": "No actions supplied"}
        if len(actions) > MAX_BATCH_ACTIONS:
            return {"error": f"Too many actions (max {MAX_BATCH_ACTIONS})"}

        atomic = bool(params.get("atomic", True))
        if atomic:
            structural = [a.get("action") for a in actions
                          if a.get("action") in STRUCTURAL_ACTIONS]
            if structural:
                return {
                    "error": (
                        "Structural actions cannot be part of an atomic "
                        "batch (pixel-snapshot rollback covers paint-layer "
                        "pixels only): " + ", ".join(structural)),
                }
        tid = None
        if atomic:
            t = self.cmd_begin_transaction({"label": "batch_actions"})
            if "error" in t:
                return t
            tid = t["transaction_id"]

        results = []
        failed = False
        start = time.time()
        for entry in actions:
            sub_action = entry.get("action")
            sub_params = entry.get("params", {})
            if sub_action == "batch_actions":
                result = {"error": "Nested batch_actions are not allowed"}
            else:
                result = self.execute_command(
                    {"action": sub_action, "params": sub_params})
            results.append(result)
            if "error" in result and not failed:
                failed = True
                if atomic:
                    break

        rolled_back = False
        if failed and atomic:
            rb = self.cmd_rollback_transaction({"transaction_id": tid})
            rolled_back = "error" not in rb
        elif atomic:
            self.cmd_commit_transaction({"transaction_id": tid})

        return {
            "status": "error" if failed else "ok",
            "results": results,
            "executed": len(results),
            "rolled_back": rolled_back,
            "elapsed_ms": int((time.time() - start) * 1000),
        }

    def cmd_capture(self, params):
        """Capture a crop of the composite projection (or a node) as PNG.

        Returns the file path under an allowlisted root. Optional max_side
        downsamples with smooth scaling. Bounded by MAX_CROP_PIXELS.
        """
        doc, err = self._resolve_document(params)
        if err:
            return err
        if not doc:
            return {"error": "No active document"}

        x = int(params.get("x", 0))
        y = int(params.get("y", 0))
        w = int(params.get("w", doc.width()))
        h = int(params.get("h", doc.height()))
        if w * h > MAX_CROP_PIXELS:
            return {"error": "Crop exceeds max_crop_pixels"}

        node_id = params.get("node_id")
        if node_id:
            node, nerr = self._resolve_node(doc, node_id)
            if nerr:
                return nerr
            data = node.pixelData(x, y, w, h)
        else:
            data = doc.pixelData(x, y, w, h)

        image = QImage(bytes(data), w, h, w * 4, QImage.Format.Format_ARGB32).copy()
        # .copy() detaches from the Python buffer so save/scale can't
        # outlive the pixelData byte object
        max_side = params.get("max_side")
        if max_side:
            image = image.scaled(int(max_side), int(max_side),
                                 aspectRatioMode=Qt.AspectRatioMode.KeepAspectRatio,
                                 transformationMode=Qt.TransformationMode.SmoothTransformation)

        filename = params.get("filename", f"capture_{int(time.time())}.png")
        if not filename.endswith(".png"):
            filename += ".png"
        filepath = _path_allowed(os.path.join(CANVAS_OUTPUT_DIR, filename))
        if not filepath:
            return {"error": "Path outside allowlisted roots"}
        if not image.save(filepath, "PNG"):
            return {"error": f"Could not save capture: {filepath}"}
        return {"status": "ok", "path": filepath, "width": image.width(),
                "height": image.height(), "revision": int(time.time())}


# Register the extension
Krita.instance().addExtension(KritaMCPExtension(Krita.instance()))
