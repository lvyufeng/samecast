# SameCast · 局域网多电视同步投屏网关

[![服务端测试](https://github.com/lvyufeng/samecast/actions/workflows/tests.yml/badge.svg)](https://github.com/lvyufeng/samecast/actions/workflows/tests.yml)
[![Android 构建](https://github.com/lvyufeng/samecast/actions/workflows/android.yml/badge.svg)](https://github.com/lvyufeng/samecast/actions/workflows/android.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

手机在任意支持 DLNA 的投屏 App 里点「投屏」，搜到一台虚拟设备（默认叫 **SameCast**）；
选中后，家里所有电视**同时播放同一内容**，任意一台暂停 / 继续 / 拖动进度，全部一起同步。

```
  手机（爱奇艺 / 哔哩哔哩 / 腾讯视频 / 优酷 … 支持 DLNA 投屏的 App）
     │  右上角「投屏」→ 在设备列表里看到「SameCast」
     ▼
┌───────────── 服务端（PVE 虚拟机 / 群晖 / 树莓派，一台就够） ─────────────┐
│ ① DLNA 门面   SSDP 广播 + AVTransport / RenderingControl / ConnectionManager │
│               手机看到的是「一台设备」，实际是发给整组                        │
│ ② 同步控制器  统一时间轴：统一起播位置，小偏差用倍速追平，大偏差才 seek      │
│ ③ 状态回传    任一台电视暂停/继续 → 广播全组 → 通过 GENA 事件回写手机 UI     │
└─────────────────────────────────────────────────────────────────────────────┘
        │                                  │
   ┌────▼─────┐                      ┌─────▼────┐
   │ 电视 A   │                      │ 电视 B   │
   │ 接收端   │                      │ 接收端   │
   └──────────┘                      └──────────┘
```

## 能做什么

- 手机只投一次，N 台电视同时播放（电视数量不设上限，实测 2 台）；
- 任意一台电视上按暂停 / 继续 / 快进，全组同步，手机上的投屏进度也会跟着变；
- 服务端不转码：各电视直接拉同一路 CDN 直链，1 核 512MB 的虚拟机就够；
- 电视端 App 全屏无 UI，开机自启，SSDP 自动发现网关（不用在电视上输 IP）；
- 别的投屏 / 别的 App 进来时自动让出画面，不跟系统自带的投屏打架；
- 手机端零改动、零安装：只要它支持标准 DLNA 投屏。

## 为什么需要这个

投屏协议（DLNA / AirPlay / Chromecast）只定义了「点对点」会话，
「一台手机同时投多台电视 + 进度互相同步」不在协议里。
商业方案（必捷、奇机、AirServer Connect 等）用私有协议 + 专用硬件实现。
本项目用标准协议自己补上这一段——手机不需要任何改动。

## 快速验证（不需要电视，5 分钟）

服务端只依赖 Python 3.9+ 标准库，没有任何第三方包：

```bash
git clone https://github.com/lvyufeng/samecast && cd samecast
cp config.example.json config.json     # 自带两个网页接收端，开箱即用
python3 server/app.py -c config.json
```

1. 打开 `http://<本机IP>:8090/` → 控制台（可粘一个 m3u8/mp4 直链测试）；
2. 另开两个窗口：`http://<本机IP>:8090/tv?id=tv1`、`...?id=tv2` → 两个「电视」；
3. 控制台点「载入并播放」，两个窗口同时播放；在任一窗口按空格暂停，另一边也会暂停。

> 网页接收端是协议原型，方便在没有电视时调试服务端；真实电视请用下面的接收端 App。

## 接入真正的电视

### 方式 A（推荐）：自研接收端 App

见 [docs/install-tv.md](docs/install-tv.md)。全屏无 UI、ExoPlayer 硬解、SSDP 自动找网关、
遥控器「确定」= 全组暂停/播放、开机自启。支持 Android 5.1+（minSdk 21），armv7 / arm64 通用。
构建不需要 Android Studio：

```bash
cd android && ./gradlew assembleDebug   # 产物：app/build/outputs/apk/debug/SameCast-0.3.0-debug.apk

# 装到电视：全程 adb，不需要遥控器点任何确认框
export SAMECAST_DEVICES="192.168.1.31:5555 192.168.1.32:5555"
./toolchain/install-tv.sh              # 装到 SAMECAST_DEVICES 里的所有电视
./toolchain/install-tv.sh 192.168.1.32:5555   # 或者只装某一台
```

构建只需要 JDK 17 + Android SDK 34；`toolchain/` 里有在 macOS 上一键装好这两样（以及 Gradle）
的脚本，国内网络可用 `toolchain/fetch-sdk-manual.sh` 手动下载。

安装走 PackageInstaller 的 session 接口（`pm install-create/write/commit`），
可以绕过部分国产电视把 `pm install` 劫持到「安装确认」界面的问题——实测微鲸 W40F 与
创维 55A3 都是零弹窗、零遥控器操作装完。

电视端 App **不需要写进 config.json**：它首次连接时网关会自动注册（`/api/recv/<id>/...` 动态创建）。

### 方式 B：Kodi（备选，不写代码但重）

1. 电视装 Kodi（Android 5.0+ 用 armv7 包）；
2. `设置 → 服务 → 控制`：开「允许通过 HTTP 远程控制」，端口改成 **8082**（避免和 8090 冲突）；
3. `config.json` 里加一条：

```json
{ "kind": "kodi", "id": "tv1", "name": "客厅电视", "host": "192.168.1.21", "port": 8082 }
```

## 部署

服务端很小（不转码），一台 PVE 虚拟机 / 群晖 / 树莓派就够。**必须是桥接网络**，
否则手机和电视发现不到它（SSDP 是组播）。

### systemd（推荐，长期运行）

在 Debian / Ubuntu 上一条命令装好（非 root 用户 `samecast` + 开机自启 + 崩溃自动拉起）：

在目标机器上能免密 ssh 的前提下（本机装则用 `sudo`）：

```bash
git clone https://github.com/lvyufeng/samecast.git && cd samecast
deploy/install-server.sh root@<服务器IP>   # 上传到 /opt/samecast 并装好 systemd 单元
```

装完访问 `http://<服务器IP>:8090/`。日志：`journalctl -u samecast -f`。

### Docker

```bash
git clone https://github.com/lvyufeng/samecast.git /opt/samecast && cd /opt/samecast
cp config.example.json config.json && vi config.json
docker compose up -d
```

> DLNA 需要**容器使用宿主网络**（SSDP 是组播），`docker-compose.yml` 里已经写了 `network_mode: host`。

## 关键设计说明

| 问题 | 做法 |
|---|---|
| 手机只该看到「一台设备」 | 虚拟出一个 MediaRenderer，整组对它来说就是一端 |
| 两台电视同时能播 | 两台拉同一路直链；若 CDN 有防盗链/限流，可加本地 HLS 中继（ffmpeg 拉一路、电视拉本地） |
| 起播不同步 | 统一时间轴 + 收到 Play 时下发同一目标位置，各端 seek 对齐 |
| 进度漂移 | 每 1s 轮询各端进度并学习每台设备的固定延迟基准；残差小于 `smooth_zone`（2s）时用倍速微调（±4%）悄悄追平，超过才 seek，且 seek 有 8s 冷却 |
| 电视端暂停要联动 | 轮询发现「某端从 playing 变成 paused」且不是本服务下发的（有宽限期防误判）→ 广播全组暂停 |
| 手机 UI 显示暂停 | 通过 GENA（UPnP 事件订阅）把 LastChange 推给手机 App |
| 播完之后 | 到片尾自动整组停止、进度归零，不会循环重载；再点播放才从头开始 |
| 有别的投屏进来 | 接收端发现画面/声音被别的投屏接管就让出并上报，整组停止且不抢回来；只是切到后台则整组暂停（详见 [docs/install-tv.md](docs/install-tv.md)） |

## 已知限制

- **只支持 DLNA**：AirPlay、Chromecast 是另一套协议，本项目目前没做它们的门面；
- **DRM 内容投不了**（Netflix / Disney+ / 部分会员片源）；
- 部分国产 App 的「投屏」走**乐播 SDK 私有协议**而不是标准 DLNA，那类内容不会出现在投屏设备列表里；
- 镜像投屏（手机整屏镜像）无法「全体暂停」，本项目只处理**媒体投屏**；
- 「检测到别的播放器在放」这条让出逻辑需要 Android 8.0（API 26）以上；
  更老的电视（如微鲸 W40F，Android 5.1.1）只有音频焦点和前台切换两条判断；
- 老电视（1GB 内存）如果卡顿，可换方式 B 或加个 200 元安卓盒子。

## 目录结构

```
samecast/
├── server/
│   ├── app.py          # 主程序：HTTP/SOAP/GENA 路由 + 启动
│   ├── dmr.py          # DLNA 设备描述、SCPD、SOAP 动作、GENA 事件
│   ├── group.py        # 同步组：时间轴、指令广播、漂移校正、用户操作检测
│   ├── receivers.py    # 接收端：Kodi(JSON-RPC) / 网页(轮询) / 电视 App(轮询)
│   ├── ssdp.py         # SSDP 发现与通告
│   └── tests/          # 服务端单元测试（时间轴/同步/让出/重载退避）
├── web/
│   ├── dashboard.html  # 控制台
│   └── tv.html         # 网页接收端（协议原型）
├── android/            # 电视端接收端 App（Kotlin + ExoPlayer）
├── deploy/             # systemd 单元 + 一键部署脚本
├── toolchain/          # 无 Android Studio 的构建 / 安装工具链
├── docs/install-tv.md  # 电视端安装、排错、与其他投屏共存
├── config.example.json
├── docker-compose.yml
├── LICENSE
└── .github/workflows/  # CI：服务端单测 + Android 构建
```

## 测试

```bash
cd server && python3 -m unittest discover -s tests
```

纯 Python 标准库，不需要连电视；覆盖时间轴、跨端暂停/播放广播、到片尾收尾、
被别的投屏接管时让出、掉线重载退避、缓冲卡死兜底。CI 会跑 3.9 / 3.10 / 3.11 / 3.13。

## 网页控制台

服务端起起来后，浏览器直接打开（不需要装任何东西）：

| 地址 | 用途 |
|---|---|
| `http://<服务器IP>:8090/` | 控制台：看各端状态、手动载入一个直链播放、整组暂停/继续 |
| `http://<服务器IP>:8090/tv?id=tv1` | 网页接收端（协议原型，也是电视 App 的调试替身） |

## 路线图

- [ ] 电视端上架 / 提供预编译 APK 下载（目前需自行构建）
- [ ] AirPlay、Chromecast 门面（让 iPhone 系统投屏也走同一套同步）
- [ ] 服务端本地 HLS 中继，解决 CDN 防盗链 / 限流
- [ ] 分电视音量与延迟微调（每台设备不同的音频延迟补偿）

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。欢迎提交「机型 + 结果」的实测报告（尤其是电视端）。

## 许可证

[MIT](LICENSE) © SameCast contributors
