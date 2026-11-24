"""SSDP responder: 在局域网里广播一个虚拟的 DLNA 渲染器(M-DMR)，让手机 App 的投屏按钮能搜到它。"""
from __future__ import annotations

import logging
import random
import socket
import threading
import time

LOG = logging.getLogger("ssdp")

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
MULTICAST_TTL = 2


class SSDPServer(threading.Thread):
    """监听 M-SEARCH 并回应，同时周期性发送 ssdp:alive 通告。"""

    def __init__(self, udn: str, location: str, friendly_name: str,
                 device_type: str = "urn:schemas-upnp-org:device:MediaRenderer:1",
                 services: list[str] | None = None,
                 announce_interval: int = 300,
                 extra_st: str | None = None,
                 gateway_location: str | None = None):
        super().__init__(daemon=True, name="ssdp")
        self.udn = udn if udn.lower().startswith("uuid:") else "uuid:" + udn
        self.location = location
        self.friendly_name = friendly_name
        self.device_type = device_type
        self.services = services or []
        self.announce_interval = announce_interval
        self.extra_st = extra_st
        # 自研接收端 App 按 LOCATION 去掉 "/device.xml" 得到 API 根地址，
        # 所以私有发现类型必须回 /device.xml，不能跟 B 站描述文件共用路径。
        self.gateway_location = gateway_location
        self._stop = threading.Event()
        self._sock = None
        self._last_usn_by_st: dict[str, int] = {}
        self._unknown_st: set[str] = set()

    # 手机端 App（B 站/爱奇艺/优酷等）可能只搜这些类型，统一应答
    COMPAT_ST = (
        "urn:schemas-upnp-org:device:MediaRenderer:1",
        "urn:schemas-upnp-org:device:MediaRenderer:2",
        "urn:schemas-upnp-org:device:MediaRenderer:3",
        "urn:schemas-upnp-org:service:AVTransport:1",
        "urn:schemas-upnp-org:service:AVTransport:2",
        "urn:schemas-upnp-org:service:RenderingControl:1",
        "urn:schemas-upnp-org:service:RenderingControl:2",
        "urn:schemas-upnp-org:service:ConnectionManager:1",
        "urn:schemas-upnp-org:service:ConnectionManager:2",
        "urn:dial-multiscreen-org:service:dial:1",
        # B 站小电视：云视听小电视用这个私有服务类型识别「小电视」，扫到才显示小电视图标并走官方高清投屏
        "urn:app-bilibili-com:service:NirvanaControl:3",
        "urn:app-bilibili-com:service:NirvanaControl:2",
        "urn:app-bilibili-com:service:NirvanaControl:1",
    )

    # -- 目标设备类型(ST)列表 ------------------------------------------------
    def _targets(self) -> list[tuple[str, str]]:
        """返回 (ST, USN) 列表。"""
        out = [("upnp:rootdevice", self.udn + "::upnp:rootdevice"),
               (self.udn, self.udn)]
        out.append((self.device_type, self.udn + "::" + self.device_type))
        for svc in self.services:
            out.append((svc, self.udn + "::" + svc))
        # 兼容 ST：部分国产 App 只查旧版本设备/服务类型，全部应答能明显提高发现率
        seen = {item[0] for item in out}
        for st in self.COMPAT_ST:
            if st not in seen:
                out.append((st, self.udn + "::" + st))
        if self.extra_st:
            # 给自研接收端 App 用的私有发现类型
            out.append((self.extra_st, self.udn + "::" + self.extra_st))
        return out

    def _open(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", SSDP_PORT))
        mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, MULTICAST_TTL)
        sock.settimeout(1.0)
        self._sock = sock

    def _location_for(self, st: str) -> str:
        if self.gateway_location and self.extra_st and st == self.extra_st:
            return self.gateway_location
        return self.location

    def _message(self, kind: str, st: str, usn: str) -> bytes:
        lines = [
            "NOTIFY * HTTP/1.1" if kind != "response" else "HTTP/1.1 200 OK",
            "HOST: %s:%d" % (SSDP_ADDR, SSDP_PORT) if kind != "response" else None,
            "CACHE-CONTROL: max-age=1800",
            "DATE: " + time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
            "EXT:",
            "LOCATION: " + self._location_for(st),
            "SERVER: Linux/3.0.0, UPnP/1.0, Platinum/1.0.5.13",
            "ST: " + st,
            "USN: " + usn,
            "NT: " + st if kind != "response" else None,
            "NTS: " + kind if kind != "response" else None,
        ]
        body = "\r\n".join([ln for ln in lines if ln is not None]) + "\r\n\r\n"
        return body.encode("utf-8")

    def announce(self, nts: str = "ssdp:alive") -> None:
        sock = self._sock
        if sock is None:  # 还没启动
            return
        for st, usn in self._targets():
            msg = self._message(nts, st, usn)
            for _ in range(2):
                try:
                    sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
                except OSError as exc:  # 网卡抖动时忽略
                    LOG.debug("announce failed: %s", exc)

    def _handle_msearch(self, data: bytes, addr) -> None:
        text = data.decode("utf-8", "ignore")
        lines = text.splitlines()
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().upper()] = v.strip()
        st = headers.get("ST", "").strip()
        if not st:
            return
        mx = headers.get("MX", "1")
        try:
            mx_val = min(max(int(mx), 1), 3)
        except ValueError:
            mx_val = 1
        targets = self._targets()
        matches = [t for t in targets if st == "ssdp:all" or st == t[0]]
        if not matches:
            # 没匹配到的 ST 只记一次，方便排查「手机 App 搜不到」的问题
            if st not in self._unknown_st:
                self._unknown_st.add(st)
                LOG.info("M-SEARCH from %s st=%s ua=%s -> 未匹配",
                         addr[0], st[:70] or "(空)",
                         (headers.get("USER-AGENT") or "")[:40])
            return
        # 按 SSDP 规范在 MX 秒内随机延迟回应，避免网络风暴
        if mx_val > 0:
            time.sleep(random.uniform(0.05, min(mx_val, 1.0)))
        # 用绑定在 1900 的接收 socket 回包：SSDP 规范要求回应来自 1900 端口，
        # 有些电视（如微鲸）只认这个源端口，用临时端口回它收不到。
        for target_st, usn in matches:
            msg = self._message("response", target_st, usn)
            try:
                self._sock.sendto(msg, addr)
            except OSError as exc:
                LOG.debug("reply failed: %s", exc)
        LOG.info("M-SEARCH from %s:%s st=%s ua=%s -> 已回应", addr[0], addr[1], st,
                 (headers.get("USER-AGENT") or "")[:30])

    def run(self) -> None:
        self._open()
        self.announce("ssdp:alive")
        last_announce = time.time()
        LOG.info("SSDP 已启动，设备名=%s", self.friendly_name)
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                data, addr = b"", None
            except OSError:
                break
            if data and addr:
                head = data[:20].upper()
                if head.startswith(b"M-SEARCH"):
                    try:
                        self._handle_msearch(data, addr)
                    except Exception:  # noqa: BLE001 - 单条报文异常不应影响服务
                        LOG.exception("处理 M-SEARCH 失败")
            if time.time() - last_announce > self.announce_interval:
                self.announce("ssdp:alive")
                last_announce = time.time()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.announce("ssdp:byebye")
        except Exception:  # noqa: BLE001
            pass
        sock = self._sock
        try:
            if sock:
                sock.close()
        except OSError:
            pass
