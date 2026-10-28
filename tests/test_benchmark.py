import socket
import threading
import tempfile
import hashlib
import datetime
from pathlib import Path
import unittest

from precision_kvm.benchmark import echo, percentile, probe
from precision_kvm.transport import FramedSocket, TLSListener, authenticate, connect_tls


class BenchmarkTests(unittest.TestCase):
    def test_percentiles(self):
        self.assertIsNone(percentile([], .5))
        self.assertEqual(percentile([3, 1, 2, 4], .5), 2.5)
        self.assertEqual(percentile([3], .99), 3)

    def test_concurrent_echo_probe(self):
        left, right = socket.socketpair()
        server, client = FramedSocket(left), FramedSocket(right)
        errors = []
        def serve():
            try:
                echo(server)
            except Exception as exc:
                errors.append(exc)
            finally:
                server.close()
        worker = threading.Thread(target=serve)
        worker.start()
        report = probe(client, [120, 1000], .1)
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(report["errors"], [])
        self.assertFalse(report["hardware_latency_measured"])
        for result in report["results"]:
            self.assertGreater(result["sent"], 1)
            self.assertEqual(result["unanswered"], 0)
            self.assertEqual(result["queue_high_water"], 1)
            self.assertGreaterEqual(result["rtt_ms"]["max"], result["rtt_ms"]["p50"])

    def test_invalid_probe_rejected(self):
        left, right = socket.socketpair()
        server, client = FramedSocket(left), FramedSocket(right)
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        client.send({"type": "key", "key": 1})
        with self.assertRaises(ValueError):
            echo(server)

    def test_real_tls_sustained_duplex(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        private = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local-test")])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                       .public_key(private.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now - datetime.timedelta(minutes=1))
                       .not_valid_after(now + datetime.timedelta(days=1))
                       .sign(private, hashes.SHA256()))
        fingerprint = hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            cert, key = Path(temporary) / "cert.pem", Path(temporary) / "key.pem"
            cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key.write_bytes(private.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            listener = TLSListener(str(cert), str(key), "127.0.0.1", 0)
            self.addCleanup(listener.close)
            errors = []
            def serve():
                channel = None
                try:
                    channel = listener.accept()
                    authenticate(channel, "t" * 32, server=True)
                    echo(channel)
                except Exception as exc:
                    errors.append(exc)
                finally:
                    if channel:
                        channel.close()
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            channel = connect_tls("127.0.0.1", listener.sock.getsockname()[1], fingerprint)
            authenticate(channel, "t" * 32, server=False)
            report = probe(channel, [120, 240, 500, 1000], .25)
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(report["errors"], [])
            for result in report["results"]:
                self.assertEqual(result["unanswered"], 0)


if __name__ == "__main__":
    unittest.main()
