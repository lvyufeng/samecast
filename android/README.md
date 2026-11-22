# SameCast 电视端 App（Android）

电视端只装这一个 App。它**不是**一个投屏设备，而是网关的一个"播放端"：
自动找到网关 → 接收播放指令 → 播放并上报进度与暂停状态。

- 手机投屏：系统投屏按钮只看到网关那**一台**虚拟设备（`SameCast`）
- 多台电视：同时播放同一个流，任意一台按遥控器「确定」暂停，全部一起暂停
- 自带播放界面：待机页 / 载入中 / 播放 HUD / 暂停 / 失败重试全在电视上，遥控器可操作，不依赖 Kodi
- 开机自启，连上网关就待命；被别的投屏抢占时自动让出画面，对方结束后自动回到待机页

## 构建 APK（不需要 Android Studio）

```bash
cd android
./build.sh
# 产物：app/build/outputs/apk/debug/SameCast-0.4.0-debug.apk
```

`build.sh` 会自动选构建方式：

| 本机环境 | 构建方式 |
|---|---|
| 有 JDK 17 + Android SDK 34（设了 `ANDROID_SDK_ROOT`，或写成 `local.properties` 里的 `sdk.dir`） | `./gradlew assembleDebug`，十几秒 |
| 只有 docker | `Dockerfile.build` 镜像（约 1GB，含 SDK + Gradle） |

两种都需要联网下载 Gradle 依赖（AndroidX Media3）。想自己加代码用 Android Studio 打开 `android/` 也行。

## 安装到电视

```bash
adb connect <电视IP>:5555
adb install -r app/build/outputs/apk/debug/SameCast-*.apk
```

批量装到多台电视（免遥控器二次确认、装完自动拉起）：

```bash
SAMECAST_DEVICES="<电视A IP>:5555 <电视B IP>:5555" ../toolchain/install-tv.sh
```

没有 ADB 就用 U 盘：把 APK 拷到 FAT32 U 盘 → 电视文件管理器/应用调试里安装
（创维 55A3：工程模式 → 应用调试，密码 `69573028`；微鲸 W40F：设置里允许未知来源 → 本地播放里装）。

## 首次运行

App 打开后显示「正在查找 SameCast 网关…」，2–4 秒内会自动通过 SSDP 找到同一局域网里的网关，
然后显示「已连接 SameCast 网关」并进入等待投屏状态。**不需要在电视上输入任何 IP。**

网关侧会自动注册这台电视（`/api/recv/<id>/...` 动态创建），无需改配置重启。

## 工作方式

| 方向 | 接口 |
|---|---|
| 发现网关 | SSDP `M-SEARCH` + `ST: urn:samecast:service:Gateway:1` |
| 收指令 | `GET /api/recv/<id>/poll?since=<seq>` （350ms 轮询） |
| 上报状态 | `POST /api/recv/<id>/state` `{"state":"playing","position":12.3}` |
| 上报遥控操作 | `POST /api/recv/<id>/command` `{"cmd":"pause","position":12.3}` |

和网页接收端 `/tv` 用的是同一套协议，所以可以先在浏览器里验证网关，再上电视。

## 实现要点

- **ExoPlayer (Media3)** 播放 HLS/MP4，硬件解码；不依赖系统播放器
- 遥控器 `DPAD_CENTER` / `MEDIA_PLAY_PAUSE` 映射为全组暂停/播放
- 进度每 800ms 上报一次；网关侧发现偏差 >0.6s 会下发 seek 校正
- 明文 HTTP 已允许（`usesCleartextTraffic`），局域网直连不需要证书
- `minSdk 21`（Android 5.0），覆盖微鲸 W40F 的 Android 5.x

## 可能踩的坑

| 现象 | 处理 |
|---|---|
| 一直「正在查找 SameCast 网关」 | 先确认电视和网关在同一网段（`ping` 得通）；网关回包必须来自 UDP 1900 端口，旧版网关用临时端口回包时，部分电视（如微鲸 W40F）会丢包导致一直找不到 |
| 播放失败 `ERROR_CODE_IO_BAD_HTTP_STATUS` | 直链带防盗链（爱奇艺/B站），需要网关侧加 HLS 中继（下一步） |
| 花屏/黑屏但声音正常 | 老芯片硬解 H.264 High profile 4K 吃力，让网关转成 1080p 中继 |
| 装不上 | 酷开系统要先用「应用调试」打开安装权限；微鲸要在设置里允许未知来源 |

## 界面

电视端没有按钮菜单，全部靠遥控器，状态都在画面上：

| 场景 | 电视上看到 |
|---|---|
| 等投屏 | 品牌页、电视型号、网关连接状态（绿点=已连）、支持的 App 提示、版本号 |
| 载入中 | 居中转圈 + 「正在载入片源…」提示 |
| 播放中 | 全屏画面；按任意方向键/确定键出信息条（标题、进度、状态、按键提示，5 秒自动隐藏） |
| 暂停 | 半透明大标题 + 当前进度 + 「按确定或手机上点播放，所有电视一起继续」 |
| 播放结束 | 片名 + 总进度 + 提示在手机上选下一集 |
| 播放失败 | 失败原因 + 「按确定重试」 |
| 被其他投屏抢占 | 「已被其他投屏接管」，SameCast 自动让出画面/声音；对方投屏结束后自动回到待机页 |
| 网关断线 | 底部提示「画面继续播放，恢复连接后自动重新同步」 |

应用图标和 Android TV 横幅由 `android/tools/gen_brand_assets.py` 生成（Pillow），改配色后重跑即可。
