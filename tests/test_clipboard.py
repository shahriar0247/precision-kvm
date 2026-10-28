"""Clipboard tests use fake backends only; never touch the operating-system clipboard."""
import base64
from contextlib import contextmanager, nullcontext
import hashlib
import io
import json
import socket
import struct
import threading
import time
import zlib
from unittest.mock import Mock

import pytest

from precision_kvm.clipboard import (
    CHUNK_BYTES, PNG, TEXT, ClipboardBridge, ClipboardServer, MacClipboard,
    _serve_link, bundle_digest, negotiate_limit, receive_bundle, send_bundle, validate_bundle,
)
from precision_kvm.transport import FramedSocket, ProtocolError


class Messages:
    def __init__(self, messages=None):
        self.messages = list(messages or [])

    def send(self, value):
        self.messages.append(value)

    def recv(self):
        if not self.messages:
            raise EOFError()
        return self.messages.pop(0)


class FakeClipboard:
    def __init__(self, bundle=None):
        self.bundle, self.count, self.writes = bundle, 0, 0

    def revision(self):
        return self.count

    def read(self, max_bytes):
        return self.bundle

    def write(self, bundle, max_bytes):
        self.bundle = bundle
        self.count += 1
        self.writes += 1


def png_bytes():
    from PIL import Image
    output = io.BytesIO()
    Image.new("RGBA", (8, 8), (10, 20, 30, 120)).save(output, format="PNG")
    return output.getvalue()


def test_large_unicode_text_and_png_round_trip_in_bounded_frames():
    bundle = {TEXT: ("🌍 clipboard text\n" * 30000).encode(), PNG: png_bytes()}
    wire = Messages()
    send_bundle(wire, bundle, 2 * 1024 * 1024, threading.Event())
    assert len(wire.messages) > 10
    assert all(len(json.dumps(message).encode()) < 65536 for message in wire.messages)
    assert receive_bundle(wire, wire.recv(), 2 * 1024 * 1024, threading.Event()) == bundle
    assert wire.messages == []


def test_socket_pair_transfer_is_larger_than_max_input_frame():
    left, right = socket.socketpair()
    sender, receiver = FramedSocket(left), FramedSocket(right)
    bundle = {TEXT: b"x" * (CHUNK_BYTES * 12 + 7)}
    errors = []

    def send():
        try:
            send_bundle(sender, bundle, 1024 * 1024, threading.Event())
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=send)
    worker.start()
    try:
        assert receive_bundle(receiver, receiver.recv(), 1024 * 1024, threading.Event()) == bundle
    finally:
        sender.close()
        receiver.close()
        worker.join(3)
    assert not worker.is_alive()
    assert errors == []


def test_initial_clipboard_is_not_published_and_remote_writes_are_not_reflected():
    backend = FakeClipboard({TEXT: b"already present"})
    bridge = ClipboardBridge(backend, 1024)
    bridge.baseline()
    assert bridge.changed() is None
    bridge.apply({TEXT: b"from remote"})
    assert backend.writes == 1
    assert bridge.changed() is None
    bridge.apply({TEXT: b"from remote"})
    assert backend.writes == 1
    backend.write({TEXT: b"new local copy"}, 1024)
    assert bridge.changed() == {TEXT: b"new local copy"}
    assert bridge.changed() is None


@pytest.mark.parametrize("size", [-1, True, 2 ** 40, "100"])
def test_untrusted_sizes_are_rejected_before_receiving_data(size):
    manifest = {"type": "clipboard_begin", "id": "a" * 32,
                "items": [{"mime": TEXT, "size": size, "sha256": "0" * 64}]}
    channel = Mock()
    with pytest.raises(ProtocolError):
        receive_bundle(channel, manifest, 1024, threading.Event())
    channel.recv.assert_not_called()


def test_hash_mismatch_never_applies_partial_clipboard():
    wire = Messages()
    send_bundle(wire, {TEXT: b"good"}, 1024, threading.Event())
    wire.messages[1]["data"] = base64.b64encode(b"evil").decode()
    backend = FakeClipboard({TEXT: b"original"})
    with pytest.raises(ProtocolError, match="checksum"):
        result = receive_bundle(wire, wire.recv(), 1024, threading.Event())
        ClipboardBridge(backend, 1024).apply(result)
    assert backend.bundle == {TEXT: b"original"}
    assert backend.writes == 0


def test_chunk_reordering_and_overflow_rejected():
    for change in ({"offset": 100}, {"item": True}, {"data": "%%%"},
                   {"data": base64.b64encode(b"overflow").decode()}):
        wire = Messages()
        send_bundle(wire, {TEXT: b"x"}, 1024, threading.Event())
        wire.messages[1].update(change)
        with pytest.raises(ProtocolError):
            receive_bundle(wire, wire.recv(), 1024, threading.Event())


def test_cancellation_does_not_complete_transfer():
    stopped = threading.Event()
    stopped.set()
    wire = Messages()
    with pytest.raises(EOFError, match="cancelled"):
        send_bundle(wire, {TEXT: b"cancel me"}, 1024, stopped)
    assert all(message["type"] != "clipboard_end" for message in wire.messages)
    with pytest.raises(EOFError):
        receive_bundle(wire, wire.recv(), 1024, stopped)


def test_invalid_text_and_decompression_bomb_dimensions_rejected():
    with pytest.raises(ProtocolError, match="UTF-8"):
        validate_bundle({TEXT: b"\xff"}, 1024)
    image = bytearray(png_bytes())
    image[16:24] = (100000).to_bytes(4, "big") * 2
    with pytest.raises(ProtocolError, match="dimensions"):
        validate_bundle({PNG: bytes(image)}, 1024)


def test_empty_text_is_valid_and_bundle_hash_preserves_format_boundaries():
    wire = Messages()
    send_bundle(wire, {TEXT: b""}, 1024, threading.Event())
    assert receive_bundle(wire, wire.recv(), 1024, threading.Event()) == {TEXT: b""}
    assert bundle_digest({TEXT: b"x"}) != hashlib.sha256(b"x").hexdigest()


def test_disabled_server_never_opens_native_clipboard_or_network():
    service = ClipboardServer("not-a-cert", "not-a-key", "x" * 32, enabled=False)
    service.start()
    assert service.thread is None
    service.stop()


def test_bidirectional_link_syncs_without_overwriting_initial_contents_or_looping():
    left, right = socket.socketpair()
    channels = [FramedSocket(left), FramedSocket(right)]
    backends = [FakeClipboard({TEXT: b"initial PC"}), FakeClipboard({TEXT: b"initial Mac"})]
    stopped = threading.Event()
    errors = []
    statuses = []

    def run(index):
        try:
            _serve_link(channels[index], backends[index], 1024 * 1024, stopped, statuses.append)
        except (OSError, EOFError):
            pass
        except Exception as exc:
            errors.append(exc)

    workers = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()

    def wait_for(predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert predicate()

    try:
        time.sleep(0.1)  # Let both links baseline; the real clipboard is never used.
        assert [backend.writes for backend in backends] == [0, 0]
        backends[0].write({TEXT: b"PC copy"}, 1024)
        wait_for(lambda: backends[1].bundle == {TEXT: b"PC copy"})
        backends[1].write({PNG: png_bytes(), TEXT: b"Mac image caption"}, 1024)
        wait_for(lambda: backends[0].bundle == backends[1].bundle)
        time.sleep(0.3)
        assert [backend.writes for backend in backends] == [2, 2]
        assert statuses.count("Clipboard sent") == 2
        assert statuses.count("Clipboard received") == 2
    finally:
        stopped.set()
        for channel in channels:
            channel.close()
        for worker in workers:
            worker.join(3)
    assert all(not worker.is_alive() for worker in workers)
    assert errors == []


def test_stop_prevents_applying_received_clipboard():
    backend = FakeClipboard({TEXT: b"original"})
    bridge = ClipboardBridge(backend, 1024)
    stopped = threading.Event()
    stopped.set()
    bridge.apply({TEXT: b"remote"}, stopped)
    assert backend.bundle == {TEXT: b"original"}


def test_heartbeats_can_interleave_large_transfer_without_corrupting_it():
    wire = Messages()
    bundle = {TEXT: b"z" * (CHUNK_BYTES * 3 + 1)}
    send_bundle(wire, bundle, 1024 * 1024, threading.Event())
    interleaved = []
    for message in wire.messages:
        interleaved.extend([message, {"type": "clipboard_ping"}])
    wire = Messages(interleaved)
    assert receive_bundle(wire, wire.recv(), 1024 * 1024, threading.Event()) == bundle


def test_baseline_does_not_read_or_convert_existing_clipboard():
    backend = Mock()
    backend.revision.return_value = 7
    bridge = ClipboardBridge(backend, 1024)
    bridge.baseline()
    backend.read.assert_not_called()
    assert bridge.changed() is None


def test_peers_negotiate_smaller_limit_before_reading_clipboard():
    left, right = socket.socketpair()
    server, client = FramedSocket(left), FramedSocket(right)
    results = []
    worker = threading.Thread(target=lambda: results.append(negotiate_limit(server, 128 * 1024, server=True)))
    worker.start()
    try:
        assert negotiate_limit(client, 512 * 1024, server=False) == 128 * 1024
    finally:
        worker.join(3)
        server.close()
        client.close()
    assert results == [128 * 1024]


def test_invalid_peer_limit_rejected_before_clipboard_access():
    wire = Messages([{"type": "clipboard_ready", "version": 1, "max_bytes": 2 ** 40}])
    with pytest.raises(ProtocolError, match="limit"):
        negotiate_limit(wire, 1024, server=True)


def test_oversized_native_copy_is_only_attempted_once_per_revision():
    backend = Mock()
    backend.revision.return_value = 1
    backend.read.side_effect = ValueError("too large")
    bridge = ClipboardBridge(backend, 1024)
    with pytest.raises(ValueError):
        bridge.changed()
    assert bridge.changed() is None
    backend.read.assert_called_once()


@pytest.mark.parametrize("corrupt", ["truncated", "crc", "deflate"])
def test_invalid_png_never_clears_mac_clipboard(corrupt):
    image = bytearray(png_bytes())
    if corrupt == "truncated":
        image = image[:30]
    elif corrupt == "crc":
        image[29] ^= 0xFF
    else:
        chunks, offset = [], 8
        while offset < len(image):
            size = struct.unpack("!I", image[offset:offset + 4])[0]
            kind = bytes(image[offset + 4:offset + 8])
            data = bytes(image[offset + 8:offset + 8 + size])
            if kind == b"IDAT":
                data = b"not-valid-zlib"
            chunks.append(struct.pack("!I", len(data)) + kind + data
                          + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF))
            offset += size + 12
        image = image[:8] + b"".join(chunks)
    backend = MacClipboard.__new__(MacClipboard)
    backend.appkit, backend.foundation = Mock(), Mock()
    backend._autorelease_pool = Mock(side_effect=nullcontext)
    with pytest.raises(ProtocolError, match="PNG"):
        backend.write({PNG: bytes(image)}, 1024)
    backend.appkit.NSPasteboard.generalPasteboard.assert_not_called()
    backend._autorelease_pool.assert_not_called()


def test_mac_oversized_text_is_checked_before_allocating_bytes():
    class HugeData:
        def __len__(self):
            return 1025

        def __bytes__(self):
            raise AssertionError("Oversized NSData must not be copied")

    backend = MacClipboard.__new__(MacClipboard)
    backend.appkit, backend.foundation = Mock(), Mock()
    backend._autorelease_pool = Mock(side_effect=nullcontext)
    board = backend.appkit.NSPasteboard.generalPasteboard.return_value
    board.dataForType_.return_value = HugeData()
    with pytest.raises(ValueError, match="size"):
        backend.read(1024)
    board.stringForType_.assert_not_called()


def test_mac_calls_use_owned_autorelease_contexts_without_manual_pool_drain():
    entries, exits = [], []

    @contextmanager
    def pool():
        entries.append(True)
        try:
            yield
        finally:
            exits.append(True)

    backend = MacClipboard.__new__(MacClipboard)
    backend.appkit, backend.foundation = Mock(), Mock()
    backend._autorelease_pool = pool
    board = backend.appkit.NSPasteboard.generalPasteboard.return_value
    board.changeCount.return_value = 7
    board.dataForType_.return_value = None
    board.writeObjects_.return_value = True
    assert backend.revision() == 7
    assert backend.read(1024) is None
    backend.write({TEXT: b"safe fake text"}, 1024)
    assert len(entries) == len(exits) == 3
    backend.foundation.NSAutoreleasePool.alloc.assert_not_called()


def test_mac_autorelease_context_exits_when_native_access_fails():
    entered, exited = [], []

    @contextmanager
    def pool():
        entered.append(True)
        try:
            yield
        finally:
            exited.append(True)

    backend = MacClipboard.__new__(MacClipboard)
    backend.appkit, backend.foundation = Mock(), Mock()
    backend._autorelease_pool = pool
    backend.appkit.NSPasteboard.generalPasteboard.side_effect = OSError("fake pasteboard unavailable")
    with pytest.raises(OSError):
        backend.revision()
    assert entered == exited == [True]


def test_slow_image_conversion_does_not_trigger_two_second_idle_disconnect():
    class SlowClipboard(FakeClipboard):
        def read(self, max_bytes):
            time.sleep(2.2)
            return self.bundle

    left, right = socket.socketpair()
    channels = [FramedSocket(left), FramedSocket(right)]
    backends = [SlowClipboard(), FakeClipboard()]
    stopped = threading.Event()
    errors = []
    statuses = []

    def run(index):
        try:
            _serve_link(channels[index], backends[index], 1024 * 1024, stopped, statuses.append)
        except (OSError, EOFError) as exc:
            if not stopped.is_set():
                errors.append(exc)

    workers = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()
    try:
        time.sleep(0.1)
        image = {PNG: png_bytes()}
        backends[0].write(image, 1024)
        deadline = time.monotonic() + 5
        while backends[1].bundle != image and time.monotonic() < deadline:
            time.sleep(0.01)
        assert backends[1].bundle == image
        assert not errors
    finally:
        stopped.set()
        for channel in channels:
            channel.close()
        for worker in workers:
            worker.join(3)
    assert all(not worker.is_alive() for worker in workers)
