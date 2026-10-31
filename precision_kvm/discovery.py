"""mDNS advertisement and browsing so clients find hosts automatically."""
from __future__ import annotations

import socket
import time
from typing import Any

SERVICE = "_precisionkvm._tcp.local."


def _addresses() -> list[bytes]:
    found = {answer[4][0] for answer in
             socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    found.discard("127.0.0.1")
    packed = [socket.inet_aton(str(address)) for address in sorted(found)]
    return packed or [socket.inet_aton("127.0.0.1")]


class Advertiser:
    """Broadcasts this host on the LAN; pairing still requires the token."""
    def __init__(self, port: int, properties: dict | None = None):
        self.port = int(port)
        self.properties = {str(k): str(v) for k, v in (properties or {}).items()}
        self._zeroconf: Any = None
        self._info: Any = None

    def start(self):
        from zeroconf import ServiceInfo, Zeroconf
        if self._zeroconf:
            return
        host = socket.gethostname()
        name = f"PrecisionKVM {host}.{SERVICE}"
        self._info = ServiceInfo(SERVICE, name, addresses=_addresses(),
                                 port=self.port, properties=self.properties,
                                 server=f"{host}.local.")
        self._zeroconf = Zeroconf()
        self._zeroconf.register_service(self._info)

    def stop(self):
        if self._zeroconf:
            try:
                if self._info:
                    self._zeroconf.unregister_service(self._info)
            finally:
                self._zeroconf.close()
            self._zeroconf = self._info = None


def browse(timeout: float = 2.5) -> list[dict]:
    """Return advertised hosts as {name, host, port, tls, fingerprint}."""
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    found: list[dict] = []
    zeroconf = Zeroconf()
    try:
        class Listener(ServiceListener):
            def add_service(self, zc, service_type, name):
                info = zc.get_service_info(service_type, name, timeout=800)
                if info:
                    found.append(_describe(info))
            def update_service(self, zc, service_type, name):
                self.add_service(zc, service_type, name)
            def remove_service(self, zc, service_type, name):
                pass

        ServiceBrowser(zeroconf, SERVICE, Listener())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not found:
            time.sleep(0.05)
        time.sleep(0.1)  # let simultaneous advertisers settle
    finally:
        zeroconf.close()
    return found


def _describe(info) -> dict:
    props = {}
    for key, value in (info.properties or {}).items():
        key = key.decode() if isinstance(key, bytes) else str(key)
        value = value.decode() if isinstance(value, bytes) else value
        props[key] = value
    return {"name": info.name.removesuffix(f".{SERVICE}"),
            "host": info.parsed_addresses()[0] if info.parsed_addresses() else "",
            "port": int(info.port), "tls": props.get("tls", "1") != "0",
            "fingerprint": props.get("fp", "")}
