"""接收端实现测试：重点验证重启后不要重放旧指令、水位不倒退。"""
from __future__ import annotations

import logging
import os
import sys
import unittest

logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from receivers import WebReceiver  # noqa: E402


class WebReceiverPollTest(unittest.TestCase):
    def test_delivers_commands_in_order(self) -> None:
        rec = WebReceiver("tv-a", "电视-tva")
        rec.load("http://example.com/v.mp4")
        rec.play(None)
        cmds, seq = rec.poll(0)
        self.assertEqual([c["cmd"] for c in cmds], ["load", "play"])
        self.assertEqual(seq, 2)
        # 接收端回传水位后不应重复下发
        cmds, seq = rec.poll(seq)
        self.assertEqual(cmds, [])
        self.assertEqual(seq, 2)

    def test_restart_drops_stale_commands(self) -> None:
        """电视 App 重启后从 0 开始要指令：旧时间轴的 seek 不能重放。"""
        rec = WebReceiver("tv-a", "电视-tva")
        rec.load("http://example.com/v.mp4")
        rec.seek(4736.7)            # 很久以前的校正指令
        cmds, seq = rec.poll(0)     # App 首次取指令
        self.assertEqual(len(cmds), 2)
        # App 掉线/重启，序号又回到 0
        cmds, seq = rec.poll(0)
        self.assertEqual(cmds, [])  # 旧指令被丢弃，等 Group 按当前时间轴重发
        # 之后新指令仍然能正常送达
        rec.seek(5113.9)
        cmds, _ = rec.poll(seq)
        self.assertEqual([c["cmd"] for c in cmds], ["seek"])
        self.assertAlmostEqual(cmds[0]["position"], 5113.9)

    def test_backward_sequence_also_drops(self) -> None:
        rec = WebReceiver("tv-a", "电视-tva")
        rec.load("http://example.com/v.mp4")
        rec.play(None)
        _, seq = rec.poll(0)
        rec.seek(10.0)
        rec.seek(20.0)
        cmds, _ = rec.poll(seq)
        self.assertEqual(len(cmds), 2)
        # 客户端回退到更小的序号（页面重新加载会这样）
        cmds, _ = rec.poll(1)
        self.assertEqual(cmds, [])

    def test_first_poll_keeps_queued_commands(self) -> None:
        """首次轮询不算重启：先上报状态再取指令的接收端不能丢指令。"""
        rec = WebReceiver("tv-a", "电视-tva")
        rec.report({"state": "idle", "position": 0.0})
        for _ in range(3):
            rec.load("http://example.com/v.mp4")
        cmds, _ = rec.poll(0)
        self.assertEqual(len(cmds), 3)

    def test_no_false_restart_when_nothing_acked_yet(self) -> None:
        """空队列的首次轮询水位是 0，之后来的指令不能被当成重启丢掉。"""
        rec = WebReceiver("tv-a", "电视-tva")
        cmds, seq = rec.poll(0)
        self.assertEqual((cmds, seq), ([], 0))
        rec.load("http://example.com/v.mp4")
        cmds, seq = rec.poll(0)
        self.assertEqual([c["cmd"] for c in cmds], ["load"])
        self.assertEqual(seq, 1)

    def test_server_restart_keeps_client_watermark(self) -> None:
        """服务端重启后自己的序号从 0 开始，不能把接收端的水位拽回去。"""
        rec = WebReceiver("tv-a", "电视-tva")   # 相当于服务端刚重启
        cmds, seq = rec.poll(42)                # 电视还记着上一次的水位
        self.assertEqual(cmds, [])
        self.assertEqual(seq, 42)
        rec.load("http://example.com/v.mp4")
        cmds, seq = rec.poll(42)
        self.assertEqual([c["cmd"] for c in cmds], ["load"])
        self.assertEqual(seq, 43)
        # 水位没有倒退，不该被判成重启
        cmds, seq = rec.poll(43)
        self.assertEqual(cmds, [])


class WebReceiverRejoinTest(unittest.TestCase):
    """接收端重启要能被 Group 看见（靠递增的重连计数，不是易丢的布尔标志）。"""

    def test_restart_increments_reconnect_counter(self) -> None:
        rec = WebReceiver("tv-a", "电视-tva")
        rec.report({"state": "playing", "position": 10.0})
        rec.load("http://example.com/v.mp4")
        _, seq = rec.poll(0)
        self.assertEqual(rec.status().get("reconnects"), 0)
        rec.poll(0)                     # App 重启，序号归零
        self.assertEqual(rec.status().get("reconnects"), 1)
        rec.poll(0)                     # 又刷新一次页面
        self.assertEqual(rec.status().get("reconnects"), 2)

    def test_counter_survives_later_reports(self) -> None:
        """重连事件不能被随后到达的状态上报抹掉，否则 Group 可能看漏。"""
        rec = WebReceiver("tv-a", "电视-tva")
        rec.report({"state": "playing", "position": 10.0})
        rec.load("http://example.com/v.mp4")
        _, seq = rec.poll(0)            # 拿到过指令，水位 1
        rec.poll(0)                     # App 重启，序号归零
        rec.report({"state": "idle", "position": 0.0})
        self.assertEqual(rec.status().get("reconnects"), 1)

    def test_poll_keeps_receiver_online(self) -> None:
        """只要还在轮询就算在线，别等状态上报才续命。"""
        rec = WebReceiver("tv-a", "电视-tva")
        rec._last_seen = 0.0            # 很久没上报了
        rec.poll(0)
        self.assertTrue(rec.online())


if __name__ == "__main__":
    unittest.main()
