"""Input-free TLS echo probe. Measures transport RTT, never keyboard/mouse latency."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import ctypes
import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

from .transport import EventBuffer, TLSListener, authenticate, connect_tls


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def echo(channel) -> None:
    """Echo only bounded numeric probe messages, with no injector imported."""
    while True:
        message = channel.recv()
        received_ns = time.perf_counter_ns()
        if message == {"type": "finish"}:
            return
        if (message.get("type") != "probe" or type(message.get("id")) is not int
                or type(message.get("sent_ns")) is not int):
            raise ValueError("Invalid probe frame")
        channel.send(dict(message, echo_processing_ns=time.perf_counter_ns() - received_ns))


@dataclass
class _Probe:
    rate: int
    sequence: int
    sent_ns: int
    offset_ms: float
    send_gap_ms: float | None
    send_ms: float = 0
    rtt_ms: float = 0
    receive_gap_ms: float | None = None
    echo_processing_ms: float | None = None


def distribution(values: list[float]) -> dict:
    return {label: round(value, 3) if value is not None else None
            for label, value in {"p50": percentile(values, .5), "p95": percentile(values, .95),
                                 "p99": percentile(values, .99), "max": max(values, default=None)}.items()}


@contextmanager
def timer_resolution() -> Iterator[bool]:
    """Scope Windows' 1 ms scheduling request to this probe and always restore it."""
    timer = None
    if sys.platform == "win32":
        candidate = ctypes.WinDLL("winmm")
        candidate.timeBeginPeriod.argtypes = [ctypes.c_uint]
        candidate.timeBeginPeriod.restype = ctypes.c_uint
        candidate.timeEndPeriod.argtypes = [ctypes.c_uint]
        candidate.timeEndPeriod.restype = ctypes.c_uint
        if candidate.timeBeginPeriod(1) == 0:
            timer = candidate
    try:
        yield timer is not None
    finally:
        if timer is not None:
            timer.timeEndPeriod(1)


def probe(channel, rates: list[int], seconds: float) -> dict:
    with timer_resolution() as timer_active:
        report = _probe(channel, rates, seconds)
        report["windows_1ms_timer_active"] = timer_active
        return report


def _probe(channel, rates: list[int], seconds: float) -> dict:
    if not 0.1 <= seconds <= 60 or not rates or any(not 30 <= rate <= 1000 for rate in rates):
        raise ValueError("Use 30–1000 Hz and 0.1–60 seconds per rate")
    condition = threading.Condition()
    pending: dict[int, _Probe] = {}
    samples: dict[int, list[float]] = {rate: [] for rate in rates}
    traces: dict[int, list[_Probe]] = {rate: [] for rate in rates}
    errors = []
    stopped = threading.Event()

    def receive():
        previous_received: int | None = None
        try:
            while not stopped.is_set():
                response = channel.recv()
                received = time.perf_counter_ns()
                if response.get("type") != "probe":
                    raise ValueError("Invalid echo response")
                with condition:
                    entry = pending.pop(response.get("id"), None)
                    if entry is None or response.get("sent_ns") != entry.sent_ns:
                        raise ValueError("Unexpected echo sequence")
                    entry.rtt_ms = (received - entry.sent_ns) / 1e6
                    entry.receive_gap_ms = ((received - previous_received) / 1e6
                                            if previous_received is not None else None)
                    previous_received = received
                    processing_ns = response.get("echo_processing_ns")
                    if isinstance(processing_ns, int):
                        entry.echo_processing_ms = processing_ns / 1e6
                    samples[entry.rate].append(entry.rtt_ms)
                    traces[entry.rate].append(entry)
                    condition.notify_all()
        except (OSError, EOFError, ValueError) as exc:
            if not stopped.is_set():
                errors.append(type(exc).__name__ + ": " + str(exc))
            with condition:
                condition.notify_all()

    worker = threading.Thread(target=receive, name="probe-echo-reader", daemon=True)
    worker.start()
    results = []
    sequence = 0
    buffer = None
    try:
        for rate in rates:
            buffer = EventBuffer(rate_hz=rate)
            started = time.perf_counter()
            previous_sent: int | None = None
            intervals = []
            send_durations = []
            count, peak_pending = 0, 0
            while time.perf_counter() - started < seconds:
                if errors:
                    raise RuntimeError(errors[0])
                buffer.put({"type": "move", "dx": 1, "dy": 0})
                batch = buffer.next_batch(timeout=1)
                if not batch:
                    continue
                sent = time.perf_counter_ns()
                gap = (sent - previous_sent) / 1e6 if previous_sent is not None else None
                if gap is not None:
                    intervals.append(gap)
                previous_sent = sent
                entry = _Probe(rate, sequence, sent, (sent / 1e9 - started) * 1000, gap)
                with condition:
                    pending[sequence] = entry
                    peak_pending = max(peak_pending, len(pending))
                    if len(pending) > 2048:
                        raise RuntimeError("Probe echo backlog exceeded 2048 messages")
                channel.send({"type": "probe", "id": sequence, "sent_ns": sent})
                entry.send_ms = (time.perf_counter_ns() - sent) / 1e6
                send_durations.append(entry.send_ms)
                sequence += 1
                count += 1
            elapsed = time.perf_counter() - started
            deadline = time.perf_counter() + 1
            with condition:
                while any(entry.rate == rate for entry in pending.values()) and not errors:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        break
                    condition.wait(remaining)
                values = list(samples[rate])
            results.append({
                "requested_hz": rate, "actual_send_hz": round(count / elapsed, 2),
                "seconds": round(elapsed, 3), "sent": count, "echoed": len(values),
                "unanswered": count - len(values), "peak_in_flight": peak_pending,
                "queue_high_water": buffer.metrics.queue_high_water,
                "max_queue_ms": round(buffer.metrics.max_queue_ms, 3),
                "rtt_ms": distribution(values), "send_interval_ms": distribution(intervals),
                "send_call_ms": distribution(send_durations),
                "tail_samples": [{"sequence": entry.sequence, "offset_ms": round(entry.offset_ms, 3),
                                  **{key: round(value, 3) if value is not None else None
                                     for key, value in {"rtt_ms": entry.rtt_ms,
                                                        "send_call_ms": entry.send_ms,
                                                        "send_gap_ms": entry.send_gap_ms,
                                                        "receive_gap_ms": entry.receive_gap_ms,
                                                        "echo_processing_ms": entry.echo_processing_ms}.items()}}
                                 for entry in sorted(traces[rate], key=lambda sample: sample.rtt_ms, reverse=True)[:5]],
            })
            buffer.close()
        return {"kind": "input-free TLS transport probe", "hardware_latency_measured": False,
                "results": results, "errors": errors}
    finally:
        stopped.set()
        if buffer:
            buffer.close()
        try:
            channel.send({"type": "finish"})
        except OSError:
            pass
        channel.close()
        worker.join(3)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("server")
    server.add_argument("--host", default="0.0.0.0")
    server.add_argument("--port", type=int, default=24820)
    server.add_argument("--cert")
    server.add_argument("--key")
    server.add_argument("--config", type=Path, help="Local JSON containing token (never pass token on command line)")
    server.add_argument("--wait-seconds", type=float, default=60)
    client = commands.add_parser("probe")
    client.add_argument("--config", type=Path, required=True, help="Local paired-client JSON")
    client.add_argument("--host")
    client.add_argument("--port", type=int, default=24820)
    client.add_argument("--fingerprint")
    client.add_argument("--rates", default="120,240,500,1000")
    client.add_argument("--seconds", type=float, default=3)
    args = parser.parse_args(argv)
    if args.command == "server":
        if bool(args.cert) != bool(args.key):
            parser.error("--cert and --key must be supplied together")
        if args.cert and not args.config:
            parser.error("Explicit certificates require --config containing the pairing token")
        if args.cert:
            cert, key = args.cert, args.key
            token = json.loads(args.config.read_text(encoding="utf-8"))["token"]
        else:
            from .config import identity
            cert, key, token, _ = identity()
            if args.config:
                token = json.loads(args.config.read_text(encoding="utf-8"))["token"]
        listener = TLSListener(str(cert), str(key), args.host, args.port)
        deadline = time.monotonic() + args.wait_seconds
        try:
            while time.monotonic() < deadline:
                try:
                    channel = listener.accept()
                    break
                except socket.timeout:
                    pass
            else:
                raise TimeoutError("No probe connected before deadline")
            try:
                authenticate(channel, token, server=True)
                echo(channel)
            finally:
                channel.close()
        finally:
            listener.close()
    else:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        rates = [int(rate) for rate in args.rates.split(",")]
        if len(set(rates)) != len(rates):
            parser.error("Each rate must be unique")
        channel = connect_tls(args.host or config["host"], args.port,
                              args.fingerprint or config["fingerprint"])
        try:
            authenticate(channel, config["token"], server=False)
            print(json.dumps(probe(channel, rates, args.seconds), indent=2))
        finally:
            channel.close()


if __name__ == "__main__":
    main()
