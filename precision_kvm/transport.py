"""Bounded, ordered input transport. No capture or injection happens here."""
from __future__ import annotations

import hashlib
import hmac
import json
import ctypes
import select
import socket
import ssl
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

MAX_FRAME = 64 * 1024


class ProtocolError(ValueError):
    pass


class BackpressureError(RuntimeError):
    """Disconnect instead of silently losing a key release or replaying stale input."""


@dataclass
class Metrics:
    sent_events: int = 0
    received_events: int = 0
    sent_frames: int = 0
    coalesced_moves: int = 0
    queue_high_water: int = 0
    max_queue_ms: float = 0.0


class _WindowsWake:
    """Interruptible high-resolution timer; no spinning and no global timer change."""
    def __init__(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateEventW.restype = ctypes.c_void_p
        kernel.CreateWaitableTimerExW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint]
        kernel.CreateWaitableTimerExW.restype = ctypes.c_void_p
        kernel.SetWaitableTimer.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_longlong), ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        kernel.SetWaitableTimer.restype = ctypes.c_int
        kernel.SetEvent.argtypes = [ctypes.c_void_p]
        kernel.SetEvent.restype = ctypes.c_int
        kernel.WaitForMultipleObjects.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int, ctypes.c_uint]
        kernel.WaitForMultipleObjects.restype = ctypes.c_uint
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        self.kernel = kernel
        self.event = kernel.CreateEventW(None, False, False, None)
        self.timer = kernel.CreateWaitableTimerExW(None, None, 2, 0x1F0003)
        if not self.event or not self.timer:
            self.dispose()
            raise OSError("High-resolution waitable timer unavailable")

    def notify(self):
        self.kernel.SetEvent(self.event)

    def wait(self, seconds: float):
        due = ctypes.c_longlong(-max(1, round(seconds * 10_000_000)))
        if not self.kernel.SetWaitableTimer(self.timer, ctypes.byref(due), 0, None, None, False):
            raise ctypes.WinError(ctypes.get_last_error())
        handles = (ctypes.c_void_p * 2)(self.event, self.timer)
        if self.kernel.WaitForMultipleObjects(2, handles, False, 0xFFFFFFFF) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())

    def dispose(self):
        for name in ("event", "timer"):
            handle = getattr(self, name, None)
            if handle:
                self.kernel.CloseHandle(handle)
                setattr(self, name, None)

    def __del__(self):
        # The wait() stack owns self while waiting, so handles cannot close under it.
        self.dispose()


class FramedSocket:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._send_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._tls = isinstance(sock, ssl.SSLSocket)
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
        sock.setblocking(False) if self._tls else sock.settimeout(2.0)

    def _tls_call(self, operation, value):
        """Serialize SSL state calls, waiting outside the lock for duplex traffic.

        SSL objects have mutable shared state. A blocking SSL_read concurrent with
        SSL_write can race; nonblocking operations hold the lock only briefly.
        """
        deadline = time.monotonic() + 2
        while True:
            with self._io_lock:
                try:
                    return operation(value)
                except ssl.SSLWantReadError:
                    reading = True
                except ssl.SSLWantWriteError:
                    reading = False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("TLS operation timed out")
            ready = select.select([self.sock] if reading else [], [] if reading else [self.sock], [], remaining)
            if not ready[0] and not ready[1]:
                raise socket.timeout("TLS operation timed out")

    def send(self, message: dict) -> None:
        payload = json.dumps(message, separators=(",", ":"), allow_nan=False).encode()
        if len(payload) > MAX_FRAME:
            raise ProtocolError("Frame exceeds size limit")
        with self._send_lock:
            wire = struct.pack("!I", len(payload)) + payload
            if self._tls:
                remaining = memoryview(wire)
                while remaining:
                    sent = self._tls_call(self.sock.send, remaining)
                    if not sent:
                        raise EOFError("Peer disconnected")
                    remaining = remaining[sent:]
            else:
                self.sock.sendall(wire)

    def _read(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            data = (self._tls_call(self.sock.recv, size - len(result)) if self._tls
                    else self.sock.recv(size - len(result)))
            if not data:
                raise EOFError("Peer disconnected")
            result.extend(data)
        return bytes(result)

    def recv(self) -> dict:
        size = struct.unpack("!I", self._read(4))[0]
        if not 0 < size <= MAX_FRAME:
            raise ProtocolError("Invalid frame length")
        try:
            message = json.loads(self._read(size))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProtocolError("Invalid JSON") from exc
        if not isinstance(message, dict):
            raise ProtocolError("Expected object")
        return message

    def close(self) -> None:
        with self._io_lock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()


def connect_tls(host: str, port: int, fingerprint: str, timeout: float = 3) -> FramedSocket:
    expected = fingerprint.replace(":", "").lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("A SHA-256 certificate fingerprint is required")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # Explicit certificate pinning below.
    raw = socket.create_connection((host, port), timeout)
    try:
        secured = context.wrap_socket(raw, server_hostname=host)
        certificate = secured.getpeercert(binary_form=True)
        if not certificate:
            secured.close()
            raise ssl.SSLError("Server did not supply a certificate")
        actual = hashlib.sha256(certificate).hexdigest()
        if not hmac.compare_digest(expected, actual):
            secured.close()
            raise ssl.SSLError("Server certificate fingerprint mismatch")
        return FramedSocket(secured)
    except BaseException:
        raw.close()
        raise


def connect_plain(host: str, port: int, timeout: float = 3) -> FramedSocket:
    """Cleartext channel for trusted networks; the pairing token still applies."""
    return FramedSocket(socket.create_connection((host, port), timeout))


def connect(host: str, port: int, fingerprint: str = "", tls: bool = True,
            timeout: float = 3) -> FramedSocket:
    if tls:
        return connect_tls(host, port, fingerprint, timeout)
    return connect_plain(host, port, timeout)


def fetch_fingerprint(host: str, port: int, timeout: float = 3) -> str:
    """Read the server certificate fingerprint without pinning — the caller
    decides whether to trust it."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((host, port), timeout)
    try:
        with context.wrap_socket(raw, server_hostname=host) as secured:
            certificate = secured.getpeercert(binary_form=True)
            if not certificate:
                raise ssl.SSLError("Server did not supply a certificate")
            return hashlib.sha256(certificate).hexdigest()
    finally:
        raw.close()


class TLSListener:
    def __init__(self, certfile: str, keyfile: str, host: str = "0.0.0.0", port: int = 24819):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(certfile, keyfile)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(2)
        self.sock.settimeout(1)

    def accept(self) -> FramedSocket:
        raw, _ = self.sock.accept()
        raw.settimeout(3)
        try:
            return FramedSocket(self.context.wrap_socket(raw, server_side=True))
        except BaseException:
            raw.close()
            raise

    def close(self) -> None:
        self.sock.close()


class PlainListener:
    """Cleartext accept loop for trusted networks; pairing auth still applies.

    Shares TLSListener's signature so hosts can swap transport by setting.
    """
    def __init__(self, _certfile: str | None = None, _keyfile: str | None = None,
                 host: str = "0.0.0.0", port: int = 24819):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(2)
        self.sock.settimeout(1)

    def accept(self) -> FramedSocket:
        raw, _ = self.sock.accept()
        raw.settimeout(3)
        try:
            return FramedSocket(raw)
        except BaseException:
            raw.close()
            raise

    def close(self) -> None:
        self.sock.close()


def authenticate(channel: FramedSocket, token: str, *, server: bool) -> None:
    if len(token) < 32:
        raise ValueError("Pairing token must contain at least 32 characters")
    if server:
        message = channel.recv()
        supplied = message.get("token")
        if message.get("type") != "auth" or not isinstance(supplied, str) or not hmac.compare_digest(supplied, token):
            channel.close()
            raise ProtocolError("Pairing rejected")
        channel.send({"type": "ready", "version": 1})
    else:
        channel.send({"type": "auth", "token": token})
        if channel.recv() != {"type": "ready", "version": 1}:
            channel.close()
            raise ProtocolError("Pairing rejected")


class EventBuffer:
    """Coalesce adjacent relative moves, preserving distance and discrete ordering.

    The writer samples at the configured maximum rate. Discrete events wake it
    immediately. Overflow or an aged queue fails closed rather than dropping keys.
    """
    def __init__(self, rate_hz: int = 120, max_events: int = 256, max_age_ms: float = 100):
        if not 30 <= rate_hz <= 1000 or max_events < 2 or max_age_ms <= 0:
            raise ValueError("Invalid transport limits")
        self.rate_hz = rate_hz
        self.max_events = max_events
        self.max_age_ms = max_age_ms
        self.metrics = Metrics()
        self._items: deque[tuple[float, dict]] = deque()
        self._condition = threading.Condition()
        self._consumer_lock = threading.Lock()
        self._closed = False
        self._next_due = 0.0
        self._waiting = False
        self._wake = None
        if sys.platform == "win32":
            try:
                self._wake = _WindowsWake()
            except OSError:
                pass  # Absolute-deadline Condition fallback on older Windows.

    def _notify(self):
        self._condition.notify_all()
        if self._wake:
            self._wake.notify()

    def put(self, event: dict) -> None:
        with self._condition:
            if self._closed:
                raise EOFError("Input buffer closed")
            if event.get("type") == "move" and self._items and self._items[-1][1].get("type") == "move":
                stamp, previous = self._items[-1]
                self._items[-1] = (stamp, {"type": "move", "dx": previous["dx"] + event["dx"], "dy": previous["dy"] + event["dy"]})
                self.metrics.coalesced_moves += 1
            else:
                if len(self._items) >= self.max_events:
                    self._closed = True
                    self._items.clear()
                    self._notify()
                    raise BackpressureError("Input queue overflow; release remote keys and reconnect")
                self._items.append((time.perf_counter(), dict(event)))
                self.metrics.queue_high_water = max(self.metrics.queue_high_water, len(self._items))
            self._notify()

    def next_batch(self, timeout: float = 0.25) -> list[dict]:
        # A native waitable timer has one consumer. Serialize callers so close
        # cannot dispose a handle while another consumer is still using it.
        with self._consumer_lock:
            return self._next_batch(timeout)

    def _next_batch(self, timeout: float) -> list[dict]:
        deadline = time.perf_counter() + timeout
        with self._condition:
            while True:
                if self._closed:
                    raise EOFError("Input buffer closed")
                now = time.perf_counter()
                if self._items:
                    age = (now - self._items[0][0]) * 1000
                    self.metrics.max_queue_ms = max(self.metrics.max_queue_ms, age)
                    if age > self.max_age_ms:
                        self._closed = True
                        self._items.clear()
                        raise BackpressureError("Input queue exceeded latency limit")
                    discrete = any(item[1].get("type") != "move" for item in self._items)
                    due = self._next_due
                    if discrete or now >= due:
                        batch = [item[1] for item in self._items]
                        self._items.clear()
                        period = 1 / self.rate_hz
                        # Keep phase when scheduling is late, but skip missed
                        # slots rather than emitting a burst of stale samples.
                        self._next_due = due + period if 0 <= now - due < period else now + period
                        return batch
                    remaining = min(due - now, deadline - now)
                else:
                    remaining = deadline - now
                if remaining <= 0:
                    return []
                if self._wake:
                    wake = self._wake
                    self._waiting = True
                    self._condition.release()
                    try:
                        wake.wait(remaining)
                    finally:
                        self._condition.acquire()
                        self._waiting = False
                        if self._closed and self._wake:
                            self._wake.dispose()
                            self._wake = None
                else:
                    self._condition.wait(remaining)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._items.clear()
            self._notify()
            if self._wake and not self._waiting:
                self._wake.dispose()
                self._wake = None


class Sender:
    def __init__(self, channel: FramedSocket, buffer: EventBuffer):
        self.channel, self.buffer = channel, buffer
        self.metrics = buffer.metrics
        self._stop = threading.Event()

    def run(self) -> None:
        sequence = 0
        try:
            while not self._stop.is_set():
                batch = self.buffer.next_batch()
                self.channel.send({"type": "events", "seq": sequence, "events": batch})
                sequence += 1
                self.metrics.sent_frames += 1
                self.metrics.sent_events += len(batch)
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        self.buffer.close()
        self.channel.close()


class Receiver:
    def __init__(self, channel: FramedSocket, on_event: Callable[[dict], None], on_disconnect: Callable[[], None] = lambda: None):
        self.channel, self.on_event, self.on_disconnect = channel, on_event, on_disconnect
        self.metrics = Metrics()
        self._stop = threading.Event()

    def run(self) -> None:
        expected = 0
        try:
            while not self._stop.is_set():
                message = self.channel.recv()
                events = message.get("events")
                if message.get("type") != "events" or message.get("seq") != expected or not isinstance(events, list) or len(events) > 256 or any(not isinstance(event, dict) for event in events):
                    raise ProtocolError("Invalid input batch")
                expected += 1
                for event in events:
                    self.on_event(event)
                    self.metrics.received_events += 1
        finally:
            self.stop()
            self.on_disconnect()

    def stop(self) -> None:
        self._stop.set()
        self.channel.close()
