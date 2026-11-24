"""DLNA MediaRenderer(DMR) 门面：设备描述、SCPD、SOAP 动作、GENA 事件。"""
from __future__ import annotations

import logging
import threading
import time
import urllib.request
import uuid as uuidlib
import xml.etree.ElementTree as ET

import nirvana

LOG = logging.getLogger("dmr")

AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RCS = "urn:schemas-upnp-org:service:RenderingControl:1"
CMS = "urn:schemas-upnp-org:service:ConnectionManager:1"
# B 站私有服务：设备描述里带上它，手机 App 才会把 SameCast 认成「小电视」
NVA = nirvana.SERVICE_TYPE
NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS_ENC = "http://schemas.xmlsoap.org/soap/encoding/"

SERVICE_PATHS = {
    AVT: "/AVTransport",
    RCS: "/RenderingControl",
    CMS: "/ConnectionManager",
    NVA: nirvana.SERVICE_PATH,
}

# 路径沿用真实小电视的结构：/bilibili/<服务>/<token>/<文件>，
# 老的 /<服务>/<文件> 也继续提供服务，避免已经缓存的设备描述失效。
_SERVICE_BY_NAME = {
    "AVTransport": AVT,
    "RenderingControl": RCS,
    "ConnectionManager": CMS,
    "NirvanaControl": NVA,
}
_PATH_KINDS = {
    "scpd.xml": "scpd",
    "control.xml": "soap",
    "event.xml": "gena",
    # 老路径（没有 /bilibili 前缀、不带 .xml）继续兼容，避免缓存描述失效
    "scpd": "scpd",
    "control": "soap",
    "event": "gena",
}

# 说明：ottVersion 是手机端识别「电视端能力」的版本号：
#   >= 104001 手机端才会显示小电视标志（xv32 / NirvanaEngine 的版本门槛）
#   >= 104300 才放开 4K 清晰度
# 投屏链路本身不依赖这个值——实测 103900 和 108300 都能正常收到
# SetAVTransportURI + Play；这里填官方云视听小电视 1.8.x 的 108300，和真机一致。
DEVICE_XML = """<?xml version="1.0" encoding="utf-8"?>
<root configId="8693519" xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>{friendly_name}</friendlyName>
    <manufacturer>Bilibili Inc.</manufacturer>
    <manufacturerURL>https://bilibili.com/</manufacturerURL>
    <modelDescription>云视听小电视</modelDescription>
    <modelName>W40F</modelName>
    <X_brandName>apollo</X_brandName>
    <hostVersion>22</hostVersion>
    <modelNumber>1024</modelNumber>
    <serialNumber>1024</serialNumber>
    <ottVersion>108300</ottVersion>
    <channelName>weijing</channelName>
    <capability>255</capability>
    <modelURL>https://app.bilibili.com/</modelURL>
    <UDN>uuid:{udn}</UDN>
    <dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC>
    <dlna:X_DLNACAP>playcontainer-1-0</dlna:X_DLNACAP>
    <serviceList>
      <service>
        <serviceType>{avt}</serviceType>
        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
        <SCPDURL>/bilibili/AVTransport/{token}/scpd.xml</SCPDURL>
        <controlURL>/bilibili/AVTransport/{token}/control.xml</controlURL>
        <eventSubURL>/bilibili/AVTransport/{token}/event.xml</eventSubURL>
      </service>
      <service>
        <serviceType>{rcs}</serviceType>
        <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
        <SCPDURL>/bilibili/RenderingControl/{token}/scpd.xml</SCPDURL>
        <controlURL>/bilibili/RenderingControl/{token}/control.xml</controlURL>
        <eventSubURL>/bilibili/RenderingControl/{token}/event.xml</eventSubURL>
      </service>
      <service>
        <serviceType>{cms}</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/bilibili/ConnectionManager/{token}/scpd.xml</SCPDURL>
        <controlURL>/bilibili/ConnectionManager/{token}/control.xml</controlURL>
        <eventSubURL>/bilibili/ConnectionManager/{token}/event.xml</eventSubURL>
      </service>
      <service>
        <serviceType>{nva}</serviceType>
        <serviceId>{nva_id}</serviceId>
        <SCPDURL>/bilibili/NirvanaControl/{token}/scpd.xml</SCPDURL>
        <controlURL>/bilibili/NirvanaControl/{token}/control.xml</controlURL>
        <eventSubURL>/bilibili/NirvanaControl/{token}/event.xml</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>
"""


def parse_service_path(path: str):
    """把 /AVTransport/control 或 /bilibili/AVTransport/<token>/control.xml 归一成 (服务, 类型)。

    类型是 scpd / soap / gena 之一；认不出来返回 (None, None)。
    """
    parts = [p for p in (path or "").split("?")[0].split("/") if p]
    if len(parts) > 1 and parts[0] == "bilibili":
        parts = parts[1:]
    if len(parts) < 2:
        return None, None
    service = _SERVICE_BY_NAME.get(parts[0])
    kind = _PATH_KINDS.get(parts[-1])
    if service is None or kind is None:
        return None, None
    return service, kind


def _var(name: str, dtype: str, events: str = "no", allowed: str = "") -> str:
    allowed_xml = ("<allowedValueList>%s</allowedValueList>"
                   % "".join("<allowedValue>%s</allowedValue>" % v for v in allowed.split("|"))
                   ) if allowed else ""
    return ("<stateVariable sendEvents=\"%s\"><name>%s</name><dataType>%s</dataType>%s</stateVariable>"
            % (events, name, dtype, allowed_xml))


SCPD_AVT = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>SetAVTransportURI</name><argumentList>{a_seturi}</argumentList></action>
    <action><name>SetNextAVTransportURI</name><argumentList>{a_setnext}</argumentList></action>
    <action><name>GetMediaInfo</name><argumentList>{a_getmediainfo}</argumentList></action>
    <action><name>GetTransportInfo</name><argumentList>{a_gettransportinfo}</argumentList></action>
    <action><name>GetPositionInfo</name><argumentList>{a_getpositioninfo}</argumentList></action>
    <action><name>GetDeviceCapabilities</name><argumentList>{a_getdevcaps}</argumentList></action>
    <action><name>GetTransportSettings</name><argumentList>{a_getsettings}</argumentList></action>
    <action><name>Stop</name><argumentList>{a_stop}</argumentList></action>
    <action><name>Play</name><argumentList>{a_play}</argumentList></action>
    <action><name>Pause</name><argumentList>{a_pause}</argumentList></action>
    <action><name>Seek</name><argumentList>{a_seek}</argumentList></action>
    <action><name>Next</name><argumentList>{a_next}</argumentList></action>
    <action><name>Previous</name><argumentList>{a_prev}</argumentList></action>
    <action><name>GetCurrentTransportActions</name><argumentList>{a_actions}</argumentList></action>
  </actionList>
  <serviceStateTable>
    {v_avt}
  </serviceStateTable>
</scpd>
"""


def _arg(name: str, direction: str, related: str) -> str:
    return ("<argument><name>%s</name><direction>%s</direction>"
            "<relatedStateVariable>%s</relatedStateVariable></argument>"
            % (name, direction, related))


def build_avt_scpd() -> str:
    return SCPD_AVT.format(
        a_seturi=_arg("InstanceID", "in", "A_ARG_TYPE_InstanceID") + _arg("CurrentURI", "in", "AVTransportURI") + _arg("CurrentURIMetaData", "in", "AVTransportURIMetaData"),
        a_setnext=_arg("InstanceID", "in", "A_ARG_TYPE_InstanceID") + _arg("NextURI", "in", "NextAVTransportURI") + _arg("NextURIMetaData", "in", "NextAVTransportURIMetaData"),
        a_getmediainfo=_arg("InstanceID", "in", "A_ARG_TYPE_InstanceID") + _arg("NrTracks", "out", "NumberOfTracks") + _arg("MediaDuration", "out", "CurrentMediaDuration") + _arg("CurrentURI", "out", "AVTransportURI") + _arg("CurrentURIMetaData", "out", "AVTransportURIMetaData") + _arg("NextURI", "out", "NextAVTransportURI") + _arg("NextURIMetaData", "out", "NextAVTransportURIMetaData") + _arg("PlayMedium", "out", "PlaybackStorageMedium") + _arg("RecordMedium", "out", "RecordStorageMedium") + _arg("WriteStatus", "out", "RecordMediumWriteStatus"),
        a_gettransportinfo=_arg("InstanceID", "in", "A_ARG_TYPE_InstanceID") + _arg("CurrentTransportState", "out", "TransportState") + _arg("CurrentTransportStatus", "out", "TransportStatus") + _arg("CurrentSpeed", "out", "TransportPlaySpeed"),
        a_getpositioninfo=_arg("InstanceID", "in", "A_ARG_TYPE_InstanceID") + _arg("Track", "out", "CurrentTrack") + _arg("TrackDuration", "out", "CurrentTrackDuration") + _arg("TrackMetaData", "out", "CurrentTrackMetaData") + _arg("TrackURI", "out", "CurrentTrackURI") + _arg("RelTime", "out", "RelativeTimePosition") + _arg("AbsTime", "out", "AbsoluteTimePosition") + _arg("RelCount", "out", "RelativeCounterPosition") + _arg("AbsCount", "out", "AbsoluteCounterPosition"),
        a_getdevcaps=_arg("InstanceID", "in", "A_ARG_TYPE_InstanceID") + _arg("PlayMedia", "out", "PossiblePlaybackStorageMedia") + _arg("RecMedia", "out", "PossibleRecordStorageMedia") + _arg("RecQualityModes", "out", "PossibleRecordQualityModes"),
        a_getsettings=_arg("InstanceID", "in", "A_ARG_TYPE_InstanceID") + _arg("PlayMode", "out", "CurrentPlayMode") + _arg("RecQualityMode", "out", "CurrentRecordQualityMode"),
        a_stop=_arg("InstanceID", "in", "A_ARG_TYPE_InstanceID"),
        a_play=_arg("InstanceID", "in", "A_ARG_TYPE_InstanceID") + _arg("Speed", "in", "TransportPlaySpeed"),
        a_pause=_arg("InstanceID", "in", "A_ARG_TYPE_InstanceID"),
        a_seek=_arg("InstanceID", "in", "A_ARG_TYPE_InstanceID") + _arg("Unit", "in", "A_ARG_TYPE_SeekMode") + _arg("Target", "in", "A_ARG_TYPE_SeekTarget"),
        a_next=_arg("InstanceID", "in", "A_ARG_TYPE_InstanceID"),
        a_prev=_arg("InstanceID", "in", "A_ARG_TYPE_InstanceID"),
        a_actions=_arg("InstanceID", "in", "A_ARG_TYPE_InstanceID") + _arg("Actions", "out", "CurrentTransportActions"),
        v_avt="".join([
            _var("TransportState", "string", "no", "STOPPED|PLAYING|PAUSED_PLAYBACK|TRANSITIONING|NO_MEDIA_PRESENT|PLAYING|PAUSED_PLAYBACK"),
            _var("TransportStatus", "string", "no", "OK|ERROR_OCCURRED"),
            _var("TransportPlaySpeed", "string", "no", "1|1/2|2"),
            _var("CurrentTransportActions", "string"),
            _var("PlaybackStorageMedium", "string"),
            _var("RecordStorageMedium", "string"),
            _var("PossiblePlaybackStorageMedia", "string"),
            _var("PossibleRecordStorageMedia", "string"),
            _var("CurrentPlayMode", "string"),
            _var("RecordMediumWriteStatus", "string"),
            _var("CurrentRecordQualityMode", "string"),
            _var("PossibleRecordQualityModes", "string"),
            _var("NumberOfTracks", "ui4"),
            _var("CurrentTrack", "ui4"),
            _var("CurrentTrackDuration", "string"),
            _var("CurrentMediaDuration", "string"),
            _var("CurrentTrackMetaData", "string"),
            _var("CurrentTrackURI", "string"),
            _var("AVTransportURI", "string"),
            _var("AVTransportURIMetaData", "string", "yes"),
            _var("NextAVTransportURI", "string"),
            _var("NextAVTransportURIMetaData", "string"),
            _var("RelativeTimePosition", "string"),
            _var("AbsoluteTimePosition", "string"),
            _var("RelativeCounterPosition", "i4"),
            _var("AbsoluteCounterPosition", "i4"),
            _var("LastChange", "string", "yes"),
            _var("A_ARG_TYPE_SeekMode", "string", "no", "REL_TIME|TRACK_NR|ABS_TIME"),
            _var("A_ARG_TYPE_SeekTarget", "string"),
            _var("A_ARG_TYPE_InstanceID", "ui4"),
        ]),
    )


SCPD_RCS = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetVolume</name><argumentList>{getvol}</argumentList></action>
    <action><name>SetVolume</name><argumentList>{setvol}</argumentList></action>
    <action><name>GetMute</name><argumentList>{getmute}</argumentList></action>
    <action><name>SetMute</name><argumentList>{setmute}</argumentList></action>
    <action><name>GetVolumeDBRange</name><argumentList>{db}</argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>LastChange</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_InstanceID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Channel</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_DesiredVolume</name><dataType>ui2</dataType><allowedValueRange><minimum>0</minimum><maximum>100</maximum><step>1</step></allowedValueRange></stateVariable>
    <stateVariable sendEvents="no"><name>Volume</name><dataType>ui2</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_CurrentVolume</name><dataType>ui2</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Mute</name><dataType>boolean</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_VolumeRangeMin</name><dataType>ui2</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_VolumeRangeMax</name><dataType>ui2</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"""


def build_rcs_scpd() -> str:
    inst = _arg("InstanceID", "in", "A_ARG_TYPE_InstanceID")
    ch = _arg("Channel", "in", "A_ARG_TYPE_Channel")
    return SCPD_RCS.format(
        getvol=inst + ch + _arg("CurrentVolume", "out", "A_ARG_TYPE_CurrentVolume"),
        setvol=inst + ch + _arg("DesiredVolume", "in", "A_ARG_TYPE_DesiredVolume"),
        getmute=inst + ch + _arg("CurrentMute", "out", "A_ARG_TYPE_Mute"),
        setmute=inst + ch + _arg("DesiredMute", "in", "A_ARG_TYPE_Mute"),
        db=inst + ch + _arg("MinValue", "out", "A_ARG_TYPE_VolumeRangeMin") + _arg("MaxValue", "out", "A_ARG_TYPE_VolumeRangeMax"),
    )


SCPD_CMS = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetProtocolInfo</name><argumentList>{proto}</argumentList></action>
    <action><name>GetCurrentConnectionIDs</name><argumentList>{ids}</argumentList></action>
    <action><name>GetCurrentConnectionInfo</name><argumentList>{info}</argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="yes"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentConnectionIDs</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionStatus</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionManager</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Direction</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionID</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_AVTransportID</name><dataType>i4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_RcsID</name><dataType>i4</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"""


def build_cms_scpd() -> str:
    return SCPD_CMS.format(
        proto=_arg("SourceProtocolInfo", "out", "SourceProtocolInfo") + _arg("SinkProtocolInfo", "out", "SinkProtocolInfo"),
        ids=_arg("CurrentConnectionIDs", "out", "CurrentConnectionIDs"),
        info=_arg("ConnectionID", "in", "A_ARG_TYPE_ConnectionID") + _arg("RcsID", "out", "A_ARG_TYPE_RcsID") + _arg("AVTransportID", "out", "A_ARG_TYPE_AVTransportID") + _arg("ProtocolInfo", "out", "A_ARG_TYPE_ProtocolInfo") + _arg("PeerConnectionManager", "out", "A_ARG_TYPE_ConnectionManager") + _arg("PeerConnectionID", "out", "A_ARG_TYPE_ConnectionID") + _arg("Direction", "out", "A_ARG_TYPE_Direction") + _arg("Status", "out", "A_ARG_TYPE_ConnectionStatus"),
    )


# 我们"能播"的协议列表。视频 App 一般只按 http-get 匹配。
SINK_PROTOCOL_INFO = ",".join([
    "http-get:*:video/mp4:*",
    "http-get:*:video/x-matroska:*",
    "http-get:*:video/webm:*",
    "http-get:*:application/vnd.apple.mpegurl:*",
    "http-get:*:application/x-mpegURL:*",
    "http-get:*:video/mpeg:*",
    "http-get:*:video/MP2T:*",
    "http-get:*:audio/mpeg:*",
    "http-get:*:audio/mp4:*",
])


def escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def parse_soap(body: bytes):
    """返回 (service_type, action, args)。命名空间无关。"""
    root = ET.fromstring(body)
    body_el = root.find("{%s}Body" % NS_SOAP)
    if body_el is None:
        # 有些实现不带前缀
        for child in root:
            if child.tag.endswith("Body"):
                body_el = child
                break
    if body_el is None or len(body_el) == 0:
        raise ValueError("no SOAP body")
    action_el = body_el[0]
    tag = action_el.tag
    if tag.startswith("{"):
        service_type = tag[1:].split("}")[0]
        action = tag.split("}")[1]
    else:
        service_type, _, action = tag.rpartition(":")
    args = {child.tag.split("}")[-1]: (child.text or "") for child in action_el}
    return service_type, action, args


def soap_response(service_type: str, action: str, out: dict[str, str]) -> bytes:
    parts = []
    for key, value in out.items():
        parts.append("<%s>%s</%s>" % (key, escape(str(value)), key))
    xml = ('<?xml version="1.0" encoding="utf-8"?>'
           '<s:Envelope xmlns:s="%s" s:encodingStyle="%s">'
           '<s:Body><u:%sResponse xmlns:u="%s">%s</u:%sResponse></s:Body>'
           '</s:Envelope>') % (NS_SOAP, NS_ENC, action, service_type, "".join(parts), action)
    return xml.encode("utf-8")


def soap_fault(code: int = 401, description: str = "Invalid Action") -> bytes:
    xml = ('<?xml version="1.0" encoding="utf-8"?>'
           '<s:Envelope xmlns:s="%s" s:encodingStyle="%s"><s:Body>'
           '<s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>'
           '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
           '<errorCode>%d</errorCode><errorDescription>%s</errorDescription>'
           '</UPnPError></detail></s:Fault></s:Body></s:Envelope>') % (NS_SOAP, NS_ENC, code, description)
    return xml.encode("utf-8")


def _fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return "%02d:%02d:%02d" % (h, m, s)


def parse_time(text: str) -> float:
    text = (text or "").strip().strip('"')
    if not text:
        return 0.0
    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return 0.0
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0]


class DmrFacade:
    """把 DLNA 动作翻译成组控制指令。"""

    def __init__(self, group, udn: str, friendly_name: str, base_url: str):
        self.group = group
        self.udn = udn.lower().replace("uuid:", "")
        self.friendly_name = friendly_name
        self.base_url = base_url
        self._subscribers: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._seq = 0
        # 手机投屏接管钩子：callable(uri, metadata) -> bool，返回 True 表示地址已被服务端接管
        self.on_cast = None
        # 当前投屏来源标签（phone / bili），用于电视端顶部显示
        self._cast_source = "phone"
        # 小电视 NirvanaControl 的宿主机回调，由 app.py 注入
        self.on_bili_signed_in = None
        self.on_bili_vip = None
        self.on_mirror_url = None
        self.on_bili_play_info = None
        self.on_bili_quality = None

    # ---- 小电视（NirvanaControl）宿主机回调 ------------------------------
    def bili_signed_in(self) -> bool:
        if self.on_bili_signed_in is None:
            return False
        try:
            return bool(self.on_bili_signed_in())
        except Exception:  # noqa: BLE001 - 小电视查询不能因为登录态异常而失败
            LOG.exception("读取 B 站登录态失败")
            return False

    def bili_vip(self) -> bool:
        if self.on_bili_vip is None:
            return False
        try:
            return bool(self.on_bili_vip())
        except Exception:  # noqa: BLE001
            LOG.exception("读取 B 站会员状态失败")
            return False

    def mirror_url(self) -> str:
        if self.on_mirror_url is None:
            return ""
        try:
            return str(self.on_mirror_url() or "")
        except Exception:  # noqa: BLE001
            LOG.exception("生成镜像投屏地址失败")
            return ""

    def bili_play_info(self) -> dict:
        if self.on_bili_play_info is None:
            return {}
        try:
            return self.on_bili_play_info() or {}
        except Exception:  # noqa: BLE001
            LOG.exception("读取 B 站播放信息失败")
            return {}

    def bili_switch_quality(self, qn: int) -> None:
        if self.on_bili_quality is None:
            return
        try:
            self.on_bili_quality(qn)
        except Exception:  # noqa: BLE001
            LOG.exception("切换 B 站清晰度失败")

    # ---- 描述 -----------------------------------------------------------
    def device_xml(self) -> bytes:
        return DEVICE_XML.format(friendly_name=escape(self.friendly_name), udn=self.udn,
                                 token=self.udn, avt=AVT, rcs=RCS, cms=CMS,
                                 nva=nirvana.SERVICE_TYPE,
                                 nva_id=nirvana.SERVICE_ID).encode("utf-8")

    def scpd(self, service_type: str) -> bytes:
        if service_type == AVT:
            return build_avt_scpd().encode("utf-8")
        if service_type == RCS:
            return build_rcs_scpd().encode("utf-8")
        if service_type == CMS:
            return build_cms_scpd().encode("utf-8")
        if service_type == NVA:
            return nirvana.scpd()
        raise KeyError(service_type)

    # ---- SOAP 动作 -------------------------------------------------------
    def handle(self, service_type: str, action: str, args: dict) -> dict | None:
        g = self.group
        if service_type == NVA:
            return nirvana.handle(action, args, self)
        if service_type == CMS:
            if action == "GetProtocolInfo":
                return {"SourceProtocolInfo": "", "SinkProtocolInfo": SINK_PROTOCOL_INFO}
            if action == "GetCurrentConnectionIDs":
                return {"CurrentConnectionIDs": "0"}
            if action == "GetCurrentConnectionInfo":
                return {"RcsID": "0", "AVTransportID": "0", "ProtocolInfo": "",
                        "PeerConnectionManager": "", "PeerConnectionID": "-1",
                        "Direction": "Input", "Status": "OK"}
            raise KeyError(action)

        if service_type == RCS:
            if action == "GetVolume":
                return {"CurrentVolume": str(int(g.volume))}
            if action == "SetVolume":
                g.set_volume(float(args.get("DesiredVolume") or 0), source=self._cast_source)
                return {}
            if action == "GetMute":
                return {"CurrentMute": "0"}
            if action == "SetMute":
                return {}
            if action == "GetVolumeDBRange":
                return {"MinValue": "0", "MaxValue": "100"}
            raise KeyError(action)

        if service_type == AVT:
            if action == "SetAVTransportURI":
                uri = args.get("CurrentURI", "")
                meta = args.get("CurrentURIMetaData", "")
                taken = False
                if self.on_cast is not None:
                    try:
                        taken = bool(self.on_cast(uri, meta))
                    except Exception:  # noqa: BLE001 - 接管失败不能影响正常投屏
                        LOG.exception("接管手机投屏失败，回退手机原始地址")
                        taken = False
                if taken:
                    self._cast_source = "bili"
                    return {}
                self._cast_source = "phone"
                g.load(uri, meta, source="phone")
                return {}
            if action == "SetNextAVTransportURI":
                return {}
            if action == "Play":
                g.play(source=self._cast_source, position=g.position_if_stopped())
                return {}
            if action == "Pause":
                g.pause(source=self._cast_source)
                return {}
            if action == "Stop":
                g.stop(source=self._cast_source)
                return {}
            if action == "Seek":
                target = args.get("Target", "")
                unit = (args.get("Unit") or "REL_TIME").upper()
                if unit == "TRACK_NR":
                    return {}
                g.seek(parse_time(target), source=self._cast_source)
                return {}
            if action == "Next" or action == "Previous":
                return {}
            if action == "GetTransportInfo":
                return {"CurrentTransportState": g.transport_state(),
                        "CurrentTransportStatus": "OK", "CurrentSpeed": "1"}
            if action == "GetPositionInfo":
                pos = g.position()
                return {"Track": "1", "TrackDuration": _fmt_time(g.duration),
                        "TrackMetaData": g.metadata or "", "TrackURI": g.uri or "",
                        "RelTime": _fmt_time(pos), "AbsTime": _fmt_time(pos),
                        "RelCount": "2147483647", "AbsCount": "2147483647"}
            if action == "GetMediaInfo":
                pos_info = _fmt_time(g.duration)
                return {"NrTracks": "1", "MediaDuration": pos_info,
                        "CurrentURI": g.uri or "", "CurrentURIMetaData": g.metadata or "",
                        "NextURI": "", "NextURIMetaData": "",
                        "PlayMedium": "NETWORK", "RecordMedium": "NOT_IMPLEMENTED",
                        "WriteStatus": "NOT_IMPLEMENTED"}
            if action == "GetDeviceCapabilities":
                return {"PlayMedia": "NETWORK", "RecMedia": "NOT_IMPLEMENTED",
                        "RecQualityModes": "NOT_IMPLEMENTED"}
            if action == "GetTransportSettings":
                return {"PlayMode": "NORMAL", "RecQualityMode": "NOT_IMPLEMENTED"}
            if action == "GetCurrentTransportActions":
                return {"Actions": g.current_actions()}
            raise KeyError(action)

        raise KeyError(service_type)

    # ---- GENA 订阅 -------------------------------------------------------
    def subscribe(self, service_type: str, callback: str, timeout: int = 300) -> tuple[str, int]:
        sid = "uuid:" + str(uuidlib.uuid4())
        with self._lock:
            self._subscribers[sid] = {"service": service_type, "callback": callback,
                                      "expires": time.time() + timeout, "seq": 0}
        LOG.info("订阅 %s SID=%s callback=%s", service_type, sid, callback)
        return sid, timeout

    def renew(self, sid: str, timeout: int = 300) -> bool:
        with self._lock:
            sub = self._subscribers.get(sid)
            if not sub:
                return False
            sub["expires"] = time.time() + timeout
            return True

    def unsubscribe(self, sid: str) -> bool:
        with self._lock:
            return self._subscribers.pop(sid, None) is not None

    def _notify(self, service_type: str, property_xml: str) -> None:
        now = time.time()
        with self._lock:
            subs = [(sid, dict(sub)) for sid, sub in self._subscribers.items()
                    if sub["service"] == service_type and sub["expires"] > now]
        for sid, sub in subs:
            with self._lock:
                sub["seq"] = (sub.get("seq", 0) + 1) % 2 ** 32
                seq = sub["seq"]
            body = ('<?xml version="1.0" encoding="utf-8"?>'
                    '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
                    '<e:property><LastChange>%s</LastChange></e:property>'
                    '</e:propertyset>') % escape(property_xml)
            payload = body.encode("utf-8")
            for url in [u.strip() for u in sub["callback"].split() if u.strip()]:
                try:
                    req = urllib.request.Request(url, data=payload, method="NOTIFY")
                    req.add_header("Content-Type", "text/xml; charset=utf-8")
                    req.add_header("NT", "upnp:event")
                    req.add_header("NTS", "upnp:propchange")
                    req.add_header("SID", sid)
                    req.add_header("SEQ", str(seq))
                    urllib.request.urlopen(req, timeout=3).close()
                    break
                except Exception as exc:  # noqa: BLE001
                    LOG.debug("NOTIFY 失败 %s: %s", url, exc)

    def notify_state(self) -> None:
        g = self.group
        state = g.transport_state()
        pos = g.position()
        event = ('<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
                 '<InstanceID val="0">'
                 '<TransportState val="%s"/>'
                 '<TransportStatus val="OK"/>'
                 '<CurrentTransportActions val="%s"/>'
                 '<CurrentTrackDuration val="%s"/>'
                 '<CurrentMediaDuration val="%s"/>'
                 '<AVTransportURI val="%s"/>'
                 '<CurrentTrackURI val="%s"/>'
                 '<RelativeTimePosition val="%s"/>'
                 '<AbsoluteTimePosition val="%s"/>'
                 '</InstanceID></Event>') % (
            state, g.current_actions(), _fmt_time(g.duration), _fmt_time(g.duration),
            escape(g.uri or ""), escape(g.uri or ""), _fmt_time(pos), _fmt_time(pos))
        self._notify(AVT, event)
        rcs_event = ('<Event xmlns="urn:schemas-upnp-org:metadata-1-0/RCS/">'
                     '<InstanceID val="0"><Volume channel="Master" val="%d"/>'
                     '</InstanceID></Event>') % int(g.volume)
        self._notify(RCS, rcs_event)
