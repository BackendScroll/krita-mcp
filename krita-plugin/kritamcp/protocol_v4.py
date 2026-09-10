"""Pure protocol-v4 validation and session state for the Krita bridge.

This module intentionally has no Krita or Qt imports.  It is shared by the
embedded plugin and exercised by ordinary Python contract/security tests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, Callable, Iterable


PROTOCOL_VERSION = 4
PLUGIN_VERSION = "v4"
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_QUEUE_DEPTH = 64
MAX_TRACED_STROKES = 32
MAX_UNTRACED_STROKES = 64
SESSION_TIMEOUT_SECONDS = 120.0

READ_ACTIONS = frozenset(
    {
        "get_capabilities",
        "get_state",
        "list_layers",
        "list_brushes",
        "capture_region",
    }
)
WRITE_ACTIONS = frozenset(
    {
        "create_document",
        "open_document",
        "save_document",
        "export_document",
        "close_document",
        "create_layer",
        "update_layer",
        "delete_layer",
        "set_selection_from_mask",
        "clear_selection",
        "render_brush_probe",
        "begin_paint_transaction",
        "paint_strokes",
        "commit_paint_transaction",
        "rollback_paint_transaction",
    }
)
ACTIONS = READ_ACTIONS | WRITE_ACTIONS

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEX_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_UNSUPPORTED_SENSORS = frozenset({"tilt", "rotation", "speed"})


class ProtocolError(Exception):
    """A safe, typed error suitable for returning over HTTP/MCP."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable
        self.http_status = http_status

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }


def error_response(request_id: str | None, error: ProtocolError) -> dict[str, Any]:
    return {
        "ok": False,
        "request_id": request_id,
        "error": error.as_dict(),
    }


def _require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ProtocolError(
            "invalid_request",
            f"{field} must be a non-empty ASCII identifier of at most 128 characters",
            details={"field": field},
        )
    return value


def validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("invalid_request", "request body must be a JSON object")
    if value.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError(
            "protocol_mismatch",
            "only Krita MCP protocol version 4 is accepted",
            details={
                "expected": PROTOCOL_VERSION,
                "received": value.get("protocol_version"),
            },
        )

    request_id = _require_id(value.get("request_id"), "request_id")
    session_id = _require_id(value.get("session_id"), "session_id")
    document_id = value.get("document_id")
    if document_id is not None:
        document_id = _require_id(document_id, "document_id")
    action = value.get("action")
    if action not in ACTIONS:
        raise ProtocolError(
            "unknown_action",
            f"unsupported v4 action: {action!r}",
            details={"supported_actions": sorted(ACTIONS)},
        )
    params = value.get("params")
    if not isinstance(params, dict):
        raise ProtocolError("invalid_request", "params must be a JSON object")

    expected_revision = value.get("expected_revision")
    if action in WRITE_ACTIONS:
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ProtocolError(
                "expected_revision_required",
                "mutating v4 actions require an integer expected_revision",
            )
        if expected_revision < 0:
            raise ProtocolError("invalid_request", "expected_revision cannot be negative")
    elif expected_revision is not None and (
        not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
    ):
        raise ProtocolError("invalid_request", "expected_revision must be an integer")

    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "session_id": session_id,
        "document_id": document_id,
        "expected_revision": expected_revision,
        "action": action,
        "params": params,
    }


def parse_json_body(body: bytes, declared_length: int | None = None) -> dict[str, Any]:
    if declared_length is not None and declared_length > MAX_BODY_BYTES:
        raise ProtocolError(
            "body_too_large",
            f"request body exceeds {MAX_BODY_BYTES} bytes",
            http_status=413,
        )
    if len(body) > MAX_BODY_BYTES:
        raise ProtocolError(
            "body_too_large",
            f"request body exceeds {MAX_BODY_BYTES} bytes",
            http_status=413,
        )
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid_json", "request body is not valid UTF-8 JSON") from exc
    return validate_request(value)


def _point(value: Any, field: str) -> tuple[float, float]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(not isinstance(item, (int, float)) or isinstance(item, bool) for item in value)
    ):
        raise ProtocolError("invalid_geometry", f"{field} must be [x, y]")
    return float(value[0]), float(value[1])


def _pressure(value: Any) -> tuple[float, float]:
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise ProtocolError(
            "invalid_pressure",
            "line pressure must contain exactly start and end",
        )
    start, end = value["start"], value["end"]
    if any(
        not isinstance(item, (int, float))
        or isinstance(item, bool)
        or not 0.0 <= float(item) <= 1.0
        for item in (start, end)
    ):
        raise ProtocolError("invalid_pressure", "line pressure values must be in [0, 1]")
    return float(start), float(end)


def validate_strokes(strokes: Any, traced: bool) -> list[dict[str, Any]]:
    if not isinstance(strokes, list) or not strokes:
        raise ProtocolError("invalid_strokes", "strokes must be a non-empty array")
    limit = MAX_TRACED_STROKES if traced else MAX_UNTRACED_STROKES
    if len(strokes) > limit:
        raise ProtocolError(
            "batch_limit",
            f"at most {limit} {'traced' if traced else 'untraced'} strokes are allowed",
            details={"limit": limit, "received": len(strokes), "traced": traced},
        )

    normalized: list[dict[str, Any]] = []
    for index, stroke in enumerate(strokes):
        if not isinstance(stroke, dict):
            raise ProtocolError("invalid_strokes", "each stroke must be an object")
        required_settings = {
            "stroke_id",
            "preset_id",
            "size",
            "opacity",
            "flow",
            "blend_mode",
            "foreground_rgba",
            "erase",
        }
        missing = sorted(required_settings.difference(stroke))
        if missing:
            raise ProtocolError(
                "invalid_brush_settings",
                "every stroke must provide explicit brush settings",
                details={"stroke_index": index, "missing": missing},
            )
        _require_id(stroke["stroke_id"], "stroke_id")
        if not isinstance(stroke["preset_id"], str) or not stroke["preset_id"]:
            raise ProtocolError("invalid_brush_settings", "preset_id cannot be empty")
        for field, lower, upper in (
            ("size", 0.1, 4000.0),
            ("opacity", 0.0, 1.0),
            ("flow", 0.0, 1.0),
        ):
            value = stroke[field]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not lower <= float(value) <= upper
            ):
                raise ProtocolError(
                    "invalid_brush_settings",
                    f"{field} must be in [{lower}, {upper}]",
                    details={"stroke_index": index, "field": field},
                )
        if not isinstance(stroke["blend_mode"], str) or not stroke["blend_mode"]:
            raise ProtocolError("invalid_brush_settings", "blend_mode cannot be empty")
        rgba = stroke["foreground_rgba"]
        if (
            not isinstance(rgba, (list, tuple))
            or len(rgba) != 4
            or any(not isinstance(channel, int) or isinstance(channel, bool) or not 0 <= channel <= 255 for channel in rgba)
        ):
            raise ProtocolError(
                "invalid_brush_settings",
                "foreground_rgba must contain four integer channels in [0, 255]",
                details={"stroke_index": index},
            )
        if not isinstance(stroke["erase"], bool):
            raise ProtocolError("invalid_brush_settings", "erase must be boolean")
        present_sensors = sorted(_UNSUPPORTED_SENSORS.intersection(stroke))
        if present_sensors:
            raise ProtocolError(
                "unsupported_sensor",
                "tilt, rotation, and speed are not supported by the Krita Node API",
                details={"stroke_index": index, "sensors": present_sensors},
            )
        geometry = stroke.get("geometry")
        if not isinstance(geometry, dict):
            raise ProtocolError("invalid_geometry", "stroke geometry must be an object")
        geometry_type = geometry.get("type")
        item = dict(stroke)
        if geometry_type == "line":
            start = _point(geometry.get("start"), "geometry.start")
            end = _point(geometry.get("end"), "geometry.end")
            pressure = _pressure(stroke.get("pressure"))
            item["geometry"] = {"type": "line", "start": start, "end": end}
            item["pressure"] = {"start": pressure[0], "end": pressure[1]}
        elif geometry_type == "cubic":
            if "pressure" in stroke:
                raise ProtocolError(
                    "unsupported_sensor",
                    "cubic paintPath strokes do not accept pressure",
                    details={"stroke_index": index, "sensor": "pressure"},
                )
            if stroke.get("fixed_pressure") is not True:
                raise ProtocolError(
                    "unsupported_sensor",
                    "cubic paintPath strokes require a calibrated fixed-pressure preset",
                    details={"stroke_index": index, "sensor": "pressure"},
                )
            start = _point(geometry.get("start"), "geometry.start")
            commands = geometry.get("commands")
            if not isinstance(commands, list) or not commands:
                raise ProtocolError(
                    "invalid_geometry", "cubic geometry requires at least one command"
                )
            normalized_commands = []
            for command in commands:
                if not isinstance(command, dict):
                    raise ProtocolError("invalid_geometry", "cubic command must be an object")
                normalized_commands.append(
                    {
                        "control1": _point(command.get("control1"), "control1"),
                        "control2": _point(command.get("control2"), "control2"),
                        "end": _point(command.get("end"), "end"),
                    }
                )
            item["geometry"] = {
                "type": "cubic",
                "start": start,
                "commands": normalized_commands,
            }
        else:
            raise ProtocolError(
                "invalid_geometry", "geometry.type must be 'line' or 'cubic'"
            )
        normalized.append(item)
    return normalized


def brush_fingerprint(
    resource_type: str,
    name: str,
    filename: str | None,
    file_hash: str | None,
) -> str:
    identity = "\0".join(
        (resource_type, name, filename or "", (file_hash or "unreadable").lower())
    ).encode("utf-8")
    return f"{resource_type}:{hashlib.sha256(identity).hexdigest()}"


def ensure_token(path: str | os.PathLike[str]) -> str:
    """Create a 256-bit bearer token atomically and keep it mode 0600."""
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        existing = target.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        if not _HEX_TOKEN_RE.fullmatch(existing):
            raise ProtocolError(
                "invalid_token_file",
                "the Krita MCP token file is malformed; remove it and restart Krita",
            )
        os.chmod(target, 0o600)
        return existing

    token = secrets.token_hex(32)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return token


class PathGuard:
    """Resolve paths and confine them to explicit read/write roots."""

    def __init__(
        self,
        read_roots: Iterable[str | os.PathLike[str]],
        write_roots: Iterable[str | os.PathLike[str]],
    ) -> None:
        self.read_roots = tuple(Path(root).expanduser().resolve() for root in read_roots)
        self.write_roots = tuple(Path(root).expanduser().resolve() for root in write_roots)
        if not self.read_roots or not self.write_roots:
            raise ValueError("at least one read root and one write root are required")

    @staticmethod
    def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
        return any(path == root or root in path.parents for root in roots)

    def _resolve(self, value: str | os.PathLike[str], roots: tuple[Path, ...]) -> Path:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raise ProtocolError("path_not_absolute", "paths must be absolute")
        resolved = raw.resolve(strict=False)
        if not self._inside(resolved, roots):
            raise ProtocolError(
                "path_outside_roots",
                "resolved path is outside the configured roots",
                details={"path": str(raw)},
            )
        return resolved

    def resolve_read(self, value: str | os.PathLike[str]) -> Path:
        resolved = self._resolve(value, self.read_roots)
        if not resolved.is_file():
            raise ProtocolError("path_not_found", "input file does not exist")
        return resolved

    def resolve_write(self, value: str | os.PathLike[str]) -> Path:
        resolved = self._resolve(value, self.write_roots)
        parent = resolved.parent
        if not parent.exists() or not parent.is_dir():
            raise ProtocolError(
                "parent_not_found", "output parent directory must already exist"
            )
        return resolved


class ProtocolState:
    """Session writer lease, monotonic revision, idempotency, and transactions."""

    def __init__(self, token: str, session_timeout: float = SESSION_TIMEOUT_SECONDS) -> None:
        if not _HEX_TOKEN_RE.fullmatch(token):
            raise ValueError("token must be 64 lowercase hexadecimal characters")
        self._token = token
        self.session_timeout = float(session_timeout)
        self.revision = 0
        self.writer_session: str | None = None
        self.writer_last_seen = 0.0
        self.transactions: dict[str, dict[str, Any]] = {}
        self._responses: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
        self._lock = threading.RLock()

    def authenticate(self, authorization: str | None) -> None:
        prefix = "Bearer "
        supplied = authorization[len(prefix) :] if authorization and authorization.startswith(prefix) else ""
        if not supplied or not hmac.compare_digest(supplied, self._token):
            raise ProtocolError(
                "unauthorized", "a valid bearer token is required", http_status=401
            )

    def _expire_writer(self) -> None:
        if (
            self.writer_session is not None
            and time.monotonic() - self.writer_last_seen > self.session_timeout
        ):
            self.release_session(self.writer_session)

    def obtain_writer(self, session_id: str) -> None:
        with self._lock:
            self._expire_writer()
            if self.writer_session not in (None, session_id):
                raise ProtocolError(
                    "writer_busy",
                    "another bridge session owns the Krita writer lease",
                    details={"owner": self.writer_session},
                    retryable=True,
                    http_status=409,
                )
            self.writer_session = session_id
            self.writer_last_seen = time.monotonic()

    def touch(self, session_id: str) -> None:
        with self._lock:
            if self.writer_session == session_id:
                self.writer_last_seen = time.monotonic()

    def release_session(self, session_id: str) -> None:
        with self._lock:
            if self.writer_session == session_id:
                self.writer_session = None
                self.writer_last_seen = 0.0
            for transaction_id in [
                key
                for key, value in self.transactions.items()
                if value["session_id"] == session_id
            ]:
                self.transactions.pop(transaction_id, None)

    def register_transaction(
        self, transaction_id: str, session_id: str, document_id: str, layer_id: str
    ) -> None:
        with self._lock:
            self.transactions[transaction_id] = {
                "session_id": session_id,
                "document_id": document_id,
                "layer_id": layer_id,
            }

    def require_transaction(
        self, transaction_id: str, session_id: str, document_id: str
    ) -> dict[str, Any]:
        with self._lock:
            transaction = self.transactions.get(transaction_id)
            if transaction is None:
                raise ProtocolError("transaction_not_found", "paint transaction not found")
            if transaction["session_id"] != session_id:
                raise ProtocolError(
                    "transaction_owner_mismatch",
                    "paint transaction belongs to another session",
                    http_status=409,
                )
            if transaction["document_id"] != document_id:
                raise ProtocolError(
                    "transaction_document_mismatch",
                    "paint transaction belongs to another document",
                    http_status=409,
                )
            return dict(transaction)

    def finish_transaction(self, transaction_id: str) -> None:
        with self._lock:
            self.transactions.pop(transaction_id, None)

    @staticmethod
    def _digest(request: dict[str, Any]) -> str:
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def dispatch(
        self,
        raw_request: dict[str, Any],
        execute: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        request = validate_request(raw_request)
        request_id = request["request_id"]
        session_id = request["session_id"]
        mutating = request["action"] in WRITE_ACTIONS
        digest = self._digest(request)

        with self._lock:
            if mutating:
                cached = self._responses.get(session_id, {}).get(request_id)
                if cached is not None:
                    cached_digest, response = cached
                    if cached_digest != digest:
                        raise ProtocolError(
                            "request_id_conflict",
                            "request_id was already used with a different request",
                            http_status=409,
                        )
                    return dict(response)

                self.obtain_writer(session_id)
                if request["expected_revision"] != self.revision:
                    raise ProtocolError(
                        "revision_conflict",
                        "expected_revision does not match the bridge revision",
                        details={
                            "expected": request["expected_revision"],
                            "actual": self.revision,
                        },
                        retryable=True,
                        http_status=409,
                    )

            result = execute(request["action"], request["params"])
            if not isinstance(result, dict):
                raise ProtocolError(
                    "internal_error",
                    "bridge action returned an invalid result",
                    http_status=500,
                )
            if mutating:
                self.revision += 1
                self.touch(session_id)
            response = {
                "ok": True,
                "request_id": request_id,
                "document_revision": self.revision,
                "result": result,
                "warnings": [],
            }
            if mutating:
                self._responses.setdefault(session_id, {})[request_id] = (
                    digest,
                    dict(response),
                )
            return response
