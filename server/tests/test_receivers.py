"""接收端实现测试：重点验证重启后不要重放旧指令。"""
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
        # 客户端拿到序号后回传，不应重复下发
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

    def test_first_poll_on_empty_queue(self) -> None:
        rec = WebReceiver("tv-a", "电视-tva")
        cmds, seq = rec.poll(0)
        self.assertEqual(cmds, [])
        self.assertEqual(seq, 0)


if __name__ == "__main__":
    unittest.main()
