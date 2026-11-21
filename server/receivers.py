"""接收端实现：Kodi(JSON-RPC) 和 浏览器(网页) 两种。"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.request

from group import Receiver

LOG = logging.getLogger("receivers")


def _jsonrpc(url: str, method: str, params: dict | None = None, timeout: float = 3.0,
             auth: str | None = None) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params or {}}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


class KodiReceiver(Receiver):
    """通过 Kodi 的 JSON-RPC 控制电视上的 Kodi。

    需要电视上的 Kodi 打开：设置 -> 服务 -> 控制 -> 允许通过 HTTP 远程控制。
    """

    kind = "kodi"

    def __init__(self, rid: str, name: str, host: str, port: int = 8080,
                 username: str = "", password: str = ""):
        self.id = rid
        self.name = name
        self.host = host
        self.port = int(port)
        self.url = "http://%s:%d/jsonrpc" % (host, self.port)
        self.auth = None
        if username:
            self.auth = "%s:%s" % (username, password)
        self._playerid: int | None = None

    def _call(self, method: str, params: dict | None = None) -> dict:
        return _jsonrpc(self.url, method, params, auth=self.auth)

    # ---- 控制 ----
    def load(self, uri: str, metadata: str = "") -> None:
        try:
            self._call("Player.Open", {"item": {"file": uri}})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("[%s] Kodi 打开失败: %s", self.id, exc)
            raise

    def _current_player(self) -> int | None:
        try:
            res = self._call("Player.GetActivePlayers")
            players = res.get("result") or []
            for p in players:
                if p.get("type") == "video":
                    self._playerid = p.get("playerid")
                    return self._playerid
            if players:
                self._playerid = players[0].get("playerid")
                return self._playerid
        except Exception as exc:  # noqa: BLE001
            LOG.debug("[%s] 获取播放器失败: %s", self.id, exc)
        self._playerid = None
        return None

    def play(self, position: float | None = None) -> None:
        pid = self._current_player()
        if pid is None:
            if self._last_uri:
                self.load(self._last_uri)
            return
        try:
            self._call("Player.PlayPause", {"playerid": pid, "play": True})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("[%s] Kodi 播放失败: %s", self.id, exc)
            raise

    def pause(self) -> None:
        pid = self._current_player() or self._playerid
        if pid is None:
            return
        try:
            self._call("Player.PlayPause", {"playerid": pid, "play": False})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("[%s] Kodi 暂停失败: %s", self.id, exc)

    def seek(self, position: float) -> None:
        pid = self._current_player() or self._playerid
        if pid is None:
            return
        total = max(0.0, float(position))
        h = int(total // 3600)
        m = int((total % 3600) // 60)
        s = int(total % 60)
        ms = int((total - int(total)) * 1000)
        try:
            self._call("Player.Seek", {"playerid": pid, "value": {
                "time": {"hours": h, "minutes": m, "seconds": s, "milliseconds": ms}}})
        except Exception as exc:  # noqa: BLE001
            LOG.debug("[%s] Kodi 跳转失败: %s", self.id, exc)

    def stop(self) -> None:
        pid = self._current_player() or self._playerid
        if pid is None:
            return
        try:
            self._call("Player.Stop", {"playerid": pid})
        except Exception as exc:  # noqa: BLE001
            LOG.debug("[%s] Kodi 停止失败: %s", self.id, exc)

    def set_volume(self, volume: int) -> None:
        try:
            self._call("Application.SetVolume", {"volume": int(volume)})
        except Exception as exc:  # noqa: BLE001
            LOG.debug("[%s] Kodi 音量失败: %s", self.id, exc)

    # ---- 状态 ----
    _last_uri: str = ""

    def status(self) -> dict:
        pid = self._current_player()
        if pid is None:
            return {"state": "idle", "position": 0.0}
        try:
            res = self._call("Player.GetProperties", {
                "playerid": pid,
                "properties": ["position", "speed", "paused", "totaltime"]})
        except Exception as exc:  # noqa: BLE001
            LOG.debug("[%s] 读取属性失败: %s", self.id, exc)
            return {"state": "unknown", "position": 0.0}
        result = res.get("result") or {}
        pos = _kodi_time_to_seconds(result.get("position", 0))
        paused = bool(result.get("paused")) or float(result.get("speed") or 1) == 0.0
        return {"state": "paused" if paused else "playing", "position": pos}


def _kodi_time_to_seconds(value) -> float:
    if isinstance(value, dict):
        hours = float(value.get("hours") or 0)
        minutes = float(value.get("minutes") or 0)
        seconds = float(value.get("seconds") or 0)
        millis = float(value.get("milliseconds") or 0)
        return hours * 3600 + minutes * 60 + seconds + millis / 1000.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class WebReceiver(Receiver):
    """浏览器/网页接收端：服务端把指令排队，页面轮询取指令、回报状态。

    用于在电脑、手机浏览器上先验证同步效果，不依赖电视。
    """

    kind = "web"

    def __init__(self, rid: str, name: str):
        self.id = rid
        self.name = name
        self._lock = threading.Lock()
        self._seq = 0
        # 上一次返回给接收端的水位。接收端下一次必须带着不小于它的序号回来，
        # 否则说明它重启/刷新了，队里的旧指令作废。
        self._expect_since = 0
        self._queue: list[dict] = []
        self._status = {"state": "idle", "position": 0.0}
        self._last_seen = time.time()
        # App/网页重启次数。Group 发现它变大就立刻按当前时间轴接回去，
        # 用计数器而不是布尔标志，免得轮询/上报的时序把事件吃掉。
        self._reconnects = 0

    # ---- 给网页的接口 ----
    def poll(self, since: int) -> tuple[list[dict], int]:
        with self._lock:
            # 接收端重启/网页刷新后序号会归零。队里攒着的是旧时间轴的指令
            # （比如几分钟前的 seek），重放会把电视拉回旧位置，所以直接丢掉，
            # 交给 Group 按当前时间轴重新下发 load/play/seek。
            if since < self._expect_since:
                dropped = len(self._queue)
                self._queue = []
                self._reconnects += 1
                LOG.info("[%s] 接收端重新连接（序号 %d -> %d），丢弃旧指令 %d 条",
                         self.id, self._expect_since, since, dropped)
            # 服务端自己重启后序号从 0 开始，但接收端还记着上一次的水位。
            # 以接收端为准继续往后编号，别让水位倒退（否则它会一直以为自己重启了）。
            if since > self._seq:
                self._seq = since
            cmds = [c for c in self._queue if c["seq"] > since]
            self._expect_since = max(self._seq, since)
            # 轮询本身就是"我还活着"的信号，用不着等下一份状态上报
            self._last_seen = time.time()
            return cmds, self._expect_since

    def report(self, payload: dict) -> None:
        with self._lock:
            self._status = {"state": payload.get("state", "unknown"),
                            "position": float(payload.get("position") or 0.0),
                            "duration": float(payload.get("duration") or 0.0),
                            # 电视端报告"画面已被其他投屏接管"，用于整组让位
                            "preempted": bool(payload.get("preempted"))}
            name = payload.get("name")
            if isinstance(name, str) and name.strip():
                self.name = name.strip()[:32]
            self._last_seen = time.time()

    def online(self) -> bool:
        return (time.time() - self._last_seen) < 15

    def _push(self, cmd: dict) -> None:
        with self._lock:
            self._seq += 1
            cmd = dict(cmd)
            cmd["seq"] = self._seq
            self._queue.append(cmd)
            if len(self._queue) > 200:
                self._queue = self._queue[-100:]

    # ---- Receiver 接口 ----
    def load(self, uri: str, metadata: str = "") -> None:
        self._push({"cmd": "load", "uri": uri, "metadata": metadata})

    def play(self, position: float | None = None) -> None:
        self._push({"cmd": "play", "position": position})

    def pause(self) -> None:
        self._push({"cmd": "pause"})

    def seek(self, position: float) -> None:
        self._push({"cmd": "seek", "position": position})

    def stop(self) -> None:
        self._push({"cmd": "stop"})

    def set_volume(self, volume: int) -> None:
        self._push({"cmd": "volume", "volume": volume})

    def set_speed(self, speed: float) -> None:
        self._push({"cmd": "speed", "speed": round(float(speed), 3)})

    def status(self) -> dict:
        with self._lock:
            if not self.online():
                return {"state": "idle", "position": 0.0}
            st = dict(self._status)
            # 重启次数递增时，Group 用它触发"立刻接回时间轴"
            st["reconnects"] = self._reconnects
            return st
