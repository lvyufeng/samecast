"""B 站「小电视」私有投屏 nva_ext 解码单元测试。

三种线上形态都要能解：明文 JSON、hex(AES(JSON))、base64url(AES(deflate(JSON)))。
夹具由 openssl 用同一把 key/iv 生成，保证和我们解出来的字节一致。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import unittest
from urllib.parse import quote, urlencode

logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nvaext  # noqa: E402

PAYLOAD = {
    "aid": 114514, "cid": 1919810, "seasonId": 0, "epId": 0, "bvid": "BV1xx411c7mD",
    "userDesireQn": 80, "contentType": 0, "autoNext": True, "mobileVersion": 7000300,
    "sessionId": "abc123", "seekTs": "12", "projType": 1, "danmakuSwitchSave": False,
}
HEX = ("bbbbfc893675fdd4fadde2282ba91299a501029e82811b2cffa45435c7a0f951f14222af488ffc10"
       "82e9d11f6e21c452fb428496edcbed311fdf39267685ea55a5ee41bbe69dae1d06a41b19dc4c622cbf"
       "5cd6074f5d9dee56d8e34dac6f74f1d7544998f13183d392bdb54b755021f7296cbd6155bea259ed4"
       "41be67081d8d44ee16998f85822dde5c7c4e247166bda70f6965401cfc9c45b2e4c28c89cbd5522c6"
       "d82d83b5c5bcede702ca72f56a3a4332b49308da1a3175fa9b314a5c2c9317a53ecb61b47a4898f"
       "22067efa0eabd2ab0ffa7f574c0b64a23b7acaf1752a9")
B64 = ("Tlm-4Z0KWt0NH1sBTVymc4RCg2YFtnknLR_FzgBBbRwvCMMrjfVb8SuPh9NX0QU1XyHQiHab0WbTcclK0rb"
       "9rHkymaozonyQTMDy5wiLQcVLqXWGdK6X1HbQT09c5KaGlfKhhUQ603thc4x7pKqDiXNUjqzGNxLDZwdO"
       "jJglZBGmq4qp6oRP9mNCkcN71fcsrs-1A9fTc6mnkMFJJUo7FDRqpsStvuEcdZKeqonP4Zs")


class AesTest(unittest.TestCase):
    def test_fips197_vector(self):
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        plain = bytes(nvaext._decrypt_block(list(cipher), nvaext._expand_key(key)))
        self.assertEqual(plain.hex(), "00112233445566778899aabbccddeeff")


class DecodeTest(unittest.TestCase):
    def test_plain_json_with_envelope(self):
        raw = json.dumps({"ver": 2, "content": PAYLOAD})
        self.assertEqual(nvaext.decode(raw), dict(PAYLOAD, ver=2))

    def test_hex_aes_without_deflate(self):
        self.assertEqual(nvaext.decode(HEX), PAYLOAD)

    def test_base64url_aes_with_deflate(self):
        self.assertEqual(nvaext.decode(B64), PAYLOAD)

    def test_garbage_returns_none(self):
        for value in ("", "not-a-real-value", "%%%%", "0" * 16):
            self.assertIsNone(nvaext.decode(value), value)

    def test_cast_uri_all_forms(self):
        import app as app_module

        fake = object.__new__(app_module.App)
        for params in ({"nva_ext": json.dumps(PAYLOAD)},
                       {"_nva_ext_": HEX},
                       {"_nva_ext_": B64}):
            uri = "bilibili://projection?" + urlencode(params)
            cast = fake._parse_bili_cast_uri(uri)
            self.assertIsNotNone(cast, params.keys())
            self.assertEqual(cast["aid"], 114514)
            self.assertEqual(cast["cid"], 1919810)
            self.assertEqual(cast["qn"], 80)
            self.assertEqual(cast["bvid"], "BV1xx411c7mD")
            self.assertTrue(cast["auto_next"])
            self.assertAlmostEqual(cast["seek"], 12.0)

    def test_cast_uri_without_ids_returns_none(self):
        import app as app_module

        fake = object.__new__(app_module.App)
        uri = "bilibili://projection?" + quote("proj_source=bilibili")
        self.assertIsNone(fake._parse_bili_cast_uri(uri))


if __name__ == "__main__":
    unittest.main()
