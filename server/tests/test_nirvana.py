"""NirvanaControl 应答单元测试。

回归点：GetAppInfo.CurrentSignedIn 曾经被写死成 "1"，让手机端把虚拟设备当成
「已登录的官方云视听小电视」，于是投屏改走 NVA 私有推流
（SETUP /projection NVA/1.0），本服务不支持 NVA，投屏就断在半路，
电视端永远 idle。这里锁定「如实上报登录态」的行为。
"""
from __future__ import annotations

import logging
import os
import sys
import unittest

logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nirvana  # noqa: E402


class _Host:
    """只实现 nirvana.handle 需要的账号接口。"""

    def __init__(self, signed_in: bool = False, vip: bool = False) -> None:
        self._signed_in = signed_in
        self._vip = vip

    def bili_signed_in(self) -> bool:
        return self._signed_in

    def bili_vip(self) -> bool:
        return self._vip


class GetAppInfoTest(unittest.TestCase):
    def test_not_signed_in_reports_zero(self) -> None:
        reply = nirvana.handle("GetAppInfo", {}, _Host(signed_in=False))
        self.assertEqual(reply["CurrentSignedIn"], "0")
        self.assertEqual(reply["PackageName"], nirvana.PACKAGE_NAME)

    def test_signed_in_non_vip_reports_one(self) -> None:
        reply = nirvana.handle("GetAppInfo", {}, _Host(signed_in=True))
        self.assertEqual(reply["CurrentSignedIn"], "1")


class GetAccountInfoTest(unittest.TestCase):
    def test_not_signed_in_reports_minus_one(self) -> None:
        reply = nirvana.handle("GetAccountInfo", {}, _Host(signed_in=False))
        self.assertEqual(reply["VipInfo"], "-1")

    def test_signed_in_non_vip_reports_zero(self) -> None:
        reply = nirvana.handle("GetAccountInfo", {}, _Host(signed_in=True, vip=False))
        self.assertEqual(reply["VipInfo"], "0")

    def test_signed_in_vip_reports_one(self) -> None:
        reply = nirvana.handle("GetAccountInfo", {}, _Host(signed_in=True, vip=True))
        self.assertEqual(reply["VipInfo"], "1")


class AccountStateConsistencyTest(unittest.TestCase):
    def test_appinfo_and_accountinfo_agree(self) -> None:
        for signed_in in (False, True):
            for vip in (False, True):
                host = _Host(signed_in=signed_in, vip=vip)
                app_info = nirvana.handle("GetAppInfo", {}, host)
                account = nirvana.handle("GetAccountInfo", {}, host)
                self.assertEqual(
                    app_info["CurrentSignedIn"] == "1",
                    account["VipInfo"] != "-1",
                    msg=f"登录态自相矛盾：signed_in={signed_in} vip={vip}",
                )


if __name__ == "__main__":
    unittest.main()
