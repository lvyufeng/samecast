"""SameCast 主程序：DLNA 门面 + 组同步 + Kodi 接收端 + 网页接收端。"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import bili
import dmr
import nvaext
from group import Group
from receivers import KodiReceiver, WebReceiver
from ssdp import SSDPServer

LOG = logging.getLogger("app")
VERSION = "0.6.0"
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
    def __init__(self, config: dict, config_path: str = ""):
        self.config = config
        self.config_path = config_path
        self.port = int(config.get("port", 8090))
        self.ip = config.get("advertise_ip") or local_ip()
        self.base_url = config.get("base_url") or "http://%s:%d" % (self.ip, self.port)
        # 默认带上「我的小电视」前缀：B 站手机 App 只对名字以它开头的设备放开 1080P
        self.friendly_name = config.get("friendly_name") or "我的小电视-SameCast"

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
        # 手机 App 投屏 B 站视频时由服务端接管，换成最高清晰度的地址再下发
        self.facade.on_cast = self._on_phone_cast
        self._takeover_key = ""
        self._takeover_at = 0.0
        self._takeover_taken = False
        # 「小电视」私有协议投屏时记下视频标识，后面换清晰度要用
        self._bili_cast: dict = {}
        self.ssdp = SSDPServer(
            udn=udn,
            # 与真实小电视一致：设备描述放在 /bilibili/description.xml
            location=self.base_url + "/bilibili/description.xml",
            friendly_name=self.friendly_name,
            services=[dmr.AVT, dmr.RCS, dmr.CMS, dmr.NVA],
            announce_interval=int(config.get("announce_interval", 300)),
            extra_st=config.get("discovery_st", DISCOVERY_ST),
        )
        # 「小电视」私有协议要报当前播放状态和清晰度；默认按 1080P 应答
        self._bili_qn = 80
        self.bili = None
        try:
            self.bili = bili.Bili(config.get("bilibili") or {}, on_cookie=self._save_bili_cookie)
        except Exception:  # noqa: BLE001
            LOG.exception("B 站模块初始化失败，投屏功能不受影响")
        # 「小电视」私有协议：B 站 App 会问登录状态、会员状态和当前播放状态
        self.facade.on_bili_signed_in = self._bili_signed_in
        self.facade.on_bili_vip = self._bili_vip
        self.facade.on_bili_play_info = self._bili_play_info
        self.facade.on_bili_quality = self._bili_switch_quality
        self.facade.on_mirror_url = self._bili_mirror_url
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

    # ---------- B 站「小电视」模拟 ----------
    def _save_bili_cookie(self, sessdata: str) -> None:
        """扫码登录成功后，只把 SESSDATA 写进服务器本地 config.json，不外传、不打日志。"""
        block = self.config.setdefault("bilibili", {})
        if sessdata:
            block["sessdata"] = sessdata
        else:
            block.pop("sessdata", None)
        if self.config_path:
            try:
                save_config(self.config_path, self.config)
            except OSError:
                LOG.exception("保存 SESSDATA 到配置文件失败")

    # ---------- 小电视私有协议用的账号/播放状态 ----------
    def _bili_signed_in(self) -> bool:
        if self.bili is None:
            return False
        try:
            return bool(self.bili.logged_in())
        except Exception:  # noqa: BLE001
            return False

    def _bili_vip(self) -> bool:
        if self.bili is None:
            return False
        try:
            return bool(self.bili.nav().get("vip"))
        except Exception:  # noqa: BLE001
            return False

    # 清晰度档位：qn 是 B 站协议里的编号，电视端选轨按高度做
    _QN_BY_HEIGHT = {360: 16, 480: 32, 720: 64, 1080: 80}

    def _bili_play_info(self) -> dict:
        """当前播放状态，按官方电视端 PlayInfo 的字段回给手机（不在播就回空）。"""
        try:
            snap = self.group.snapshot()
        except Exception:  # noqa: BLE001
            return {}
        uri = str(snap.get("uri") or "")
        if not uri:
            return {}
        state = str(snap.get("state") or "")
        if state == "PLAYING":
            status = 4       # 官方电视端里 4 = 播放中
        elif state == "PAUSED_PLAYBACK":
            status = 3
        elif state == "STOPPED":
            status = 6       # 停止/播完
        else:
            status = 5       # 缓冲/加载中
        height = self._cast_bili_quality(uri)
        qn = self._QN_BY_HEIGHT.get(height) or self._bili_qn
        info = {"playerStatus": status,
                "position": int(float(snap.get("position") or 0)),
                "duration": int(float(snap.get("duration") or 0)),
                "danmakuStatus": 0,
                "qn": qn}
        cast = self._bili_cast
        if cast:
            # 官方电视端会把当前分集的标识一起回给手机，自动连播 / 选集要用
            info["aid"] = str(cast.get("aid") or "")
            info["cid"] = str(cast.get("cid") or "")
            info["epId"] = str(cast.get("ep_id") or "")
            info["seasonId"] = str(cast.get("season_id") or "")
            info["roomId"] = str(cast.get("room_id") or "")
        return info

    def _bili_switch_quality(self, qn: int) -> None:
        """手机点击换清晰度：记住档位，下一次 GetPlayInfo 回显。

        真正选哪条轨道由电视端按高度决定，这里不重新加载，避免打断播放。
        """
        try:
            qn = int(qn or 0)
        except (TypeError, ValueError):
            return
        if qn <= 0:
            return
        self._bili_qn = qn
        LOG.info("手机切换 B 站清晰度：qn=%d", qn)
        cast = self._bili_cast
        if cast:
            # 私有协议投屏时，官方电视端会换轨重放；取流比较慢，放到后台线程
            threading.Thread(target=self._reload_bili_cast_quality,
                             args=(cast, qn), daemon=True).start()

    def _bili_mirror_url(self) -> str:
        """官方电视端在「镜像投屏」时返回自己 5223 端口的地址。

        SameCast 只做视频投屏、不做镜像推送，但协议里这个字段必须有值，
        否则手机端可能直接报错；返回网关地址，让不支持的场景自然失败。
        """
        return "http://%s:5223" % self.ip

    def _bili_status(self) -> dict:
        if self.bili is None:
            return {"ok": False, "error": "B 站模块不可用"}
        try:
            info = self.bili.status()
        except Exception:  # noqa: BLE001
            LOG.debug("查询 B 站账号状态失败", exc_info=True)
            info = {"logged_in": False, "uname": "", "vip": False,
                    "max_quality": "720P", "hint": "查询失败，仍可粘贴链接播放"}
        info["ok"] = True
        info["receivers"] = len([r for r in self.receivers.values()])
        return info

    @staticmethod
    def _bili_metadata(title: str, quality: str) -> str:
        import html as _html
        label = (title or "B 站视频")[:120]
        if quality:
            label = label + " [" + quality + "]"
        safe = _html.escape(label, quote=False)
        return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
                ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
                ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
                '<item id="samecast-bili" parentID="-1" restricted="1">'
                '<dc:title>%s</dc:title>'
                '<upnp:class>object.item.videoItem</upnp:class>'
                '</item></DIDL-Lite>' % safe)

    def play_bili(self, text: str) -> dict:
        """解析 B 站链接 → 生成 MPD → 下发给所有电视（服务静态地址，不暴露 CDN 链接）。"""
        if self.bili is None:
            return {"ok": False, "error": "服务器没有启用 B 站模块"}
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "请粘贴 B 站视频链接或分享文字"}
        try:
            result = self.bili.resolve(text)
        except bili.BiliError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:  # noqa: BLE001
            LOG.exception("解析 B 站链接失败")
            return {"ok": False, "error": "解析失败，请换个链接再试"}
        meta = self._bili_metadata(result.get("title") or "", result.get("quality") or "")
        if result.get("kind") == "file":
            uri = result.get("url") or ""
        else:
            token = self.bili.register_stream(result["mpd"], result)
            uri = self.base_url + "/api/bili/stream/" + token + ".mpd"
        if not uri:
            return {"ok": False, "error": "没有拿到可播放地址"}
        self.group.load(uri, meta, source="bili")
        self.group.play(source="bili")
        LOG.info("B 站播放：%s [%s %dx%d] 共 %d 台", (result.get("title") or "")[:40],
                 result.get("quality") or "?", int(result.get("width") or 0),
                 int(result.get("height") or 0), len(self.receivers))
        return {"ok": True, "title": result.get("title") or "",
                "quality": result.get("quality") or "", "codec": result.get("codec") or "",
                "width": int(result.get("width") or 0), "height": int(result.get("height") or 0),
                "duration": float(result.get("duration") or 0),
                "receivers": len(self.receivers),
                "logged_in": self._bili_status().get("logged_in", False)}

    # ---------- 手机 App（B 站）投屏接管 ----------
    # 手机 App 私有投屏 nva_ext（官方电视端的 NvaExtContent）字段有新旧多种写法：
    # Gson 别名是 eid / sid / bd / sk / an 这类短名，旧版本还会再套一层 content。
    _CAST_KEYS = {
        "aid": ("aid", "avid"),
        "cid": ("cid", "pageId"),
        "ep_id": ("epId", "ep_id", "eid"),
        "season_id": ("seasonId", "season_id", "sid"),
        "room_id": ("roomId", "room_id"),
        "bvid": ("bvid", "bd"),
        "qn": ("userDesireQn", "user_desire_qn", "qn"),
        "content_type": ("contentType", "ct"),
        "seek": ("seekTs", "sk", "offset"),
        "auto_next": ("autoNext", "an"),
    }

    def _parse_bili_cast_uri(self, uri: str) -> dict | None:
        """解析手机 App 走「小电视」私有协议投屏时递来的 bilibili:// 地址。

        官方电视端只从这个地址里取视频标识（aid / epId / cid / 想要的清晰度），
        真正的播放地址由电视端自己再取一次，服务端也照做。
        """
        if not uri or "bilibili://" not in uri.lower():
            return None
        try:
            query = parse_qs(urlparse(uri).query, keep_blank_values=True)
        except ValueError:
            return None
        # 新版 App 会把参数压缩加密进 _nva_ext_（hex 或 base64url），旧版是明文 nva_ext
        raw = (query.get("nva_ext") or [""])[0] or (query.get("_nva_ext_") or [""])[0]
        if not raw:
            LOG.info("手机投屏地址里没有 nva_ext，交回直链接管逻辑")
            return None
        data = nvaext.decode(raw)
        if data is None:
            LOG.info("nva_ext 解不开（加密形态未知），交回直链接管逻辑")
            return None
        content = data.get("content")
        if not isinstance(content, dict):
            content = data
        cast: dict = {}
        for name, keys in self._CAST_KEYS.items():
            value = None
            for key in keys:
                for holder in (content, data):
                    if key in holder and holder[key] not in (None, ""):
                        value = holder[key]
                        break
                if value is not None:
                    break
            if value is None:
                continue
            try:
                if name == "seek":
                    cast[name] = float(value)
                elif name == "auto_next":
                    cast[name] = bool(value)
                elif name == "bvid":
                    cast[name] = str(value)
                else:
                    cast[name] = int(value)
            except (TypeError, ValueError):
                continue
        if not any(cast.get(key) for key in ("aid", "ep_id", "season_id")):
            LOG.info("nva_ext 里没有可识别的视频编号，交回直链接管逻辑")
            return None
        LOG.info("手机私有协议投屏：aid=%s ep=%s 分集=%s 请求清晰度=%s",
                 cast.get("aid") or "-", cast.get("ep_id") or "-",
                 cast.get("cid") or "-", cast.get("qn") or "-")
        return cast

    def _play_bili_cast(self, cast: dict) -> bool:
        """手机 B 站 App 走私有协议投屏：服务端按视频标识重新取流再下发。"""
        try:
            result = self.bili.resolve_cast(cast, qn=int(cast.get("qn") or 0))
        except bili.BiliError as exc:
            LOG.info("B 站私有协议投屏取流失败，交回直链接管逻辑：%s", exc)
            return False
        except Exception:  # noqa: BLE001 - 接管失败要回退，不能影响投屏
            LOG.exception("解析 B 站私有协议投屏地址失败")
            return False
        try:
            if result.get("kind") == "file":
                new_uri = result.get("url") or ""
            else:
                token = self.bili.register_stream(result["mpd"], result)
                new_uri = self.base_url + "/api/bili/stream/" + token + ".mpd"
        except Exception:  # noqa: BLE001
            LOG.exception("生成 B 站私有协议投屏地址失败")
            return False
        if not new_uri:
            return False
        meta = self._bili_metadata(result.get("title") or "", result.get("quality") or "")
        self._bili_cast = cast
        if result.get("quality_id"):
            self._bili_qn = int(result["quality_id"])
        self.group.load(new_uri, meta, source="bili")
        LOG.info("B 站手机投屏已接管（私有协议）：%s [%s] 共 %d 台",
                 (result.get("title") or "")[:40], result.get("quality") or "?",
                 len(self.receivers))
        return True

    def _reload_bili_cast_quality(self, cast: dict, qn: int) -> None:
        """手机在「小电视」上换清晰度：按新档位重新取流，尽量停在原进度。"""
        try:
            snap = self.group.snapshot()
            position = float(snap.get("position") or 0)
            paused = str(snap.get("state") or "").startswith("PAUSED")
            result = self.bili.resolve_cast(cast, qn=qn)
            if result.get("kind") == "file":
                new_uri = result.get("url") or ""
            else:
                token = self.bili.register_stream(result["mpd"], result)
                new_uri = self.base_url + "/api/bili/stream/" + token + ".mpd"
            if not new_uri:
                return
            meta = self._bili_metadata(result.get("title") or "",
                                       result.get("quality") or "")
            self.group.load(new_uri, meta, source="bili")
            self.group.play(position=position if position > 0 else None)
            if paused:
                self.group.pause()
            if result.get("quality_id"):
                self._bili_qn = int(result["quality_id"])
            LOG.info("B 站私有协议换清晰度：%s [%s]",
                     (result.get("title") or "")[:40], result.get("quality") or "?")
        except Exception:  # noqa: BLE001
            LOG.exception("B 站私有协议换清晰度失败")

    @staticmethod
    def _cast_bili_target(uri: str, metadata: str) -> dict | None:
        """从手机递来的投屏地址里认出 B 站视频目标（aid / bvid / ep_id）。"""
        blob = (uri or "") + "\n" + (metadata or "")
        lowered = blob.lower()
        if "bilivideo" not in lowered and "bilibili" not in lowered:
            return None
        for pattern, key in ((r"(?:^|[/?&=])ep(\d{3,})", "ep_id"),
                             (r"ep_id=(\d+)", "ep_id"),
                             (r"/(\d{6,})-1-", "aid"),
                             (r"(?:^|[?&])aid=(\d{4,})", "aid"),
                             (r"(BV[0-9A-Za-z]{10})", "bvid")):
            match = re.search(pattern, blob)
            if not match:
                continue
            value = match.group(1)
            return {"what": key, key: value if key == "bvid" else int(value)}
        return None

    # B 站 App 投屏直链的文件名末尾带清晰度编号：<aid>-1-<qn>.mp4
    _QN_HEIGHT = {16: 360, 32: 480, 64: 720, 74: 720, 80: 1080, 112: 1080,
                  116: 1080, 120: 2160, 125: 2160, 126: 2160, 127: 4320,
                  192: 1080}

    def _cast_bili_quality(self, uri: str) -> int:
        """从手机投屏直链猜出 App 自己选的清晰度（高度），猜不到返回 0。"""
        match = re.search(r"-1-(\d{1,4})\.(?:mp4|m4s|flv|ts)(?=$|[?&])", uri or "")
        if not match:
            return 0
        try:
            return self._QN_HEIGHT.get(int(match.group(1)), 0)
        except ValueError:
            return 0

    def _on_phone_cast(self, uri: str, metadata: str) -> bool:
        """接管手机 App 的 B 站投屏：服务端重新解析一遍，能更清晰才替换。

        手机 App 递过来的是它自己签名的 CDN 直链，清晰度是 App 里选的档位。
        服务端解析一遍既能绕开部分电视拉不到签名链接的情况，也能在登录后拿到更高清晰度；
        但如果服务端解析出来不比手机直链清晰，就保持手机原地址，绝不把画面降档。
        """
        if self.bili is None:
            return False
        # 「小电视」私有协议：手机递来的是 bilibili:// 地址，只带视频标识
        private = "bilibili://" in (uri or "").lower()
        cast = self._parse_bili_cast_uri(uri)
        if cast:
            if self._play_bili_cast(cast):
                return True
            if private:
                LOG.info("私有投屏取流失败，按已接管处理，避免电视播无效地址")
                return True
            return False
        target = self._cast_bili_target(uri, metadata)
        if not target:
            if private:
                # 认不出来的私有地址不能丢给电视，否则电视只会报播放失败
                LOG.info("「小电视」私有投屏地址里没有视频编号，忽略这次广播")
                return True
            return False
        key = "%s|%s" % (target, (uri or "")[:160])
        now = time.time()
        if self._takeover_key == key and now - self._takeover_at < 20.0:
            # App 重试 SetAVTransportURI 时不要重复解析，直接用上次的结论
            return self._takeover_taken
        self._takeover_key = key
        self._takeover_at = now
        self._takeover_taken = False
        try:
            if target.get("ep_id"):
                result = self.bili.resolve(
                    "https://www.bilibili.com/bangumi/play/ep%d" % target["ep_id"])
            elif target.get("bvid"):
                result = self.bili.resolve(target["bvid"])
            else:
                result = self.bili.resolve_aid(target["aid"])
        except Exception as exc:  # noqa: BLE001 - 接管失败要回退，不能影响投屏
            LOG.info("B 站手机投屏接管失败，沿用手机原始地址：%s", exc)
            return False
        server_height = int(result.get("height") or 0)
        app_height = self._cast_bili_quality(uri)
        if app_height and server_height <= app_height:
            LOG.info("B 站手机投屏保持原地址：手机 %dP，服务端 %dP，不降档",
                     app_height, server_height)
            return False
        meta = self._bili_metadata(result.get("title") or "", result.get("quality") or "")
        if result.get("kind") == "file":
            new_uri = result.get("url") or ""
        else:
            token = self.bili.register_stream(result["mpd"], result)
            new_uri = self.base_url + "/api/bili/stream/" + token + ".mpd"
        if not new_uri:
            return False
        self._takeover_taken = True
        self.group.load(new_uri, meta, source="bili")
        LOG.info("B 站手机投屏已接管：%s [%s %dx%d] 共 %d 台",
                 (result.get("title") or "")[:40], result.get("quality") or "?",
                 int(result.get("width") or 0), int(result.get("height") or 0),
                 len(self.receivers))
        return True

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
    server_version = "SameCast/0.6.0"

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
        if body and not getattr(self, "_head_only", False):
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

    # ---- HEAD：部分电视 / App 会先探活，头照给、正文留空 ----
    def do_HEAD(self) -> None:  # noqa: N802
        self._head_only = True
        try:
            self.do_GET()
        except Exception:  # noqa: BLE001
            LOG.debug("HEAD %s 失败", self.path, exc_info=True)
        finally:
            self._head_only = False

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
            if path in ("/device.xml", "/bilibili/description.xml"):
                LOG.info("设备描述请求：%s UA=%s", self.client_address[0],
                         (self.headers.get("USER-AGENT") or "")[:40])
                return self._send(200, app.facade.device_xml(), "text/xml; charset=utf-8")
            svc, svc_kind = dmr.parse_service_path(path)
            if svc is not None and svc_kind == "scpd":
                return self._send(200, app.facade.scpd(svc), "text/xml; charset=utf-8")
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
                                             "volume": snap["volume"],
                                             "source": snap.get("source") or ""}})
            if path == "/api/receivers":
                return self._json([r.describe() for r in app.receivers.values()])
            if path == "/api/info":
                return self._json({"name": app.friendly_name, "version": VERSION,
                                   "discovery_st": DISCOVERY_ST,
                                   "bilibili": app.bili is not None,
                                   "receivers": [r.describe() for r in app.receivers.values()]})
            if path == "/api/bili/status":
                return self._json(app._bili_status())
            if path.startswith("/api/bili/stream/"):
                token = path[len("/api/bili/stream/"):].strip("/")
                if token.endswith(".mpd"):
                    token = token[:-4]
                entry = app.bili.get_stream(token) if app.bili is not None else None
                if not entry:
                    return self._send(404, b"stream expired")
                # MPD 里是 B 站 CDN 的限时直链，只给电视端，不写进页面和日志
                return self._send(200, entry["mpd"].encode("utf-8"),
                                  "application/dash+xml; charset=utf-8")
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
            svc, svc_kind = dmr.parse_service_path(path)
            if svc is not None and svc_kind == "soap":
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
                elif cmd == "bili":
                    return self._json(app.play_bili(payload.get("text") or ""))
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
            if path == "/api/bili/play":
                payload = self._read_json()
                return self._json(app.play_bili(payload.get("text") or ""))
            if path == "/api/bili/qr":
                if app.bili is None:
                    return self._json({"ok": False, "error": "服务器没有启用 B 站模块"})
                try:
                    data = app.bili.qr_start()
                except bili.BiliError as exc:
                    return self._json({"ok": False, "error": str(exc)})
                except Exception:  # noqa: BLE001
                    LOG.exception("生成登录二维码失败")
                    return self._json({"ok": False, "error": "生成登录二维码失败，请稍后再试"})
                data["ok"] = True
                return self._json(data)
            if path == "/api/bili/qr/poll":
                if app.bili is None:
                    return self._json({"ok": False, "error": "服务器没有启用 B 站模块"})
                payload = self._read_json()
                try:
                    data = app.bili.qr_poll(payload.get("key") or "")
                except bili.BiliError as exc:
                    return self._json({"ok": False, "error": str(exc)})
                except Exception:  # noqa: BLE001
                    LOG.exception("查询扫码状态失败")
                    return self._json({"ok": False, "error": "查询扫码状态失败，请稍后再试"})
                data["ok"] = True
                return self._json(data)
            if path == "/api/bili/logout":
                if app.bili is not None:
                    app.bili.logout()  # 会同时清掉配置文件里的 SESSDATA
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
        elif cmd == "bili":
            self.app.play_bili(payload.get("text") or "")

    def _handle_soap(self, path: str) -> None:
        body = self._read_body()
        service_type, action, args = dmr.parse_soap(body)
        if action == "SetAVTransportURI":
            LOG.info("投屏请求来自 %s UA=%s", self.client_address[0],
                     (self.headers.get("USER-AGENT") or "")[:40])
        safe = {k: re.sub(r"https?://[^\s]+", "<redacted-url>", str(v))[:60]
                for k, v in args.items()}
        LOG.info("SOAP %s#%s %s", service_type, action, safe)
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
        service, service_kind = dmr.parse_service_path(path)
        if service is None or service_kind != "gena":
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


def save_config(path: str, config: dict) -> None:
    """原子写配置文件（只用于补记 SESSDATA，别把运行态写进去）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def main() -> None:
    parser = argparse.ArgumentParser(description="多台电视同步投屏网关")
    parser.add_argument("-c", "--config", default=os.path.join(BASE_DIR, "..", "config.json"))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s")
    config = load_config(args.config)
    app = App(config, args.config)
    try:
        app.start()
    except KeyboardInterrupt:
        LOG.info("退出")
    finally:
        app.shutdown()


if __name__ == "__main__":
    sys.exit(main())
