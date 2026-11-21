"""SameCast 组控制器：统一时间轴、下发指令、接收各端状态、检测用户暂停、漂移校正。"""
from __future__ import annotations

import logging
import threading
import time

LOG = logging.getLogger("group")

# DLNA 的 AVTransport 状态值
STOPPED = "STOPPED"
PLAYING = "PLAYING"
PAUSED = "PAUSED_PLAYBACK"
TRANSITIONING = "TRANSITIONING"


class Receiver:
    """所有接收端(电视)的基类。"""

    id: str = "base"
    name: str = "receiver"
    kind: str = "base"

    def load(self, uri: str, metadata: str = "") -> None: ...
    def play(self, position: float | None = None) -> None: ...
    def pause(self) -> None: ...
    def seek(self, position: float) -> None: ...
    def stop(self) -> None: ...
    def set_volume(self, volume: int) -> None: ...
    def set_speed(self, speed: float) -> None: ...
    def status(self) -> dict:  # {"state": "playing|paused|stopped|idle|unknown", "position": float}
        return {"state": "unknown", "position": 0.0}

    def describe(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind}


class Group:
    def __init__(self, receivers: list[Receiver], on_change=None,
                 drift_tolerance: float = 1.5, poll_interval: float = 1.0,
                 seek_cooldown: float = 8.0, smooth_zone: float = 2.0,
                 hard_seek_zone: float = 5.0, stall_timeout: float = 20.0):
        self.receivers = {r.id: r for r in receivers}
        self.on_change = on_change
        # 小于 drift_tolerance 认为同步，同时用来学习每台设备的固定延迟基准
        self.drift_tolerance = drift_tolerance
        self.poll_interval = poll_interval
        # 校正策略：小偏差倍速微调，大偏差 seek，seek 之间受冷却限制
        self.seek_cooldown = float(seek_cooldown)
        self.smooth_zone = float(smooth_zone)
        self.hard_seek_zone = float(hard_seek_zone)
        # 电视一直缓冲不回来（源卡住/被限速）超过这个时长就按掉线处理，重新载入
        self.stall_timeout = float(stall_timeout)

        self._lock = threading.RLock()
        self.uri = ""
        self.metadata = ""
        self.title = ""
        self.duration = 0.0
        self.volume = 50
        self.state = STOPPED

        self._anchor_pos = 0.0
        self._anchor_time = time.monotonic()
        self._grace_until = 0.0
        self._loaded_receivers: set[str] = set()
        self._receiver_status: dict[str, dict] = {}
        self._prev_receiver_state: dict[str, str] = {}

        # 漂移校正：按设备基准延迟(bias)校准，小偏差用倍速微调，大偏差才 seek
        self._bias: dict[str, float] = {}
        self._last_seek: dict[str, float] = {}
        self._speed: dict[str, float] = {}
        self._buffering_since: dict[str, float] = {}
        # 已被其他投屏接管的接收端：不再自动拉回播放，避免和别的 App 抢画面
        self._preempted: set[str] = set()
        # 掉线重载的退避：避免源不可用时每轮都重发 load（无限重载风暴）
        self.reload_min_interval = 5.0
        self.reload_max_attempts = 3
        self._reload_last: dict[str, float] = {}
        self._reload_count: dict[str, int] = {}
        # 放弃重试后歇一会儿再放行新一轮：电视重启、重新打开 App 后能自己接回来
        self.reload_retry_cooldown = 30.0
        self._reload_giveup: dict[str, float] = {}
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- 时间轴 ----------
    def position(self) -> float:
        with self._lock:
            if self.state == PLAYING:
                pos = self._anchor_pos + (time.monotonic() - self._anchor_time)
            else:
                pos = self._anchor_pos
            if self.duration > 0:
                pos = min(pos, self.duration)
            return max(0.0, pos)

    def add_receiver(self, receiver: Receiver) -> None:
        """运行期动态加入的接收端（例如电视端 App 首次上线）。"""
        with self._lock:
            self.receivers[receiver.id] = receiver
        LOG.info("新接收端加入: %s (%s)", receiver.name, receiver.kind)
        self._notify()

    def position_if_stopped(self) -> float | None:
        with self._lock:
            return self._anchor_pos if self.state in (STOPPED, PAUSED) else None

    def _set_anchor(self, pos: float) -> None:
        self._anchor_pos = max(0.0, pos)
        self._anchor_time = time.monotonic()

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                LOG.exception("on_change 回调失败")

    # ---------- 指令 ----------
    def load(self, uri: str, metadata: str = "", source: str = "") -> None:
        uri = (uri or "").strip()
        if not uri:
            return
        with self._lock:
            changed = uri != self.uri
            self.uri = uri
            self.metadata = metadata or ""
            self.title = _extract_title(metadata) or _guess_name(uri)
            if changed:
                self.duration = 0.0
                self.state = STOPPED
                self._set_anchor(0.0)
                self._loaded_receivers.clear()
                self._bias.clear()
                self._speed.clear()
                self._last_seek.clear()
                self._buffering_since.clear()
                self._preempted.clear()
                self._reload_last.clear()
                self._reload_count.clear()
                self._reload_giveup.clear()
            LOG.info("载入 %s (来源 %s)", self.title or uri, source or "-")
        if changed:
            for rec in self.receivers.values():
                try:
                    rec.load(uri, metadata)
                    self._loaded_receivers.add(rec.id)
                except Exception:  # noqa: BLE001
                    LOG.exception("下发 load 给 %s 失败", rec.id)
        self._notify()

    def play(self, source: str = "", position: float | None = None) -> None:
        with self._lock:
            if not self.uri:
                return
            pos = position if position is not None else self._anchor_pos
            self.state = PLAYING
            self._set_anchor(pos)
            self._grace_until = time.monotonic() + 4.0
            for rid in list(self._speed):
                self._speed[rid] = 1.0
            self._reload_last.clear()
            self._reload_count.clear()
            self._reload_giveup.clear()
            recs = list(self.receivers.values())
            LOG.info("播放 @%.1fs (来源 %s)", pos, source or "-")
        for rec in recs:
            try:
                if rec.id not in self._loaded_receivers:
                    rec.load(self.uri, self.metadata)
                    with self._lock:
                        self._loaded_receivers.add(rec.id)
                rec.play(None)
            except Exception:  # noqa: BLE001
                LOG.exception("下发 play 给 %s 失败", rec.id)
        self._notify()

    def pause(self, source: str = "") -> None:
        with self._lock:
            if self.state != PLAYING:
                return
            pos = self.position()
            self.state = PAUSED
            self._set_anchor(pos)
            self._grace_until = time.monotonic() + 2.0
            recs = list(self.receivers.values())
            LOG.info("暂停 @%.1fs (来源 %s)", pos, source or "-")
        for rec in recs:
            try:
                rec.pause()
            except Exception:  # noqa: BLE001
                LOG.exception("下发 pause 给 %s 失败", rec.id)
        self._notify()

    def stop(self, source: str = "") -> None:
        with self._lock:
            self.state = STOPPED
            self._set_anchor(0.0)
            self._loaded_receivers.clear()
            recs = list(self.receivers.values())
            LOG.info("停止 (来源 %s)", source or "-")
        for rec in recs:
            try:
                rec.stop()
            except Exception:  # noqa: BLE001
                LOG.exception("下发 stop 给 %s 失败", rec.id)
        self._notify()

    def seek(self, position: float, source: str = "") -> None:
        with self._lock:
            if not self.uri:
                return
            pos = max(0.0, position)
            if self.duration > 0:
                pos = min(pos, self.duration)
            was_playing = self.state == PLAYING
            self._set_anchor(pos)
            self._grace_until = time.monotonic() + 3.0
            recs = list(self.receivers.values())
            LOG.info("跳转 %.1fs (来源 %s, 播放中=%s)", pos, source or "-", was_playing)
        for rec in recs:
            try:
                rec.seek(pos)
                if not was_playing:
                    rec.pause()
            except Exception:  # noqa: BLE001
                LOG.exception("下发 seek 给 %s 失败", rec.id)
        self._notify()

    def set_volume(self, volume: float, source: str = "") -> None:
        vol = max(0, min(100, int(round(volume))))
        with self._lock:
            self.volume = vol
            recs = list(self.receivers.values())
        for rec in recs:
            try:
                rec.set_volume(vol)
            except Exception:  # noqa: BLE001
                LOG.exception("下发 volume 给 %s 失败", rec.id)
        self._notify()

    # ---------- 状态 ----------
    def transport_state(self) -> str:
        with self._lock:
            if self.state == STOPPED and not self.uri:
                return "NO_MEDIA_PRESENT"
            return self.state

    def current_actions(self) -> str:
        with self._lock:
            if not self.uri:
                return "Play"
            if self.state == PLAYING:
                return "Pause,Stop,Seek"
            return "Play,Stop,Seek"

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "uri": self.uri,
                "title": self.title,
                "state": self.state,
                "position": round(self.position(), 2),
                "duration": round(self.duration, 2),
                "volume": self.volume,
                "receivers": [
                    {"id": r.id, "name": r.name, "kind": r.kind,
                     **self._receiver_status.get(r.id, {"state": "unknown", "position": 0.0})}
                    for r in self.receivers.values()
                ],
            }

    # ---------- 片尾 ----------
    def _finish_if_ended(self) -> bool:
        """播到片尾就自然结束，别把电视当成"掉线"反复重载。"""
        with self._lock:
            if self.state != PLAYING or self.duration <= 0 or not self.uri:
                return False
            if self.position() < self.duration - 0.5:
                return False
            dur = self.duration
            title = self.title or self.uri
            recs = list(self.receivers.values())
        LOG.info("%s 播放到片尾 %.1fs，结束", title, dur)
        with self._lock:
            self.state = STOPPED
            self._set_anchor(0.0)          # 重新播放时从头开始
            self._loaded_receivers.clear()
            self._speed.clear()
            self._buffering_since.clear()
            self._reload_last.clear()
            self._reload_count.clear()
        for rec in recs:
            try:
                rec.stop()
            except Exception:  # noqa: BLE001
                LOG.exception("片尾停止 %s 失败", rec.id)
        self._notify()
        return True

    # ---------- 后台循环 ----------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="group-sync")
        self._thread.start()

    def stop_loop(self) -> None:
        self._stop_evt.set()

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                LOG.exception("同步循环异常")
            self._stop_evt.wait(self.poll_interval)

    def tick(self) -> None:
        """轮询各接收端状态，做两件事：1) 检测电视端的用户操作 2) 校正进度漂移。"""
        self._finish_if_ended()
        for rid, rec in list(self.receivers.items()):
            try:
                st = rec.status()
            except Exception as exc:  # noqa: BLE001
                LOG.debug("读取 %s 状态失败: %s", rid, exc)
                continue
            prev = self._prev_receiver_state.get(rid)
            self._receiver_status[rid] = st
            self._prev_receiver_state[rid] = st.get("state", "unknown")
            # 电视端被其他投屏接管：立刻让位，不等宽限期（这不是用户操作）
            if st.get("preempted"):
                self._on_preempted(rid)
                continue
            self._preempted.discard(rid)
            if prev is None:
                continue
            if time.monotonic() < self._grace_until:
                continue
            self._react_to_receiver(rid, prev, st)
        self._notify()

    def _on_preempted(self, rid: str) -> None:
        """某台电视被别的投屏（电视自带或第三方 App）接管：整组停止让位。

        记入 _preempted 后不再自动把画面拉回来，免得和对方抢屏、抢声音；
        新的一次 load 或该端重新上报"未被抢占"才恢复正常。
        """
        rec = self.receivers.get(rid)
        name = rec.name if rec else rid
        with self._lock:
            first = rid not in self._preempted
            self._preempted.add(rid)
            state = self.state
        if not first:
            return
        LOG.info("%s(%s) 被其他投屏接管，SameCast 让出画面", name, rid)
        if state in (PLAYING, PAUSED) and self.uri:
            self.stop(source="preempted:" + rid)

    def _react_to_receiver(self, rid: str, prev: dict | str, st: dict) -> None:
        state = st.get("state", "unknown")
        prev_state = prev if isinstance(prev, str) else prev.get("state", "unknown")
        if state != "buffering":
            self._buffering_since.pop(rid, None)
        # 手机投屏时通常不带时长，从电视端上报里学习一次，避免进度越过片尾
        dur = float(st.get("duration") or 0.0)
        if dur > 1.0:
            with self._lock:
                if self.duration <= 0 or abs(self.duration - dur) > 2.0:
                    LOG.info("%s 端上报时长 %.1fs，同步到整组", rid, dur)
                    self.duration = dur
        with self._lock:
            group_state = self.state
            group_pos = self.position()
        if group_state == PLAYING and state == "paused" and prev_state not in ("paused", "unknown"):
            LOG.info("%s 端被暂停 -> 广播暂停", rid)
            self.pause(source=rid)
            return
        if group_state == PAUSED and state == "playing" and prev_state not in ("playing", "unknown"):
            LOG.info("%s 端被播放 -> 广播播放", rid)
            self.play(source=rid)
            return
        if group_state == PLAYING and state == "playing":
            self._reload_count.pop(rid, None)
            self._reload_last.pop(rid, None)
            self._correct_drift(rid, st, group_pos)
        elif state == "buffering":
            # 电视正在缓冲，别去 seek，越追越卡
            since = self._buffering_since.setdefault(rid, time.monotonic())
            # 一直缓冲不回来就是卡死了：按掉线处理，重载一次（带退避，源真挂了也不会风暴）
            if group_state in (PLAYING, PAUSED) and self.uri \
                    and rid not in self._preempted \
                    and time.monotonic() - since >= self.stall_timeout:
                self._buffering_since[rid] = time.monotonic()
                if self._should_reload(rid):
                    LOG.warning("%s 端缓冲超过 %.0fs，判定卡死，重新载入",
                                rid, self.stall_timeout)
                    self._reload_receiver(rid, group_state, group_pos)
        elif group_state in (PLAYING, PAUSED) and state in ("idle", "stopped") \
                and self.uri and rid not in self._preempted:
            if not self._should_reload(rid):
                return
            LOG.info("%s 端已掉出播放，重新载入", rid)
            self._reload_receiver(rid, group_state, group_pos)

    def _reload_receiver(self, rid: str, group_state: str, group_pos: float) -> None:
        """把一端重新拉回整组时间轴（掉线 / 卡死共用）。"""
        try:
            self.receivers[rid].load(self.uri, self.metadata)
            with self._lock:
                self._loaded_receivers.add(rid)
            if group_state == PLAYING:
                self.receivers[rid].play(None)
                self.receivers[rid].seek(group_pos)
            else:
                self.receivers[rid].seek(group_pos)
                self.receivers[rid].pause()
        except Exception:  # noqa: BLE001
            LOG.exception("重新载入 %s 失败", rid)

    def _should_reload(self, rid: str) -> bool:
        """退避：同一端两次重载至少间隔 reload_min_interval，连续失败超过上限就歇一会儿。

        源不可用（文件被删、服务挂了）时，否则会变成每轮一次 load 的无限重载风暴。
        但电视重启、重新打开 App 之后还得能自己接回来：停手 reload_retry_cooldown 秒后
        再放行新一轮；该端正常上报 playing 或来了新的播放指令会立即清零计数。
        """
        now = time.monotonic()
        with self._lock:
            last = self._reload_last.get(rid, 0.0)
            count = self._reload_count.get(rid, 0)
            if count >= self.reload_max_attempts:
                if count == self.reload_max_attempts:
                    self._reload_count[rid] = count + 1
                    self._reload_giveup[rid] = now
                    LOG.warning("%s 连续 %d 次重载失败，暂停自动重试（%.0fs 后再试）",
                                rid, count, self.reload_retry_cooldown)
                    return False
                if now - self._reload_giveup.get(rid, now) < self.reload_retry_cooldown:
                    return False
                LOG.info("%s 停手 %.0fs 后重新尝试载入", rid, self.reload_retry_cooldown)
                count = 0
                self._reload_count[rid] = 0
                self._reload_giveup.pop(rid, None)
                last = 0.0
            if now - last < self.reload_min_interval:
                return False
            self._reload_last[rid] = now
            self._reload_count[rid] = count + 1
        return True

    # ---------- 漂移校正 ----------
    def _apply_speed(self, rid: str, speed: float) -> None:
        speed = max(0.5, min(2.0, speed))
        if abs(self._speed.get(rid, 1.0) - speed) < 0.005:
            return
        rec = self.receivers.get(rid)
        if rec is None:
            return
        try:
            rec.set_speed(speed)
            self._speed[rid] = speed
        except Exception:  # noqa: BLE001
            LOG.debug("设置 %s 倍速失败", rid, exc_info=True)

    def _hard_seek(self, rid: str, position: float, residual: float) -> None:
        rec = self.receivers.get(rid)
        if rec is None:
            return
        self._last_seek[rid] = time.monotonic()
        self._speed[rid] = 1.0
        LOG.info("%s 偏差 %.2fs 超过阈值，seek 到 %.2fs", rid, residual, position)
        try:
            rec.set_speed(1.0)
        except Exception:  # noqa: BLE001
            pass
        try:
            rec.seek(position)
        except Exception:  # noqa: BLE001
            LOG.exception("校正 %s 失败", rid)

    def _correct_drift(self, rid: str, st: dict, group_pos: float) -> None:
        """按设备基准延迟校准漂移：小偏差倍速微调，大偏差才 seek。"""
        if self.duration > 0 and group_pos >= self.duration - 1.0:
            return                              # 已播到片尾，不必再追
        pos = float(st.get("position") or 0.0)
        drift = pos - group_pos                 # <0 表示电视落后于时间轴
        bias = self._bias.get(rid, 0.0)
        residual = drift - bias                 # 扣掉本机固定延迟后的真实偏差
        now = time.monotonic()

        if abs(residual) <= self.drift_tolerance:
            self._bias[rid] = bias * 0.85 + drift * 0.15
            self._apply_speed(rid, 1.0)
            return

        if abs(residual) >= self.hard_seek_zone:
            self._hard_seek(rid, group_pos, residual)
            return

        factor = max(-0.04, min(0.04, residual * 0.03))
        if abs(residual) >= self.smooth_zone and \
                now - self._last_seek.get(rid, 0.0) >= self.seek_cooldown:
            self._hard_seek(rid, group_pos, residual)
            return
        if abs(self._speed.get(rid, 1.0) - 1.0) < 0.005:
            LOG.info("%s 偏差 %.2fs，倍速微调 %.3fx（不重新缓冲）", rid, residual, 1.0 - factor)
        self._apply_speed(rid, 1.0 - factor)

def _extract_title(metadata: str) -> str:
    if not metadata:
        return ""
    import re
    m = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", metadata, re.S)
    if m:
        return m.group(1).strip()[:200]
    return ""


def _guess_name(uri: str) -> str:
    from urllib.parse import unquote, urlparse
    path = urlparse(uri).path
    name = unquote(path.rsplit("/", 1)[-1]) or uri
    return name[:200]
