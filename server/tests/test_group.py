"""Group 控制器单元测试（不依赖真实电视，只验证调度逻辑）。"""
from __future__ import annotations

import logging
import os
import sys
import time
import unittest

# 测试里故意触发告警分支，别把日志刷到测试输出里
logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from group import Group, Receiver, PAUSED, PLAYING, STOPPED  # noqa: E402


class FakeReceiver(Receiver):
    """内存里的假接收端：记录收到的指令，状态可手工设定。"""

    def __init__(self, rid: str, name: str = "") -> None:
        self.id = rid
        self.name = name or rid
        self.kind = "fake"
        self.state = "idle"
        self.position = 0.0
        self.preempted = False
        self.cmds: list[str] = []

    # 指令
    def load(self, uri: str, metadata: str = "") -> None:
        self.cmds.append("load")
        self.state = "buffering"

    def play(self, position: float | None = None) -> None:
        self.cmds.append("play")
        self.state = "playing"

    def pause(self) -> None:
        self.cmds.append("pause")
        self.state = "paused"

    def seek(self, position: float) -> None:
        self.cmds.append("seek")

    def stop(self) -> None:
        self.cmds.append("stop")
        self.state = "idle"
        self.position = 0.0

    def set_volume(self, volume: int) -> None:
        self.cmds.append("volume")

    def set_speed(self, speed: float) -> None:
        self.cmds.append("speed")

    def status(self) -> dict:
        return {
            "state": self.state,
            "position": self.position,
            "preempted": self.preempted,
        }


def make_group(*rids: str) -> tuple[Group, list[FakeReceiver]]:
    recs = [FakeReceiver(r) for r in rids]
    group = Group(recs)
    group.load("http://example.com/video.mp4")
    group.play()
    # 第一轮 tick 只记录基线状态
    group.tick()
    return group, recs


class PreemptTest(unittest.TestCase):
    def test_preempt_stops_whole_group(self) -> None:
        group, recs = make_group("tv-a", "tv-b")
        self.assertEqual(group.state, PLAYING)
        for rec in recs:
            rec.state = "playing"
        group.tick()

        # 电视 A 被电视自带的投屏接管
        recs[0].preempted = True
        recs[0].state = "idle"
        group.tick()

        self.assertEqual(group.state, STOPPED)
        self.assertIn("tv-a", group._preempted)
        # 两台都收到 stop，一起退出，不留半吊子状态
        self.assertIn("stop", recs[0].cmds)
        self.assertIn("stop", recs[1].cmds)

    def test_preempted_receiver_not_pulled_back(self) -> None:
        group, recs = make_group("tv-a", "tv-b")
        # 两台都在播，然后 tv-a 被别的投屏接管
        for rec in recs:
            rec.state = "playing"
        group.tick()
        recs[0].preempted = True
        recs[0].state = "idle"
        group.tick()
        self.assertEqual(group.state, STOPPED)

        # 之后 tv-a 持续上报"已被抢占、空闲"：调度器不应该自动重载把它拉回来
        loads_before = recs[0].cmds.count("load")
        for _ in range(3):
            recs[0].state = "idle"
            group.tick()
        self.assertEqual(recs[0].cmds.count("load"), loads_before)
        self.assertEqual(recs[0].state, "idle")

    def test_new_load_clears_preempt(self) -> None:
        group, recs = make_group("tv-a")
        recs[0].preempted = True
        recs[0].state = "idle"
        group.tick()
        self.assertTrue(group._preempted)

        group.load("http://example.com/other.mp4")
        self.assertEqual(group._preempted, set())
        self.assertIn("load", recs[0].cmds)

    def test_receiver_recovers_when_it_reports_normal(self) -> None:
        group, recs = make_group("tv-a")
        recs[0].preempted = True
        recs[0].state = "idle"
        group.tick()
        self.assertIn("tv-a", group._preempted)

        # 用户回到 SameCast，接收端重新上报正常状态
        recs[0].preempted = False
        recs[0].state = "playing"
        group.tick()
        self.assertNotIn("tv-a", group._preempted)

    def test_no_stop_when_group_already_stopped(self) -> None:
        recs = [FakeReceiver("tv-a")]
        group = Group(recs)
        group.load("http://example.com/video.mp4")
        group.stop()
        recs[0].cmds.clear()

        recs[0].preempted = True
        recs[0].state = "idle"
        group.tick()

        self.assertEqual(group.state, STOPPED)
        self.assertEqual(recs[0].cmds, [])
        self.assertIn("tv-a", group._preempted)


class SyncTest(unittest.TestCase):
    def test_remote_pause_pauses_group(self) -> None:
        group, recs = make_group("tv-a", "tv-b")
        for rec in recs:
            rec.state = "playing"
        group.tick()
        group._grace_until = 0.0

        recs[0].state = "paused"
        group.tick()
        self.assertEqual(group.state, PAUSED)

    def test_remote_play_resumes_group(self) -> None:
        group, recs = make_group("tv-a", "tv-b")
        for rec in recs:
            rec.state = "playing"
        group.tick()
        group._grace_until = 0.0
        recs[0].state = "paused"
        group.tick()
        self.assertEqual(group.state, PAUSED)

        group._grace_until = 0.0
        recs[0].state = "playing"
        group.tick()
        self.assertEqual(group.state, PLAYING)

    def test_duration_learned_from_receiver(self) -> None:
        group, recs = make_group("tv-a")
        recs[0].state = "playing"
        group.tick()
        group._grace_until = 0.0
        recs[0].status = lambda: {"state": "playing", "position": 3.0,
                                  "duration": 1425.0, "preempted": False}
        group.tick()
        self.assertAlmostEqual(group.duration, 1425.0, places=1)


class EndOfMediaTest(unittest.TestCase):
    """播到片尾要自然结束，不能当成"掉线"无限重载。"""

    def _make(self, rids=("tv-a", "tv-b")):
        group, recs = make_group(*rids)
        group.duration = 100.0
        for rec in recs:
            rec.state = "playing"
            rec.position = 99.0
        group.tick()
        group._grace_until = 0.0
        return group, recs

    def test_group_stops_at_end(self) -> None:
        group, recs = self._make()
        group._set_anchor(99.9)
        for rec in recs:
            rec.cmds.clear()
        group.tick()
        self.assertEqual(group.state, STOPPED)
        for rec in recs:
            self.assertIn("stop", rec.cmds)
        # 结束后回到开头，重新播放能从头开始
        self.assertAlmostEqual(group.position(), 0.0, places=1)

    def test_no_reload_storm_after_end(self) -> None:
        group, recs = self._make()
        group._set_anchor(99.9)
        group.tick()
        for rec in recs:
            rec.state = "idle"
            rec.cmds.clear()
        for _ in range(10):
            group.tick()
        for rec in recs:
            self.assertEqual(rec.cmds.count("load"), 0)

    def test_not_ended_while_midway(self) -> None:
        group, recs = self._make()
        group._set_anchor(50.0)
        group.tick()
        self.assertEqual(group.state, PLAYING)


class ReloadBackoffTest(unittest.TestCase):
    """源不可用时不要每轮都重发 load。"""

    def test_reload_is_throttled(self) -> None:
        group, recs = make_group("tv-a")
        group.duration = 600.0
        recs[0].state = "playing"
        group.tick()
        group._grace_until = 0.0
        recs[0].cmds.clear()
        recs[0].state = "idle"          # 掉出播放（但没到片尾）
        for _ in range(10):             # 连续 10 轮
            group.tick()
        # 5 秒退避内只应重载一次
        self.assertEqual(recs[0].cmds.count("load"), 1)

    def test_gives_up_after_max_attempts(self) -> None:
        group, recs = make_group("tv-a")
        group.duration = 600.0
        recs[0].state = "playing"
        group.tick()
        recs[0].cv = 0
        for i in range(12):
            group._grace_until = 0.0
            recs[0].state = "idle"
            group._reload_last[recs[0].id] = 0.0     # 绕过时间退避
            group.tick()
        self.assertGreaterEqual(group._reload_count.get("tv-a", 0), group.reload_max_attempts)
        self.assertLessEqual(recs[0].cmds.count("load"), 4)

    def test_retries_again_after_cooldown(self) -> None:
        """电视重启/重新开 App 后，停手期满要能再把它拉回来。"""
        group, recs = make_group("tv-a")
        group.duration = 600.0
        group.reload_retry_cooldown = 0.0   # 等价于"已经歇够"
        recs[0].state = "playing"
        group.tick()
        # 已放弃的状态：计数停在 max+次数，放弃时刻留给"很久以前"
        group._reload_count["tv-a"] = group.reload_max_attempts + 1
        group._reload_giveup["tv-a"] = 0.0
        group._grace_until = 0.0
        recs[0].cmds.clear()
        recs[0].state = "idle"
        group.tick()
        self.assertEqual(recs[0].cmds.count("load"), 1)

    def test_stays_given_up_during_cooldown(self) -> None:
        """停手期内不要重载，避免源真挂了时又变成风暴。"""
        group, recs = make_group("tv-a")
        group.duration = 600.0
        recs[0].state = "playing"
        group.tick()
        group._reload_count["tv-a"] = group.reload_max_attempts + 1
        group._reload_giveup["tv-a"] = time.monotonic()
        group._grace_until = 0.0
        recs[0].cmds.clear()
        recs[0].state = "idle"
        for _ in range(5):
            group.tick()
        self.assertEqual(recs[0].cmds.count("load"), 0)

    def test_recovery_resets_backoff(self) -> None:
        group, recs = make_group("tv-a")
        group.duration = 600.0
        recs[0].state = "playing"
        group.tick()
        group._reload_count["tv-a"] = 3
        group._grace_until = 0.0
        recs[0].state = "playing"
        group.tick()
        self.assertNotIn("tv-a", group._reload_count)


class StallTest(unittest.TestCase):
    """一直缓冲不回来要兜底重载，但短时间缓冲不能被当成卡死。"""

    def test_buffering_not_reloaded_immediately(self) -> None:
        group, recs = make_group("tv-a")
        group.duration = 600.0
        recs[0].state = "playing"
        group.tick()
        group._grace_until = 0.0
        recs[0].cmds.clear()
        recs[0].state = "buffering"
        for _ in range(5):
            group.tick()
        # 默认 20 秒阈值内，缓冲只记录时间，不该重载
        self.assertEqual(recs[0].cmds.count("load"), 0)

    def test_stalled_buffering_triggers_reload(self) -> None:
        group, recs = make_group("tv-a")
        group.duration = 600.0
        group.stall_timeout = 0.0
        recs[0].state = "playing"
        group.tick()
        group._grace_until = 0.0
        recs[0].cmds.clear()
        recs[0].state = "buffering"
        group.tick()
        self.assertEqual(recs[0].cmds.count("load"), 1)

    def test_stall_ignored_when_preempted(self) -> None:
        group, recs = make_group("tv-a")
        group.duration = 600.0
        group.stall_timeout = 0.0
        recs[0].state = "playing"
        group.tick()
        group._grace_until = 0.0
        recs[0].cmds.clear()
        recs[0].state = "buffering"
        recs[0].preempted = True   # 已被别的投屏接管：卡死也不再拉回来
        group.tick()
        self.assertEqual(recs[0].cmds.count("load"), 0)


if __name__ == "__main__":
    unittest.main()
