"""SSDP 应答单元测试：不占 1900 端口，用一个假 socket 验证明细。

回归点：M-SEARCH 的 200 OK 必须从绑定 1900 的接收 socket 发出。
之前用临时端口的另一只 socket 回包，微鲸电视收不到，导致一直“正在查找网关”。
"""
from __future__ import annotations

import logging
import os
import sys
import unittest
from unittest import mock

logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ssdp import SSDPServer  # noqa: E402

LOCATION = "http://192.168.1.10:8090/bilibili/description.xml"
GATEWAY_LOCATION = "http://192.168.1.10:8090/device.xml"
PRIVATE_ST = "urn:samecast:service:Gateway:1"
NVA_ST = "urn:app-bilibili-com:service:NirvanaControl:3"
MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 1\r\n"
    "ST: {st}\r\n\r\n"
)


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr) -> None:
        self.sent.append((data, addr))


def make_server() -> SSDPServer:
    srv = SSDPServer(
        udn="9c8f6a52-0000-4000-8000-000000000001",
        location=LOCATION,
        friendly_name="SameCast",
        services=["urn:schemas-upnp-org:service:AVTransport:1", NVA_ST],
        extra_st=PRIVATE_ST,
        gateway_location=GATEWAY_LOCATION,
        announce_interval=3600,
    )
    srv._sock = FakeSocket()
    return srv


class SSDPServerTest(unittest.TestCase):
    def test_reply_comes_from_bound_socket(self) -> None:
        srv = make_server()
        client = ("192.168.100.129", 40001)
        with mock.patch("ssdp.time.sleep", return_value=None):
            srv._handle_msearch(MSEARCH.format(st=PRIVATE_ST).encode(), client)

        sock = srv._sock
        assert isinstance(sock, FakeSocket)
        self.assertEqual(len(sock.sent), 1)
        data, addr = sock.sent[0]
        self.assertEqual(addr, client)
        text = data.decode()
        self.assertTrue(text.startswith("HTTP/1.1 200 OK\r\n"))
        self.assertIn("LOCATION: " + GATEWAY_LOCATION, text)
        self.assertIn("ST: " + PRIVATE_ST, text)
        self.assertIn("USN: uuid:9c8f6a52-0000-4000-8000-000000000001::" + PRIVATE_ST, text)
        # 没有第二只发送 socket 了，避免回包源端口不是 1900
        self.assertFalse(hasattr(srv, "_send_sock"))

    def test_ssdp_all_answers_every_target(self) -> None:
        srv = make_server()
        with mock.patch("ssdp.time.sleep", return_value=None):
            srv._handle_msearch(MSEARCH.format(st="ssdp:all").encode(), ("10.0.0.9", 1900))
        sock = srv._sock
        assert isinstance(sock, FakeSocket)
        sts = [part for data, _ in sock.sent for part in data.decode().split("\r\n") if part.startswith("ST: ")]
        self.assertIn("ST: upnp:rootdevice", sts)
        self.assertIn("ST: urn:schemas-upnp-org:device:MediaRenderer:1", sts)
        self.assertIn("ST: " + PRIVATE_ST, sts)

    def test_location_depends_on_target(self) -> None:
        """自研 App 按私有 ST 找网关（/device.xml），其他设备/服务一律给 B 站描述（/bilibili/description.xml）。

        回归点：LOCATION 曾经只有一个地址，旧版 App 从 /bilibili/description.xml 反推 API
        基础地址失败，导致服务端和电视 App 连不上。
        """
        srv = make_server()
        with mock.patch("ssdp.time.sleep", return_value=None):
            srv._handle_msearch(MSEARCH.format(st=PRIVATE_ST).encode(), ("10.0.0.9", 1900))
            srv._handle_msearch(MSEARCH.format(st="urn:schemas-upnp-org:device:MediaRenderer:1").encode(), ("10.0.0.9", 1900))
            srv._handle_msearch(MSEARCH.format(st=NVA_ST).encode(), ("10.0.0.9", 1900))
        sock = srv._sock
        assert isinstance(sock, FakeSocket)
        locations = [line for data, _ in sock.sent
                     for line in data.decode().split("\r\n") if line.startswith("LOCATION: ")]
        self.assertEqual(locations, ["LOCATION: " + GATEWAY_LOCATION,
                                     "LOCATION: " + LOCATION,
                                     "LOCATION: " + LOCATION])

    def test_nirvana_control_target_is_answered(self) -> None:
        """B 站手机 App 靠 NirvanaControl:3 认「小电视」；扫到这个类型才有小电视标志。"""
        srv = make_server()
        with mock.patch("ssdp.time.sleep", return_value=None):
            srv._handle_msearch(MSEARCH.format(st=NVA_ST).encode(), ("10.0.0.9", 1900))
        sock = srv._sock
        assert isinstance(sock, FakeSocket)
        self.assertEqual(len(sock.sent), 1)
        text = sock.sent[0][0].decode()
        self.assertIn("ST: " + NVA_ST, text)
        self.assertIn("::" + NVA_ST, text)

    def test_unknown_target_is_ignored(self) -> None:
        srv = make_server()
        with mock.patch("ssdp.time.sleep", return_value=None):
            srv._handle_msearch(MSEARCH.format(st="urn:schemas-upnp-org:device:MediaServer:1").encode(), ("10.0.0.9", 1900))
        sock = srv._sock
        assert isinstance(sock, FakeSocket)
        self.assertEqual(sock.sent, [])

    def test_missing_st_is_ignored(self) -> None:
        srv = make_server()
        srv._handle_msearch(b"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n\r\n", ("10.0.0.9", 1900))
        sock = srv._sock
        assert isinstance(sock, FakeSocket)
        self.assertEqual(sock.sent, [])


if __name__ == "__main__":
    unittest.main()
