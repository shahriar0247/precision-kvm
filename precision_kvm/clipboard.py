"""Bounded text/image clipboard sync over a dedicated authenticated TLS channel.

No clipboard is opened merely by importing this module. Input traffic never
shares this connection. Native clipboard contents and transfer data are never
logged, persisted, or queued in an unbounded work queue.
"""
from __future__ import annotations

import base64
import binascii
import ctypes
from ctypes import wintypes
import hashlib
import io
import socket
import struct
import sys
import threading
import time
import uuid
from typing import Callable

from .transport import (FramedSocket, PlainListener, ProtocolError, TLSListener,
                        authenticate, connect)

DEFAULT_MAX_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 32 * 1024
TEXT = "text/plain;charset=utf-8"
PNG = "image/png"
FORMATS = (TEXT, PNG)


def _limit(max_bytes):
    if type(max_bytes) is not int or not 1024 <= max_bytes <= 512 * 1024 * 1024:
        raise ValueError("Clipboard limit must be between 1 KiB and 512 MiB")
    return max_bytes


def validate_bundle(bundle: dict[str, bytes], max_bytes: int) -> None:
    if not isinstance(bundle, dict) or not 1 <= len(bundle) <= len(FORMATS):
        raise ProtocolError("Invalid clipboard format bundle")
    total = 0
    for mime, value in bundle.items():
        if mime not in FORMATS or not isinstance(value, bytes):
            raise ProtocolError("Unsupported clipboard format")
        total += len(value)
        if total > max_bytes:
            raise ProtocolError("Clipboard exceeds configured size limit")
        if mime == TEXT:
            try:
                value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProtocolError("Invalid clipboard UTF-8") from exc
        elif mime == PNG:
            if len(value) < 24 or value[:8] != b"\x89PNG\r\n\x1a\n" or value[12:16] != b"IHDR":
                raise ProtocolError("Invalid clipboard PNG header")
            width, height = struct.unpack("!II", value[16:24])
            if not width or not height or width * height * 4 > max_bytes:
                raise ProtocolError("Clipboard image dimensions exceed configured limit")


def bundle_digest(bundle: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for mime in sorted(bundle):
        digest.update(mime.encode())
        digest.update(struct.pack("!Q", len(bundle[mime])))
        digest.update(bundle[mime])
    return digest.hexdigest()


def verify_png(value, max_bytes=DEFAULT_MAX_BYTES):
    """Validate PNG structure, CRCs and bounded pixel decoding before mutation."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(value)) as image:
            if image.format != "PNG":
                raise ValueError("Expected PNG")
            if len(value) > max_bytes or image.width * image.height * 4 > max_bytes:
                raise ValueError("Clipboard image exceeds configured limit")
            image.verify()
        # verify() checks chunk CRCs but does not inflate pixel data. A PNG
        # with valid chunk CRCs and corrupt zlib payload must also be rejected.
        with Image.open(io.BytesIO(value)) as image:
            image.load()
    except (OSError, SyntaxError, ValueError, struct.error, Image.DecompressionBombError) as exc:
        raise ProtocolError("Invalid clipboard PNG data") from exc


def negotiate_limit(channel, max_bytes, *, server):
    """Both peers enforce the smaller configured cap before any native read."""
    own = {"type": "clipboard_ready", "version": 1, "max_bytes": _limit(max_bytes)}
    if not server:
        channel.send(own)
    peer = channel.recv()
    if peer.get("type") != "clipboard_ready" or peer.get("version") != 1:
        raise ProtocolError("Invalid clipboard negotiation")
    try:
        effective = min(max_bytes, _limit(peer.get("max_bytes")))
    except ValueError as exc:
        raise ProtocolError("Invalid peer clipboard limit") from exc
    if server:
        channel.send(own)
    return effective


def send_bundle(channel: FramedSocket, bundle: dict[str, bytes], max_bytes: int,
                stopped: threading.Event) -> None:
    validate_bundle(bundle, max_bytes)
    identifier = uuid.uuid4().hex
    items: list[dict] = [{"mime": mime, "size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                        for mime, value in sorted(bundle.items())]
    channel.send({"type": "clipboard_begin", "id": identifier, "items": items})
    for index, item in enumerate(items):
        value = bundle[item["mime"]]
        for offset in range(0, len(value), CHUNK_BYTES):
            if stopped.is_set():
                raise EOFError("Clipboard transfer cancelled")
            channel.send({"type": "clipboard_chunk", "id": identifier, "item": index,
                          "offset": offset,
                          "data": base64.b64encode(value[offset:offset + CHUNK_BYTES]).decode("ascii")})
    if stopped.is_set():
        raise EOFError("Clipboard transfer cancelled")
    channel.send({"type": "clipboard_end", "id": identifier})


def receive_bundle(channel: FramedSocket, begin: dict, max_bytes: int,
                   stopped: threading.Event) -> dict[str, bytes]:
    identifier, items = begin.get("id"), begin.get("items")
    if (begin.get("type") != "clipboard_begin" or not isinstance(identifier, str)
            or len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier)
            or not isinstance(items, list) or not 1 <= len(items) <= len(FORMATS)):
        raise ProtocolError("Invalid clipboard manifest")
    total, seen = 0, set()
    for item in items:
        if not isinstance(item, dict):
            raise ProtocolError("Invalid clipboard manifest item")
        mime, size, digest = item.get("mime"), item.get("size"), item.get("sha256")
        if (mime not in FORMATS or mime in seen or type(size) is not int or size < 0
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise ProtocolError("Invalid clipboard format metadata")
        total += size
        if total > max_bytes:
            raise ProtocolError("Clipboard exceeds configured size limit")
        seen.add(mime)
    deadline = time.monotonic() + 120

    def next_message():
        while not stopped.is_set() and time.monotonic() < deadline:
            message = channel.recv()
            if message != {"type": "clipboard_ping"}:
                return message
        raise EOFError("Clipboard transfer cancelled or timed out")

    result = {}
    for index, item in enumerate(items):
        data = bytearray()
        while len(data) < item["size"]:
            if stopped.is_set() or time.monotonic() >= deadline:
                raise EOFError("Clipboard transfer cancelled or timed out")
            chunk = next_message()
            encoded = chunk.get("data")
            if (chunk.get("type") != "clipboard_chunk" or chunk.get("id") != identifier
                    or type(chunk.get("item")) is not int or chunk.get("item") != index
                    or type(chunk.get("offset")) is not int or chunk.get("offset") != len(data)
                    or not isinstance(encoded, str) or len(encoded) > (CHUNK_BYTES + 2) // 3 * 4):
                raise ProtocolError("Invalid clipboard chunk sequence")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ProtocolError("Invalid clipboard chunk encoding") from exc
            if not decoded or len(decoded) > CHUNK_BYTES or len(data) + len(decoded) > item["size"]:
                raise ProtocolError("Clipboard chunk exceeds declared size")
            data.extend(decoded)
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ProtocolError("Clipboard content checksum mismatch")
        result[item["mime"]] = bytes(data)
    if stopped.is_set():
        raise EOFError("Clipboard transfer cancelled")
    if next_message() != {"type": "clipboard_end", "id": identifier}:
        raise ProtocolError("Invalid clipboard transfer terminator")
    validate_bundle(result, max_bytes)
    return result


class WindowsClipboard:
    """CF_UNICODETEXT, registered PNG and CF_DIB/CF_DIBV5 interoperability."""
    def __init__(self):
        if sys.platform != "win32":
            raise OSError("Windows clipboard requires Windows")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        u, k = self.user, self.kernel
        u.OpenClipboard.argtypes = [wintypes.HWND]
        u.OpenClipboard.restype = wintypes.BOOL
        u.CloseClipboard.argtypes = []
        u.CloseClipboard.restype = wintypes.BOOL
        u.EmptyClipboard.argtypes = []
        u.EmptyClipboard.restype = wintypes.BOOL
        u.GetClipboardSequenceNumber.argtypes = []
        u.GetClipboardSequenceNumber.restype = wintypes.DWORD
        u.GetClipboardData.argtypes = [wintypes.UINT]
        u.GetClipboardData.restype = wintypes.HANDLE
        u.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        u.IsClipboardFormatAvailable.restype = wintypes.BOOL
        u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u.SetClipboardData.restype = wintypes.HANDLE
        u.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        u.RegisterClipboardFormatW.restype = wintypes.UINT
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                     wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                     wintypes.HINSTANCE, wintypes.LPVOID]
        u.CreateWindowExW.restype = wintypes.HWND
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.DestroyWindow.restype = wintypes.BOOL
        k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = wintypes.HANDLE
        k.GlobalFree.argtypes = [wintypes.HANDLE]
        k.GlobalFree.restype = wintypes.HANDLE
        k.GlobalLock.argtypes = [wintypes.HANDLE]
        k.GlobalLock.restype = wintypes.LPVOID
        k.GlobalUnlock.argtypes = [wintypes.HANDLE]
        k.GlobalUnlock.restype = wintypes.BOOL
        k.GlobalSize.argtypes = [wintypes.HANDLE]
        k.GlobalSize.restype = ctypes.c_size_t
        self.png_format = u.RegisterClipboardFormatW("PNG")
        self.window = u.CreateWindowExW(0, "STATIC", "PrecisionKVM Clipboard", 0, 0, 0, 0, 0,
                                        wintypes.HWND(-3), None, None, None)
        if not self.window or not self.png_format:
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.window:
            self.user.DestroyWindow(self.window)
            self.window = None

    def revision(self):
        return int(self.user.GetClipboardSequenceNumber())

    def _open(self):
        if not self.user.OpenClipboard(self.window):
            raise OSError("Clipboard is busy")

    def _get(self, format_id, max_bytes):
        handle = self.user.GetClipboardData(format_id)
        if not handle:
            return None
        size = self.kernel.GlobalSize(handle)
        if size > max_bytes:
            raise ValueError("Native clipboard exceeds configured size limit")
        pointer = self.kernel.GlobalLock(handle)
        if not pointer:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(pointer, size)
        finally:
            self.kernel.GlobalUnlock(handle)

    def read(self, max_bytes):
        self._open()
        dib = None
        try:
            result = {}
            if self.user.IsClipboardFormatAvailable(13):
                raw = self._get(13, max_bytes * 2 + 2)
                if raw is not None:
                    result[TEXT] = raw.decode("utf-16-le", errors="replace").split("\0", 1)[0].encode("utf-8")
            if self.user.IsClipboardFormatAvailable(self.png_format):
                png = self._get(self.png_format, max_bytes)
                if png:
                    result[PNG] = png
            else:
                for format_id in (17, 8):
                    if self.user.IsClipboardFormatAvailable(format_id):
                        dib = self._get(format_id, max_bytes)
                        break
        finally:
            self.user.CloseClipboard()
        # Encoding a large screenshot may be slow; never hold the Windows
        # clipboard lock while Pillow compresses it.
        if dib:
            from PIL import Image
            with Image.open(io.BytesIO(dib)) as image:
                if image.width * image.height * 4 > max_bytes:
                    raise ValueError("Clipboard image dimensions exceed configured limit")
                output = io.BytesIO()
                image.save(output, format="PNG")
                result[PNG] = output.getvalue()
        if result:
            validate_bundle(result, max_bytes)
        return result or None

    def _set(self, format_id, value):
        handle = self.kernel.GlobalAlloc(0x0002, len(value))
        if not handle:
            raise MemoryError("Could not allocate clipboard memory")
        owned = True
        try:
            pointer = self.kernel.GlobalLock(handle)
            if not pointer:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                ctypes.memmove(pointer, value, len(value))
            finally:
                self.kernel.GlobalUnlock(handle)
            if not self.user.SetClipboardData(format_id, handle):
                raise ctypes.WinError(ctypes.get_last_error())
            owned = False  # Windows owns successfully installed HGLOBAL memory.
        finally:
            if owned:
                self.kernel.GlobalFree(handle)

    def write(self, bundle, max_bytes):
        validate_bundle(bundle, max_bytes)
        formats = []
        if TEXT in bundle:
            formats.append((13, bundle[TEXT].decode("utf-8").encode("utf-16-le") + b"\0\0"))
        if PNG in bundle:
            from PIL import Image
            verify_png(bundle[PNG], max_bytes)
            output = io.BytesIO()
            with Image.open(io.BytesIO(bundle[PNG])) as image:
                image.convert("RGB").save(output, format="BMP")
            formats.extend(((self.png_format, bundle[PNG]), (8, output.getvalue()[14:])))
        self._open()
        try:
            if not self.user.EmptyClipboard():
                raise ctypes.WinError(ctypes.get_last_error())
            for format_id, value in formats:
                self._set(format_id, value)
        finally:
            self.user.CloseClipboard()


class MacClipboard:
    def __init__(self):
        import AppKit
        import Foundation
        import objc
        self.appkit, self.foundation = AppKit, Foundation
        self._autorelease_pool = objc.autorelease_pool

    def revision(self):
        with self._autorelease_pool():
            return int(self.appkit.NSPasteboard.generalPasteboard().changeCount())

    def read(self, max_bytes):
        with self._autorelease_pool():
            a = self.appkit
            board = a.NSPasteboard.generalPasteboard()
            result = {}
            # NSPasteboardTypeString is public.utf8-plain-text. Check the
            # NSData length before copying/decoding a potentially huge string.
            text = board.dataForType_(a.NSPasteboardTypeString)
            if text is not None:
                if len(text) > max_bytes:
                    raise ValueError("Native clipboard exceeds configured size limit")
                result[TEXT] = bytes(text)
            png = board.dataForType_(a.NSPasteboardTypePNG)
            if png is not None:
                if len(png) > max_bytes:
                    raise ValueError("Native clipboard exceeds configured size limit")
                result[PNG] = bytes(png)
            else:
                tiff = board.dataForType_(a.NSPasteboardTypeTIFF)
                if tiff is not None:
                    if len(tiff) > max_bytes:
                        raise ValueError("Native clipboard exceeds configured size limit")
                    image = a.NSBitmapImageRep.imageRepWithData_(tiff)
                    if image is not None:
                        if image.pixelsWide() * image.pixelsHigh() * 4 > max_bytes:
                            raise ValueError("Clipboard image dimensions exceed configured limit")
                        result[PNG] = bytes(image.representationUsingType_properties_(a.NSBitmapImageFileTypePNG, {}))
            if result:
                validate_bundle(result, max_bytes)
            return result or None

    def write(self, bundle, max_bytes):
        validate_bundle(bundle, max_bytes)
        if PNG in bundle:
            verify_png(bundle[PNG], max_bytes)
        with self._autorelease_pool():
            a, f = self.appkit, self.foundation
            item = a.NSPasteboardItem.alloc().init()
            if TEXT in bundle:
                item.setString_forType_(bundle[TEXT].decode("utf-8"), a.NSPasteboardTypeString)
            if PNG in bundle:
                data = f.NSData.dataWithBytes_length_(bundle[PNG], len(bundle[PNG]))
                item.setData_forType_(data, a.NSPasteboardTypePNG)
            board = a.NSPasteboard.generalPasteboard()
            board.clearContents()
            if not board.writeObjects_([item]):
                raise OSError("macOS refused clipboard write")

    def close(self):
        pass


def native_backend():
    if sys.platform == "win32":
        return WindowsClipboard()
    if sys.platform == "darwin":
        return MacClipboard()
    raise OSError("Clipboard sharing supports Windows and macOS")


class ClipboardBridge:
    """One local backend and one connection, with loop suppression and no work queue."""
    def __init__(self, backend, max_bytes=DEFAULT_MAX_BYTES):
        self.backend, self.max_bytes = backend, _limit(max_bytes)
        self.lock = threading.Lock()
        self.last_revision = None
        self.last_digest = None

    def baseline(self):
        with self.lock:
            self.last_revision = self.backend.revision()
            # Do not decode a potentially huge existing image on connection.
            # Initial clipboard contents are deliberately not sent to the peer.
            self.last_digest = None

    def changed(self):
        with self.lock:
            revision = self.backend.revision()
            if revision == self.last_revision:
                return None
            try:
                bundle = self.backend.read(self.max_bytes)
            except ValueError:
                # An oversized or unsupported copy is skipped once, not
                # re-decoded four times per second until the next user copy.
                self.last_revision = revision
                raise
            self.last_revision = revision
            if not bundle:
                self.last_digest = None
                return None
            digest = bundle_digest(bundle)
            if digest == self.last_digest:
                return None
            self.last_digest = digest
            return bundle

    def apply(self, bundle, stopped=None):
        validate_bundle(bundle, self.max_bytes)
        with self.lock:
            if stopped is not None and stopped.is_set():
                return
            digest = bundle_digest(bundle)
            if digest == self.last_digest:
                return
            self.backend.write(bundle, self.max_bytes)
            self.last_digest = digest
            self.last_revision = self.backend.revision()


def _serve_link(channel, backend, max_bytes, stopped, status):
    if stopped.is_set():
        return
    bridge = ClipboardBridge(backend, max_bytes)
    try:
        bridge.baseline()
    except (OSError, ValueError):
        pass  # Busy/unsupported/oversized clipboard must not take down input.
    ended = threading.Event()

    def send():
        try:
            while not stopped.is_set() and not ended.is_set():
                try:
                    bundle = bridge.changed()
                except OSError:
                    bundle = None
                except ValueError:
                    status("Clipboard skipped (unsupported format or configured size limit)")
                    bundle = None
                if bundle:
                    send_bundle(channel, bundle, max_bytes, stopped)
                    status("Clipboard sent")
                if ended.wait(0.25):
                    break
        except Exception:
            ended.set()
            channel.close()

    def heartbeat():
        try:
            while not stopped.is_set() and not ended.is_set():
                channel.send({"type": "clipboard_ping"})
                if ended.wait(0.5):
                    break
        except Exception:
            ended.set()
            channel.close()

    worker = threading.Thread(target=send, name="kvm-clipboard-send", daemon=True)
    heart = threading.Thread(target=heartbeat, name="kvm-clipboard-heartbeat", daemon=True)
    worker.start()
    heart.start()
    try:
        while not stopped.is_set() and not ended.is_set():
            message = channel.recv()
            if message == {"type": "clipboard_ping"}:
                continue
            bundle = receive_bundle(channel, message, max_bytes, stopped)
            try:
                bridge.apply(bundle, stopped)
                status("Clipboard received")
            except (OSError, ValueError):
                status("Clipboard write unavailable; original local clipboard retained where possible")
    finally:
        ended.set()
        channel.close()
        worker.join(3)
        heart.join(3)


class ClipboardServer:
    def __init__(self, cert, key, token, port=24821, max_bytes=DEFAULT_MAX_BYTES,
                 enabled=True, status: Callable[[str], None] = print, backend=None):
        self.cert, self.key, self.token, self.port = str(cert), str(key), token, int(port)
        self.max_bytes, self.enabled, self.status = _limit(max_bytes), enabled, status
        self.backend = backend
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None
        self.listener: TLSListener | PlainListener | None = None
        self.channel: FramedSocket | None = None

    def start(self):
        if not self.enabled or (self.thread and self.thread.is_alive()):
            return
        self.stopped.clear()
        self.thread = threading.Thread(target=self._run, name="kvm-clipboard-server", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.channel:
            self.channel.close()
        if self.listener:
            self.listener.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(5)

    def _run(self):
        backend = None
        try:
            backend = self.backend if self.backend is not None else native_backend()
            listener_t = TLSListener if self.cert and self.key else PlainListener
            listener = listener_t(self.cert, self.key, port=self.port)
            self.listener = listener
            while not self.stopped.is_set():
                try:
                    self.channel = listener.accept()
                    authenticate(self.channel, self.token, server=True)
                    effective = negotiate_limit(self.channel, self.max_bytes, server=True)
                    self.status(f"Clipboard connected (limit {effective / 1024 ** 2:g} MiB)")
                    _serve_link(self.channel, backend, effective, self.stopped, self.status)
                except socket.timeout:
                    pass
                except (OSError, EOFError, ProtocolError, ValueError):
                    if not self.stopped.is_set():
                        self.status("Clipboard connection unavailable; waiting for peer")
                finally:
                    if self.channel:
                        self.channel.close()
                    self.channel = None
        except Exception as exc:
            if not self.stopped.is_set():
                self.status(f"Clipboard unavailable: {type(exc).__name__}")
        finally:
            if self.listener:
                self.listener.close()
            if backend and hasattr(backend, "close"):
                backend.close()


class ClipboardClient:
    def __init__(self, config, max_bytes=DEFAULT_MAX_BYTES,
                 status: Callable[[str], None] = print, backend=None):
        self.config, self.max_bytes, self.status = dict(config), _limit(max_bytes), status
        self.backend = backend
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None
        self.channel: FramedSocket | None = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stopped.clear()
        self.thread = threading.Thread(target=self._run, name="kvm-clipboard-client", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.channel:
            self.channel.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(5)

    def _run(self):
        backend = None
        try:
            backend = self.backend if self.backend is not None else native_backend()
            while not self.stopped.is_set():
                started = time.monotonic()
                try:
                    self.channel = connect(self.config["host"],
                        int(self.config.get("clipboard_port", int(self.config["port"]) + 2)),
                        self.config.get("fingerprint", ""),
                        tls=bool(self.config.get("tls", True)), timeout=0.8)
                    authenticate(self.channel, self.config["token"], server=False)
                    effective = negotiate_limit(self.channel, self.max_bytes, server=False)
                    self.status(f"Clipboard connected (limit {effective / 1024 ** 2:g} MiB)")
                    _serve_link(self.channel, backend, effective, self.stopped, self.status)
                except (OSError, EOFError, ProtocolError, ValueError):
                    pass
                finally:
                    if self.channel:
                        self.channel.close()
                    self.channel = None
                self.stopped.wait(max(0, 1 - (time.monotonic() - started)))
        except Exception as exc:
            if not self.stopped.is_set():
                self.status(f"Clipboard unavailable: {type(exc).__name__}")
        finally:
            if backend and hasattr(backend, "close"):
                backend.close()
