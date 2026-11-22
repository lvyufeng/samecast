"""SameCast 主程序：DLNA 门面 + 组同步 + Kodi 接收端 + 网页接收端。"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import dmr
from group import Group
from receivers import KodiReceiver, WebReceiver
from ssdp import SSDPServer

LOG = logging.getLogger("app")
VERSION = "0.5.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 电视端 App 用这个 ST 做自动发现，避免在电视上输入 IP
DISCOVERY_ST = "urn:samecast:service:Gateway:1"
WEB_DIR = os.path.join(BASE_DIR, "..", "web")


def local_ip() -> str:
    """取一个局域网 IP（UDP connect 技巧，不会真的发包）。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


class App:
    def __init__(self, config: dict):
        self.config = config
        self.port = int(config.get("port", 8090))
        self.ip = config.get("advertise_ip") or local_ip()
        self.base_url = config.get("base_url") or "http://%s:%d" % (self.ip, self.port)
        self.friendly_name = config.get("friendly_name", "SameCast")

        self.receivers = {}
        for item in config.get("receivers", []):
            kind = item.get("kind")
            rid = item.get("id") or (kind + "-" + str(len(self.receivers) + 1))
            if kind == "kodi":
                rec = KodiReceiver(rid, item.get("name", rid), item.get("host", "127.0.0.1"),
                                   item.get("port", 8080), item.get("username", ""),
                                   item.get("password", ""))
            elif kind == "web":
                rec = WebReceiver(rid, item.get("name", rid))
            else:
                LOG.warning("未知接收端类型 %s，跳过", kind)
                continue
            self.receivers[rid] = rec

        if not self.receivers:
            LOG.warning("没有配置任何接收端，服务仍会启动（只有 DLNA 门面）")

        udn = config.get("udn") or self._load_udn()
        self.group = Group(list(self.receivers.values()), on_change=self._on_group_change,
                           drift_tolerance=float(config.get("drift_tolerance", 1.5)),
                           poll_interval=float(config.get("poll_interval", 1.0)),
                           seek_cooldown=float(config.get("seek_cooldown", 8.0)),
                           smooth_zone=float(config.get("smooth_zone", 2.0)),
                           hard_seek_zone=float(config.get("hard_seek_zone", 5.0)))
        self.facade = dmr.DmrFacade(self.group, udn, self.friendly_name, self.base_url)
        self.ssdp = SSDPServer(
            udn=udn,
            location=self.base_url + "/device.xml",
            friendly_name=self.friendly_name,
            services=[dmr.AVT, dmr.RCS, dmr.CMS],
            announce_interval=int(config.get("announce_interval", 300)),
            extra_st=config.get("discovery_st", DISCOVERY_ST),
        )
        self.httpd: ThreadingHTTPServer | None = None

    def _load_udn(self) -> str:
        path = os.path.join(BASE_DIR, "..", "data", "udn.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                value = fh.read().strip()
                if value:
                    return value
        value = str(uuidlib.uuid4())
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value)
        return value

    def ensure_receiver(self, rid: str, kind: str = ""):
        """电视端 App 首次上线时自动注册，不需要改配置重启。

        电视 App 和网页接收端共用同一套轮询协议，客户端用 kind 自报类型；
        不报的按电视端算（老版本 App），网页版会显式传 kind=web。
        """
        if kind not in ("app", "web"):
            kind = ""
        rec = self.receivers.get(rid)
        if isinstance(rec, WebReceiver):
            if kind and rec.kind != kind:
                rec.kind = kind
            return rec
        if rec is not None:
            return None
        rec = WebReceiver(rid, ("网页-" if kind == "web" else "电视-") + rid[:4])
        rec.kind = kind or "app"
        self.receivers[rid] = rec
        self.group.add_receiver(rec)
        return rec

    def _on_group_change(self) -> None:
        try:
            self.facade.notify_state()
        except Exception:  # noqa: BLE001
            LOG.exception("发送 GENA 事件失败")

    def start(self) -> None:
        ThreadingHTTPServer.allow_reuse_address = True
        self.httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self.httpd.app = self  # type: ignore[attr-defined]
        self.ssdp.start()
        self.group.start()
        LOG.info("服务已启动: %s  设备名=%s", self.base_url, self.friendly_name)
        LOG.info("接收端: %s", ", ".join("%s(%s)" % (r.name, r.kind) for r in self.receivers.values()))
        LOG.info("网页接收端测试地址: %s/tv?id=<接收端id>", self.base_url)
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.group.stop_loop()
        self.ssdp.stop()
        if self.httpd:
            self.httpd.shutdown()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SameCast/0.5.0"

    # ---- 工具 ----
    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # noqa: A003 - 让日志走 logging
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes = b"", content_type: str = "text/plain; charset=utf-8",
              extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> dict:
        try:
            return json.loads(self._read_body().decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    @staticmethod
    def _static(name: str) -> bytes:
        path = os.path.join(WEB_DIR, name)
        with open(path, "rb") as fh:
            return fh.read()

    # ---- GET ----
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        app = self.app
        try:
            if path == "/" or path == "/index.html":
                return self._send(200, self._static("dashboard.html"), "text/html; charset=utf-8")
            if path == "/tv" or path == "/tv.html":
                return self._send(200, self._static("tv.html"), "text/html; charset=utf-8")
            if path == "/device.xml":
                return self._send(200, app.facade.device_xml(), "text/xml; charset=utf-8")
            if path in ("/AVTransport/scpd.xml", "/RenderingControl/scpd.xml", "/ConnectionManager/scpd.xml"):
                service = {"/AVTransport/scpd.xml": dmr.AVT,
                           "/RenderingControl/scpd.xml": dmr.RCS,
                           "/ConnectionManager/scpd.xml": dmr.CMS}[path]
                return self._send(200, app.facade.scpd(service), "text/xml; charset=utf-8")
            if path == "/api/status":
                return self._json(app.group.snapshot())
            if path.startswith("/api/recv/") and path.endswith("/poll"):
                rid = path[len("/api/recv/"):-len("/poll")].strip("/")
                rec = app.ensure_receiver(rid, (query.get("kind") or [""])[0])
                if not isinstance(rec, WebReceiver):
                    return self._json({"error": "unknown web receiver"}, 404)
                since = int((query.get("since") or ["0"])[0])
                cmds, seq = rec.poll(since)
                snap = app.group.snapshot()
                return self._json({"seq": seq, "commands": cmds,
                                   "group": {"state": snap["state"], "position": snap["position"],
                                             "volume": snap["volume"]}})
            if path == "/api/receivers":
                return self._json([r.describe() for r in app.receivers.values()])
            if path == "/api/info":
                return self._json({"name": app.friendly_name, "version": VERSION,
                                   "discovery_st": DISCOVERY_ST,
                                   "receivers": [r.describe() for r in app.receivers.values()]})
            return self._send(404, b"not found")
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            LOG.exception("GET %s 失败", path)
            self._send(500, b"internal error")

    # ---- POST：SOAP 控制 + 网页 API ----
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        kind = (query.get("kind") or [""])[0]
        app = self.app
        try:
            if path in ("/AVTransport/control", "/RenderingControl/control", "/ConnectionManager/control"):
                return self._handle_soap(path)
            if path == "/api/control":
                payload = self._read_json()
                cmd = payload.get("cmd")
                group = app.group
                if cmd == "play":
                    group.play(source="web-ui")
                elif cmd == "pause":
                    group.pause(source="web-ui")
                elif cmd == "stop":
                    group.stop(source="web-ui")
                elif cmd == "seek":
                    group.seek(float(payload.get("position") or 0), source="web-ui")
                elif cmd == "volume":
                    group.set_volume(float(payload.get("volume") or 0), source="web-ui")
                elif cmd == "load":
                    group.load(payload.get("uri", ""), payload.get("metadata", ""), source="web-ui")
                elif cmd == "resync":
                    # 电视端「同步」面板的「立即对齐」：不看冷却，直接把整组钉回同一位置
                    snap = group.snapshot()
                    snap["aligned"] = group.resync(source="web-ui")
                    return self._json(snap)
                else:
                    return self._json({"error": "unknown cmd"}, 400)
                return self._json(group.snapshot())
            if path.startswith("/api/recv/") and path.endswith("/state"):
                rid = path[len("/api/recv/"):-len("/state")].strip("/")
                rec = app.ensure_receiver(rid, kind)
                if not isinstance(rec, WebReceiver):
                    return self._json({"error": "unknown web receiver"}, 404)
                rec.report(self._read_json())
                return self._json({"ok": True})
            if path.startswith("/api/recv/") and path.endswith("/command"):
                rid = path[len("/api/recv/"):-len("/command")].strip("/")
                rec = app.ensure_receiver(rid, kind)
                if not isinstance(rec, WebReceiver):
                    return self._json({"error": "unknown web receiver"}, 404)
                payload = self._read_json()
                self._apply_remote_command(payload, source=rid)
                return self._json({"ok": True})
            return self._send(404, b"not found")
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            LOG.exception("POST %s 失败", path)
            self._send(500, b"internal error")

    def _apply_remote_command(self, payload: dict, source: str) -> None:
        group = self.app.group
        cmd = payload.get("cmd")
        if cmd == "play":
            group.play(source=source)
        elif cmd == "pause":
            group.pause(source=source)
        elif cmd == "stop":
            group.stop(source=source)
        elif cmd == "seek":
            group.seek(float(payload.get("position") or 0), source=source)
        elif cmd == "volume":
            group.set_volume(float(payload.get("volume") or 0), source=source)
        elif cmd == "resync":
            group.resync(source=source)

    def _handle_soap(self, path: str) -> None:
        body = self._read_body()
        service_type, action, args = dmr.parse_soap(body)
        LOG.info("SOAP %s#%s %s", service_type, action, {k: v[:60] for k, v in args.items()})
        try:
            out = self.app.facade.handle(service_type, action, args)
        except KeyError:
            return self._send(500, dmr.soap_fault(401, "Invalid Action"),
                              "text/xml; charset=utf-8")
        except Exception:  # noqa: BLE001
            LOG.exception("SOAP 处理失败")
            return self._send(500, dmr.soap_fault(501, "Action Failed"),
                              "text/xml; charset=utf-8")
        if out is None:
            out = {}
        self._send(200, dmr.soap_response(service_type, action, out), "text/xml; charset=utf-8")

    # ---- GENA 订阅 ----
    def do_SUBSCRIBE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        service = {"/AVTransport/event": dmr.AVT,
                   "/RenderingControl/event": dmr.RCS,
                   "/ConnectionManager/event": dmr.CMS}.get(path)
        if not service:
            return self._send(404, b"not found")
        sid = self.headers.get("SID")
        timeout = self._parse_timeout(self.headers.get("TIMEOUT"))
        if sid:
            ok = self.app.facade.renew(sid, timeout)
            if not ok:
                return self._send(412, b"precondition failed")
            return self._send(200, b"", "text/xml; charset=utf-8",
                              {"SID": sid, "TIMEOUT": "Second-%d" % timeout})
        callback = self.headers.get("CALLBACK") or ""
        if not callback:
            return self._send(412, b"missing CALLBACK")
        sid, timeout = self.app.facade.subscribe(service, callback, timeout)
        # 立即回一次当前状态，部分控制器依赖首次事件
        threading.Thread(target=self.app.facade.notify_state, daemon=True).start()
        return self._send(200, b"", "text/xml; charset=utf-8",
                          {"SID": sid, "TIMEOUT": "Second-%d" % timeout})

    def do_UNSUBSCRIBE(self) -> None:  # noqa: N802
        sid = self.headers.get("SID")
        if not sid or not self.app.facade.unsubscribe(sid):
            return self._send(412, b"precondition failed")
        return self._send(200, b"", "text/xml; charset=utf-8")

    @staticmethod
    def _parse_timeout(value: str | None) -> int:
        if not value:
            return 300
        value = value.strip()
        if value.lower().startswith("second-"):
            try:
                return max(30, min(int(value.split("-", 1)[1]), 1800))
            except ValueError:
                return 300
        return 300


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main() -> None:
    parser = argparse.ArgumentParser(description="多台电视同步投屏网关")
    parser.add_argument("-c", "--config", default=os.path.join(BASE_DIR, "..", "config.json"))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s")
    config = load_config(args.config)
    app = App(config)
    try:
        app.start()
    except KeyboardInterrupt:
        LOG.info("退出")
    finally:
        app.shutdown()


if __name__ == "__main__":
    sys.exit(main())
