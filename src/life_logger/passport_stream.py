"""AI Passport same-LAN PCM stream receiver compatible with the firmware PoC."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import threading
import wave
import zlib
from collections.abc import Callable, Mapping
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")
MAX_CHUNK_INDEX = 1_000_000
CAPTURE_SOURCE = "ai-passport-wifi"
PASSPORT_STREAM_PORT = 8766
PASSPORT_DISCOVERY_PORT = 8767
PASSPORT_STALE_SESSION_SECONDS = 30


def receiver_discovery_response(request: bytes, receiver_port: int) -> bytes | None:
    """Reply to the nonce-based UDP discovery packet emitted by the firmware."""
    if not 1 <= receiver_port <= 65535:
        return None
    try:
        fields = request.decode("ascii").split()
    except UnicodeDecodeError:
        return None
    if (
        len(fields) != 2
        or fields[0] != "LL_DISCOVER_V1"
        or re.fullmatch(r"[0-9A-Fa-f]{8}", fields[1]) is None
    ):
        return None
    return f"LL_RECEIVER_V1 {fields[1]} {receiver_port}".encode("ascii")


def start_receiver_discovery(
    *, host: str, discovery_port: int, receiver_port: int
) -> tuple[threading.Event, threading.Thread, int]:
    """Bind the LAN discovery socket before returning, then serve it in a thread."""
    discovery = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        discovery.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        discovery.bind((host, discovery_port))
        discovery.settimeout(0.25)
    except OSError:
        discovery.close()
        raise

    stop = threading.Event()

    def serve() -> None:
        with discovery:
            while not stop.is_set():
                try:
                    request, peer = discovery.recvfrom(128)
                except socket.timeout:
                    continue
                except OSError:
                    if stop.is_set():
                        return
                    raise
                response = receiver_discovery_response(request, receiver_port)
                if response is not None:
                    try:
                        discovery.sendto(response, peer)
                    except OSError:
                        if not stop.is_set():
                            raise

    thread = threading.Thread(target=serve, name="passport-discovery", daemon=True)
    thread.start()
    return stop, thread, int(discovery.getsockname()[1])


def _iso() -> str:
    return datetime.now().astimezone().isoformat()


def safe_id(value: str, fallback: str) -> str:
    cleaned = SAFE_ID.sub("_", str(value or "")).strip("._")
    return cleaned[:80] or fallback


def crc32_header(body: bytes) -> str:
    return f"crc32:{zlib.crc32(body) & 0xFFFFFFFF:08x}"


class SessionState:
    """Track transport ordering without treating missing audio as complete."""

    def __init__(self) -> None:
        self.seen: dict[int, str] = {}
        self.next_expected = 0
        self.duplicates = 0
        self.out_of_order = 0

    def observe_chunk(self, index: int, checksum: str) -> tuple[bool, bool]:
        if index < 0 or index > MAX_CHUNK_INDEX:
            raise ValueError("chunk index out of range")
        previous = self.seen.get(index)
        if previous is not None:
            if previous != checksum:
                raise ValueError("duplicate chunk has different checksum")
            self.duplicates += 1
            return True, False

        out_of_order = index != self.next_expected
        if out_of_order:
            self.out_of_order += 1
        self.seen[index] = checksum
        while self.next_expected in self.seen:
            self.next_expected += 1
        return False, out_of_order

    def summary(self, expected_count: int) -> dict[str, Any]:
        if expected_count < 0 or expected_count > MAX_CHUNK_INDEX:
            raise ValueError("expected chunk count out of range")
        missing = [index for index in range(expected_count) if index not in self.seen]
        unexpected = sorted(index for index in self.seen if index >= expected_count)
        return {
            "expected_chunks": expected_count,
            "received_unique_chunks": len(self.seen),
            "missing_chunks": missing,
            "unexpected_chunks": unexpected,
            "duplicate_chunks": self.duplicates,
            "out_of_order_chunks": self.out_of_order,
            "complete": not missing and not unexpected,
        }


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """原子写：唯一临时名（mkstemp）+ fsync(file/dir) + os.replace，统一走 life_logger.atomic。

    【2026-10-29】原实现用固定的 `<name>.tmp`：两个进程同时写同一目标时会互相顶掉对方的
    临时文件，后 replace 的一方拿到 FileNotFoundError（实测 30 轮 27 次），或静默丢写。
    """
    from life_logger.atomic import write_bytes_atomic

    text = json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n"
    write_bytes_atomic(path, text.encode("utf-8"))


def assemble_wav(session_dir: Path, chunk_count: int, sample_rate: int) -> Path | None:
    if chunk_count == 0:
        return None
    if chunk_count < 0 or sample_rate <= 0:
        raise ValueError("invalid recording length or sample rate")

    target = session_dir / "recording.wav"
    temporary = session_dir / "recording.wav.tmp"
    try:
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            for index in range(chunk_count):
                body = (session_dir / f"chunk-{index:08d}.s16le").read_bytes()
                if not body or len(body) % 2:
                    raise ValueError(f"invalid audio chunk {index}")
                output.writeframesraw(body)
        temporary.replace(target)
        return target
    except (OSError, ValueError):
        temporary.unlink(missing_ok=True)
        raise


def _manifest_records(session_dir: Path) -> list[dict[str, Any]]:
    path = session_dir / "manifest.jsonl"
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _event_count(root: Path, device: str, session: str) -> int:
    path = root / device / "events.jsonl"
    if not path.is_file():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("session") == session:
            count += 1
    return count


def _session_activity_age_seconds(
    updated_at: str,
    reference_now: datetime,
) -> float | None:
    try:
        activity = datetime.fromisoformat(updated_at)
    except ValueError:
        return None
    if activity.tzinfo is None:
        activity = activity.replace(tzinfo=reference_now.tzinfo)
    age = (reference_now - activity).total_seconds()
    return max(0.0, age)


def list_passport_sessions(
    root: Path,
    *,
    limit: int = 50,
    reference_now: datetime | None = None,
    stale_after_seconds: float = PASSPORT_STALE_SESSION_SECONDS,
) -> list[dict[str, Any]]:
    """Return receiver sessions with stalled streams distinguished from live ones."""

    root = Path(root)
    if not root.is_dir():
        return []
    now = reference_now or datetime.now().astimezone()
    sessions: list[dict[str, Any]] = []
    for device_dir in root.iterdir():
        if not device_dir.is_dir():
            continue
        for session_dir in device_dir.iterdir():
            if not session_dir.is_dir():
                continue
            summary_path = session_dir / "summary.json"
            summary: dict[str, Any] = {}
            if summary_path.is_file():
                try:
                    loaded = json.loads(summary_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    loaded = {}
                if isinstance(loaded, dict):
                    summary = loaded
            records = _manifest_records(session_dir)
            audio_records = [
                item
                for item in records
                if item.get("type") == "audio" and not item.get("end")
            ]
            updated_at = str(
                summary.get("updated_at")
                or (records[-1].get("received_at") if records else "")
                or datetime.fromtimestamp(session_dir.stat().st_mtime)
                .astimezone()
                .isoformat()
            )
            activity_age_seconds = _session_activity_age_seconds(updated_at, now)
            stalled = bool(
                not summary_path.is_file()
                and audio_records
                and activity_age_seconds is not None
                and activity_age_seconds >= max(1.0, float(stale_after_seconds))
            )
            sessions.append(
                {
                    "capture_source": CAPTURE_SOURCE,
                    "device": str(summary.get("device") or device_dir.name),
                    "device_label": "AI Passport",
                    "session": str(summary.get("session") or session_dir.name),
                    "status": (
                        "event"
                        if summary.get("event_only")
                        else "complete"
                        if summary.get("complete")
                        else "incomplete"
                        if summary_path.is_file()
                        else "stalled"
                        if stalled
                        else "receiving"
                    ),
                    "complete": bool(summary.get("complete")),
                    "expected_chunks": summary.get("expected_chunks"),
                    "received_unique_chunks": summary.get(
                        "received_unique_chunks",
                        len({item.get("chunk") for item in audio_records}),
                    ),
                    "missing_chunks": summary.get("missing_chunks"),
                    "unexpected_chunks": summary.get("unexpected_chunks"),
                    "duplicate_chunks": summary.get("duplicate_chunks", 0),
                    "out_of_order_chunks": summary.get("out_of_order_chunks", 0),
                    "sample_rate": summary.get("sample_rate"),
                    "format": summary.get("format"),
                    "received_started_at": summary.get("received_started_at"),
                    "updated_at": updated_at,
                    "activity_age_seconds": (
                        round(activity_age_seconds, 1)
                        if activity_age_seconds is not None
                        else None
                    ),
                    "stale_after_seconds": max(1.0, float(stale_after_seconds)),
                    "recording_available": bool(
                        summary.get("complete")
                        and (session_dir / "recording.wav").is_file()
                    ),
                    "job_id": summary.get("job_id"),
                    "import_error": summary.get("import_error"),
                    "event_count": _event_count(
                        root,
                        device_dir.name,
                        session_dir.name,
                    ),
                }
            )
    sessions.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return sessions[: max(0, int(limit))]


CompleteCallback = Callable[[Path, dict[str, Any]], str | None]


class PassportStreamStore:
    """Persist firmware-compatible chunks and optionally import complete WAVs."""

    def __init__(
        self,
        root: Path,
        *,
        on_complete: CompleteCallback | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.on_complete = on_complete
        self._lock = threading.RLock()
        self._states: dict[tuple[str, str], SessionState] = {}

    def _session_dir(self, device: str, session: str) -> Path:
        path = self.root / device / session
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _load_state(self, device: str, session: str) -> SessionState:
        key = (device, session)
        existing = self._states.get(key)
        if existing is not None:
            return existing
        state = SessionState()
        session_dir = self._session_dir(device, session)
        for record in _manifest_records(session_dir):
            if record.get("type") != "audio" or record.get("end"):
                continue
            try:
                index = int(record["chunk"])
                checksum = str(record["checksum"])
            except (KeyError, TypeError, ValueError):
                continue
            if index in state.seen:
                if state.seen[index] == checksum:
                    state.duplicates += 1
                continue
            state.seen[index] = checksum
        while state.next_expected in state.seen:
            state.next_expected += 1
        self._states[key] = state
        return state

    def receive(
        self,
        request_path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, Any]]:
        if request_path not in {"/v1/audio/chunk", "/v1/audio/end"}:
            return HTTPStatus.NOT_FOUND, {"error": "not found"}

        try:
            session = safe_id(headers.get("X-LifeLogger-Session", ""), "unknown")
            device = safe_id(headers.get("X-LifeLogger-Device", ""), "device")
            chunk = int(headers.get("X-LifeLogger-Chunk", "0"))
        except ValueError:
            return HTTPStatus.BAD_REQUEST, {"error": "invalid stream headers"}
        if chunk < 0 or chunk > MAX_CHUNK_INDEX:
            return HTTPStatus.BAD_REQUEST, {"error": "invalid stream counters"}

        checksum = str(headers.get("X-LifeLogger-Checksum", "")).lower()
        if checksum != crc32_header(body):
            return HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "checksum mismatch"}

        stream_type = str(headers.get("X-LifeLogger-Type", "audio")).lower()
        if stream_type == "event":
            if body:
                return HTTPStatus.BAD_REQUEST, {"error": "event body must be empty"}
            event_name = safe_id(headers.get("X-LifeLogger-Event", ""), "event")
            record = {
                "capture_source": CAPTURE_SOURCE,
                "device": device,
                "session": session,
                "type": "event",
                "event": event_name,
                "event_time_ms": headers.get("X-LifeLogger-Event-Time-Ms", ""),
                "checksum": checksum,
                "checksum_ok": True,
                "received_at": _iso(),
            }
            device_dir = self.root / device
            device_dir.mkdir(parents=True, exist_ok=True)
            session_dir = self._session_dir(device, session)
            with self._lock:
                events_path = device_dir / "events.jsonl"
                with events_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                has_audio = any(
                    item.get("type") == "audio" and not item.get("end")
                    for item in _manifest_records(session_dir)
                )
                if not has_audio:
                    summary_path = session_dir / "summary.json"
                    summary_payload = {
                        "schema_version": "ai-passport-stream-session-v1",
                        "capture_source": CAPTURE_SOURCE,
                        "device": device,
                        "device_label": "AI Passport",
                        "session": session,
                        "event_only": True,
                        "complete": False,
                        "updated_at": record["received_at"],
                    }
                    write_json_atomic(summary_path, summary_payload)
            return HTTPStatus.OK, {"ok": True, **record}
        if stream_type != "audio":
            return HTTPStatus.BAD_REQUEST, {"error": "unsupported stream type"}

        try:
            sample_rate = int(headers.get("X-LifeLogger-Sample-Rate", "0"))
        except ValueError:
            return HTTPStatus.BAD_REQUEST, {"error": "invalid sample rate"}
        stream_format = str(headers.get("X-LifeLogger-Format", ""))
        end_marker = (
            request_path == "/v1/audio/end"
            or str(headers.get("X-LifeLogger-End", "0")) == "1"
        )
        if end_marker and body:
            return HTTPStatus.BAD_REQUEST, {
                "error": "end marker must have an empty body"
            }
        if not end_marker and (not body or len(body) % 2):
            return HTTPStatus.BAD_REQUEST, {"error": "invalid PCM chunk size"}
        if stream_format != "s16le" or sample_rate <= 0:
            return HTTPStatus.BAD_REQUEST, {"error": "unsupported audio format"}

        session_dir = self._session_dir(device, session)
        received_at = _iso()
        digest = hashlib.sha256(body).hexdigest()
        with self._lock:
            state = self._load_state(device, session)
            duplicate = False
            out_of_order = False
            summary: dict[str, Any] | None = None
            if end_marker:
                try:
                    summary = state.summary(chunk)
                except ValueError as error:
                    return HTTPStatus.BAD_REQUEST, {"error": str(error)}
            else:
                try:
                    duplicate, out_of_order = state.observe_chunk(chunk, checksum)
                except ValueError as error:
                    return HTTPStatus.CONFLICT, {"error": str(error)}

            record = {
                "capture_source": CAPTURE_SOURCE,
                "device": device,
                "session": session,
                "type": "audio",
                "chunk": chunk,
                "bytes": len(body),
                "sha256": digest,
                "checksum": checksum,
                "checksum_ok": True,
                "sample_rate": sample_rate,
                "format": stream_format,
                "start_ms": headers.get("X-LifeLogger-Chunk-Start-Ms", ""),
                "end": end_marker,
                "duplicate": duplicate,
                "out_of_order": out_of_order,
                "received_at": received_at,
            }
            manifest_path = session_dir / "manifest.jsonl"
            with manifest_path.open("a", encoding="utf-8") as manifest:
                manifest.write(json.dumps(record, ensure_ascii=False) + "\n")

            if body and not duplicate:
                target = session_dir / f"chunk-{chunk:08d}.s16le"
                # 唯一临时名 + fsync + replace（重复上传同一 chunk 时不再互相踩 tmp）
                from life_logger.atomic import write_bytes_atomic

                write_bytes_atomic(target, body)

            if summary is not None:
                prior: dict[str, Any] = {}
                summary_path = session_dir / "summary.json"
                if summary_path.is_file():
                    try:
                        loaded = json.loads(summary_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        loaded = {}
                    if isinstance(loaded, dict):
                        prior = loaded
                records = _manifest_records(session_dir)
                first_received = next(
                    (
                        item.get("received_at")
                        for item in records
                        if item.get("type") == "audio" and not item.get("end")
                    ),
                    None,
                )
                summary_payload: dict[str, Any] = {
                    "schema_version": "ai-passport-stream-session-v1",
                    "capture_source": CAPTURE_SOURCE,
                    "device": device,
                    "device_label": "AI Passport",
                    "session": session,
                    "sample_rate": sample_rate,
                    "format": stream_format,
                    "received_started_at": first_received,
                    "updated_at": received_at,
                    **summary,
                }
                if prior.get("job_id"):
                    summary_payload["job_id"] = prior["job_id"]
                    summary_payload["imported_at"] = prior.get("imported_at")
                if summary["complete"]:
                    try:
                        wav = assemble_wav(session_dir, chunk, sample_rate)
                        if wav is not None:
                            summary_payload["recording_path"] = str(wav)
                    except (OSError, ValueError) as error:
                        summary_payload["complete"] = False
                        summary_payload["assembly_error"] = str(error)
                write_json_atomic(summary_path, summary_payload)

                if (
                    summary_payload.get("complete")
                    and summary_payload.get("recording_path")
                    and not summary_payload.get("job_id")
                    and self.on_complete is not None
                ):
                    try:
                        job_id = self.on_complete(session_dir, summary_payload)
                    except Exception as error:  # pragma: no cover
                        summary_payload["import_error"] = str(error)
                    else:
                        if job_id:
                            summary_payload["job_id"] = str(job_id)
                            summary_payload["imported_at"] = _iso()
                            summary_payload.pop("import_error", None)
                    write_json_atomic(summary_path, summary_payload)
                summary = summary_payload

        response: dict[str, Any] = {"ok": True, **record}
        if summary is not None:
            response["summary"] = summary
        return HTTPStatus.OK, response


class PassportStreamHandler(BaseHTTPRequestHandler):
    server_version = "LifeLoggerPassportStream/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/health":
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        payload = json.dumps(
            {
                "ok": True,
                "service": "LifeLoggerReceiver",
                "version": 1,
                "port": self.server.server_port,
            }
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "invalid content length")
            return
        if size < 0:
            self.send_error(HTTPStatus.BAD_REQUEST, "invalid content length")
            return
        body = self.rfile.read(size)
        status, response = self.server.stream_store.receive(  # type: ignore[attr-defined]
            self.path,
            self.headers,
            body,
        )
        payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")


class PassportStreamServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        address: tuple[str, int],
        stream_store: PassportStreamStore,
    ) -> None:
        super().__init__(address, PassportStreamHandler)
        self.stream_store = stream_store


class PassportReceiverRuntime:
    """Own the HTTP stream receiver and UDP discovery responder together."""

    def __init__(
        self,
        *,
        server: PassportStreamServer,
        http_thread: threading.Thread,
        discovery_stop: threading.Event,
        discovery_thread: threading.Thread,
        discovery_port: int,
    ) -> None:
        self.server = server
        self.http_thread = http_thread
        self.discovery_stop = discovery_stop
        self.discovery_thread = discovery_thread
        self.discovery_port = discovery_port

    def status(self) -> dict[str, Any]:
        http_running = self.http_thread.is_alive()
        discovery_running = self.discovery_thread.is_alive()
        running = http_running and discovery_running
        degraded_reason = None
        if http_running and not discovery_running:
            degraded_reason = "UDP discovery thread stopped"
        elif discovery_running and not http_running:
            degraded_reason = "HTTP receiver thread stopped"
        elif not http_running and not discovery_running:
            degraded_reason = "receiver threads stopped"
        return {
            "enabled": True,
            "running": running,
            "http_running": http_running,
            "discovery_running": discovery_running,
            "degraded": not running,
            "degraded_reason": degraded_reason,
            "host": str(self.server.server_address[0]),
            "http_port": int(self.server.server_port),
            "discovery_port": int(self.discovery_port),
            "capture_source": CAPTURE_SOURCE,
            "health_path": "/health",
            "chunk_path": "/v1/audio/chunk",
        }

    def close(self) -> None:
        self.discovery_stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.http_thread.join(timeout=2)
        self.discovery_thread.join(timeout=1)


def start_passport_receiver(
    *,
    host: str,
    port: int,
    discovery_port: int,
    output_root: Path,
    on_complete: CompleteCallback | None = None,
) -> PassportReceiverRuntime:
    """Start the firmware-compatible HTTP + UDP receiver as one runtime."""

    server = create_passport_stream_server(
        host=host,
        port=port,
        output_root=output_root,
        on_complete=on_complete,
    )
    http_thread = threading.Thread(
        target=server.serve_forever,
        name="passport-stream-http",
        daemon=True,
    )
    http_thread.start()
    try:
        discovery_stop, discovery_thread, actual_discovery_port = (
            start_receiver_discovery(
                host=host,
                discovery_port=discovery_port,
                receiver_port=server.server_port,
            )
        )
    except OSError:
        server.shutdown()
        server.server_close()
        http_thread.join(timeout=2)
        raise
    return PassportReceiverRuntime(
        server=server,
        http_thread=http_thread,
        discovery_stop=discovery_stop,
        discovery_thread=discovery_thread,
        discovery_port=actual_discovery_port,
    )


def create_passport_stream_server(
    *,
    host: str,
    port: int,
    output_root: Path,
    on_complete: CompleteCallback | None = None,
) -> PassportStreamServer:
    return PassportStreamServer(
        (host, port),
        PassportStreamStore(output_root, on_complete=on_complete),
    )
