"""Authenticated protocol-v5 HTTP bridge for Krita 6.

The HTTP thread validates and queues envelopes.  Every Krita API call runs on
the Qt main thread.  V5 is a clean cutover from v4 (new HTTP paths, new
protocol_version, no dual-version support) adding a unified get_state and
per-node pixel-occupancy/content-hash queries; like v4 before it, no legacy
action aliases or pixel-raster stroke fallbacks are registered.
"""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import traceback
import uuid

from krita import Extension, InfoObject, Krita, ManagedColor, Selection
from PyQt6.QtCore import QByteArray, QPointF, QRect, Qt, QThread, QTimer, QUuid
from PyQt6.QtGui import QColor, QColorSpace, QImage, QPainterPath

from .protocol_v5 import (
    ACTIONS,
    CHANNEL_DEPTH_BYTES,
    MAX_BODY_BYTES,
    MAX_QUEUE_DEPTH,
    MAX_TRACED_STROKES,
    MAX_UNTRACED_STROKES,
    PLUGIN_VERSION,
    PROTOCOL_VERSION,
    PathGuard,
    ProtocolError,
    ProtocolState,
    brush_fingerprint,
    content_hash,
    count_marked_pixels,
    ensure_token,
    error_response,
    parse_json_body,
    trace_diff_rgba,
    validate_bbox,
    validate_strokes,
)


SERVER_HOST = "127.0.0.1"
SERVER_PORT = int(os.environ.get("KRITA_MCP_PORT", "5678"))
COLOR_MODEL = "RGBA"
COLOR_DEPTH = "U16"
COLOR_PROFILE = "sRGB-elle-V2-srgbtrc.icc"
RESOLUTION_PPI = 300.0
MAX_CAPTURE_PIXELS = 16 * 1024 * 1024
TRANSACTION_PREFIX = "__krita_mcp_v4_candidate__"

_runtime_base = os.environ.get("XDG_RUNTIME_DIR")
if not _runtime_base:
    _runtime_base = os.path.join(tempfile.gettempdir(), f"runtime-{os.getuid()}")
TOKEN_PATH = Path(_runtime_base) / "krita-mcp" / "token"
ASSET_ROOT = Path(
    os.environ.get(
        "KRITA_MCP_ASSET_ROOT",
        str(Path.home() / "Development" / "Workspaces" / "creation"),
    )
).expanduser()
TRACE_ROOT = Path(
    os.environ.get(
        "KRITA_MCP_TRACE_ROOT", str(Path(tempfile.gettempdir()) / "krita-mcp-traces")
    )
).expanduser()
TRACE_ROOT.mkdir(parents=True, exist_ok=True)

# Unhandled action errors are sanitised before they reach the client, so
# without this the real traceback is lost and every fault looks like an opaque
# "internal_error". Overridable, and never allowed to raise.
ERROR_LOG_PATH = Path(
    os.environ.get("KRITA_MCP_ERROR_LOG", str(TRACE_ROOT / "kritamcp-errors.log"))
).expanduser()


def log_unhandled_error(request_id, action) -> None:
    try:
        ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(
                "=== %s request_id=%s action=%s ===\n"
                % (time.strftime("%Y-%m-%dT%H:%M:%S"), request_id, action)
            )
            traceback.print_exc(file=stream)
            stream.write("\n")
    except Exception:
        pass


def log_event(message: str) -> None:
    """Append a diagnostic line to the same log as unhandled errors."""
    try:
        ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write("=== %s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message))
    except Exception:
        pass

TOKEN = ensure_token(TOKEN_PATH)
PATH_GUARD = PathGuard([ASSET_ROOT, TRACE_ROOT], [ASSET_ROOT, TRACE_ROOT])
PROTOCOL_STATE = ProtocolState(TOKEN)


def _capabilities() -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "plugin_version": PLUGIN_VERSION,
        "actions": sorted(ACTIONS),
        "document": {
            "color_model": COLOR_MODEL,
            "color_depth": COLOR_DEPTH,
            "color_profile": COLOR_PROFILE,
            "resolution_ppi": RESOLUTION_PPI,
        },
        "geometry": ["line", "cubic"],
        "line_pressure": True,
        "cubic_pressure": False,
        "unsupported_sensors": ["rotation", "speed", "tilt"],
        "transactions": "ephemeral_candidate_layer",
        "captures": "png_path",
        "state_capture": {
            "unified_state": "get_state",
            "node_occupancy": "get_node_state",
            "occupancy_source": "pixel_data",
            "stroke_occupancy_fields": ["painted_pixels", "coverage"],
            "node_occupancy_fields": ["marked_pixels", "coverage", "content_hash"],
        },
        "limits": {
            "max_body_bytes": MAX_BODY_BYTES,
            "max_queue_depth": MAX_QUEUE_DEPTH,
            "max_traced_strokes": MAX_TRACED_STROKES,
            "max_untraced_strokes": MAX_UNTRACED_STROKES,
            "max_capture_pixels": MAX_CAPTURE_PIXELS,
        },
    }


class CommandQueue:
    """Bounded request queue linking HTTP workers to Krita's main thread."""

    def __init__(self) -> None:
        self._pending: list[tuple[str, dict]] = []
        self._results: dict[str, tuple[threading.Event, dict | None]] = {}
        self._lock = threading.Lock()

    def push(self, envelope: dict) -> str:
        queue_id = str(uuid.uuid4())
        with self._lock:
            if len(self._pending) >= MAX_QUEUE_DEPTH:
                raise ProtocolError(
                    "queue_full",
                    "the Krita command queue is full",
                    retryable=True,
                    http_status=503,
                )
            self._results[queue_id] = (threading.Event(), None)
            self._pending.append((queue_id, envelope))
        return queue_id

    def pop(self) -> tuple[str, dict] | None:
        with self._lock:
            return self._pending.pop(0) if self._pending else None

    def set_result(self, queue_id: str, value: dict) -> None:
        with self._lock:
            item = self._results.get(queue_id)
            if item is None:
                return
            event, _ = item
            self._results[queue_id] = (event, value)
            event.set()

    def get_result(self, queue_id: str, timeout: float = 180.0) -> dict:
        with self._lock:
            item = self._results.get(queue_id)
        if item is None:
            raise ProtocolError("internal_error", "queued command disappeared", http_status=500)
        event, _ = item
        if not event.wait(timeout):
            with self._lock:
                self._results.pop(queue_id, None)
            raise ProtocolError(
                "command_timeout",
                "Krita did not finish the command before the bridge timeout",
                retryable=True,
                http_status=504,
            )
        with self._lock:
            _, result = self._results.pop(queue_id)
        if result is None:
            raise ProtocolError("internal_error", "command completed without a result", http_status=500)
        return result


COMMAND_QUEUE = CommandQueue()


class _LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # socketserver defaults to 5. A client that abandons a request leaves its
    # connection in the accept queue, so a handful of timeouts made the bridge
    # permanently unreachable while Krita itself was healthy.
    request_queue_size = 64

    def handle_error(self, request, client_address) -> None:
        # The default implementation prints the traceback to stderr, which is
        # invisible inside a running Krita. Route per-connection failures to
        # the same log as handler failures so they stop being silent.
        log_unhandled_error(None, "connection:%s" % (client_address,))


class V4RequestHandler(BaseHTTPRequestHandler):
    """Strict HTTP surface: /health plus authenticated /v4 endpoints."""

    server_version = "krita-mcp-v5"
    sys_version = ""

    def log_message(self, _format, *_args) -> None:
        return

    def _send(self, value: dict, status: int = 200) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, error: ProtocolError, request_id: str | None = None) -> None:
        self._send(error_response(request_id, error), error.http_status)

    def _authenticate(self) -> None:
        PROTOCOL_STATE.authenticate(self.headers.get("Authorization"))

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send({"status": "ok"})
            return
        try:
            if self.path != "/v5/capabilities":
                raise ProtocolError(
                    "protocol_mismatch",
                    "use authenticated /v5/capabilities or /v5/command",
                    http_status=426,
                )
            self._authenticate()
            self._send(
                {
                    "ok": True,
                    "request_id": self.headers.get("X-Request-ID"),
                    "document_revision": PROTOCOL_STATE.revision,
                    "result": _capabilities(),
                    "warnings": [],
                }
            )
        except ProtocolError as error:
            self._fail(error)

    def do_POST(self) -> None:
        request_id = None
        try:
            if self.path != "/v5/command":
                raise ProtocolError(
                    "protocol_mismatch",
                    "only /v5/command accepts command envelopes",
                    http_status=426,
                )
            self._authenticate()
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ProtocolError("length_required", "Content-Length is required", http_status=411)
            try:
                content_length = int(raw_length)
            except ValueError as exc:
                raise ProtocolError("invalid_length", "Content-Length must be an integer") from exc
            if content_length < 0 or content_length > MAX_BODY_BYTES:
                raise ProtocolError(
                    "body_too_large",
                    f"request body exceeds {MAX_BODY_BYTES} bytes",
                    http_status=413,
                )
            envelope = parse_json_body(self.rfile.read(content_length), content_length)
            request_id = envelope["request_id"]
            queue_id = COMMAND_QUEUE.push(envelope)
            self._send(COMMAND_QUEUE.get_result(queue_id))
        except ProtocolError as error:
            self._fail(error, request_id)


class ServerThread(QThread):
    """Owns the accept loop, and must outlive any single failure inside it."""

    def __init__(self, port: int) -> None:
        super().__init__()
        self.port = port
        self.server: _LoopbackHTTPServer | None = None
        self._stopping = False
        self.restarts = 0

    def run(self) -> None:
        # An exception escaping serve_forever() ends this thread while the
        # listening socket stays bound, and that failure is invisible from
        # outside: the process is still alive, the socket is still in LISTEN,
        # but nothing ever calls accept() again, so client connections sit
        # unanswered in the accept queue (Recv-Q > 0) until they time out.
        # Nothing is written anywhere either -- the action-level error log
        # only covers command dispatch, not the serve loop -- which is why
        # this looked like a wedged Krita rather than a dead accept loop.
        #
        # Supervise it: every exit is logged, and a failed loop is rebuilt
        # rather than left dead.
        backoff = 0.5
        while not self._stopping:
            try:
                if self.server is None:
                    self.server = _LoopbackHTTPServer(
                        (SERVER_HOST, self.port), V4RequestHandler
                    )
                self.server.serve_forever(poll_interval=0.25)
                if self._stopping:
                    return
                log_event("serve loop returned without a stop request")
            except Exception:
                log_unhandled_error(None, "serve_forever")
            if self._stopping:
                return
            self.restarts += 1
            log_event(
                "accept loop died; rebuilding listener (restart #%d)" % self.restarts
            )
            try:
                if self.server is not None:
                    self.server.server_close()
            except Exception:
                pass
            self.server = None
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 10.0)

    def stop(self) -> None:
        self._stopping = True
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


class KritaMCPExtension(Extension):
    """Main-thread implementation of the protocol-v4 actions."""

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.server_thread: ServerThread | None = None
        self.timer: QTimer | None = None
        self._documents: dict[str, object] = {}
        self._document_ids: dict[int, str] = {}
        self._brushes: dict[str, object] = {}
        # Guards process_commands against re-entry from a nested Qt event
        # loop spun by a Krita operation already running on the main thread.
        self._draining = False
        self._reentry_deferred = 0

    def setup(self) -> None:
        Krita.instance().setBatchmode(True)
        return

    def createActions(self, _window) -> None:
        if self.server_thread is None:
            self.server_thread = ServerThread(SERVER_PORT)
            self.server_thread.start()
            print(f"[KritaMCP] protocol v5 listening on {SERVER_HOST}:{SERVER_PORT}")
        if self.timer is None:
            self.timer = QTimer()
            self.timer.timeout.connect(self.process_commands)
            self.timer.start(20)

    def process_commands(self) -> None:
        # Krita's main-thread operations (waitForDone, refreshProjection, and
        # action.trigger for merge/flatten) pump the Qt event loop so the
        # status bar stays live -- that is what draws the "script ... is
        # working" progress bar. Pumping re-enters this 20 ms timer slot while
        # the outer command is still mid-flight. Without a guard the drain
        # then starts a SECOND brush operation on the same document from
        # inside the first one's wait, the stroke scheduler deadlocks, and the
        # main thread never returns. Because that thread holds the GIL, the
        # HTTP ServerThread cannot run either, so accept() stops and the
        # listening socket accumulates Recv-Q with the process still alive --
        # exactly the observed stall.
        #
        # One command at a time on the main thread. Deferred work is not lost:
        # the timer fires again every 20 ms once the outer command returns.
        if self._draining:
            self._reentry_deferred += 1
            if self._reentry_deferred in (1, 10, 100, 1000):
                log_event(
                    "reentrancy: nested event loop re-entered process_commands; "
                    "deferred=%d (this is the guard working)" % self._reentry_deferred
                )
            return
        self._draining = True
        try:
            item = COMMAND_QUEUE.pop()
            if item is None:
                return
            queue_id, envelope = item
            COMMAND_QUEUE.set_result(queue_id, self.execute_envelope(envelope))
        finally:
            self._draining = False

    def execute_envelope(self, envelope: dict) -> dict:
        request_id = envelope.get("request_id")
        try:
            return PROTOCOL_STATE.dispatch(
                envelope,
                lambda action, params: self._execute_action(action, params, envelope),
            )
        except ProtocolError as error:
            return error_response(request_id, error)
        except Exception:
            log_unhandled_error(request_id, envelope.get("action"))
            return error_response(
                request_id,
                ProtocolError(
                    "internal_error",
                    "Krita could not complete the command; inspect the local Krita log",
                    retryable=False,
                    http_status=500,
                ),
            )

    def _execute_action(self, action: str, params: dict, envelope: dict) -> dict:
        handlers = {
            "get_capabilities": self._get_capabilities,
            "get_state": self._get_state,
            "get_node_state": self._get_node_state,
            "create_document": self._create_document,
            "open_document": self._open_document,
            "save_document": self._save_document,
            "export_document": self._export_document,
            "close_document": self._close_document,
            "get_canvas_state": self._get_canvas_state,
            "list_layers": self._list_layers,
            "create_layer": self._create_layer,
            "update_layer": self._update_layer,
            "delete_layer": self._delete_layer,
            "flatten_layer": self._flatten_layer,
            "set_selection_from_mask": self._set_selection_from_mask,
            "clear_selection": self._clear_selection,
            "list_brushes": self._list_brushes,
            "render_brush_probe": self._render_brush_probe,
            "begin_paint_transaction": self._begin_transaction,
            "paint_strokes": self._paint_strokes,
            "commit_paint_transaction": self._commit_transaction,
            "rollback_paint_transaction": self._rollback_transaction,
            "capture_region": self._capture_region,
        }
        return handlers[action](params, envelope)

    @staticmethod
    def _app():
        return Krita.instance()

    def _active_view(self):
        window = self._app().activeWindow()
        view = window.activeView() if window else None
        if view is None:
            raise ProtocolError("no_active_view", "Krita has no active canvas view")
        return view

    def _register_document(self, document) -> str:
        object_key = id(document)
        document_id = self._document_ids.get(object_key)
        if document_id is None:
            document_id = str(uuid.uuid4())
            self._document_ids[object_key] = document_id
            self._documents[document_id] = document
        # Batch mode is PER DOCUMENT for save/export. Krita.instance()
        # .setBatchmode(True) in setup() is not enough: setup() runs before any
        # document exists, and exportImage()/saveAs() consult the document's
        # own flag. Without this, exportImage() raises the PNG export options
        # dialog and blocks the main thread forever -- observed live on
        # 2026-09-16, where it presented as a bridge_stall on export_document
        # that no client timeout could have resolved.
        self._set_batchmode(document)
        return document_id

    @staticmethod
    def _set_batchmode(document) -> None:
        """Best-effort: never let a missing binding break a command."""
        try:
            document.setBatchmode(True)
        except Exception:
            log_unhandled_error(None, "setBatchmode")

    def _forget_document(self, document_id: str) -> None:
        document = self._documents.pop(document_id, None)
        if document is not None:
            self._document_ids.pop(id(document), None)

    def _document(self, envelope: dict):
        document_id = envelope.get("document_id")
        open_documents = self._app().documents()
        if document_id:
            document = self._documents.get(document_id)
            if document is None or document not in open_documents:
                self._forget_document(document_id)
                raise ProtocolError("document_not_found", "document_id is not open")
            return document_id, document
        document = self._app().activeDocument()
        if document is None:
            raise ProtocolError("no_active_document", "Krita has no active document")
        return self._register_document(document), document

    @staticmethod
    def _node(document, node_id: str):
        if not node_id:
            raise ProtocolError("node_id_required", "node_id is required")
        node = document.nodeByUniqueID(QUuid(str(node_id)))
        if node is None:
            raise ProtocolError("node_not_found", "node_id was not found in the document")
        return node

    @staticmethod
    def _node_entry(node) -> dict:
        return {
            "id": node.uniqueId().toString(),
            "name": node.name(),
            "type": node.type(),
            "visible": node.visible(),
            "locked": node.locked(),
            "opacity": node.opacity(),
            "blend_mode": node.blendingMode(),
            "children": [KritaMCPExtension._node_entry(child) for child in node.childNodes()],
        }

    def _document_state(self, document_id: str, document) -> dict:
        active = document.activeNode()
        return {
            "document_id": document_id,
            "name": document.name(),
            "file_path": document.fileName(),
            "width": document.width(),
            "height": document.height(),
            "resolution_ppi": document.resolution(),
            "color_model": document.colorModel(),
            "color_depth": document.colorDepth(),
            "color_profile": document.colorProfile(),
            "modified": document.modified(),
            "active_node_id": active.uniqueId().toString() if active else None,
        }

    def _get_capabilities(self, _params: dict, _envelope: dict) -> dict:
        result = _capabilities()
        result["krita_version"] = self._app().version()
        result["profile_available"] = COLOR_PROFILE in self._app().profiles(
            COLOR_MODEL, COLOR_DEPTH
        )
        return result

    @staticmethod
    def _transaction_entry(transaction_id: str, transaction: dict) -> dict:
        return {
            "transaction_id": transaction_id,
            "document_id": transaction["document_id"],
            "session_id": transaction["session_id"],
            "candidate_layer_id": transaction["layer_id"],
            "target_layer_id": transaction.get("target_layer_id"),
            "label": transaction.get("label"),
        }

    def _get_state(self, _params: dict, envelope: dict) -> dict:
        # Lease expiry is lazy (it runs inside obtain_writer), so a raw read of
        # writer_session reports True forever after any session has held the
        # lease — even long past the 120 s timeout. Expire before reporting.
        # Everything read from PROTOCOL_STATE happens under one lock
        # acquisition so the writer/transactions snapshot is internally
        # consistent (no transaction from a session that released mid-read).
        with PROTOCOL_STATE._lock:
            PROTOCOL_STATE._expire_writer()
            writer_session = PROTOCOL_STATE.writer_session
            writer_last_seen = PROTOCOL_STATE.writer_last_seen
            transactions = dict(PROTOCOL_STATE.transactions)
        now = time.monotonic()
        result = {
            "bridge_revision": PROTOCOL_STATE.revision,
            "writer_lease": writer_session is not None,
            # None when idle; otherwise enough to answer "who holds the
            # writer, and is it about to expire" without a second call.
            "writer_lease_details": (
                {
                    "session_id": writer_session,
                    "seconds_since_touch": round(now - writer_last_seen, 3),
                    "seconds_remaining": round(
                        max(0.0, PROTOCOL_STATE.session_timeout - (now - writer_last_seen)), 3
                    ),
                }
                if writer_session is not None
                else None
            ),
        }
        try:
            document_id, document = self._document(envelope)
        except ProtocolError as error:
            if error.code != "no_active_document":
                raise
            result["document"] = None
            result["layers"] = None
            result["transactions"] = [
                self._transaction_entry(transaction_id, transaction)
                for transaction_id, transaction in transactions.items()
            ]
            return result
        # list_layers duplicated this tree in a second round trip; folding it
        # in here is the point of the v5 state-capture pass -- one call for
        # "what does the bridge think is true right now".
        result["document"] = self._document_state(document_id, document)
        result["layers"] = self._node_entry(document.rootNode())
        result["transactions"] = [
            self._transaction_entry(transaction_id, transaction)
            for transaction_id, transaction in transactions.items()
            if transaction["document_id"] == document_id
        ]
        return result

    def _get_node_state(self, params: dict, envelope: dict) -> dict:
        """Node-level pixel occupancy AND content identity, on demand.

        The general-purpose sibling of the per-stroke painted_pixels/coverage
        telemetry in _paint_one: that runs automatically as a side effect of
        painting, this runs whenever a caller wants to check a node's actual
        content without capturing a PNG. Reads the node's own paint device
        via _node_snapshot, never document.projection(), so it never calls
        refreshProjection() and cannot trigger the modal busy-wait dialog
        documented on _paint_one / _capture_region.
        """
        document_id, document = self._document(envelope)
        node = self._node(document, params.get("node_id"))
        bbox = validate_bbox(params.get("bbox")) or [0, 0, document.width(), document.height()]
        channel_depth = self._channel_depth_bytes(document)
        marked, total, node_content_hash = self._node_snapshot(node, bbox, channel_depth)
        if marked >= 0:
            coverage = round(marked / total, 6) if total else 0.0
        else:
            coverage = -1.0
        return {
            "document_id": document_id,
            "node": self._node_entry(node),
            "bbox": bbox,
            # -1 / null mean "could not measure", not "painted nothing" --
            # same convention as paint_strokes' per-stroke result.
            "marked_pixels": marked,
            "total_pixels": total,
            "coverage": coverage,
            "content_hash": node_content_hash,
        }

    @staticmethod
    def _validate_canvas_size(width: int, height: int) -> tuple[int, int]:
        if not isinstance(width, int) or not isinstance(height, int):
            raise ProtocolError("invalid_document", "width and height must be integers")
        if not 1 <= width <= 16384 or not 1 <= height <= 16384:
            raise ProtocolError("invalid_document", "canvas dimensions must be in [1, 16384]")
        return width, height

    @staticmethod
    def _rgba(value) -> tuple[int, int, int, int]:
        if (
            not isinstance(value, list)
            or len(value) != 4
            or any(not isinstance(channel, int) or not 0 <= channel <= 255 for channel in value)
        ):
            raise ProtocolError("invalid_color", "RGBA must be four integer channels in [0, 255]")
        return tuple(value)

    @staticmethod
    def _fill_u16(layer, width: int, height: int, rgba: tuple[int, int, int, int]) -> None:
        red, green, blue, alpha = (channel * 257 for channel in rgba)
        pixel = struct.pack("=HHHH", blue, green, red, alpha)
        # Keep the Python/Qt transfer bounded for production canvases. A
        # 3000-square U16 RGBA substrate is 72 MB and a one-shot QByteArray can
        # fail inside PyQt/Krita even though the document itself is valid.
        rows_per_chunk = max(1, min(128, (8 * 1024 * 1024) // (width * len(pixel))))
        for top in range(0, height, rows_per_chunk):
            rows = min(rows_per_chunk, height - top)
            data = QByteArray(pixel * (width * rows))
            if not layer.setPixelData(data, 0, top, width, rows):
                raise ProtocolError(
                    "substrate_failed",
                    "could not initialize the neutral substrate",
                    details={"top": top, "rows": rows},
                )

    def _create_document(self, params: dict, _envelope: dict) -> dict:
        width, height = self._validate_canvas_size(params.get("width"), params.get("height"))
        name = str(params.get("name") or "AutoPainter v4")
        background = self._rgba(params.get("background_rgba", [232, 228, 218, 255]))
        if COLOR_PROFILE not in self._app().profiles(COLOR_MODEL, COLOR_DEPTH):
            raise ProtocolError(
                "color_profile_unavailable",
                f"required Krita profile is unavailable: {COLOR_PROFILE}",
            )
        document = self._app().createDocument(
            width,
            height,
            name,
            COLOR_MODEL,
            COLOR_DEPTH,
            COLOR_PROFILE,
            RESOLUTION_PPI,
        )
        if document is None:
            raise ProtocolError("document_create_failed", "Krita could not create the document")
        window = self._app().activeWindow()
        if window is not None:
            window.addView(document)
        substrate = document.activeNode()
        if substrate is None:
            substrate = document.createNode("00 Ground", "paintlayer")
            document.rootNode().addChildNode(substrate, None)
        substrate.setName("00 Ground")
        self._fill_u16(substrate, width, height, background)
        document.setActiveNode(substrate)
        document.refreshProjection()
        document_id = self._register_document(document)
        state = self._document_state(document_id, document)
        expected = (COLOR_MODEL, COLOR_DEPTH, COLOR_PROFILE, int(RESOLUTION_PPI))
        actual = (
            state["color_model"],
            state["color_depth"],
            state["color_profile"],
            int(state["resolution_ppi"]),
        )
        if actual != expected:
            # _fill_u16 above dirtied the document, and close() on a modified
            # document blocks forever on Krita's native "save changes?" dialog
            # (batch mode does not suppress it in this build). Clearing the
            # dirty flag first is what keeps this error path from hanging the
            # main thread instead of returning the invariant failure.
            document.setModified(False)
            document.close()
            self._forget_document(document_id)
            raise ProtocolError(
                "document_invariant_failed",
                "Krita created a document with unexpected color settings",
                details={"expected": expected, "actual": actual},
            )
        state["substrate_node_id"] = substrate.uniqueId().toString()
        return state

    def _open_document(self, params: dict, _envelope: dict) -> dict:
        path = PATH_GUARD.resolve_read(params.get("path", ""))
        document = self._app().openDocument(str(path))
        if document is None:
            raise ProtocolError("document_open_failed", "Krita could not open the document")
        window = self._app().activeWindow()
        if window is not None:
            window.addView(document)
        document_id = self._register_document(document)
        return self._document_state(document_id, document)

    def _save_document(self, params: dict, envelope: dict) -> dict:
        document_id, document = self._document(envelope)
        requested = params.get("path")
        if requested:
            path = PATH_GUARD.resolve_write(requested)
            if path.suffix.lower() != ".kra":
                raise ProtocolError("invalid_format", "save_document requires a .kra path")
            self._set_batchmode(document)
            ok = document.saveAs(str(path))
        else:
            if not document.fileName():
                raise ProtocolError("path_required", "an unsaved document requires a .kra path")
            path = PATH_GUARD.resolve_write(document.fileName())
            if path.suffix.lower() != ".kra":
                raise ProtocolError("invalid_format", "save_document requires a .kra path")
            self._set_batchmode(document)
            ok = document.save()
        if not ok:
            raise ProtocolError("save_failed", "Krita could not save the KRA document")
        return {"document_id": document_id, "path": str(path)}

    def _export_document(self, params: dict, envelope: dict) -> dict:
        document_id, document = self._document(envelope)
        path = PATH_GUARD.resolve_write(params.get("path", ""))
        if path.suffix.lower() != ".png":
            raise ProtocolError("invalid_format", "export_document currently requires a .png path")
        # Every PNG property must be specified. Krita asks for anything left
        # unset via a modal options dialog, which on a headless bridge means
        # the main thread never returns.
        configuration = InfoObject()
        configuration.setProperty("alpha", True)
        configuration.setProperty("compression", 3)
        configuration.setProperty("indexed", False)
        configuration.setProperty("interlaced", False)
        configuration.setProperty("saveSRGBProfile", True)
        configuration.setProperty("forceSRGB", False)
        configuration.setProperty("transparencyFillcolor", [255, 255, 255])
        self._set_batchmode(document)
        if not document.exportImage(str(path), configuration):
            raise ProtocolError("export_failed", "Krita could not export the PNG")
        return {
            "document_id": document_id,
            "path": str(path),
            "color_depth": document.colorDepth(),
        }

    def _close_document(self, params: dict, envelope: dict) -> dict:
        document_id, document = self._document(envelope)
        save_policy = str(params.get("save_policy") or "cancel")
        if save_policy not in {"save", "discard", "cancel"}:
            raise ProtocolError(
                "invalid_save_policy",
                "save_policy must be one of: save, discard, cancel",
            )
        for transaction_id, transaction in list(PROTOCOL_STATE.transactions.items()):
            if transaction["document_id"] == document_id:
                self._rollback_internal(transaction_id, document)
        dirty = document.modified()
        if dirty and save_policy == "cancel":
            # The user-visible equivalent of pressing Cancel on Krita's native
            # "save changes?" dialog: the tab stays open, nothing is lost.
            return {
                "document_id": document_id,
                "closed": False,
                "reason": "unsaved_changes",
                "modified": True,
            }
        if dirty and save_policy == "save":
            if document.fileName():
                # Known file on disk: save in place, never raise a dialog.
                if not document.save():
                    raise ProtocolError("save_failed", "Krita could not save the document")
            else:
                requested = params.get("save_as_path")
                if not requested:
                    raise ProtocolError(
                        "unsaved_document_needs_path",
                        "document has never been saved; pass save_as_path "
                        "to choose where the save goes",
                    )
                # Guard-checked BEFORE saving so a path outside the configured
                # roots cannot reach Krita's Save As dialog.
                path = PATH_GUARD.resolve_write(requested)
                path.parent.mkdir(parents=True, exist_ok=True)
                if not document.saveAs(str(path)):
                    raise ProtocolError("save_failed", f"Krita could not save to {path}")
        # document.close() blocks forever on Krita's native "save changes?"
        # dialog for a modified document; batch mode alone does not suppress
        # it in this Krita build, so the dirty flag must be cleared first.
        # Only the "discard" branch reaches close() while still dirty.
        document.setModified(False)
        if not document.close():
            raise ProtocolError("close_failed", "Krita refused to close the document")
        self._forget_document(document_id)
        return {"document_id": document_id, "closed": True}

    def _get_canvas_state(self, _params: dict, envelope: dict) -> dict:
        """One read-only answer to "what does Krita's workspace look like":
        every open document/tab (with which one is active), and the active
        view's paint settings. Pairs with capture_region for the pixels.
        Agent-safe: no refresh, no mutation, never raises on odd states."""
        app = self._app()
        window = app.activeWindow()
        active_view = window.activeView() if window else None
        active_document = app.activeDocument()
        entries = []
        for document in app.documents():
            document_id = self._document_ids.get(id(document))
            if document_id is None:
                document_id = self._register_document(document)
            entries.append(
                {
                    "document_id": document_id,
                    "name": document.name(),
                    "file_name": document.fileName(),
                    "modified": document.modified(),
                    "width": document.width(),
                    "height": document.height(),
                    "active": active_document is not None and document == active_document,
                }
            )
        view_info = None
        if active_view is not None:
            preset = None
            try:
                resource = active_view.currentBrushPreset()
                preset = resource.name() if resource is not None else None
            except Exception:
                preset = None  # a half-initialised view must not fail the read
            view_info = {
                "brush_preset": preset,
                "brush_size": active_view.brushSize(),
                "painting_opacity": active_view.paintingOpacity(),
                "painting_flow": active_view.paintingFlow(),
                "blending_mode": active_view.currentBlendingMode(),
                "eraser_mode": active_view.eraserMode(),
            }
        return {
            "documents": entries,
            "active_document_id": self._document_ids.get(id(active_document)) if active_document else None,
            "window_count": 1 if window else 0,
            "view_count": len(window.views()) if window else 0,
            "view": view_info,
        }

    def _list_layers(self, _params: dict, envelope: dict) -> dict:
        document_id, document = self._document(envelope)
        return {"document_id": document_id, "root": self._node_entry(document.rootNode())}

    def _create_layer(self, params: dict, envelope: dict) -> dict:
        document_id, document = self._document(envelope)
        name = str(params.get("name") or "Layer")
        if name.startswith(TRANSACTION_PREFIX):
            raise ProtocolError("reserved_name", "that layer-name prefix is plugin-owned")
        node_type = params.get("type", "paintlayer")
        allowed = {"paintlayer", "grouplayer", "selectionmask"}
        if node_type not in allowed:
            raise ProtocolError(
                "unsupported_layer_type",
                "layer type must be paintlayer, grouplayer, or selectionmask",
            )
        if node_type == "grouplayer":
            node = document.createGroupLayer(name)
        elif node_type == "selectionmask":
            node = document.createSelectionMask(name)
        else:
            node = document.createNode(name, "paintlayer")
        parent = (
            self._node(document, params["parent_id"])
            if params.get("parent_id")
            else document.rootNode()
        )
        above = self._node(document, params["above_id"]) if params.get("above_id") else None
        if not parent.addChildNode(node, above):
            raise ProtocolError("layer_create_failed", "Krita could not attach the new layer")
        if params.get("select", True):
            document.setActiveNode(node)
        document.refreshProjection()
        return {
            "document_id": document_id,
            "node": self._node_entry(node),
        }

    def _update_layer(self, params: dict, envelope: dict) -> dict:
        document_id, document = self._document(envelope)
        node = self._node(document, params.get("node_id"))
        if "name" in params:
            name = str(params["name"])
            if name.startswith(TRANSACTION_PREFIX):
                raise ProtocolError("reserved_name", "that layer-name prefix is plugin-owned")
            node.setName(name)
        if "visible" in params:
            node.setVisible(bool(params["visible"]))
        if "locked" in params:
            node.setLocked(bool(params["locked"]))
        if "opacity" in params:
            opacity = params["opacity"]
            if not isinstance(opacity, int) or not 0 <= opacity <= 255:
                raise ProtocolError("invalid_layer", "opacity must be an integer in [0, 255]")
            node.setOpacity(opacity)
        if "blend_mode" in params:
            node.setBlendingMode(str(params["blend_mode"]))
        document.refreshProjection()
        return {"document_id": document_id, "node": self._node_entry(node)}

    def _delete_layer(self, params: dict, envelope: dict) -> dict:
        document_id, document = self._document(envelope)
        node = self._node(document, params.get("node_id"))
        node_id = node.uniqueId().toString()
        if node.name().startswith(TRANSACTION_PREFIX):
            orphan_cleanup = params.get("orphan_cleanup") is True
            if not orphan_cleanup:
                raise ProtocolError(
                    "transaction_required", "plugin-owned candidate layers must be rolled back"
                )
            if any(
                transaction.get("layer_id") == node_id
                for transaction in PROTOCOL_STATE.transactions.values()
            ):
                raise ProtocolError(
                    "transaction_active", "a live transaction candidate cannot be orphan-cleaned"
                )
        if not node.remove():
            raise ProtocolError("layer_delete_failed", "Krita could not delete the layer")
        document.refreshProjection()
        return {"document_id": document_id, "deleted_node_id": node_id}

    def _flatten_layer(self, params: dict, envelope: dict) -> dict:
        """Collapse a group into a single paint layer, preserving its name.

        Krita exposes no Node-level flatten, and Node.mergeDown() is unreliable
        for nested layers in this build, so this drives the application's own
        flatten_layer action against the active node.
        """
        document_id, document = self._document(envelope)
        node = self._node(document, params.get("node_id"))
        if node.name().startswith(TRANSACTION_PREFIX):
            raise ProtocolError(
                "transaction_required", "plugin-owned candidate layers cannot be flattened"
            )
        name = node.name()
        document.setActiveNode(node)
        document.waitForDone()
        action = self._app().action("flatten_layer")
        if action is None:
            raise ProtocolError("flatten_failed", "flatten_layer action unavailable")
        action.trigger()
        document.waitForDone()
        replacement = document.activeNode()
        if replacement is None:
            raise ProtocolError("flatten_failed", "Krita did not return a flattened layer")
        # Flattening discards the group's name in some builds; restore it so
        # semantic ordering survives the collapse.
        if replacement.name() != name:
            replacement.setName(name)
        document.refreshProjection()
        return {
            "document_id": document_id,
            "node_id": replacement.uniqueId().toString(),
            "name": replacement.name(),
        }

    @staticmethod
    def _image_bytes(image: QImage) -> bytes:
        pointer = image.constBits()
        pointer.setsize(image.sizeInBytes())
        raw = bytes(pointer)
        width = image.width()
        if image.bytesPerLine() == width:
            return raw
        stride = image.bytesPerLine()
        return b"".join(raw[row * stride : row * stride + width] for row in range(image.height()))

    def _set_selection_from_mask(self, params: dict, envelope: dict) -> dict:
        document_id, document = self._document(envelope)
        path = PATH_GUARD.resolve_read(params.get("path", ""))
        if path.suffix.lower() != ".png":
            raise ProtocolError("invalid_format", "selection masks must be PNG files")
        image = QImage(str(path))
        if image.isNull():
            raise ProtocolError("invalid_mask", "Krita could not decode the mask PNG")
        if image.width() != document.width() or image.height() != document.height():
            raise ProtocolError(
                "mask_size_mismatch",
                "selection mask dimensions must equal the document dimensions",
            )
        grayscale = image.convertToFormat(QImage.Format.Format_Grayscale8)
        selection = Selection()
        selection.setPixelData(
            QByteArray(self._image_bytes(grayscale)),
            0,
            0,
            document.width(),
            document.height(),
        )
        document.setSelection(selection)
        mask_node_id = params.get("selection_mask_node_id")
        if mask_node_id:
            mask_node = self._node(document, mask_node_id)
            if mask_node.type() != "selectionmask":
                raise ProtocolError("invalid_mask_node", "node is not a selection mask")
            mask_node.setSelection(selection.duplicate())
        document.refreshProjection()
        return {
            "document_id": document_id,
            "path": str(path),
            "selection_mask_node_id": mask_node_id,
        }

    def _clear_selection(self, _params: dict, envelope: dict) -> dict:
        document_id, document = self._document(envelope)
        selection = document.selection()
        if selection is not None:
            selection.clear()
            document.setSelection(selection)
        return {"document_id": document_id, "cleared": True}

    @staticmethod
    def _resource_hash(resource) -> str | None:
        filename = resource.filename()
        if not filename:
            return None
        path = Path(filename).expanduser()
        if not path.is_file():
            return None
        try:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return None

    def _catalog_brushes(self) -> list[dict]:
        catalog = []
        self._brushes = {}
        for _key, resource in self._app().resources("preset").items():
            filename = resource.filename() or None
            file_hash = self._resource_hash(resource)
            preset_id = brush_fingerprint("preset", resource.name(), filename, file_hash)
            if preset_id in self._brushes:
                raise ProtocolError(
                    "resource_collision",
                    "two installed brush resources have the same stable identity",
                    details={"preset_id": preset_id},
                )
            self._brushes[preset_id] = resource
            catalog.append(
                {
                    "preset_id": preset_id,
                    "resource_type": "preset",
                    "name": resource.name(),
                    "filename": filename,
                    "file_sha256": file_hash,
                }
            )
        return sorted(catalog, key=lambda item: (item["name"].casefold(), item["preset_id"]))

    def _list_brushes(self, params: dict, _envelope: dict) -> dict:
        brushes = self._catalog_brushes()
        query = str(params.get("query") or "").casefold()
        if query:
            brushes = [item for item in brushes if query in item["name"].casefold()]
        return {"brushes": brushes, "count": len(brushes)}

    def _brush(self, preset_id: str):
        if not self._brushes:
            self._catalog_brushes()
        resource = self._brushes.get(preset_id)
        if resource is None:
            self._catalog_brushes()
            resource = self._brushes.get(preset_id)
        if resource is None:
            raise ProtocolError("brush_not_found", "preset_id is not installed")
        return resource

    @staticmethod
    def _color(value: list[int]) -> QColor:
        red, green, blue, alpha = value
        return QColor(red, green, blue, alpha)

    def _apply_stroke_settings(self, view, stroke: dict) -> object:
        resource = self._brush(stroke["preset_id"])
        view.setCurrentBrushPreset(resource)
        view.setBrushSize(float(stroke["size"]))
        view.setPaintingOpacity(float(stroke["opacity"]))
        view.setPaintingFlow(float(stroke["flow"]))
        view.setCurrentBlendingMode(stroke["blend_mode"])
        view.setEraserMode(stroke["erase"])
        view.setForeGroundColor(
            ManagedColor.fromQColor(self._color(stroke["foreground_rgba"]), view.canvas())
        )
        return resource

    @staticmethod
    def _bbox_for_geometry(document, geometry: dict, size: float) -> list[int]:
        if geometry["type"] == "line":
            points = [geometry["start"], geometry["end"]]
        else:
            points = [geometry["start"]]
            for command in geometry["commands"]:
                points.extend((command["control1"], command["control2"], command["end"]))
        # Textured/scattered presets may place dabs outside the nominal brush
        # radius. The conservative bound keeps trace crops lossless without a
        # full-canvas snapshot.
        padding = float(size) * 2.0 + 3.0
        left = max(0, int(min(point[0] for point in points) - padding))
        top = max(0, int(min(point[1] for point in points) - padding))
        right = min(document.width(), int(max(point[0] for point in points) + padding) + 1)
        bottom = min(document.height(), int(max(point[1] for point in points) + padding) + 1)
        return [left, top, max(0, right - left), max(0, bottom - top)]

    @staticmethod
    def _read_node_pixels(node, bbox: list[int]) -> tuple[bytes | None, int]:
        """One Node.pixelData() read, isolated so _alpha_coverage and
        _node_snapshot share the exact same never-raises contract instead of
        each re-implementing the try/except around it.

        This reads the layer's pixels directly -- it does NOT touch the
        composited projection, so unlike document.projection() it needs no
        refreshProjection() and cannot trigger the busy-wait dialog that
        deadlocks the bridge (2026-09-17 backtrace: refreshProjection ->
        KisImage::waitForDone -> KisDelayedSaveDialog::blockIfImageIsBusy ->
        QDialog::exec, with PyKrita holding the GIL so the HTTP thread
        starves in take_gil).

        Returns (raw_bytes_or_None, total_pixels). None means the read
        raised -- never propagated, because this is telemetry and must not
        be able to break a paint.
        """
        left, top, width, height = bbox
        total = max(0, width) * max(0, height)
        if total <= 0:
            return b"", 0
        try:
            return bytes(node.pixelData(left, top, width, height)), total
        except Exception:
            return None, total

    @staticmethod
    def _alpha_coverage(node, bbox: list[int], channel_depth: int) -> tuple[int, int]:
        """Count non-transparent pixels in bbox on this node's OWN paint
        device. Returns (marked_pixels, total_pixels); (-1, total) means the
        read failed or the layout was unrecognised -- that is "unmeasured",
        not "painted nothing".
        """
        raw, total = KritaMCPExtension._read_node_pixels(node, bbox)
        if total <= 0:
            return 0, 0
        if raw is None:
            return -1, total
        return count_marked_pixels(raw, total, channel_depth), total

    @staticmethod
    def _node_snapshot(
        node, bbox: list[int], channel_depth: int
    ) -> tuple[int, int, str | None]:
        """Occupancy AND content identity from the same pixelData() read.

        Used by get_node_state, an on-demand query -- NOT by the _paint_one
        hot path, where hashing every stroke's bbox in a batch of dozens
        would be pure overhead nothing reads. Returns (marked_pixels,
        total_pixels, content_hash); content_hash is None whenever marked is
        -1 (unmeasured), mirroring that same convention.
        """
        raw, total = KritaMCPExtension._read_node_pixels(node, bbox)
        if total <= 0:
            return 0, 0, None
        if raw is None:
            return -1, total, None
        marked = count_marked_pixels(raw, total, channel_depth)
        return marked, total, (content_hash(raw) if marked >= 0 else None)

    @staticmethod
    def _channel_depth_bytes(document) -> int:
        try:
            depth = str(document.colorDepth())
        except Exception:
            return 2
        return CHANNEL_DEPTH_BYTES.get(depth, 2)

    # None until probed; then True if this Krita build's paintLine takes
    # QPoint (5.2-style bindings) rather than QPointF (master). The endpoint
    # type genuinely differs between builds, so it is detected once at
    # runtime instead of being hardcoded.
    _paint_line_uses_qpoint = None

    def _paint_line(self, layer, start, end, pressure_start, pressure_end):
        """One paintLine call, tolerant of both endpoint signatures.

        The probe raises before painting anything, so a failed attempt cannot
        leave a partial stroke behind.
        """
        if self._paint_line_uses_qpoint is None:
            try:
                layer.paintLine(
                    QPointF(*start),
                    QPointF(*end),
                    pressure_start,
                    pressure_end,
                    "ForegroundColor",
                )
                KritaMCPExtension._paint_line_uses_qpoint = False
                return
            except TypeError:
                KritaMCPExtension._paint_line_uses_qpoint = True
        if self._paint_line_uses_qpoint:
            layer.paintLine(
                QPointF(*start).toPoint(),
                QPointF(*end).toPoint(),
                pressure_start,
                pressure_end,
                "ForegroundColor",
            )
        else:
            layer.paintLine(
                QPointF(*start),
                QPointF(*end),
                pressure_start,
                pressure_end,
                "ForegroundColor",
            )

    def _paint_one(self, document, layer, view, stroke: dict, trace_path=None) -> dict:
        resource = self._apply_stroke_settings(view, stroke)
        geometry = stroke["geometry"]
        bbox = self._bbox_for_geometry(document, geometry, stroke["size"])
        channel_depth = self._channel_depth_bytes(document)
        # Alpha coverage on the candidate layer BEFORE the stroke, so the
        # delta afterwards isolates what THIS stroke marked (the candidate
        # accumulates every stroke in the batch). Without this the bridge
        # reports a geometry-derived bbox and nothing else, so a stroke that
        # painted nothing -- clipped away by document.setSelection(), or a
        # preset that renders no dab at this size/pressure -- is
        # indistinguishable from one that painted correctly.
        # When tracing, the raw buffer is kept so the trace is the byte diff
        # of this same read -- no second capture, no projection involved.
        if trace_path is not None:
            raw_before, total = self._read_node_pixels(layer, bbox)
            before_marked = (
                count_marked_pixels(raw_before, total, channel_depth)
                if raw_before is not None
                else -1
            )
        else:
            before_marked, total = self._alpha_coverage(layer, bbox, channel_depth)
        start_time = time.perf_counter()
        if geometry["type"] == "line":
            view.setDisablePressure(False)
            self._paint_line(
                layer,
                geometry["start"],
                geometry["end"],
                float(stroke["pressure"]["start"]),
                float(stroke["pressure"]["end"]),
            )
        else:
            view.setDisablePressure(True)
            path = QPainterPath(QPointF(*geometry["start"]))
            for command in geometry["commands"]:
                path.cubicTo(
                    QPointF(*command["control1"]),
                    QPointF(*command["control2"]),
                    QPointF(*command["end"]),
                )
            layer.paintPath(path, "ForegroundColor", "None")
        # NOTE: no document.refreshProjection() here. The projection is only
        # needed for trace captures (the bbox above is pure geometry), and
        # refreshProjection blocks in KisImage::waitForDone, which pops a
        # MODAL busy-wait dialog whose nested event loop eats every timer
        # event: accept() starves and the bridge stalls (Recv-Q > 0) — the
        # live stall captured by py-spy on 2026-09-16. Per-stroke refreshes
        # in the hot loop multiplied that exposure thousands-fold; trace
        # captures refresh explicitly just before the capture instead.
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        # Flush the image scheduler BEFORE the pixel read. paintLine/paintPath
        # apply asynchronously; without a flush the before/after buffers can
        # both read pre-stroke state, so a real stroke reports painted_px=0
        # and the trace diff is byte-identical (live-verified 2026-09-18: the
        # stroke was on canvas in capture_region while painted_pixels read 0).
        # waitForDone, not refreshProjection: the projection is not needed
        # here and the modal busy-wait exposure stays with the explicit
        # capture path.
        document.waitForDone()
        # One pixelData read for both telemetry and the trace diff.
        raw_after, total_pixels = self._read_node_pixels(layer, bbox)
        after_marked = (
            count_marked_pixels(raw_after, total_pixels, channel_depth)
            if raw_after is not None
            else -1
        )
        if before_marked >= 0 and after_marked >= 0:
            painted_pixels = max(0, after_marked - before_marked)
            coverage = round(painted_pixels / total_pixels, 6) if total_pixels else 0.0
        else:
            painted_pixels, coverage = -1, -1.0
        filename = resource.filename() or None
        result = {
            "stroke_id": stroke["stroke_id"],
            "effective_brush_fingerprint": brush_fingerprint(
                "preset", resource.name(), filename, self._resource_hash(resource)
            ),
            "bbox": bbox,
            "render_ms": elapsed_ms,
            # -1 means "could not measure", not "painted nothing".
            "painted_pixels": painted_pixels,
            "coverage": coverage,
        }
        if trace_path is not None:
            diff = trace_diff_rgba(raw_before, raw_after, total_pixels, channel_depth)
            if diff is not None:
                self._write_trace_png(diff, bbox, trace_path)
                result["trace_path"] = str(trace_path)
            # else: capture failed -- _paint_strokes falls back to the
            # projection capture for this stroke, so a missing trace is
            # never silently accepted by the animation builder.
        return result

    @staticmethod
    def _write_trace_png(bgra: bytes, bbox: list[int], path) -> None:
        """Write one per-stroke trace diff as a PNG. `bgra` is the 8-bit
        Format_ARGB32-ordered buffer produced by trace_diff_rgba; keep the
        PNG fully specified so nothing can raise a modal dialog on save."""
        left, top, width, height = bbox
        image = QImage(bgra, width, height, width * 4, QImage.Format.Format_ARGB32)
        if image.save(str(path), "PNG"):
            return
        raise ProtocolError("capture_failed", "Krita could not save the trace PNG")

    @staticmethod
    def _srgb8_projection(document, bbox: list[int] | None = None) -> QImage:
        # projection() with no arguments returns a null QImage on this
        # build (binding quirk); always pass an explicit rect.
        rect = (
            bbox
            if bbox is not None
            else [0, 0, document.width(), document.height()]
        )
        image = document.projection(*rect)
        if image.isNull():
            raise ProtocolError("capture_failed", "Krita returned an empty projection")
        srgb = QColorSpace(QColorSpace.NamedColorSpace.SRgb)
        if image.colorSpace().isValid() and image.colorSpace() != srgb:
            image = image.convertedToColorSpace(srgb)
        return image.convertToFormat(QImage.Format.Format_RGBA8888)

    def _save_projection(
        self,
        document,
        path_value: str,
        bbox: list[int] | None = None,
        max_side: int | None = None,
    ) -> dict:
        path = PATH_GUARD.resolve_write(path_value)
        if path.suffix.lower() != ".png":
            raise ProtocolError("invalid_format", "captures must use a .png path")
        capture_pixels = (
            bbox[2] * bbox[3] if bbox is not None else document.width() * document.height()
        )
        if capture_pixels > MAX_CAPTURE_PIXELS:
            raise ProtocolError(
                "capture_too_large",
                f"capture exceeds the {MAX_CAPTURE_PIXELS}-pixel limit",
            )
        image = self._srgb8_projection(document, bbox)
        if max_side is not None:
            if not isinstance(max_side, int) or not 1 <= max_side <= 4096:
                raise ProtocolError("invalid_capture", "max_side must be in [1, 4096]")
            if max(image.width(), image.height()) > max_side:
                image = image.scaled(
                    max_side,
                    max_side,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
        if not image.save(str(path), "PNG"):
            raise ProtocolError("capture_failed", "Krita could not save the projection PNG")
        return {"path": str(path), "width": image.width(), "height": image.height()}

    def _begin_transaction(self, params: dict, envelope: dict) -> dict:
        document_id, document = self._document(envelope)
        # Captured before the candidate layer exists, so a rollback can restore
        # the dirty flag to its true pre-transaction value. Read it here rather
        # than at rollback time, when the candidate has already dirtied it.
        was_modified = document.modified()
        target = self._node(document, params.get("target_layer_id"))
        if target.type() != "paintlayer":
            raise ProtocolError("invalid_transaction_target", "target layer must be a paint layer")
        transaction_id = str(uuid.uuid4())
        candidate = document.createNode(f"{TRANSACTION_PREFIX}{transaction_id}", "paintlayer")
        parent = target.parentNode()
        if parent is None or not parent.addChildNode(candidate, target):
            raise ProtocolError("transaction_begin_failed", "could not attach candidate layer")
        document.setActiveNode(candidate)
        PROTOCOL_STATE.register_transaction(
            transaction_id,
            envelope["session_id"],
            document_id,
            candidate.uniqueId().toString(),
        )
        transaction = PROTOCOL_STATE.transactions[transaction_id]
        transaction["target_layer_id"] = target.uniqueId().toString()
        transaction["label"] = str(params.get("label") or "candidate")
        transaction["was_modified"] = was_modified
        document.refreshProjection()
        return {
            "document_id": document_id,
            "transaction_id": transaction_id,
            "candidate_layer_id": candidate.uniqueId().toString(),
        }

    def _transaction(self, params: dict, envelope: dict):
        document_id, document = self._document(envelope)
        transaction_id = params.get("transaction_id")
        transaction = PROTOCOL_STATE.require_transaction(
            transaction_id, envelope["session_id"], document_id
        )
        candidate = self._node(document, transaction["layer_id"])
        if candidate.name() != f"{TRANSACTION_PREFIX}{transaction_id}":
            raise ProtocolError("transaction_corrupt", "candidate layer identity changed")
        return document_id, document, transaction_id, transaction, candidate

    def _rollback_internal(self, transaction_id: str, document) -> None:
        transaction = PROTOCOL_STATE.transactions.get(transaction_id)
        if transaction is None:
            return
        candidate = document.nodeByUniqueID(QUuid(transaction["layer_id"]))
        PROTOCOL_STATE.finish_transaction(transaction_id)
        if candidate is None:
            return
        # Deferred removal via a zero-delay timer: candidate.remove() blocks on
        # the image scheduler (KisImage's projection-update workers holding
        # QReadWriteLock::lockForRead), and immediately after a heavy
        # paint_strokes batch the scheduler is saturated — the 2026-09-16
        # wedge signature (3/3 substrate rejections wedged the main thread in
        # the rollback path even though this handler itself is non-blocking).
        # Posting the removal to the event loop lets the scheduler drain
        # first, so the removal runs against an idle image. The document
        # stays dirty either way: the removal is a real state change.
        node_ref = candidate.uniqueId().toString()

        def _remove_later() -> None:
            try:
                doc = document.nodeByUniqueID(QUuid(node_ref))
                if doc is not None:
                    doc.remove()
            except Exception:
                pass  # a rollback is an abort path; never let the timer crash

        QTimer.singleShot(0, _remove_later)

    def _paint_strokes(self, params: dict, envelope: dict) -> dict:
        document_id, document, transaction_id, _transaction, candidate = self._transaction(
            params, envelope
        )
        view = None
        previous = None
        results = []
        trace_paths: list[Path] = []
        try:
            traced = bool(params.get("trace", False))
            strokes = validate_strokes(params.get("strokes"), traced)
            trace_directory = None
            if traced:
                raw_directory = Path(str(params.get("trace_directory") or ""))
                sentinel = PATH_GUARD.resolve_write(raw_directory / ".trace-sentinel")
                trace_directory = sentinel.parent
            view = self._active_view()
            previous = {
                "preset": view.currentBrushPreset(),
                "size": view.brushSize(),
                "opacity": view.paintingOpacity(),
                "flow": view.paintingFlow(),
                "blend": view.currentBlendingMode(),
                "eraser": view.eraserMode(),
                "disable_pressure": view.disablePressure(),
                "foreground": view.foregroundColor(),
            }
            for stroke in strokes:
                if trace_directory is None or stroke.get("erase"):
                    result = self._paint_one(document, candidate, view, stroke)
                else:
                    # Traced, non-erase: the trace is the byte diff of the
                    # candidate layer's pixelData bbox before/after the
                    # stroke. NO refreshProjection() here -- it blocks in
                    # KisImage::waitForDone behind a MODAL busy-wait dialog
                    # (2026-09-17 gdb backtrace:
                    # refreshProjection -> KisDelayedSaveDialog::blockIfImageIsBusy
                    # -> QDialog::exec) holding the GIL so the accept thread
                    # starves. The pixelData read never touches the
                    # projection. Client-side _trace_composite alpha-composites
                    # the diff over the accumulating canvas; build_stroke_gif's
                    # SSIM gate verifies equivalence against preview.png.
                    trace_path = trace_directory / f"{stroke['stroke_id']}.png"
                    result = self._paint_one(
                        document, candidate, view, stroke, trace_path=trace_path
                    )
                    if "trace_path" in result:
                        trace_paths.append(Path(result["trace_path"]))
                if trace_directory is not None and "trace_path" not in result:
                    # Fallback capture for eraser strokes (alpha-compositing
                    # cannot subtract paint from the canvas) and for any
                    # stroke whose pixelData diff failed. This is the ONLY
                    # place the projection is refreshed in the stroke path:
                    # once per stroke that actually needs it, not per stroke
                    # painted.
                    document.refreshProjection()
                    trace_path = trace_directory / f"{stroke['stroke_id']}.png"
                    capture = self._save_projection(
                        document, str(trace_path), result["bbox"], None
                    )
                    result["trace_path"] = capture["path"]
                    trace_paths.append(Path(capture["path"]))
                results.append(result)
            # One line per batch naming how many strokes actually put paint
            # down. A batch that renders nothing used to be completely silent
            # -- the bridge returned a geometry-derived bbox per stroke and
            # the client had no way to tell paint from no-op, so a run could
            # "succeed" at thousands of strokes while marking almost nothing.
            measured = [item for item in results if item.get("painted_pixels", -1) >= 0]
            if measured:
                empty = [item for item in measured if item["painted_pixels"] == 0]
                painted = sum(item["painted_pixels"] for item in measured)
                log_event(
                    "paint_strokes: %d/%d strokes marked nothing; painted_px=%d "
                    "mean_coverage=%.4f traced=%s"
                    % (
                        len(empty),
                        len(measured),
                        painted,
                        sum(item["coverage"] for item in measured) / len(measured),
                        traced,
                    )
                )
                if empty:
                    sample = empty[:5]
                    log_event(
                        "paint_strokes: empty stroke sample %s"
                        % [
                            {
                                "stroke_id": item["stroke_id"],
                                "bbox": item["bbox"],
                                "preset": item.get("effective_brush_fingerprint", "")[:24],
                            }
                            for item in sample
                        ]
                    )
        except Exception:
            self._rollback_internal(transaction_id, document)
            for path in trace_paths:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        finally:
            if view is not None and previous is not None and previous["preset"] is not None:
                view.setCurrentBrushPreset(previous["preset"])
            if view is not None and previous is not None:
                view.setBrushSize(previous["size"])
                view.setPaintingOpacity(previous["opacity"])
                view.setPaintingFlow(previous["flow"])
                view.setCurrentBlendingMode(previous["blend"])
                view.setEraserMode(previous["eraser"])
                view.setDisablePressure(previous["disable_pressure"])
                view.setForeGroundColor(previous["foreground"])
        return {
            "document_id": document_id,
            "transaction_id": transaction_id,
            "strokes": results,
        }

    def _commit_transaction(self, params: dict, envelope: dict) -> dict:
        document_id, document, transaction_id, transaction, candidate = self._transaction(
            params, envelope
        )
        mode = params.get("mode", "merge")
        if mode == "merge":
            # Node.mergeDown() reliably returns None for a candidate nested
            # inside a semantic group in this Krita build, even though the
            # layer stack is structurally normal. Triggering Krita's own
            # merge_layer action operates on the same active-node context
            # through the application's real merge pipeline and succeeds
            # where the low-level Node API does not.
            document.refreshProjection()
            document.waitForDone()
            document.setActiveNode(candidate)
            action = self._app().action("merge_layer")
            if action is None:
                raise ProtocolError("transaction_commit_failed", "merge_layer action unavailable")
            action.trigger()
            document.waitForDone()
            replacement = document.activeNode()
            if replacement is None or replacement.name().startswith(TRANSACTION_PREFIX):
                raise ProtocolError("transaction_commit_failed", "candidate mergeDown failed")
            replacement_id = replacement.uniqueId().toString()
        elif mode == "retain":
            candidate.setName(str(params.get("name") or transaction.get("label") or "Candidate"))
            replacement_id = candidate.uniqueId().toString()
        else:
            raise ProtocolError("invalid_commit_mode", "commit mode must be merge or retain")
        PROTOCOL_STATE.finish_transaction(transaction_id)
        document.refreshProjection()
        return {
            "document_id": document_id,
            "transaction_id": transaction_id,
            "replacement_node_id": replacement_id,
            "mode": mode,
        }

    def _rollback_transaction(self, params: dict, envelope: dict) -> dict:
        document_id, document, transaction_id, _transaction, _candidate = self._transaction(
            params, envelope
        )
        self._rollback_internal(transaction_id, document)
        return {
            "document_id": document_id,
            "transaction_id": transaction_id,
            "rolled_back": True,
        }

    def _capture_region(self, params: dict, envelope: dict) -> dict:
        document_id, document = self._document(envelope)
        bbox = validate_bbox(params.get("bbox"))
        # The projection MUST be current or the caller reads a stale canvas.
        # AutoPainter's critic compares a before/after capture pair; with
        # tracing off nothing refreshed, so it judged two identical images and
        # rejected every candidate. Measured 2026-09-16: the form_value_planes
        # phase was rejected 60/60 with improvement ~0.00003 against a 0.001
        # gate, and contributed zero strokes to the finished painting.
        #
        # One refresh per capture, NOT per stroke: the per-traced-stroke
        # refresh that used to live in _paint_strokes ran thousands of times a
        # run and is what starved the accept loop. A capture happens once per
        # candidate, so the exposure is ~1/42 of that.
        document.refreshProjection()
        document.waitForDone()
        capture = self._save_projection(
            document,
            params.get("path", ""),
            bbox,
            params.get("max_side"),
        )
        return {"document_id": document_id, **capture}

    def _render_brush_probe(self, params: dict, envelope: dict) -> dict:
        document_id, document = self._document(envelope)
        # A probe adds a layer, paints into it, captures, and removes it again,
        # so it leaves the document content-identical -- but Krita still marks
        # it dirty. Calibration runs ~144 of these against whatever document is
        # active, which is how a canvas nobody edited ends up prompting "save
        # changes?" on close. Restore whatever the flag was on entry; real user
        # edits made before the probe are preserved by capturing, not clearing.
        was_modified = document.modified()
        preset_id = params.get("preset_id")
        size = float(params.get("size", 64.0))
        layer = document.createNode(f"{TRANSACTION_PREFIX}probe-{uuid.uuid4()}", "paintlayer")
        target = document.activeNode()
        parent = target.parentNode() if target is not None else document.rootNode()
        if not parent.addChildNode(layer, target):
            raise ProtocolError("probe_failed", "could not attach probe layer")
        view = self._active_view()
        previous = {
            "preset": view.currentBrushPreset(),
            "size": view.brushSize(),
            "opacity": view.paintingOpacity(),
            "flow": view.paintingFlow(),
            "blend": view.currentBlendingMode(),
            "eraser": view.eraserMode(),
            "disable_pressure": view.disablePressure(),
            "foreground": view.foregroundColor(),
        }
        try:
            width, height = document.width(), document.height()
            margin = max(8.0, size)
            results = []
            for index, pressure in enumerate((0.2, 0.4, 0.6, 0.8, 1.0)):
                y = margin + index * max(size * 1.5, 24.0)
                stroke = {
                    "stroke_id": f"probe-{index}",
                    "preset_id": preset_id,
                    "size": size,
                    "opacity": 1.0,
                    "flow": 1.0,
                    "blend_mode": "normal",
                    "foreground_rgba": [20, 20, 20, 255],
                    "erase": False,
                    "geometry": {
                        "type": "line",
                        "start": [margin, min(height - margin, y)],
                        "end": [max(margin, width - margin), min(height - margin, y)],
                    },
                    "pressure": {"start": pressure, "end": pressure},
                }
                results.append(self._paint_one(document, layer, view, stroke))
            # document.projection() with no bbox returns a null QImage in
            # this Krita build; an explicit full-canvas rect works correctly.
            capture = self._save_projection(
                document, params.get("path", ""), [0, 0, width, height], params.get("max_side", 1280)
            )
        finally:
            layer.remove()
            document.refreshProjection()
            # remove() and refreshProjection() are asynchronous: the image
            # scheduler finishes the removal and re-marks the document dirty
            # *after* the call returns. Without this flush, the setModified
            # below lands too early and is immediately undone -- measured, the
            # document still read modified=True on the next command.
            document.waitForDone()
            if previous["preset"] is not None:
                view.setCurrentBrushPreset(previous["preset"])
            view.setBrushSize(previous["size"])
            view.setPaintingOpacity(previous["opacity"])
            view.setPaintingFlow(previous["flow"])
            view.setCurrentBlendingMode(previous["blend"])
            view.setEraserMode(previous["eraser"])
            view.setDisablePressure(previous["disable_pressure"])
            view.setForeGroundColor(previous["foreground"])
            # Last, so nothing above can re-dirty the document.
            document.setModified(was_modified)
        return {
            "document_id": document_id,
            "preset_id": preset_id,
            "probe_strokes": results,
            **capture,
        }


Krita.instance().addExtension(KritaMCPExtension(Krita.instance()))
