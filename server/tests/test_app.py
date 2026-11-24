"""HTTP 请求解析健壮性测试：投屏端会发奇怪的连接，服务端不能崩。

历史 bug：BaseHTTPRequestHandler.parse_request() 在早期就 send_error(400)，
此时 self.path 还不存在，我们重写的 log_request() 直接读 self.path 会抛
AttributeError，于是 400 回不出去、连接被静默断开，手机端投屏流程就断了。
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


class _Server:
    """只挂 Handler 的临时 HTTP 服务，用来回放畸形请求。"""

    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.httpd.app = None  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class MalformedRequestTest(unittest.TestCase):
    def _roundtrip(self, payload: bytes) -> bytes:
        with _Server() as srv:
            with socket.create_connection(("127.0.0.1", srv.port), timeout=5) as sock:
                sock.sendall(payload)
                sock.shutdown(socket.SHUT_WR)
                chunks = []
                while True:
                    data = sock.recv(4096)
                    if not data:
                        break
                    chunks.append(data)
        return b"".join(chunks)

    def test_non_http_request_line_gets_400(self) -> None:
        """非 HTTP 的握手行（自定义协议/二进制）要回 400，而不是崩溃断连。"""
        response = self._roundtrip(b"HELLO 1.0 blink\r\n\r\n")
        self.assertTrue(response.startswith(b"HTTP/1.1 400"), response[:120])

    def test_bad_http_version_gets_400(self) -> None:
        response = self._roundtrip(b"GET /device.xml HTTP/9\r\n\r\n")
        self.assertTrue(response.startswith(b"HTTP/1.1 400"), response[:120])

    def test_empty_request_line_does_not_crash(self) -> None:
        response = self._roundtrip(b"\r\n")
        # 空行不会进入错误路径，服务端直接关连接即可
        self.assertFalse(response.startswith(b"HTTP/1.1 5"), response[:120])

    def test_redacts_urls_in_bad_line(self) -> None:
        text = app._redact_bytes(b"GET https://x.example.com/a?token=secret BAD\r\n")
        self.assertIn("<redacted-url>", text)
        self.assertNotIn("secret", text)
