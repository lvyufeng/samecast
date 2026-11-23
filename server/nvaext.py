# -*- coding: utf-8 -*-
"""B 站「小电视」私有投屏协议（nva_ext）的解码。

手机 / 电视会把投屏参数塞进 `bilibili://projection?...` 的 query 里，有三种形态：

1. `nva_ext=<JSON>`：明文 JSON，外面可能再包一层 `{"ver": 2, "content": {...}}`；
2. `_nva_ext_=<hex>`：AES-CBC/PKCS7 加密后的 JSON 的十六进制；
3. `_nva_ext_=<base64url>`：JSON 先 raw deflate，再 AES-CBC/PKCS7，最后 base64url（去掉 `=`）。

key / iv 都是 `1pzhA828t4i.6Oq@`（B 站客户端内置的兼容值，可用 `nva.ext.aes_key` 覆盖）。
这里用纯 Python 实现 AES-128-CBC 解密，不引入三方依赖。
"""

import base64
import binascii
import json
import logging
import zlib

LOG = logging.getLogger("samecast.nvaext")

DEFAULT_KEY = b"1pzhA828t4i.6Oq@"
DEFAULT_IV = b"1pzhA828t4i.6Oq@"


# ---------------------------------------------------------------- AES-128-CBC


def _rotl8(value, shift):
    return ((value << shift) | (value >> (8 - shift))) & 0xFF


def _gmul(a, b):
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        high = a & 0x80
        a = (a << 1) & 0xFF
        if high:
            a ^= 0x1B
        b >>= 1
    return result


def _build_sbox():
    sbox = [0] * 256
    for value in range(256):
        inv = 0 if value == 0 else _gpow(value, 254)
        sbox[value] = (inv ^ _rotl8(inv, 1) ^ _rotl8(inv, 2)
                       ^ _rotl8(inv, 3) ^ _rotl8(inv, 4) ^ 0x63) & 0xFF
    return sbox


def _gpow(value, exponent):
    result = 1
    base = value
    while exponent:
        if exponent & 1:
            result = _gmul(result, base)
        base = _gmul(base, base)
        exponent >>= 1
    return result


SBOX = _build_sbox()
INV_SBOX = [0] * 256
for _index, _value in enumerate(SBOX):
    INV_SBOX[_value] = _index


def _expand_key(key):
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for index in range(4, 44):
        temp = list(words[index - 1])
        if index % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[byte] for byte in temp]
            temp[0] ^= rcon
            rcon = _gmul(rcon, 2)
        words.append([words[index - 4][i] ^ temp[i] for i in range(4)])
    return [sum(words[i:i + 4], []) for i in range(0, 44, 4)]


def _inv_shift_rows(state):
    out = [0] * 16
    for row in range(4):
        for column in range(4):
            out[row + 4 * column] = state[row + 4 * ((column - row) % 4)]
    return out


def _decrypt_block(block, round_keys):
    state = [block[i] ^ round_keys[10][i] for i in range(16)]
    for round_index in range(9, 0, -1):
        state = _inv_shift_rows(state)
        state = [INV_SBOX[byte] for byte in state]
        state = [state[i] ^ round_keys[round_index][i] for i in range(16)]
        state = _inv_mix_columns(state)
    state = _inv_shift_rows(state)
    state = [INV_SBOX[byte] for byte in state]
    return [state[i] ^ round_keys[0][i] for i in range(16)]


def _inv_mix_columns(state):
    out = [0] * 16
    for column in range(4):
        a = state[column * 4:column * 4 + 4]
        b = [a[0], a[1], a[2], a[3]]
        out[column * 4 + 0] = (_gmul(a[0], 14) ^ _gmul(a[1], 11)
                               ^ _gmul(a[2], 13) ^ _gmul(a[3], 9))
        out[column * 4 + 1] = (_gmul(a[0], 9) ^ _gmul(a[1], 14)
                               ^ _gmul(a[2], 11) ^ _gmul(a[3], 13))
        out[column * 4 + 2] = (_gmul(a[0], 13) ^ _gmul(a[1], 9)
                               ^ _gmul(a[2], 14) ^ _gmul(a[3], 11))
        out[column * 4 + 3] = (_gmul(a[0], 11) ^ _gmul(a[1], 13)
                               ^ _gmul(a[2], 9) ^ _gmul(a[3], 14))
    return out


def aes_cbc_decrypt(data, key=DEFAULT_KEY, iv=DEFAULT_IV):
    if len(data) % 16:
        raise ValueError("密文长度不是 16 的倍数")
    round_keys = _expand_key(key)
    previous = list(iv)
    plain = bytearray()
    for offset in range(0, len(data), 16):
        block = list(data[offset:offset + 16])
        decrypted = _decrypt_block(block, round_keys)
        plain.extend(decrypted[i] ^ previous[i] for i in range(16))
        previous = block
    pad = plain[-1] if plain else 0
    if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
        del plain[-pad:]
    else:
        raise ValueError("PKCS7 填充不合法")
    return bytes(plain)


# ---------------------------------------------------------------- 对外接口


def _loads(text):
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _decode_encrypted(raw):
    """_nva_ext_ 有两种形态：base64url(deflate+AES) 和 hex(AES)。"""
    try:
        blob = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError):
        blob = None
    if blob and len(blob) % 16 == 0:
        try:
            data = _loads(zlib.decompress(aes_cbc_decrypt(blob), -15).decode("utf-8"))
        except Exception:  # noqa: BLE001 - 这条形态不成立就试下一条
            data = None
        if data is not None:
            return data
    try:
        plain = aes_cbc_decrypt(bytes.fromhex(raw))
    except (binascii.Error, ValueError):
        return None
    return _loads(plain.decode("utf-8", "replace"))


def decode(raw):
    """把 nva_ext / _nva_ext_ 的原始字符串解成扁平 dict，失败返回 None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    data = _loads(raw) if raw[:1] in ("{", "[") else None
    if data is None:
        data = _decode_encrypted(raw)
    if data is None:
        return None
    content = data.get("content")
    if isinstance(content, dict):
        merged = dict(content)
        for key, value in data.items():
            if key != "content":
                merged.setdefault(key, value)
        data = merged
    return data

