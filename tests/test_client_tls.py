"""Реальный HTTPS на loopback: доверие, имя узла и отсутствие insecure fallback."""
import hashlib
import http.server
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from tender_radar.client import EisClient, CA_BUNDLE


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'<html>' + b'x' * 1100 + b'</html>'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ClientTLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cert = Path(cls.tmp.name) / 'localhost.pem'
        key = Path(cls.tmp.name) / 'localhost.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-days', '1', '-subj', '/CN=localhost',
                        '-addext', 'subjectAltName=DNS:localhost',
                        '-keyout', str(key), '-out', str(cls.cert)],
                       check=True, capture_output=True)
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Page)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cls.cert, key)
        cls.server.socket = ctx.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    def test_trusted_chain_and_hostname_accepted(self):
        client = EisClient(delay=0, ca_bundle=self.cert)
        self.addCleanup(client.session.close)
        self.assertIn('<html>', client.get(f'https://localhost:{self.port}/'))

    def test_untrusted_chain_rejected_once_without_fallback(self):
        client = EisClient(delay=0, retries=3)
        self.addCleanup(client.session.close)
        with patch.object(client.session, 'get', wraps=client.session.get) as get:
            with self.assertRaisesRegex(RuntimeError, 'TLS'):
                client.get(f'https://localhost:{self.port}/')
        self.assertEqual(get.call_count, 1)

    def test_wrong_hostname_rejected_even_with_trusted_root(self):
        client = EisClient(delay=0, ca_bundle=self.cert)
        self.addCleanup(client.session.close)
        with self.assertRaisesRegex(RuntimeError, 'TLS'):
            client.get(f'https://127.0.0.1:{self.port}/')

    def test_bundled_root_fingerprint(self):
        der = ssl.PEM_cert_to_DER_cert(CA_BUNDLE.read_text())
        self.assertEqual(hashlib.sha256(der).hexdigest(),
                         'd26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31')


if __name__ == '__main__':
    unittest.main()
