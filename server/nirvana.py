"""B 站「小电视」模拟：NirvanaControl 私有服务。

B 站手机 App 判断一台电视是不是「云视听小电视」，靠的是两件事：
  1. SSDP 里能用 urn:app-bilibili-com:service:NirvanaControl:3 搜到它；
  2. 设备描述的 serviceList 里有同名服务，且它的 SCPD / 控制地址都能访问。
命中之后 App 才会在投屏列表里显示「小电视」标志，并放开官方的清晰度选项。

这里只做协议层应答：B 站账号登录依旧是可选的，实际清晰度由服务端自己解析，
拿不到更高档位时自动回退，不影响投屏。
"""
from __future__ import annotations

import json
import logging

LOG = logging.getLogger("nirvana")

SERVICE_TYPE = "urn:app-bilibili-com:service:NirvanaControl:3"
SERVICE_ID = "urn:app-bilibili-com:serviceId:NirvanaControl"
SERVICE_PATH = "/NirvanaControl"

# 兼容老版本 App 用过的版本号，SSDP 里一起应答
COMPAT_TYPES = (
    "urn:app-bilibili-com:service:NirvanaControl:3",
    "urn:app-bilibili-com:service:NirvanaControl:2",
    "urn:app-bilibili-com:service:NirvanaControl:1",
)

# 电视端「云视听小电视」的固定标识，App 用它确认是官方电视端
PACKAGE_NAME = "com.xiaodianshi.tv.yst"
APP_KEY = "4409e2ce8ffd12b8"
SIGNATURE = "7194d531cbe7960a22007b9f6bdaa38b"

# 与真实小电视回包一字不差的 SCPD，App 会递归拉取并校验
SCPD = r"""<?xml version="1.0" encoding="UTF-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion>
    <major>1</major>
    <minor>0</minor>
  </specVersion>
  <actionList>
    <action>
      <name>GetAppInfo</name>
      <argumentList>
        <argument>
          <name>PackageName</name>
          <direction>out</direction>
          <relatedStateVariable>A_ARG_TYPE_UnlimitedString</relatedStateVariable>
        </argument>
        <argument>
          <name>AppKey</name>
          <direction>out</direction>
          <relatedStateVariable>A_ARG_TYPE_UnlimitedString</relatedStateVariable>
        </argument>
        <argument>
          <name>Signature</name>
          <direction>out</direction>
          <relatedStateVariable>A_ARG_TYPE_UnlimitedString</relatedStateVariable>
        </argument>
        <argument>
          <name>CurrentSignedIn</name>
          <direction>out</direction>
          <relatedStateVariable>SignedIn</relatedStateVariable>
        </argument>
      </argumentList>
    </action>
    <action>
      <name>LoginWithCode</name>
      <argumentList>
        <argument>
          <name>Code</name>
          <direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_UnlimitedString</relatedStateVariable>
        </argument>
      </argumentList>
    </action>
    <action>
      <name>PrepareForMirrorProjection</name>
      <argumentList>
        <argument>
          <name>ScreenWidth</name>
          <direction>out</direction>
          <relatedStateVariable>A_ARG_TYPE_ScreenResolution</relatedStateVariable>
        </argument>
        <argument>
          <name>ScreenHeight</name>
          <direction>out</direction>
          <relatedStateVariable>A_ARG_TYPE_ScreenResolution</relatedStateVariable>
        </argument>
        <argument>
          <name>PushUrl</name>
          <direction>out</direction>
          <relatedStateVariable>A_ARG_TYPE_UnlimitedString</relatedStateVariable>
        </argument>
      </argumentList>
    </action>
    <action>
      <name>SetDanmakuSwitch</name>
      <argumentList>
        <argument>
          <name>DesiredSwitch</name>
          <direction>in</direction>
          <relatedStateVariable>DanmakuSwitch</relatedStateVariable>
        </argument>
      </argumentList>
    </action>
    <action>
      <name>AppendDanmaku</name>
      <argumentList>
        <argument>
          <name>Content</name>
          <direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_UnlimitedString</relatedStateVariable>
        </argument>
        <argument>
          <name>Size</name>
          <direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_UnlimitedInt</relatedStateVariable>
        </argument>
        <argument>
          <name>Type</name>
          <direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_UnlimitedInt</relatedStateVariable>
        </argument>
        <argument>
          <name>Color</name>
          <direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_UnlimitedInt</relatedStateVariable>
        </argument>
        <argument>
          <name>DanmakuId</name>
          <direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_UnlimitedString</relatedStateVariable>
        </argument>
        <argument>
          <name>Action</name>
          <direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_UnlimitedString</relatedStateVariable>
        </argument>
      </argumentList>
    </action>
    <action>
      <name>GetPlayInfo</name>
      <argumentList>
        <argument>
          <name>Params</name>
          <direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_UnlimitedString</relatedStateVariable>
        </argument>
        <argument>
          <name>Content</name>
          <direction>out</direction>
          <relatedStateVariable>A_ARG_TYPE_UnlimitedString</relatedStateVariable>
        </argument>
      </argumentList>
    </action>
    <action>
      <name>GetAccountInfo</name>
      <argumentList>
        <argument>
          <name>VipInfo</name>
          <direction>out</direction>
          <relatedStateVariable>A_ARG_TYPE_UnlimitedInt</relatedStateVariable>
        </argument>
      </argumentList>
    </action>
    <action>
      <name>SwitchQuality</name>
      <argumentList>
        <argument>
          <name>Qn</name>
          <direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_UnlimitedInt</relatedStateVariable>
        </argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no">
      <name>A_ARG_TYPE_UnlimitedString</name>
      <dataType>string</dataType>
    </stateVariable>
    <stateVariable sendEvents="no">
      <name>A_ARG_TYPE_UnlimitedInt</name>
      <dataType>i4</dataType>
    </stateVariable>
    <stateVariable sendEvents="no">
      <name>A_ARG_TYPE_ScreenResolution</name>
      <dataType>ui4</dataType>
    </stateVariable>
    <stateVariable sendEvents="no">
      <name>SignedIn</name>
      <dataType>boolean</dataType>
    </stateVariable>
    <stateVariable sendEvents="no">
      <name>DanmakuSwitch</name>
      <dataType>boolean</dataType>
    </stateVariable>
  </serviceStateTable>
</scpd>
"""


def scpd() -> bytes:
    return SCPD.encode("utf-8")


# 官方电视端在 GetPlayInfo 里回的是一个 fastjson 序列化的 PlayInfo，
# 其中 qn 字段是完整的清晰度描述（当前档位 + 可选档位列表）。
# 手机 App 用这份数据渲染清晰度菜单，列表里没有 1080P 就不会提供 1080P。
QN_LIST = (
    (16, "360P", "流畅 360P"),
    (32, "480P", "清晰 480P"),
    (64, "720P", "高清 720P"),
    (80, "1080P", "超清 1080P"),
)


def _qn_description(current_qn: int) -> dict:
    current = int(current_qn or 80)
    known = [item[0] for item in QN_LIST]
    if current not in known:
        current = 80
    return {
        "currentQn": {"quality": current, "live_hdr": 0},
        "supportQnList": [
            {"description": desc, "displayDesc": display, "live_hdr": 0,
             "need_login": False, "need_vip": False, "quality": qn, "superscript": ""}
            for qn, desc, display in QN_LIST
        ],
        "userDesireQn": current,
    }


def play_info_json(raw: dict) -> str:
    """按官方电视端的 PlayInfo 结构拼 JSON（字段名与 fastjson 输出一致）。"""
    info = {
        "aid": str(raw.get("aid") or ""),
        "cid": str(raw.get("cid") or ""),
        "danmakuStatus": int(raw.get("danmakuStatus") or 0),
        "duration": int(raw.get("duration") or 0),
        "epId": str(raw.get("epId") or ""),
        "isLastEp": bool(raw.get("isLastEp") or False),
        "playerStatus": int(raw.get("playerStatus") or 0),
        "position": int(raw.get("position") or 0),
        "qn": _qn_description(raw.get("qn") or 80),
        "roomId": str(raw.get("roomId") or ""),
        "seasonId": str(raw.get("seasonId") or ""),
    }
    return json.dumps(info, ensure_ascii=False, separators=(",", ":"))


def handle(action: str, args: dict, host) -> dict:
    """应答 NirvanaControl 动作，host 是 DmrFacade。

    这一步只保证 App 侧握手成功，所有动作都不返回 SOAP Fault，
    免得个别版本的 App 因为某个动作失败直接把设备判成「不可用」。
    """
    if action == "GetAppInfo":
        return {
            "PackageName": PACKAGE_NAME,
            "AppKey": APP_KEY,
            "Signature": SIGNATURE,
            "CurrentSignedIn": "1" if host.bili_signed_in() else "0",
        }
    if action == "GetAccountInfo":
        # 与官方电视端一致：-1 未登录、0 已登录非会员、1 电视端会员
        if not host.bili_signed_in():
            return {"VipInfo": "-1"}
        return {"VipInfo": "1" if host.bili_vip() else "0"}
    if action == "PrepareForMirrorProjection":
        return {"ScreenWidth": "1920", "ScreenHeight": "1080",
                "PushUrl": host.mirror_url()}
    if action == "GetPlayInfo":
        # 空字符串会被手机当成「没有在播」，所以没有在播时才回空
        try:
            raw = host.bili_play_info() or {}
        except Exception:  # noqa: BLE001 - 查询播放状态失败不能影响握手
            LOG.exception("读取播放状态失败")
            raw = {}
        if not isinstance(raw, dict) or not raw:
            return {"Content": ""}
        return {"Content": play_info_json(raw)}
    if action == "SwitchQuality":
        qn = _to_int(args.get("Qn"))
        LOG.info("手机切清晰度：qn=%s", qn or "?")
        host.bili_switch_quality(qn)
        return {}
    if action == "LoginWithCode":
        # 手机把电视登录码推过来时，App 已经认为设备在线；SameCast 不需要登录也能投屏，
        # 这里只确认收到，不打印码本身（属于账号凭据）。
        LOG.info("收到手机推送的 B 站登录码（%d 位），已忽略（登录可选）",
                 len((args.get("Code") or "").strip()))
        return {}
    if action in ("SetDanmakuSwitch", "AppendDanmaku"):
        return {}
    LOG.info("NirvanaControl 动作 %s 暂无实现，返回空应答", action)
    return {}


def _to_int(value) -> int:
    try:
        return int(str(value or "0").strip())
    except (TypeError, ValueError):
        return 0
