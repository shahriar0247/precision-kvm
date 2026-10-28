import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch

from precision_kvm.transport import (
    BackpressureError, EventBuffer, FramedSocket, ProtocolError, Receiver,
    authenticate, connect_tls,
)


class TransportTests(unittest.TestCase):
    def pair(self):
        left, right = socket.socketpair()
        a, b = FramedSocket(left), FramedSocket(right)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        return a, b

    def test_framing_handles_fragmented_header_and_payload(self):
        a, b = self.pair()
        payload = b'{"type":"key","key":42}'
        wire = struct.pack("!I", len(payload)) + payload
        for byte in wire:
            a.sock.send(bytes([byte]))
        self.assertEqual(b.recv(), {"type": "key", "key": 42})

    def test_frame_length_and_json_shape_rejected(self):
        for wire in (struct.pack("!I", 65537), struct.pack("!I", 2) + b"[]"):
            with self.subTest(wire=wire):
                a, b = self.pair()
                a.sock.sendall(wire)
                with self.assertRaises(ProtocolError):
                    b.recv()

    def test_pairing_success_and_invalid_token(self):
        a, b = self.pair()
        errors = []
        def server():
            try:
                authenticate(a, "a" * 32, server=True)
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=server)
        worker.start()
        authenticate(b, "a" * 32, server=False)
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        a, b = self.pair()
        b.send({"type": "auth", "token": "b" * 32})
        with self.assertRaises(ProtocolError):
            authenticate(a, "a" * 32, server=True)

    def test_adjacent_motion_preserves_distance_and_key_order(self):
        buffer = EventBuffer()
        for _ in range(10000):
            buffer.put({"type": "move", "dx": 1, "dy": -2})
        buffer.put({"type": "button", "down": True})
        buffer.put({"type": "move", "dx": 7, "dy": 8})
        buffer.put({"type": "button", "down": False})
        self.assertEqual(buffer.next_batch(), [
            {"type": "move", "dx": 10000, "dy": -20000},
            {"type": "button", "down": True},
            {"type": "move", "dx": 7, "dy": 8},
            {"type": "button", "down": False},
        ])
        self.assertEqual(buffer.metrics.coalesced_moves, 9999)
        self.assertEqual(buffer.metrics.queue_high_water, 4)

    def test_overflow_fails_closed(self):
        buffer = EventBuffer(max_events=2)
        buffer.put({"type": "key", "down": True})
        buffer.put({"type": "key", "down": False})
        with self.assertRaises(BackpressureError):
            buffer.put({"type": "key"})
        with self.assertRaises(EOFError):
            buffer.next_batch()

    def test_stale_queue_fails_closed(self):
        buffer = EventBuffer(max_age_ms=1)
        buffer.put({"type": "move", "dx": 1, "dy": 0})
        time.sleep(.005)
        with self.assertRaises(BackpressureError):
            buffer.next_batch()

    def test_discrete_event_bypasses_motion_throttle(self):
        buffer = EventBuffer(rate_hz=30)
        buffer.put({"type": "move", "dx": 1, "dy": 0})
        buffer.next_batch()
        buffer.put({"type": "key", "down": False})
        self.assertEqual(buffer.next_batch(timeout=0), [{"type": "key", "down": False}])

    def test_receiver_releases_on_disconnect(self):
        a, b = self.pair()
        events, released = [], []
        a.send({"type": "events", "seq": 0, "events": [{"type": "key", "down": True}]})
        a.close()
        receiver = Receiver(b, events.append, lambda: released.append(True))
        with self.assertRaises(EOFError):
            receiver.run()
        self.assertEqual(events, [{"type": "key", "down": True}])
        self.assertEqual(released, [True])

    def test_out_of_order_batches_rejected(self):
        a, b = self.pair()
        a.send({"type": "events", "seq": 1, "events": []})
        with self.assertRaises(ProtocolError):
            Receiver(b, lambda event: None).run()

    def test_pin_required_before_connection(self):
        with self.assertRaises(ValueError):
            connect_tls("localhost", 1, "")

    def test_absolute_pacing_preserves_phase_and_skips_stalled_slots(self):
        buffer = EventBuffer(rate_hz=1000, max_age_ms=1000)
        self.addCleanup(buffer.close)
        with patch("precision_kvm.transport.time.perf_counter", return_value=100):
            buffer.put({"type": "move", "dx": 1, "dy": 0})
            buffer.next_batch()
        with patch("precision_kvm.transport.time.perf_counter", return_value=100.0012):
            buffer.put({"type": "move", "dx": 1, "dy": 0})
            buffer.next_batch()
        self.assertAlmostEqual(buffer._next_due, 100.002)
        with patch("precision_kvm.transport.time.perf_counter", return_value=100.1):
            buffer.put({"type": "move", "dx": 1, "dy": 0})
            buffer.next_batch()
        self.assertAlmostEqual(buffer._next_due, 100.101)

    def test_close_interrupts_pacing_wait_and_releases_timer(self):
        buffer = EventBuffer(rate_hz=30)
        buffer.put({"type": "move", "dx": 1, "dy": 0})
        buffer.next_batch()
        buffer.put({"type": "move", "dx": 1, "dy": 0})
        closed = []
        def consume():
            try:
                buffer.next_batch()
            except EOFError:
                closed.append(True)
        worker = threading.Thread(target=consume)
        worker.start()
        time.sleep(.002)
        buffer.close()
        worker.join(.2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(closed, [True])
        self.assertIsNone(buffer._wake)

    def test_close_with_multiple_consumers_is_safe(self):
        buffer = EventBuffer(rate_hz=30)
        closed = []
        def consume():
            try:
                buffer.next_batch()
            except EOFError:
                closed.append(True)
        workers = [threading.Thread(target=consume) for _ in range(2)]
        for worker in workers:
            worker.start()
        time.sleep(.002)
        buffer.close()
        for worker in workers:
            worker.join(.5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(closed, [True, True])
        self.assertIsNone(buffer._wake)


if __name__ == "__main__":
    unittest.main()
