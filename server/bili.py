"""B 站解析：把视频/番剧链接换成电视直接能放的高清流。

设计要点：
- 手机 B 站 App 的 DLNA 投屏固定给 720P，服务端没法从投屏地址里拿到 bvid，
  所以高清走「链接解析」：控制台粘贴 B 站视频链接（或分享文字），服务端用
  playurl 接口拿 DASH 流，生成 MPD 交给电视端 ExoPlayer。
- 不登录也能用，但匿名只能拿 720P；登录后（扫码）UGC 可到 1080P，大会员 4K。
- SESSDATA 只存在服务端 config.json，不下发、不写日志。
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.sax.saxutils as saxutils

LOG = logging.getLogger("bili")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REFERER = "https://www.bilibili.com/"
API = "https://api.bilibili.com"

QUALITY_LABEL = {
    127: "8K", 126: "杜比视界", 125: "HDR", 120: "4K", 116: "1080P60",
    112: "1080P+", 80: "1080P", 74: "720P60", 64: "720P", 32: "480P",
    16: "360P", 6: "240P",
}
QUALITY_HEIGHT = {
    127: 4320, 126: 2160, 125: 2160, 120: 2160, 116: 1080, 112: 1080,
    80: 1080, 74: 720, 64: 720, 32: 480, 16: 360, 6: 240,
}
CODEC_LABEL = {7: "AVC", 12: "HEVC", 13: "AV1"}
AUDIO_PREFERENCE = [30280, 30232, 30216]
SESSDATA_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4")

BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")
AVID_RE = re.compile(r"(?:^|[^0-9A-Za-z])av(\d+)", re.I)
EP_RE = re.compile(r"/bangumi/play/ep(\d+)", re.I)
SS_RE = re.compile(r"/bangumi/play/ss(\d+)", re.I)
URL_RE = re.compile(r"https?://[^\s\"'<>，。！、）(]+", re.I)


class BiliError(Exception):
    """解析失败（消息可以直接给用户看）。"""


def _esc(text) -> str:
    return saxutils.escape(str(text or ""))


def _fmt_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0))
    return "PT%.3fS" % seconds


def parse_input(text: str) -> dict:
    """把用户粘贴的链接/分享文字/BV 号解析成目标标识。"""
    text = (text or "").strip()
    if not text:
        raise BiliError("请粘贴 B 站视频链接")
    url = ""
    match = URL_RE.search(text)
    if match:
        url = match.group(0)
    if url and re.search(r"b23\.tv|bili2233\.cn|bili22\.cn|bili23\.cn", url, re.I):
        url = resolve_short_link(url)
    target = url or text
    match = EP_RE.search(target)
    if match:
        return {"kind": "ep", "ep_id": int(match.group(1))}
    match = SS_RE.search(target)
    if match:
        return {"kind": "ss", "season_id": int(match.group(1))}
    match = BVID_RE.search(target)
    if match:
        return {"kind": "video", "bvid": match.group(0), "page": _page_of(target)}
    match = AVID_RE.search(target)
    if match:
        return {"kind": "video", "aid": int(match.group(1)), "page": _page_of(target)}
    raise BiliError("没认出 B 站链接，请复制视频页链接或分享文字")


def _page_of(url: str) -> int:
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return max(1, int((query.get("p") or ["1"])[0]))
    except (ValueError, TypeError):
        return 1


def resolve_short_link(url: str, timeout: float = 6.0) -> str:
    """b23.tv 短链跟随一次跳转，拿到真实地址。"""
    request = urllib.request.Request(url)
    request.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final = response.geturl()
    except urllib.error.HTTPError as exc:
        final = getattr(exc, "url", "") or url
    except (urllib.error.URLError, OSError) as exc:
        raise BiliError("短链打不开：%s" % exc)
    return final or url


def _cookie_pairs(set_cookie_headers) -> dict:
    pairs = {}
    for header in set_cookie_headers or []:
        chunk = header.split(";", 1)[0]
        if "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        if name in SESSDATA_KEYS:
            pairs[name] = value.strip()
    return pairs


class Bili:
    """B 站客户端：解析链接、选清晰度、生成 MPD、扫码登录。"""

    def __init__(self, options: dict | None = None, on_cookie=None):
        options = options or {}
        self.preferred_codec = (options.get("prefer_codec") or "avc").lower()
        self.max_height = int(options.get("max_height") or 1080)
        self.on_cookie = on_cookie
        self.lock = threading.Lock()
        self.cookies: dict[str, str] = {}
        self.streams: dict[str, dict] = {}
        self._stream_seq = 0
        self._nav_cache: dict = {}
        self._buvid_ready = False
        sessdata = (options.get("sessdata") or "").strip()
        if sessdata:
            self.cookies["SESSDATA"] = sessdata

    # ---- 网络 ----
    def _headers(self) -> dict:
        headers = {
            "User-Agent": UA,
            "Referer": REFERER,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if self.cookies:
            headers["Cookie"] = "; ".join("%s=%s" % (k, v) for k, v in self.cookies.items())
        return headers

    def _fetch(self, url: str, params: dict | None = None, referer: str | None = None,
               timeout: float = 12.0):
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        request = urllib.request.Request(url)
        for key, value in self._headers().items():
            request.add_header(key, value)
        if referer:
            request.add_header("Referer", referer)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                headers = response.headers
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            headers = exc.headers
            status = exc.code
        except (urllib.error.URLError, OSError) as exc:
            raise BiliError("连不上 B 站：%s" % exc)
        if headers.get("Content-Encoding") == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        try:
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            raise BiliError("B 站返回了看不懂的内容（HTTP %s）" % status)
        return data, _cookie_pairs(headers.get_all("Set-Cookie") if headers else None)

    def _api(self, path: str, params: dict | None = None, referer: str | None = None,
             timeout: float = 12.0) -> dict:
        url = path if path.startswith("http") else API + path
        data, cookies = self._fetch(url, params, referer=referer, timeout=timeout)
        if cookies:
            with self.lock:
                self.cookies.update(cookies)
        code = data.get("code")
        if code not in (0, None):
            message = data.get("message") or data.get("msg") or "请求失败"
            raise BiliError("%s（code %s）" % (message, code))
        return data

    # ---- 账号 ----
    def ensure_buvid(self) -> None:
        with self.lock:
            if self._buvid_ready:
                return
            self._buvid_ready = True
        try:
            data, cookies = self._fetch(API + "/x/frontend/finger/spi")
            values = data.get("data") or {}
            fresh = {}
            if values.get("b_3"):
                fresh["buvid3"] = values["b_3"]
            if values.get("b_4"):
                fresh["buvid4"] = values["b_4"]
            fresh.update(cookies)
            with self.lock:
                self.cookies.update(fresh)
            # 新 buvid 有极短的生效期，紧接着打接口偶尔会 412，等一下更稳
            time.sleep(0.45)
        except BiliError as exc:
            with self.lock:
                self._buvid_ready = False
            LOG.debug("拿 buvid 失败：%s", exc)

    def nav(self, force: bool = False) -> dict:
        with self.lock:
            cached = self._nav_cache
        if not force and cached and time.time() - cached.get("ts", 0) < 60:
            return cached.get("data") or {}
        info = {"is_login": False, "uname": "", "vip": False, "vip_label": ""}
        try:
            data = self._api("/x/web-interface/nav")
            body = data.get("data") or {}
            info["is_login"] = bool(body.get("isLogin"))
            info["uname"] = body.get("uname") or ""
            vip = body.get("vipStatus") == 1
            info["vip"] = vip
            info["vip_label"] = "大会员" if vip else ("已登录" if info["is_login"] else "")
        except BiliError as exc:
            LOG.debug("查询账号状态失败：%s", exc)
            info["is_login"] = bool(self.cookies.get("SESSDATA"))
        with self.lock:
            self._nav_cache = {"ts": time.time(), "data": info}
        return info

    def logged_in(self) -> bool:
        return bool(self.cookies.get("SESSDATA"))

    def status(self) -> dict:
        info = self.nav()
        logged_in = bool(info.get("is_login"))
        if logged_in:
            top = "4K" if info.get("vip") else "1080P+"
            hint = "已登录：走 DASH 高清，最高 " + top
        else:
            top = "1080P"
            hint = "不登录也能看：网页通道最高 1080P（码率略低）；登录后走 DASH 更清晰"
        return {
            "logged_in": logged_in,
            "uname": info.get("uname") or "",
            "vip": bool(info.get("vip")),
            "max_quality": top,
            "hint": hint,
        }

    # ---- 扫码登录 ----
    def qr_start(self) -> dict:
        self.ensure_buvid()
        data = self._api("https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
        body = data.get("data") or {}
        url = body.get("url") or ""
        key = body.get("qrcode_key") or ""
        if not url or not key:
            raise BiliError("生成二维码失败")
        with self.lock:
            self._nav_cache = {}
        return {"key": key, "url": url, "svg": qr_svg(url), "expires": 180}

    def qr_poll(self, key: str) -> dict:
        key = (key or "").strip()
        if not key:
            raise BiliError("二维码已失效，请重新获取")
        url = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
        data, cookies = self._fetch(url, {"qrcode_key": key, "source": "main-fe"})
        code = data.get("code")
        body = data.get("data") or {}
        state = body.get("code")
        if cookies:
            with self.lock:
                self.cookies.update(cookies)
        if code == 0 and state == 0:
            self._persist_cookie()
            info = self.nav(force=True)
            return {"status": "ok", "logged_in": bool(info.get("is_login")),
                    "uname": info.get("uname") or "", "vip": bool(info.get("vip"))}
        if state in (86101, None):
            return {"status": "waiting"}
        if state == 86090:
            return {"status": "scanned"}
        if state == 86038:
            return {"status": "expired"}
        return {"status": "error", "message": str(body.get("message") or "登录失败")}

    def _persist_cookie(self) -> None:
        if self.on_cookie:
            try:
                self.on_cookie(self.cookies.get("SESSDATA") or "")
            except Exception:  # noqa: BLE001
                LOG.exception("保存 SESSDATA 失败")

    def logout(self) -> None:
        with self.lock:
            self.cookies.pop("SESSDATA", None)
            self.cookies.pop("bili_jct", None)
            self.cookies.pop("DedeUserID", None)
            self.cookies.pop("DedeUserID__ckMd5", None)
            self._nav_cache = {}
        self._persist_cookie()


    # ---- 视频信息 ----
    def video_info(self, bvid: str | None = None, aid: int | None = None, page: int = 1) -> dict:
        params = {"bvid": bvid} if bvid else {"aid": int(aid or 0)}
        data = self._api("/x/web-interface/view", params, referer=REFERER)
        body = data.get("data") or {}
        pages = body.get("pages") or []
        index = max(1, min(int(page or 1), len(pages) or 1)) - 1
        cid = body.get("cid")
        part = ""
        if pages:
            cid = pages[index].get("cid") or cid
            part = pages[index].get("part") or ""
        if not cid:
            raise BiliError("拿不到视频分P信息")
        title = body.get("title") or "B 站视频"
        if part and len(pages) > 1:
            title = "%s · %s" % (title, part)
        return {"bvid": body.get("bvid") or bvid or "",
                "aid": body.get("aid") or aid, "cid": int(cid), "title": title,
                "cover": body.get("pic") or "", "duration": float(body.get("duration") or 0)}

    def pgc_info(self, ep_id: int) -> dict:
        referer = "https://www.bilibili.com/bangumi/play/ep%s" % ep_id
        try:
            data = self._api("/pgc/view/web/season", {"ep_id": ep_id}, referer=referer)
            body = data.get("result") or data.get("data") or {}
            episodes = body.get("episodes") or []
            for episode in episodes:
                if int(episode.get("id") or 0) == int(ep_id):
                    title = body.get("title") or ""
                    extra = episode.get("long_title") or episode.get("share_copy") or ""
                    if extra and extra != title:
                        title = "%s · %s" % (title, extra)
                    return {"ep_id": int(ep_id), "bvid": episode.get("bvid") or "",
                            "cid": int(episode.get("cid") or 0), "title": title or "B 站番剧",
                            "cover": body.get("cover") or episode.get("cover") or "",
                            "duration": float(episode.get("duration") or 0) / 1000.0}
        except BiliError as exc:
            LOG.debug("番剧信息失败：%s", exc)
        return {"ep_id": int(ep_id), "bvid": "", "cid": 0, "title": "B 站番剧",
                "cover": "", "duration": 0.0}

    def season_first_ep(self, season_id: int) -> int:
        data = self._api("/pgc/view/web/season", {"season_id": int(season_id)},
                         referer="https://www.bilibili.com/bangumi/play/ss%s" % season_id)
        body = data.get("result") or data.get("data") or {}
        for episode in body.get("episodes") or []:
            if episode.get("id"):
                return int(episode["id"])
        raise BiliError("这部番剧还没有可播放的集数")

    # ---- 播放地址 ----
    def playurl_video(self, bvid: str, cid: int, qn: int = 120) -> dict:
        params = {"bvid": bvid, "cid": int(cid), "qn": int(qn), "fnver": 0,
                  "fnval": 4048, "fourk": 1, "platform": "pc", "otype": "json"}
        data = self._api("/x/player/playurl", params,
                         referer="https://www.bilibili.com/video/%s" % bvid)
        return data.get("data") or {}

    def playurl_aid(self, aid: int, cid: int, qn: int = 120) -> dict:
        """手机 App 走 bilibili:// 私有协议投屏时只给 aid/cid，没有 bvid。"""
        params = {"avid": int(aid), "cid": int(cid), "qn": int(qn), "fnver": 0,
                  "fnval": 4048, "fourk": 1, "platform": "pc", "otype": "json"}
        data = self._api("/x/player/playurl", params,
                         referer="https://www.bilibili.com/video/av%d" % int(aid))
        return data.get("data") or {}

    def playurl_pgc(self, ep_id: int, qn: int = 120) -> dict:
        url = "https://api.bilibili.com/pgc/player/web/playurl"
        params = {"ep_id": int(ep_id), "qn": int(qn), "fnver": 0, "fnval": 4048, "fourk": 1}
        data = self._api(url, params,
                         referer="https://www.bilibili.com/bangumi/play/ep%s" % ep_id)
        return data.get("result") or data.get("data") or {}

    def playurl_html5(self, bvid: str, cid: int, qn: int = 120) -> dict:
        """网页版单文件通道：不登录也能拿到 1080P，音视频已封装在同一个 mp4 里。"""
        params = {"bvid": bvid, "cid": int(cid), "qn": int(qn), "fnver": 0,
                  "fnval": 16, "fourk": 1, "platform": "html5", "high_quality": 1}
        data = self._api("/x/player/playurl", params,
                         referer="https://www.bilibili.com/video/%s" % bvid)
        return data.get("data") or {}

    def playurl_aid_html5(self, aid: int, cid: int, qn: int = 120) -> dict:
        params = {"avid": int(aid), "cid": int(cid), "qn": int(qn), "fnver": 0,
                  "fnval": 16, "fourk": 1, "platform": "html5", "high_quality": 1}
        data = self._api("/x/player/playurl", params,
                         referer="https://www.bilibili.com/video/av%d" % int(aid))
        return data.get("data") or {}

    def playurl_pgc_html5(self, ep_id: int, qn: int = 120) -> dict:
        url = "https://api.bilibili.com/pgc/player/web/playurl"
        params = {"ep_id": int(ep_id), "qn": int(qn), "fnver": 0, "fnval": 16, "fourk": 1,
                  "platform": "html5", "high_quality": 1}
        data = self._api(url, params,
                         referer="https://www.bilibili.com/bangumi/play/ep%s" % ep_id)
        return data.get("result") or data.get("data") or {}

    # ---- 选流 ----
    def pick_video(self, dash: dict, max_height: int = 0) -> dict:
        videos = [item for item in (dash.get("video") or []) if item.get("baseUrl") or item.get("base_url")]
        if not videos:
            raise BiliError("这个视频没有可用的 DASH 画面")
        limit = max_height or (self.max_height if self.max_height > 0 else 10000)
        inside = [item for item in videos if int(item.get("height") or 0) <= limit]
        pool = inside or videos
        preferred = self.preferred_codec
        if preferred == "hevc":
            order = [12, 13, 7]
        elif preferred == "av1":
            order = [13, 12, 7]
        else:
            avc = [item for item in pool if int(item.get("codecid") or 0) == 7]
            if avc:
                return max(avc, key=lambda item: (int(item.get("height") or 0),
                                                  int(item.get("bandwidth") or 0)))
            order = [12, 13, 7]
        for codec in order:
            matches = [item for item in pool if int(item.get("codecid") or 0) == codec]
            if matches:
                return max(matches, key=lambda item: (int(item.get("height") or 0),
                                                      int(item.get("bandwidth") or 0)))
        return max(pool, key=lambda item: (int(item.get("height") or 0),
                                           int(item.get("bandwidth") or 0)))

    def pick_audio(self, dash: dict) -> dict | None:
        audios = [item for item in (dash.get("audio") or []) if item.get("baseUrl") or item.get("base_url")]
        if not audios:
            return None
        for wanted in AUDIO_PREFERENCE:
            matches = [item for item in audios if int(item.get("id") or 0) == wanted]
            if matches:
                return max(matches, key=lambda item: int(item.get("bandwidth") or 0))
        return max(audios, key=lambda item: int(item.get("bandwidth") or 0))

    @staticmethod
    def _segment(item: dict) -> tuple[str, str]:
        base = item.get("segment_base") or item.get("SegmentBase") or {}
        index_range = base.get("index_range") or base.get("indexRange") or ""
        init_range = base.get("initialization") or base.get("Initialization") or ""
        if isinstance(init_range, dict):
            init_range = init_range.get("range") or ""
        return str(index_range), str(init_range or "0-")

    @staticmethod
    def _codecs(item: dict, default: str) -> str:
        codecs = item.get("codecs") or item.get("codec") or ""
        if not codecs:
            codecs = default
        return str(codecs).split(",")[0]

    def build_mpd(self, dash: dict, duration: float, title: str = "",
                  max_height: int = 0) -> tuple[str, dict, dict | None]:
        video = self.pick_video(dash, max_height)
        audio = self.pick_audio(dash)
        total = float(dash.get("duration") or duration or 0)
        video_url = video.get("baseUrl") or video.get("base_url") or ""
        index_range, init_range = self._segment(video)
        height = int(video.get("height") or 0)
        width = int(video.get("width") or 0)
        frame_rate = str(video.get("frame_rate") or video.get("frameRate") or "30")
        video_codecs = self._codecs(video, "avc1.640028")
        video_bandwidth = int(video.get("bandwidth") or 1500000)
        pieces = [
            '<?xml version="1.0" encoding="utf-8"?>',
            '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"',
            ' profiles="urn:mpeg:dash:profile:isoff-on-demand:2011" type="static"',
            ' mediaPresentationDuration="%s" minBufferTime="PT2S">' % _fmt_duration(total),
            '<Period>',
            '<AdaptationSet contentType="video" mimeType="video/mp4" segmentAlignment="true"'
            ' startWithSAP="1" subsegmentAlignment="true" subsegmentStartsWithSAP="1">',
            '<Representation id="v1" bandwidth="%d" codecs="%s" width="%d" height="%d" frameRate="%s">'
            % (video_bandwidth, _esc(video_codecs), width, height, _esc(frame_rate)),
            '<BaseURL>%s</BaseURL>' % _esc(video_url),
            '<SegmentBase indexRange="%s"><Initialization range="%s"/></SegmentBase>'
            % (_esc(index_range), _esc(init_range)),
            '</Representation>',
            '</AdaptationSet>',
        ]
        if audio is not None:
            audio_url = audio.get("baseUrl") or audio.get("base_url") or ""
            audio_index, audio_init = self._segment(audio)
            pieces += [
                '<AdaptationSet contentType="audio" mimeType="audio/mp4" segmentAlignment="true"'
                ' startWithSAP="1" subsegmentAlignment="true" subsegmentStartsWithSAP="1">',
                '<Representation id="a1" bandwidth="%d" codecs="%s">'
                % (int(audio.get("bandwidth") or 128000), _esc(self._codecs(audio, "mp4a.40.2"))),
                '<AudioChannelConfiguration'
                ' schemeIdUri="urn:mpeg:dash:23003:3:audio_channel_configuration:2011" value="2"/>',
                '<BaseURL>%s</BaseURL>' % _esc(audio_url),
                '<SegmentBase indexRange="%s"><Initialization range="%s"/></SegmentBase>'
                % (_esc(audio_index), _esc(audio_init)),
                '</Representation>',
                '</AdaptationSet>',
            ]
        pieces += ['</Period>', '</MPD>']
        return "".join(pieces), video, audio

    # ---- 对外：解析成可播放结果 ----
    def resolve(self, text: str) -> dict:
        self.ensure_buvid()
        target = parse_input(text)
        kind = target["kind"]
        if kind == "ep":
            meta = self.pgc_info(target["ep_id"])
            return self._resolve_stream(
                meta, lambda: self.playurl_pgc(target["ep_id"]),
                lambda: self.playurl_pgc_html5(target["ep_id"]))
        if kind == "ss":
            ep_id = self.season_first_ep(target["season_id"])
            meta = self.pgc_info(ep_id)
            return self._resolve_stream(
                meta, lambda: self.playurl_pgc(ep_id),
                lambda: self.playurl_pgc_html5(ep_id))
        meta = self.video_info(bvid=target.get("bvid"), aid=target.get("aid"),
                               page=target.get("page", 1))
        return self._resolve_video(meta)

    def resolve_aid(self, aid: int) -> dict:
        """手机 App 投屏时只递过来 aid，用它换回最高清晰度的播放地址。"""
        self.ensure_buvid()
        meta = self.video_info(aid=int(aid))
        return self._resolve_video(meta)

    def resolve_cast(self, target: dict, qn: int = 0) -> dict:
        """手机 App 私有协议投屏：target 是从 nva_ext 里解出来的视频标识。

        与网页链路的区别是 App 只给 aid/cid/epId，没有 bvid / 分集号；
        清晰度由 App 传来的 userDesireQn 决定，没传就按最高可取。
        """
        self.ensure_buvid()
        qn = int(qn or 0)
        height = QUALITY_HEIGHT.get(qn, 0)
        play_qn = qn or 120
        ep_id = int(target.get("ep_id") or 0)
        season_id = int(target.get("season_id") or 0)
        if not ep_id and season_id:
            ep_id = self.season_first_ep(season_id)
        if ep_id:
            meta = self.pgc_info(ep_id)
            meta.setdefault("ep_id", ep_id)
            return self._resolve_stream(
                meta, lambda: self.playurl_pgc(ep_id, play_qn),
                lambda: self.playurl_pgc_html5(ep_id, play_qn), max_height=height)
        bvid = str(target.get("bvid") or "")
        aid = int(target.get("aid") or 0)
        if not (bvid or aid):
            raise BiliError("手机投屏地址里没有视频编号")
        meta = self.video_info(bvid=bvid or None, aid=aid or None)
        cid = int(target.get("cid") or 0) or int(meta.get("cid") or 0)
        if not cid:
            raise BiliError("这个视频没有可播放的分集")
        bvid = meta["bvid"]
        return self._resolve_stream(
            meta, lambda: self.playurl_video(bvid, cid, play_qn),
            lambda: self.playurl_html5(bvid, cid, play_qn), max_height=height)

    def _resolve_video(self, meta: dict) -> dict:
        bvid, cid = meta["bvid"], meta["cid"]
        return self._resolve_stream(
            meta, lambda: self.playurl_video(bvid, cid),
            lambda: self.playurl_html5(bvid, cid))

    def _resolve_stream(self, meta: dict, dash_play, web_play,
                        max_height: int = 0) -> dict:
        # 登录后 DASH 码率更高（大会员还能 4K）；没登录走网页版单文件，最高 1080P
        modes = ("dash", "web") if self.logged_in() else ("web", "dash")
        errors = []
        for mode in modes:
            try:
                play = dash_play() if mode == "dash" else web_play()
            except BiliError as exc:
                errors.append(str(exc))
                continue
            result = (self._dash_result(play, meta, max_height)
                      or self._file_result(play, meta))
            if result:
                return result
        LOG.warning("B 站取流失败：%s", "；".join(errors) or "没有可用地址")
        raise BiliError("这个视频拿不到可播放地址（可能是付费/大会员内容，或需要登录）")

    def _dash_result(self, play: dict, meta: dict, max_height: int = 0) -> dict | None:
        dash = play.get("dash") or {}
        if not dash.get("video"):
            return None
        duration = float(play.get("timelength") or 0) / 1000.0 or float(meta.get("duration") or 0)
        mpd, video, audio = self.build_mpd(dash, duration, meta.get("title") or "",
                                           max_height)
        quality_id = int(video.get("id") or 0)
        label = QUALITY_LABEL.get(quality_id) or ("%dP" % int(video.get("height") or 0))
        codec = CODEC_LABEL.get(int(video.get("codecid") or 0)) or ""
        return {"kind": "dash", "title": meta.get("title") or "", "duration": duration,
                "mpd": mpd, "quality": label, "quality_id": quality_id, "codec": codec,
                "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
                "audio": ("有音轨" if audio else "无音轨"), "cover": meta.get("cover") or ""}

    def _file_result(self, play: dict, meta: dict) -> dict | None:
        durl = play.get("durl") or []
        if not (durl and durl[0].get("url")):
            return None
        item = durl[0]
        quality_id = int(play.get("quality") or 0)
        duration = float(play.get("timelength") or 0) / 1000.0 or float(meta.get("duration") or 0)
        return {"kind": "file", "title": meta.get("title") or "", "duration": duration,
                "url": item["url"], "quality": QUALITY_LABEL.get(quality_id) or "",
                "quality_id": quality_id, "codec": "AVC",
                "width": 0, "height": int(QUALITY_HEIGHT.get(quality_id) or 0),
                "audio": "有音轨", "size": int(item.get("size") or 0),
                "cover": meta.get("cover") or ""}

    # ---- 流表：MPD 用一次性 token 提供，避免把 CDN 地址暴露给页面 ----
    def register_stream(self, mpd: str, meta: dict) -> str:
        with self.lock:
            self._stream_seq += 1
            token = "%x-%x" % (int(time.time()), self._stream_seq)
            self.streams[token] = {"mpd": mpd, "meta": dict(meta), "ts": time.time()}
            if len(self.streams) > 30:
                oldest = sorted(self.streams.items(), key=lambda kv: kv[1]["ts"])[:-30]
                for key, _ in oldest:
                    self.streams.pop(key, None)
        return token

    def get_stream(self, token: str) -> dict | None:
        with self.lock:
            entry = self.streams.get(token or "")
        if not entry:
            return None
        if time.time() - entry["ts"] > 3 * 3600:
            with self.lock:
                self.streams.pop(token, None)
            return None
        return entry

    # ---- 手机 B 站投屏的「尽力而为」升级 ----
    def search_by_title(self, title: str, limit: int = 8) -> list:
        keyword = re.sub(r"<[^>]+>", "", title or "")
        keyword = re.sub(r"\s+", " ", keyword).strip()
        if len(keyword) < 2:
            return []
        self.ensure_buvid()
        time.sleep(1.2)
        data = self._api("/x/web-interface/search/type",
                         {"search_type": "video", "keyword": keyword[:60], "page": 1})
        results = ((data.get("data") or {}).get("result") or [])
        found = []
        for item in results[:40]:
            bvid = item.get("bvid") or ""
            if bvid:
                found.append({"bvid": bvid, "aid": item.get("aid"),
                              "title": re.sub(r"<[^>]+>", "", item.get("title") or "")})
            if len(found) >= limit:
                break
        return found


def qr_svg(text: str, border: int = 4) -> str:
    """把文本画成二维码 SVG（墙内 App 扫码登录用）。"""
    try:
        from qrcodegen import QrCode
    except ImportError:
        raise BiliError("服务器缺少 qrcodegen.py，无法显示登录二维码")
    code = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    dimension = code.get_size() + border * 2
    path = []
    for y in range(code.get_size()):
        for x in range(code.get_size()):
            if code.get_module(x, y):
                path.append("M%d,%d h1 v1 h-1 z" % (x + border, y + border))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="240" height="240"'
            ' shape-rendering="crispEdges"><rect width="100%%" height="100%%" fill="#ffffff"/>'
            '<path d="%s" fill="#000000"/></svg>' % (dimension, dimension, "".join(path)))
